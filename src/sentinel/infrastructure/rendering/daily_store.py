"""每日进展 / 报告的文件布局。

    data/daily/{YYYY}/{MM}/{owner}__{repo}/{YYYY-MM-DD}.md
    data/daily/{YYYY}/{MM}/{owner}__{repo}/{YYYY-MM-DD}-report.md

三条约定：
  1. 目录名走 domain.repo_slug()，不把 owner/repo 直接拼进路径；
  2. 日期目录用「报告日」而不是生成时刻——回补昨天的数据就该落在昨天那一格；
  3. 写入复用 write_text_atomic()，中途崩溃不会留下半份 md 被报告模块读到。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ...domain.errors import ValidationError
from ...domain.frontmatter import parse_front_matter
from ...domain.progress import repo_slug
from ...domain.report import extract_sections
from ...ports.daily import DailyDocument
from .storage import write_text_atomic


class FileDailyStore:
    def __init__(self, *, daily_root: Path, data_dir: Path) -> None:
        self._root = daily_root
        self._data_dir = data_dir.resolve()

    # ------------------------------------------------------------- 进度文档
    def write_progress(self, *, repo_full_name: str, day: date, content: str) -> str:
        target = self._path(repo_full_name, day, report=False)
        write_text_atomic(target, content)
        return self._relative(target)

    def read_progress(self, repo_full_name: str, day: date) -> DailyDocument | None:
        target = self._path(repo_full_name, day, report=False)
        if not target.is_file():
            return None
        markdown = target.read_text(encoding="utf-8")
        meta, body = parse_front_matter(markdown)
        return DailyDocument(
            repo_full_name=repo_full_name,
            day=day,
            relative_path=self._relative(target),
            markdown=markdown,
            meta=meta,
            body=body,
            sections=extract_sections(body),
        )

    # ------------------------------------------------------------- 报告文档
    def write_report(self, *, repo_full_name: str, day: date, content: str) -> str:
        target = self._path(repo_full_name, day, report=True)
        write_text_atomic(target, content)
        return self._relative(target)

    def read_report(self, repo_full_name: str, day: date) -> DailyDocument | None:
        target = self._path(repo_full_name, day, report=True)
        if not target.is_file():
            return None
        markdown = target.read_text(encoding="utf-8")
        try:
            meta, body = parse_front_matter(markdown)
        except ValidationError:
            # 报告损坏时按「不存在」处理：重新生成比抛错更符合预期，
            # 而进度文档损坏必须抛错——那是源头数据，不能悄悄当没有
            return None
        return DailyDocument(
            repo_full_name=repo_full_name,
            day=day,
            relative_path=self._relative(target),
            markdown=markdown,
            meta=meta,
            body=body,
            sections={},
        )

    # ---------------------------------------------------------------- 路径
    def _path(self, repo_full_name: str, day: date, *, report: bool) -> Path:
        folder = self._root / f"{day:%Y}" / f"{day:%m}" / repo_slug(repo_full_name)
        name = f"{day:%Y-%m-%d}-report.md" if report else f"{day:%Y-%m-%d}.md"
        return folder / name

    def _relative(self, target: Path) -> str:
        resolved = target.resolve()
        try:
            return resolved.relative_to(self._data_dir).as_posix()
        except ValueError:
            # 用户把 SENTINEL_DAILY_DIR 指到 data 之外时，给绝对路径，CLI 原样打印
            return resolved.as_posix()
