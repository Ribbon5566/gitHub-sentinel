from __future__ import annotations

from enum import StrEnum


class EventKind(StrEnum):
    COMMIT = "commit"
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"
    RELEASE = "release"
    DISCUSSION = "discussion"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ReportStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class DigestStyle(StrEnum):
    BRIEF = "brief"      # 3-5 条要点，适合邮件速读
    DETAILED = "detailed"  # 分类归纳 + 风险提示，适合周报


class NotifyStatus(StrEnum):
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"


# GitHub 资源名 -> 事件类型。resource 与 REST 端点路径保持一致
RESOURCE_KINDS: dict[str, EventKind] = {
    "commits": EventKind.COMMIT,
    "issues": EventKind.ISSUE,
    "pulls": EventKind.PULL_REQUEST,
    "releases": EventKind.RELEASE,
    "discussions": EventKind.DISCUSSION,
}

# 采集顺序固定：先量大的 commit，再 issue/PR，最后 release
RESOURCE_ORDER: tuple[str, ...] = ("commits", "issues", "pulls", "releases")

DEFAULT_WATCH: frozenset[EventKind] = frozenset(
    {EventKind.COMMIT, EventKind.ISSUE, EventKind.PULL_REQUEST, EventKind.RELEASE}
)


def kinds_for_resources(resources: list[str] | tuple[str, ...]) -> set[EventKind]:
    return {RESOURCE_KINDS[r] for r in resources if r in RESOURCE_KINDS}
