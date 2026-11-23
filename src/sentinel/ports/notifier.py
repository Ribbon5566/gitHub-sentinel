from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.enums import NotifyStatus


@dataclass(frozen=True, slots=True)
class NotifyMessage:
    report_id: int
    channel: str
    target: str
    subject: str
    html: str
    text: str


@dataclass(frozen=True, slots=True)
class NotifyResult:
    status: NotifyStatus
    detail: str = ""

    @property
    def delivered(self) -> bool:
        return self.status is NotifyStatus.SENT


class Notifier(Protocol):
    channel: str

    async def send(self, message: NotifyMessage) -> NotifyResult: ...

    async def aclose(self) -> None: ...
