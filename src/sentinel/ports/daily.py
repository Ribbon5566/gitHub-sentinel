from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DailyDocument:
    """读回来的每日/区间文档。

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
    # 区间报告才有值；单日文档与 day 相同或为空
    date_from: date | None = None
    date_to: date | None = None


class DailyStorePort(Protocol):
    """每日进展 / 报告的文件存储。

    应用层不直接碰 Path：换布局（比如按季度归档）只需要改实现。

    文件名由调用方通过 `stem` 决定，存储只负责「按这个主干名拼路径」：
    命名策略属于领域（区间、口径、默认后缀），放在存储里会让两套产物
    抢同一个文件名。`stem=None` 时沿用 v2.0 的 `YYYY-MM-DD`。
    """

    def write_progress(
        self, *, repo_full_name: str, day: date, content: str, stem: str | None = None
    ) -> str: ...

    def read_progress(
        self, repo_full_name: str, day: date, *, stem: str | None = None
    ) -> DailyDocument | None: ...

    def write_report(
        self, *, repo_full_name: str, day: date, content: str, stem: str | None = None
    ) -> str: ...

    def read_report(
        self, repo_full_name: str, day: date, *, stem: str | None = None
    ) -> DailyDocument | None: ...
