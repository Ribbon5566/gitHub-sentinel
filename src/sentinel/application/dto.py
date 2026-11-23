from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.models import CollectRun, DigestReport, Subscription


@dataclass(slots=True)
class CollectOutcome:
    subscription: Subscription
    run: CollectRun
    inserted: int = 0
    skipped: int = 0
    not_modified: int = 0
    bootstrap: bool = False
    resources: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class DigestOutcome:
    report: DigestReport | None
    reused: bool = False
    reason: str | None = None

    @property
    def created(self) -> bool:
        return self.report is not None


@dataclass(slots=True)
class NotifyOutcome:
    status: str
    detail: str = ""
    skipped: bool = False


@dataclass(slots=True)
class PipelineOutcome:
    subscription_id: int
    collect: CollectOutcome
    digest: DigestOutcome | None = None
    notify: NotifyOutcome | None = None
    error: str | None = None
