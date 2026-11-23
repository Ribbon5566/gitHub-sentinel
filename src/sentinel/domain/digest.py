"""摘要输入/输出的领域结构与纯计算。

分工很明确：
  * 这里只做「规则能算出来的事」——打分、排序、统计、内容指纹；
  * 需要语言理解的部分交给 LLMProvider，失败时由 Mock 用本文件的统计结果兜底。

这样即使没有 API Key，报告也不会是空壳；而模型只负责归纳，不负责计数，
从根上避免「摘要说 12 个 PR，实际 7 个」这类幻觉。
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import BaseModel, Field

from .enums import DigestStyle, EventKind
from .models import RepoEvent, Subscription

# 各类事件的基础权重：发版最重，纯 commit 最轻
_BASE_SCORE: dict[EventKind, int] = {
    EventKind.RELEASE: 100,
    EventKind.PULL_REQUEST: 60,
    EventKind.ISSUE: 50,
    EventKind.DISCUSSION: 40,
    EventKind.COMMIT: 20,
}

_HOT_LABELS = ("breaking", "breaking-change", "security", "critical", "bug", "regression")


def score_event(event: RepoEvent) -> int:
    score = _BASE_SCORE.get(event.kind, 10)
    if event.is_terminal_state:
        score += 15
    labels = " ".join(event.labels).lower()
    if any(hot in labels for hot in _HOT_LABELS):
        score += 25
    if event.kind is EventKind.PULL_REQUEST and (event.state or "") == "merged":
        score += 10
    # 正文长的通常信息量大，但收益递减
    if event.body_excerpt:
        score += min(len(event.body_excerpt) // 200, 10)
    return score


def rank_events(events: list[RepoEvent], limit: int = 10) -> list[RepoEvent]:
    return sorted(
        events,
        key=lambda e: (score_event(e), e.authored_at),
        reverse=True,
    )[:limit]


def build_stats(events: list[RepoEvent]) -> dict[str, int]:
    stats: dict[str, int] = {
        "total": len(events),
        "commits": 0,
        "issues": 0,
        "pull_requests": 0,
        "releases": 0,
        "discussions": 0,
        "closed": 0,
        "merged": 0,
        "contributors": 0,
    }
    bucket = {
        EventKind.COMMIT: "commits",
        EventKind.ISSUE: "issues",
        EventKind.PULL_REQUEST: "pull_requests",
        EventKind.RELEASE: "releases",
        EventKind.DISCUSSION: "discussions",
    }
    authors: set[str] = set()
    for event in events:
        stats[bucket[event.kind]] += 1
        if event.author:
            authors.add(event.author)
        state = (event.state or "").lower()
        if state in {"closed", "merged"}:
            stats[state] += 1
    stats["contributors"] = len(authors)
    return stats


def content_hash(events: list[RepoEvent]) -> str:
    """事件集合的指纹，用于报告幂等：同样的输入不重复调模型。"""
    keys = "|".join(sorted(event.dedup_key for event in events))
    return hashlib.sha256(keys.encode("utf-8")).hexdigest()[:32]


class EventBrief(BaseModel):
    """喂给模型的精简事件，只保留有信息量的字段。"""

    kind: str
    title: str
    author: str = ""
    state: str | None = None
    labels: tuple[str, ...] = ()
    url: str = ""
    authored_at: datetime
    excerpt: str = ""


class DigestInput(BaseModel):
    repo_full_name: str
    repo_title: str
    period_start: datetime
    period_end: datetime
    timezone: str = "Asia/Shanghai"
    style: DigestStyle = DigestStyle.BRIEF
    language: str = "zh"
    stats: dict[str, int] = Field(default_factory=dict)
    events: list[EventBrief] = Field(default_factory=list)
    total_events: int = 0
    truncated: bool = False


class DigestSummary(BaseModel):
    headline: str = ""
    overview_md: str = ""
    highlights: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    model: str = ""

    @property
    def is_empty(self) -> bool:
        return bool(self.headline or self.overview_md or self.highlights)


def build_digest_input(
    sub: Subscription,
    events: list[RepoEvent],
    period_start: datetime,
    period_end: datetime,
    *,
    max_events: int = 120,
    max_ranked: int = 40,
) -> DigestInput:
    ranked = rank_events(events, limit=max_ranked)
    briefs = [
        EventBrief(
            kind=e.kind.value,
            title=e.title,
            author=e.author,
            state=e.state,
            labels=e.labels,
            url=e.url,
            authored_at=e.authored_at,
            excerpt=(e.body_excerpt or "")[:400],
        )
        for e in ranked
    ]
    return DigestInput(
        repo_full_name=sub.repo_full_name,
        repo_title=sub.display_name,
        period_start=period_start,
        period_end=period_end,
        timezone=sub.timezone,
        style=sub.digest_style,
        language=sub.language,
        stats=build_stats(events),
        events=briefs,
        total_events=len(events),
        truncated=len(events) > max_events,
    )
