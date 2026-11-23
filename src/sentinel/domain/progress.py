"""每日进展的领域模型与纯计算。

分工沿用 digest.py 的原则：能算的都留在这里——窗口换算、过滤、排序、统计、指纹；
模型只负责把结果写成文字。这样报告里的数字永远来自代码，不会出现「摘要说 12 个 PR，
实际 7 个」这类幻觉。

三个容易踩的点先写清楚：
  1. 所有时间在领域内一律 aware UTC；「当日」是按订阅时区切的自然日。
  2. 指纹不含 generated_at，否则每天重跑一次就会白白多调一次模型。
  3. 目录名用 repo_slug()，不把 owner/repo 直接拼进路径（Windows 路径与保留名都不安全）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .errors import ValidationError
from .filters import is_bot_author
from .models import as_utc
from .selection import ReportSelection

# 风险标签：与 digest 的判定保持同一套词表，避免两处口径漂移
RISK_LABELS = ("breaking", "breaking-change", "security", "critical", "regression", "bug")

# 资源名与 REST 端点路径一致，和 enums.RESOURCE_ORDER 是同一套命名
PROGRESS_RESOURCES: tuple[str, ...] = ("issues", "pulls", "commits", "releases")

# 统计字段的固定清单：渲染 front matter、报告拼提示词、读回报告时都用这一份，
# 避免三处各写一套 key 导致某个数字悄悄丢失
STAT_KEYS: tuple[str, ...] = (
    "issues_total",
    "issues_open",
    "issues_closed",
    "pulls_total",
    "pulls_open",
    "pulls_merged",
    "pulls_draft",
    "commits_total",
    "releases_total",
    "contributors",
    "bots_filtered",
)


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True)


class IssueRecord(_Record):
    number: int
    title: str
    url: str = ""
    author: str = ""
    state: str = "open"
    labels: tuple[str, ...] = ()
    created_at: AwareDatetime
    updated_at: AwareDatetime
    closed_at: AwareDatetime | None = None
    comments: int = 0
    body_excerpt: str = ""

    @property
    def is_closed(self) -> bool:
        return self.state.lower() == "closed"


class PullRecord(_Record):
    number: int
    title: str
    url: str = ""
    author: str = ""
    state: str = "open"
    draft: bool = False
    labels: tuple[str, ...] = ()
    created_at: AwareDatetime
    updated_at: AwareDatetime
    closed_at: AwareDatetime | None = None
    merged_at: AwareDatetime | None = None
    comments: int = 0
    review_comments: int = 0
    additions: int | None = None
    deletions: int | None = None
    changed_files: int | None = None
    body_excerpt: str = ""

    @property
    def is_merged(self) -> bool:
        return self.merged_at is not None

    @property
    def status_label(self) -> str:
        """GitHub 把「已合并」表达成 state=closed + merged_at，这里统一成三种人读的状态。"""
        if self.is_merged:
            return "merged"
        if self.draft and self.state.lower() == "open":
            return "draft"
        return self.state.lower()


class CommitRecord(_Record):
    sha: str
    title: str
    url: str = ""
    author: str = ""
    committed_at: AwareDatetime
    body_excerpt: str = ""


class ReleaseRecord(_Record):
    tag: str
    name: str = ""
    url: str = ""
    author: str = ""
    published_at: AwareDatetime
    prerelease: bool = False
    draft: bool = False
    body_excerpt: str = ""


class DailyProgress(_Record):
    repo_full_name: str
    repo_title: str
    day: date
    timezone: str
    window_start: AwareDatetime
    window_end: AwareDatetime
    generated_at: AwareDatetime

    # 区间报告才有值；单日模式为 None（day 就是那一天）
    date_from: date | None = None
    # 展示与排序的时间轴：issues 取 "updated_at"/"closed_at"，None 表示本次不纳入该资源
    issue_axis: str | None = "updated_at"
    pull_axis: str | None = "updated_at"

    issues: tuple[IssueRecord, ...] = ()
    pulls: tuple[PullRecord, ...] = ()
    commits: tuple[CommitRecord, ...] = ()
    releases: tuple[ReleaseRecord, ...] = ()

    # truncated: 翻页触顶，统计只是下界（会漏报）
    # trimmed:   展示条数超限，数据完整、统计准确，只是表格少几行
    truncated: frozenset[str] = frozenset()
    trimmed: frozenset[str] = frozenset()
    stats: dict[str, int] = Field(default_factory=dict)
    fingerprint: str = ""

    @property
    def period_label(self) -> str:
        if self.date_from is None or self.date_from == self.day:
            return f"{self.day:%Y-%m-%d}"
        return f"{self.date_from:%Y-%m-%d} ~ {self.day:%Y-%m-%d}"

    @property
    def completion_mode(self) -> bool:
        return self.issue_axis == "closed_at" or self.pull_axis == "merged_at"

    @property
    def item_count(self) -> int:
        return (
            len(self.issues) + len(self.pulls) + len(self.commits) + len(self.releases)
        )


def resolve_day_window(
    day: date, tz: tzinfo, *, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """返回 [当日 00:00, 次日 00:00) 的 aware UTC。

    传 now 时把窗口右端截到「现在」：当天报告不该把未来几小时算进去。
    窗口为左闭右开，避免相邻两天的边界条目被数两次。
    """
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    start = start_local.astimezone(UTC)
    end = end_local.astimezone(UTC)
    if now is not None:
        moment = as_utc(now) or start
        end = min(end, moment)
        if end < start:
            end = start
    return start, end


def repo_slug(repo_full_name: str) -> str:
    """owner/repo -> 可逆、Windows 安全的目录名。

    分隔符取 "__"：真实 GitHub owner 只允许字母数字和 "-"，不可能含下划线，
    所以从 slug 反解不可能切错位置。owner 里的下划线转义成 %5F，
    仓库名自身的 "__" 也转义，保证两个不同仓库不会撞到同一个目录。
    """
    owner, _, name = repo_full_name.partition("/")
    if not owner or not name:
        raise ValidationError("repo_full_name 格式应为 owner/repo")
    return f"{owner.replace('_', '%5F')}__{name.replace('__', '%5F%5F')}"


def build_daily_progress(
    *,
    repo_full_name: str,
    repo_title: str,
    day: date,
    timezone: str,
    window_start: datetime,
    window_end: datetime,
    generated_at: datetime,
    issues: Sequence[IssueRecord] = (),
    pulls: Sequence[PullRecord] = (),
    commits: Sequence[CommitRecord] = (),
    releases: Sequence[ReleaseRecord] = (),
    truncated: Iterable[str] = (),
    max_items: int = 200,
    skip_bots: bool = True,
    selection: ReportSelection | None = None,
    date_from: date | None = None,
) -> DailyProgress:
    """过滤、排序、截断、算统计与指纹，产出不可变聚合。

    传 selection 时按口径筛选（研发周期报告）；不传时保持 v2.0 的单日活动口径，
    此时 REST 已经按窗口裁过两端，这里不再做第二遍判断。
    """
    if window_end <= window_start:
        raise ValidationError("报告窗口为空：请检查日期与时区配置")

    flags = {name for name in truncated if name in PROGRESS_RESOURCES}
    trims: set[str] = set()
    bot_filtered = 0

    def keep(author: str) -> bool:
        nonlocal bot_filtered
        if skip_bots and is_bot_author(author):
            bot_filtered += 1
            return False
        return True

    def trim(items: list, resource: str) -> tuple:
        if len(items) > max_items:
            # 展示裁剪不影响统计：compute_stats 仍按全量算
            trims.add(resource)
            return tuple(items[:max_items])
        return tuple(items)

    def issue_stamp(item: IssueRecord) -> datetime:
        return selection.issue_time(item) if selection is not None else item.updated_at

    def pull_stamp(item: PullRecord) -> datetime:
        return selection.pull_time(item) if selection is not None else item.updated_at

    def keep_issue(item: IssueRecord) -> bool:
        if not keep(item.author):
            return False
        return selection is None or selection.keeps_issue(item, window_start, window_end)

    def keep_pull(item: PullRecord) -> bool:
        if not keep(item.author):
            return False
        return selection is None or selection.keeps_pull(item, window_start, window_end)

    issue_items = sorted(
        filter(keep_issue, issues),
        key=lambda item: (issue_stamp(item), item.number),
        reverse=True,
    )
    pull_items = sorted(
        filter(keep_pull, pulls),
        key=lambda item: (pull_stamp(item), item.number),
        reverse=True,
    )
    commit_items = sorted(
        (item for item in commits if keep(item.author)),
        key=lambda item: (item.committed_at, item.sha),
        reverse=True,
    )
    release_items = sorted(
        (item for item in releases if keep(item.author)),
        key=lambda item: (item.published_at, item.tag),
        reverse=True,
    )

    progress = DailyProgress(
        repo_full_name=repo_full_name,
        repo_title=repo_title or repo_full_name,
        day=day,
        date_from=date_from,
        timezone=timezone,
        window_start=window_start,
        window_end=window_end,
        generated_at=generated_at,
        issue_axis=selection.issue_time_attr if selection is not None else "updated_at",
        pull_axis=selection.pull_time_attr if selection is not None else "updated_at",
        issues=trim(issue_items, "issues"),
        pulls=trim(pull_items, "pulls"),
        commits=trim(commit_items, "commits"),
        releases=trim(release_items, "releases"),
        truncated=frozenset(flags),
        trimmed=frozenset(trims),
        stats=compute_stats(
            issues=issue_items,
            pulls=pull_items,
            commits=commit_items,
            releases=release_items,
            bot_filtered=bot_filtered,
        ),
    )
    return progress.model_copy(update={"fingerprint": progress_fingerprint(progress)})


def compute_stats(
    *,
    issues: Sequence[IssueRecord],
    pulls: Sequence[PullRecord],
    commits: Sequence[CommitRecord],
    releases: Sequence[ReleaseRecord],
    bot_filtered: int = 0,
) -> dict[str, int]:
    authors = {item.author for item in issues if item.author}
    authors |= {item.author for item in pulls if item.author}
    authors |= {item.author for item in commits if item.author}
    authors |= {item.author for item in releases if item.author}
    return {
        "issues_total": len(issues),
        "issues_open": sum(1 for item in issues if not item.is_closed),
        "issues_closed": sum(1 for item in issues if item.is_closed),
        "pulls_total": len(pulls),
        "pulls_open": sum(1 for item in pulls if item.status_label == "open"),
        "pulls_merged": sum(1 for item in pulls if item.is_merged),
        "pulls_draft": sum(1 for item in pulls if item.status_label == "draft"),
        "commits_total": len(commits),
        "releases_total": len(releases),
        "contributors": len(authors),
        "bots_filtered": bot_filtered,
    }


def progress_fingerprint(progress: DailyProgress) -> str:
    """内容指纹：只认「口径 + 对象身份 + 关键时间」，不认 generated_at。

    两个刻意的取舍：
      1. **不含 window_end**。窗口右端会被裁到「现在」，若纳入指纹，今天
         10:00 与 10:05 各跑一次就会得到不同指纹，明明数据没变却重写文件、
         重调模型。
      2. **时间取口径对应的时间轴**。完成口径下同一条 PR 在区间内合并、
         之后被评论不会改指纹（否则一条评论就白烧一次 token）；
         而「区间内重新关闭」会让 closed_at 变化，指纹随之刷新。
    """
    payload = {
        "repo": progress.repo_full_name,
        "day": progress.day.isoformat(),
        "date_from": progress.date_from.isoformat() if progress.date_from else None,
        "issue_axis": progress.issue_axis,
        "pull_axis": progress.pull_axis,
        "trimmed": sorted(progress.trimmed),
        "truncated": sorted(progress.truncated),
        "issues": [
            [item.number, item.state, _axis_stamp(item, progress.issue_axis)]
            for item in progress.issues
        ],
        "pulls": [
            [item.number, item.status_label, _axis_stamp(item, progress.pull_axis)]
            for item in progress.pulls
        ],
        "commits": [[item.sha, item.committed_at.isoformat()] for item in progress.commits],
        "releases": [
            [item.tag, item.published_at.isoformat()] for item in progress.releases
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _axis_stamp(item: IssueRecord | PullRecord, axis: str | None) -> str:
    """取口径对应的时间；轴字段缺失时退回 updated_at，避免整条记录被跳过。"""
    if axis:
        moment = getattr(item, axis, None)
        if isinstance(moment, datetime):
            return moment.isoformat()
    return item.updated_at.isoformat()


def _sort_stamp(item: IssueRecord | PullRecord, axis: str | None) -> datetime:
    if axis:
        moment = getattr(item, axis, None)
        if isinstance(moment, datetime):
            return moment
    return item.updated_at


def rank_highlights(progress: DailyProgress, limit: int = 5) -> tuple[str, ...]:
    """按「发布 > 合并 > 关闭 > 新开」的次序挑要点。

    刻意不把 commit 放进来：一天几十条提交只会把要点列表淹掉，
    提交量已经在统计表里有了。
    """
    scored: list[tuple[int, datetime, str]] = []
    for item in progress.releases:
        # GitHub 的 name 常与 tag 重复（"Release v2.1.0"），只在真的不同时才拼上去
        label = item.name.strip()
        suffix = "" if not label or label in {item.tag, f"Release {item.tag}"} else f"：{label}"
        scored.append((100, item.published_at, f"发布 {item.tag}{suffix}"))
    for item in progress.pulls:
        stamp = _sort_stamp(item, progress.pull_axis)
        if item.is_merged:
            scored.append((90, stamp, f"PR #{item.number} 已合并：{item.title}"))
        elif item.draft:
            scored.append((35, stamp, f"PR #{item.number} 仍为草稿：{item.title}"))
        else:
            scored.append((60, stamp, f"PR #{item.number} 待评审：{item.title}"))
    for item in progress.issues:
        stamp = _sort_stamp(item, progress.issue_axis)
        if item.is_closed:
            scored.append((70, stamp, f"议题 #{item.number} 已关闭：{item.title}"))
        else:
            scored.append((40, stamp, f"议题 #{item.number} 有新进展：{item.title}"))

    ranked = sorted(scored, key=lambda entry: (entry[0], entry[1]), reverse=True)
    return tuple(item[2] for item in ranked[:limit])


def detect_risks(progress: DailyProgress, limit: int = 5) -> tuple[str, ...]:
    """只按标签与状态做规则提取，供兜底报告与提示词参考。

    不做语义判断：判断「这个改动风险大不大」是模型的事，
    但「带 security 标签且还没关」是事实，可以算。

    完成口径下「未关闭/未合并」的分支恒为空，所以换成一条互补的规则：
    带风险标签却已经在区间内关闭/合并的，提示回归验证。
    """
    risks: list[str] = []
    completion = progress.completion_mode
    for item in progress.issues:
        hit = _risk_labels(item.labels)
        if not hit:
            continue
        if completion:
            if item.is_closed:
                risks.append(
                    f"议题 #{item.number}「{item.title}」带 {', '.join(hit)} 标签已在区间内关闭，"
                    "建议确认修复是否真正落地"
                )
        elif not item.is_closed:
            risks.append(
                f"议题 #{item.number}「{item.title}」带 {', '.join(hit)} 标签且仍未关闭，建议确认影响范围"
            )
    for item in progress.pulls:
        hit = _risk_labels(item.labels)
        if not hit:
            continue
        if completion:
            if item.is_merged:
                risks.append(
                    f"PR #{item.number}「{item.title}」带 {', '.join(hit)} 标签已在区间内合并，"
                    "建议安排回归验证"
                )
        elif not item.is_merged:
            risks.append(
                f"PR #{item.number}「{item.title}」带 {', '.join(hit)} 标签且尚未合并，合并前需要重点复核"
            )
    if not risks and not completion and progress.issues:
        stale = [item for item in progress.issues if not item.is_closed and item.comments == 0]
        if stale:
            numbers = "、".join(f"#{item.number}" for item in stale[:3])
            risks.append(f"{numbers} 本日有更新但零评论，可能缺少明确负责人")
    return tuple(risks[:limit])


def summary_line(progress: DailyProgress) -> str:
    """一句话概览，进度文档与报告都用它，保证两处口径一致。"""
    stats = progress.stats
    parts: list[str] = []
    if progress.issue_axis is not None:
        closed = stats.get("issues_closed", 0)
        if progress.issue_axis == "closed_at":
            parts.append(f"{closed} 个议题关闭")
        else:
            parts.append(f"{stats.get('issues_total', 0)} 个议题有更新（{closed} 个关闭）")
    if progress.pull_axis is not None:
        merged = stats.get("pulls_merged", 0)
        if progress.pull_axis == "merged_at":
            parts.append(f"{merged} 个 PR 合并")
        else:
            parts.append(f"{stats.get('pulls_total', 0)} 个 PR 有更新（{merged} 个合并）")
    parts.append(f"{stats.get('commits_total', 0)} 次提交")
    parts.append(f"{stats.get('releases_total', 0)} 个发布")
    prefix = "本期" if progress.completion_mode else "本日"
    return f"{prefix} " + "、".join(parts)


def _risk_labels(labels: tuple[str, ...]) -> tuple[str, ...]:
    hit = tuple(label for label in labels if label.lower() in RISK_LABELS)
    return hit
