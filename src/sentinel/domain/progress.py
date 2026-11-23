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

    issues: tuple[IssueRecord, ...] = ()
    pulls: tuple[PullRecord, ...] = ()
    commits: tuple[CommitRecord, ...] = ()
    releases: tuple[ReleaseRecord, ...] = ()

    truncated: frozenset[str] = frozenset()
    stats: dict[str, int] = Field(default_factory=dict)
    fingerprint: str = ""

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
) -> DailyProgress:
    """排序、过滤机器人、截断、算统计与指纹，产出不可变聚合。"""
    if window_end <= window_start:
        raise ValidationError("报告窗口为空：请检查日期与时区配置")

    flags = {name for name in truncated if name in PROGRESS_RESOURCES}
    bot_filtered = 0

    def keep(author: str) -> bool:
        nonlocal bot_filtered
        if skip_bots and is_bot_author(author):
            bot_filtered += 1
            return False
        return True

    def trim(items: list, resource: str) -> tuple:
        if len(items) > max_items:
            flags.add(resource)
            return tuple(items[:max_items])
        return tuple(items)

    issue_items = sorted(
        (item for item in issues if keep(item.author)),
        key=lambda item: (item.updated_at, item.number),
        reverse=True,
    )
    pull_items = sorted(
        (item for item in pulls if keep(item.author)),
        key=lambda item: (item.updated_at, item.number),
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
        timezone=timezone,
        window_start=window_start,
        window_end=window_end,
        generated_at=generated_at,
        issues=trim(issue_items, "issues"),
        pulls=trim(pull_items, "pulls"),
        commits=trim(commit_items, "commits"),
        releases=trim(release_items, "releases"),
        truncated=frozenset(flags),
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
    """内容指纹：只认「对象身份 + 关键时间」，不认 generated_at。

    同一天同样的数据重跑必然得到同一指纹，报告才能靠指纹跳过一次模型调用。
    """
    payload = {
        "repo": progress.repo_full_name,
        "day": progress.day.isoformat(),
        "window_start": progress.window_start.isoformat(),
        "window_end": progress.window_end.isoformat(),
        "issues": [
            [item.number, item.state, item.updated_at.isoformat()]
            for item in progress.issues
        ],
        "pulls": [
            [item.number, item.status_label, item.updated_at.isoformat()]
            for item in progress.pulls
        ],
        "commits": [[item.sha, item.committed_at.isoformat()] for item in progress.commits],
        "releases": [
            [item.tag, item.published_at.isoformat()] for item in progress.releases
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


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
        if item.is_merged:
            scored.append((90, item.updated_at, f"PR #{item.number} 已合并：{item.title}"))
        elif item.draft:
            scored.append((35, item.updated_at, f"PR #{item.number} 仍为草稿：{item.title}"))
        else:
            scored.append((60, item.updated_at, f"PR #{item.number} 待评审：{item.title}"))
    for item in progress.issues:
        if item.is_closed:
            scored.append((70, item.updated_at, f"议题 #{item.number} 已关闭：{item.title}"))
        else:
            scored.append((40, item.updated_at, f"议题 #{item.number} 有新进展：{item.title}"))

    ranked = sorted(scored, key=lambda entry: (entry[0], entry[1]), reverse=True)
    return tuple(item[2] for item in ranked[:limit])


def detect_risks(progress: DailyProgress, limit: int = 5) -> tuple[str, ...]:
    """只按标签与状态做规则提取，供兜底报告与提示词参考。

    不做语义判断：判断「这个改动风险大不大」是模型的事，
    但「带 security 标签且还没关」是事实，可以算。
    """
    risks: list[str] = []
    for item in progress.issues:
        hit = _risk_labels(item.labels)
        if hit and not item.is_closed:
            risks.append(
                f"议题 #{item.number}「{item.title}」带 {', '.join(hit)} 标签且仍未关闭，建议确认影响范围"
            )
    for item in progress.pulls:
        hit = _risk_labels(item.labels)
        if hit and not item.is_merged:
            risks.append(
                f"PR #{item.number}「{item.title}」带 {', '.join(hit)} 标签且尚未合并，合并前需要重点复核"
            )
    if not risks and progress.issues:
        stale = [item for item in progress.issues if not item.is_closed and item.comments == 0]
        if stale:
            numbers = "、".join(f"#{item.number}" for item in stale[:3])
            risks.append(f"{numbers} 本日有更新但零评论，可能缺少明确负责人")
    return tuple(risks[:limit])


def summary_line(progress: DailyProgress) -> str:
    """一句话概览，进度文档与报告都用它，保证两处口径一致。"""
    stats = progress.stats
    return (
        f"本日 {stats.get('issues_total', 0)} 个议题有更新"
        f"（{stats.get('issues_closed', 0)} 个关闭）、"
        f"{stats.get('pulls_total', 0)} 个 PR 有更新"
        f"（{stats.get('pulls_merged', 0)} 个合并）、"
        f"{stats.get('commits_total', 0)} 次提交、"
        f"{stats.get('releases_total', 0)} 个发布"
    )


def _risk_labels(labels: tuple[str, ...]) -> tuple[str, ...]:
    hit = tuple(label for label in labels if label.lower() in RISK_LABELS)
    return hit
