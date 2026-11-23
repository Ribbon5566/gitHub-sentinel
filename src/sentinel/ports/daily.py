from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DailyDocument:
    """读回来的每日文档。

    `sections` 来自进度 md 里的 `<!-- section:x -->` 块，
    报告生成时直接复用这些表格，既不重新请求 GitHub、也不重新拼。
    """

    repo_full_name: str
    day: date
    relative_path: str
    markdown: str
    meta: dict[str, str]
    body: str
    sections: dict[str, str]


class DailyStorePort(Protocol):
    """每日进展 / 报告的文件存储。

    应用层不直接碰 Path：换布局（比如按季度归档）只需要改实现。
    """

    def write_progress(self, *, repo_full_name: str, day: date, content: str) -> str: ...

    def read_progress(self, repo_full_name: str, day: date) -> DailyDocument | None: ...

    def write_report(self, *, repo_full_name: str, day: date, content: str) -> str: ...

    def read_report(self, repo_full_name: str, day: date) -> DailyDocument | None: ...
