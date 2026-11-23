from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta

from ...domain.enums import EventKind
from ...domain.models import RepoEvent, RepoMeta, excerpt
from ...domain.progress import CommitRecord, IssueRecord, PullRecord, ReleaseRecord
from ...ports.clock import Clock
from ...ports.github import FetchQuery, Page, ProgressQuery, ProgressResult, RateStatus

_COMMIT_MESSAGES = (
    "fix: 修正分页边界导致的重复采集",
    "feat: 支持自定义通知渠道",
    "refactor: 拆分采集器与报告生成",
    "perf: 命中 ETag 时跳过解析",
    "chore: 升级依赖并清理告警",
    "test: 补充去重键的边界用例",
    "docs: 补充分阶段落地说明",
    "fix: 修复水位回退问题",
)

_ISSUE_TITLES = (
    "偶发 502：采集任务中途退出",
    "希望支持按标签过滤",
    "报告里的时间显示有偏差",
    "配置项缺少说明文档",
    "长时间运行后内存占用偏高",
)

_PR_TITLES = (
    "feat: 增加周报汇总模式",
    "fix: 修复 issue 重复播报",
    "perf: 减少 30% 的 API 调用",
    "refactor: 统一事件映射逻辑",
)

_AUTHORS = ("alice", "bob", "carol", "dave", "erin", "frank")
_LABELS = ("bug", "enhancement", "breaking-change", "security", "docs", "performance")


class FakeGitHub:
    """离线可用的 GitHub 替身。

    刻意实现了 304 分支：同一个水位 + 同一个 ETag 再次请求会返回 not_modified，
    这样「增量没变时不重复入库」的链路在本地就能被验证，而不是等上线才发现。
    """

    def __init__(self, clock: Clock, *, timezone_name: str = "Asia/Shanghai") -> None:
        self._clock = clock
        self._tz_name = timezone_name

    async def get_repo_meta(self, repo_full_name: str) -> RepoMeta:
        seed = int(hashlib.sha1(repo_full_name.encode()).hexdigest()[:6], 16)
        rng = random.Random(seed)
        return RepoMeta(
            full_name=repo_full_name,
            description=f"{repo_full_name} 的离线演示数据（FakeGitHub）",
            stars=rng.randint(120, 9800),
            forks=rng.randint(10, 900),
            default_branch="main",
            html_url=f"https://github.com/{repo_full_name}",
            pushed_at=self._clock.now(),
        )

    async def fetch(self, query: FetchQuery) -> Page:
        now = self._clock.now()
        since = query.since or (now - timedelta(days=1))
        etag = f'W/"fake-{query.resource}-{int(since.timestamp())}"'

        if query.etag and query.etag == etag:
            return Page(resource=query.resource, etag=etag, not_modified=True)

        events = self._synthesize(
            query.repo_full_name, query.resource, since, now, query.subscription_id
        )
        return Page(resource=query.resource, events=events, etag=etag)

    async def rate_status(self) -> RateStatus:
        return RateStatus(remaining=4998, limit=5000, reset_at=None)

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------ 造数
    def _synthesize(
        self,
        repo: str,
        resource: str,
        since: datetime,
        until: datetime,
        sub_id: int | None,
    ) -> list[RepoEvent]:
        rng = random.Random(f"{repo}|{resource}|{since:%Y%m%d%H}|{until:%Y%m%d%H}")
        span = max(3600.0, (until - since).total_seconds())
        events: list[RepoEvent] = []

        def moment() -> datetime:
            # 从窗口末端往回取，平方后更靠近 now：
            # 演示需要「最近一天」有足够事件，否则日报窗口经常是空的
            return until - timedelta(seconds=(rng.random() ** 2) * span)

        if resource == "commits":
            for index in range(rng.randint(4, 9)):
                ts = moment()
                sha = hashlib.sha1(f"{repo}:{ts.isoformat()}:{index}".encode()).hexdigest()
                events.append(
                    RepoEvent(
                        subscription_id=sub_id,
                        repo_full_name=repo,
                        kind=EventKind.COMMIT,
                        external_id=sha,
                        title=rng.choice(_COMMIT_MESSAGES),
                        url=f"https://github.com/{repo}/commit/{sha[:10]}",
                        author=rng.choice(_AUTHORS),
                        authored_at=ts,
                        body_excerpt=excerpt("改动涉及采集编排与报告模板，详见 diff。"),
                    )
                )

        elif resource == "issues":
            for _ in range(rng.randint(1, 3)):
                ts = moment()
                number = rng.randint(100, 999)
                state = rng.choice(["open", "closed", "open"])
                events.append(
                    RepoEvent(
                        subscription_id=sub_id,
                        repo_full_name=repo,
                        kind=EventKind.ISSUE,
                        external_id=str(number),
                        title=rng.choice(_ISSUE_TITLES),
                        url=f"https://github.com/{repo}/issues/{number}",
                        author=rng.choice(_AUTHORS),
                        authored_at=ts,
                        source_updated_at=ts,
                        state=state,
                        labels=(rng.choice(_LABELS),),
                        body_excerpt=excerpt("现象：定时任务偶发中断；期望：失败后自动重试并记录原因。"),
                    )
                )

        elif resource == "pulls":
            for _ in range(rng.randint(1, 3)):
                ts = moment()
                number = rng.randint(10, 99)
                state = rng.choice(["merged", "open", "merged"])
                events.append(
                    RepoEvent(
                        subscription_id=sub_id,
                        repo_full_name=repo,
                        kind=EventKind.PULL_REQUEST,
                        external_id=str(number),
                        title=rng.choice(_PR_TITLES),
                        url=f"https://github.com/{repo}/pull/{number}",
                        author=rng.choice(_AUTHORS),
                        authored_at=ts,
                        source_updated_at=ts,
                        state=state,
                        labels=(rng.choice(_LABELS),),
                        body_excerpt=excerpt(
                            "把采集与渲染解耦，新增 12 个单测；风险：需要重新生成一次水位。"
                        ),
                    )
                )

        elif resource == "releases":
            if rng.random() < 0.55:
                ts = moment()
                tag = f"v0.{rng.randint(1, 9)}.{rng.randint(0, 9)}"
                events.append(
                    RepoEvent(
                        subscription_id=sub_id,
                        repo_full_name=repo,
                        kind=EventKind.RELEASE,
                        external_id=tag,
                        title=f"Release {tag}",
                        url=f"https://github.com/{repo}/releases/tag/{tag}",
                        author=rng.choice(_AUTHORS),
                        authored_at=ts,
                        state="published",
                        labels=("release",),
                        body_excerpt=excerpt(
                            "本次发布包含：增量采集水位修复、报告模板重构、若干依赖升级。"
                        ),
                    )
                )

        events.sort(key=lambda event: event.authored_at)
        return events

    # ------------------------------------------------------------- 每日进展
    async def fetch_progress(self, query: ProgressQuery) -> ProgressResult:
        """按「仓库 + 窗口」确定性造数：同一仓库同一天多次调用结果完全一致。

        刻意保证「至少 1 个已关闭议题 + 1 个已合并 PR」，否则离线演示时
        报告里全是 open，看不出汇总与风险判定的逻辑。
        所有时间强制落在窗口内——这是每日进展最重要的正确性约束。
        """
        rng = random.Random(f"{query.repo_full_name}|progress|{query.window_start:%Y%m%d%H%M}")
        span = max(1.0, (query.window_end - query.window_start).total_seconds())

        def moment() -> datetime:
            return query.window_start + timedelta(seconds=rng.random() * span)

        result = ProgressResult(
            repo_full_name=query.repo_full_name,
            window_start=query.window_start,
            window_end=query.window_end,
        )

        if "issues" in query.resources:
            base = rng.randint(100, 700)
            for index in range(rng.randint(2, 4)):
                updated = moment()
                # 前两条固定一关一开：演示时既能看到「已关闭」也能看到风险判定
                state = ("closed", "open")[index] if index < 2 else rng.choice(["open", "closed"])
                labels = tuple(rng.sample(_LABELS, k=rng.randint(0, 2)))
                result.issues.append(
                    IssueRecord(
                        number=base + index,
                        title=rng.choice(_ISSUE_TITLES),
                        url=f"https://github.com/{query.repo_full_name}/issues/{base + index}",
                        author=rng.choice(_AUTHORS),
                        state=state,
                        labels=labels,
                        created_at=updated - timedelta(hours=rng.randint(1, 96)),
                        updated_at=updated,
                        closed_at=updated if state == "closed" else None,
                        comments=rng.randint(0, 9),
                        body_excerpt=excerpt(
                            "现象：定时任务偶发中断；期望：失败后自动重试并记录原因。",
                            query.body_excerpt_chars,
                        )
                        or "",
                    )
                )

        if "pulls" in query.resources:
            base = rng.randint(10, 90)
            for index in range(rng.randint(2, 3)):
                updated = moment()
                merged = index == 0
                result.pulls.append(
                    PullRecord(
                        number=base + index,
                        title=rng.choice(_PR_TITLES),
                        url=f"https://github.com/{query.repo_full_name}/pull/{base + index}",
                        author=rng.choice(_AUTHORS),
                        state="closed" if merged else "open",
                        draft=(not merged) and rng.random() < 0.3,
                        labels=tuple(rng.sample(_LABELS, k=rng.randint(0, 2))),
                        created_at=updated - timedelta(hours=rng.randint(2, 120)),
                        updated_at=updated,
                        closed_at=updated if merged else None,
                        merged_at=updated if merged else None,
                        comments=rng.randint(0, 12),
                        review_comments=rng.randint(0, 8),
                        additions=rng.randint(5, 300),
                        deletions=rng.randint(0, 120),
                        changed_files=rng.randint(1, 12),
                        body_excerpt=excerpt(
                            "把采集与渲染解耦，新增 12 个单测；风险：需要重新生成一次水位。",
                            query.body_excerpt_chars,
                        )
                        or "",
                    )
                )
            # 一条机器人 PR：让「过滤机器人」在离线演示里也能被看到
            result.pulls.append(
                PullRecord(
                    number=base + 99,
                    title="chore(deps): bump httpx from 0.27 to 0.28",
                    url=f"https://github.com/{query.repo_full_name}/pull/{base + 99}",
                    author="dependabot[bot]",
                    state="open",
                    labels=("dependencies",),
                    created_at=moment(),
                    updated_at=moment(),
                )
            )

        if "commits" in query.resources:
            for index in range(rng.randint(3, 8)):
                ts = moment()
                sha = hashlib.sha1(
                    f"{query.repo_full_name}:progress:{ts.isoformat()}:{index}".encode()
                ).hexdigest()
                result.commits.append(
                    CommitRecord(
                        sha=sha,
                        title=rng.choice(_COMMIT_MESSAGES),
                        url=f"https://github.com/{query.repo_full_name}/commit/{sha[:10]}",
                        author=rng.choice(_AUTHORS),
                        committed_at=ts,
                        body_excerpt=excerpt("改动涉及采集编排与报告模板，详见 diff。"),
                    )
                )

        if "releases" in query.resources:
            tag = f"v0.{rng.randint(1, 9)}.{rng.randint(0, 9)}"
            published = moment()
            result.releases.append(
                ReleaseRecord(
                    tag=tag,
                    name=f"Release {tag}",
                    url=f"https://github.com/{query.repo_full_name}/releases/tag/{tag}",
                    author=rng.choice(_AUTHORS),
                    published_at=published,
                    body_excerpt=excerpt(
                        "本次发布包含：增量采集水位修复、报告模板重构、若干依赖升级。"
                    )
                    or "",
                )
            )

        result.truncated = {name: False for name in query.resources}
        return result
