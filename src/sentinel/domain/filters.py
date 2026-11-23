"""事件过滤规则。

约定：过滤在**入库前**执行。否则被过滤掉的事件仍然占用去重键，
用户改规则后也拿不回来，排查时还会看到「库里有但报告里没有」的幽灵数据。
"""

from __future__ import annotations

from dataclasses import dataclass

from .enums import EventKind
from .models import RepoEvent, Subscription

_BOT_SUFFIXES = ("[bot]",)
_KNOWN_BOTS = frozenset({"dependabot", "dependabot-preview", "renovate", "github-actions"})


def _haystack(text: str | None) -> str:
    return (text or "").lower()


def is_bot_author(author: str | None) -> bool:
    """机器人判定。抽出来给每日进展复用，避免两套「什么算 bot」的标准。"""
    name = _haystack(author)
    if not name:
        return False
    return name.endswith(_BOT_SUFFIXES) or name in _KNOWN_BOTS


def _matches_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles if needle)


@dataclass(frozen=True, slots=True)
class EventFilter:
    watch: frozenset[EventKind]
    skip_bots: bool = True
    label_include: tuple[str, ...] = ()
    label_exclude: tuple[str, ...] = ()
    author_exclude: tuple[str, ...] = ()
    keyword_include: tuple[str, ...] = ()
    keyword_exclude: tuple[str, ...] = ()

    @classmethod
    def from_subscription(cls, sub: Subscription) -> EventFilter:
        def norm(values: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(v.strip().lower() for v in values if v and v.strip())

        return cls(
            watch=frozenset(sub.watch),
            skip_bots=sub.skip_bots,
            label_include=norm(sub.label_include),
            label_exclude=norm(sub.label_exclude),
            author_exclude=norm(sub.author_exclude),
            keyword_include=norm(sub.keyword_include),
            keyword_exclude=norm(sub.keyword_exclude),
        )

    def rejection_reason(self, event: RepoEvent) -> str | None:
        if event.kind not in self.watch:
            return f"kind {event.kind.value} 未订阅"

        author = _haystack(event.author)
        if self.skip_bots and is_bot_author(event.author):
            return "机器人账号"

        if author and author in self.author_exclude:
            return "作者在黑名单"

        labels = tuple(label.lower() for label in event.labels)
        if self.label_exclude and any(label in self.label_exclude for label in labels):
            return "标签命中黑名单"

        body = f"{_haystack(event.title)} {_haystack(event.body_excerpt)}"
        if self.keyword_exclude and _matches_any(body, self.keyword_exclude):
            return "关键词命中黑名单"

        if self.label_include and not any(label in self.label_include for label in labels):
            return "标签不在白名单"

        if self.keyword_include and not _matches_any(body, self.keyword_include):
            return "关键词不在白名单"

        return None

    def accepts(self, event: RepoEvent) -> bool:
        return self.rejection_reason(event) is None


def filter_events(
    events: list[RepoEvent], rule: EventFilter
) -> tuple[list[RepoEvent], int]:
    """返回 (保留的事件, 被丢弃数量)。"""
    kept = [event for event in events if rule.accepts(event)]
    return kept, len(events) - len(kept)
