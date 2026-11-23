from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """时间来源抽象。

    测试里替换成 FakeClock 就能把「今天几点」「是不是跨天了」变成可控输入，
    比 freezegun 打补丁更可靠（那类方案对 asyncio 与 C 扩展都不友好）。
    """

    def now(self) -> datetime: ...
