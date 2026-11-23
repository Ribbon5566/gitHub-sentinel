"""报告落盘。

布局：data/reports/YYYY/MM/{subscription_id}/{stem}.html
数据库里只存**相对 data/ 的路径**，整个 data 目录可以直接打包搬走。

写入用「临时文件 + 原子替换」：中途崩溃不会留下半截 HTML 被看板读到。
每日进展的 md 也走同一个函数，避免两处各写一套 tmp+replace 后漂移。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


def write_text_atomic(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


class ReportStorage:
    def __init__(self, reports_root: Path, data_dir: Path) -> None:
        self._root = reports_root
        self._data_dir = data_dir.resolve()

    def write(
        self, *, sub_id: int, moment: datetime, stem: str, suffix: str, content: str
    ) -> str:
        target = self._root / f"{moment:%Y}" / f"{moment:%m}" / str(sub_id) / f"{stem}{suffix}"
        write_text_atomic(target, content)
        return target.resolve().relative_to(self._data_dir).as_posix()

    def resolve(self, relative: str) -> Path:
        return (self._data_dir / relative).resolve()
