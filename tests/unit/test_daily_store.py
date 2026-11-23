from __future__ import annotations

from datetime import date

import pytest

from sentinel.domain.errors import ValidationError
from sentinel.infrastructure.rendering.daily_store import FileDailyStore

DAY = date(2026, 11, 23)

PROGRESS = """---
repo: fastapi/fastapi
date: 2026-11-23
fingerprint: abc123
---

# FastAPI 每日进展 · 2026-11-23

## 议题（1）

<!-- section:issues:begin -->
| # | 标题 |
|---:|---|
| [#1](https://x) | 偶发 502 |
<!-- section:issues:end -->
"""


@pytest.fixture
def store(tmp_path) -> FileDailyStore:
    return FileDailyStore(daily_root=tmp_path / "data" / "daily", data_dir=tmp_path / "data")


def test_progress_path_includes_repo_slug_and_date(store, tmp_path) -> None:
    relative = store.write_progress(repo_full_name="fastapi/fastapi", day=DAY, content=PROGRESS)
    assert relative == "daily/2026/11/fastapi__fastapi/2026-11-23.md"
    assert (tmp_path / "data" / relative).is_file()


def test_read_progress_returns_meta_body_and_sections(store) -> None:
    store.write_progress(repo_full_name="fastapi/fastapi", day=DAY, content=PROGRESS)
    doc = store.read_progress("fastapi/fastapi", DAY)
    assert doc is not None
    assert doc.meta["fingerprint"] == "abc123"
    assert doc.body.startswith("# FastAPI 每日进展")
    assert "| [#1](https://x) | 偶发 502 |" in doc.sections["issues"]


def test_missing_progress_returns_none(store) -> None:
    assert store.read_progress("fastapi/fastapi", DAY) is None


def test_report_path_and_roundtrip(store) -> None:
    content = "---\nrepo: fastapi/fastapi\ndegraded: false\n---\n\n# 报告\n"
    relative = store.write_report(repo_full_name="fastapi/fastapi", day=DAY, content=content)
    assert relative.endswith("2026-11-23-report.md")
    doc = store.read_report("fastapi/fastapi", DAY)
    assert doc is not None
    assert doc.meta["degraded"] == "false"


def test_corrupted_report_is_treated_as_missing(store, tmp_path) -> None:
    """报告坏了就当没有，重新生成；进度文档坏了必须抛错（那是源头数据）。"""
    target = tmp_path / "data" / "daily" / "2026" / "11" / "fastapi__fastapi" / "2026-11-23-report.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nrepo: fastapi/fastapi\n# 没闭合\n", encoding="utf-8")
    assert store.read_report("fastapi/fastapi", DAY) is None


def test_corrupted_progress_raises(store, tmp_path) -> None:
    target = tmp_path / "data" / "daily" / "2026" / "11" / "fastapi__fastapi" / "2026-11-23.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nrepo: fastapi/fastapi\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        store.read_progress("fastapi/fastapi", DAY)


def test_rewrite_overwrites_without_leaving_tmp_files(store, tmp_path) -> None:
    store.write_progress(repo_full_name="fastapi/fastapi", day=DAY, content=PROGRESS)
    store.write_progress(repo_full_name="fastapi/fastapi", day=DAY, content=PROGRESS + "\n追加\n")
    folder = tmp_path / "data" / "daily" / "2026" / "11" / "fastapi__fastapi"
    assert sorted(path.name for path in folder.iterdir()) == ["2026-11-23.md"]
    assert "追加" in (folder / "2026-11-23.md").read_text(encoding="utf-8")


def test_repo_names_with_dash_and_dot_are_safe(store) -> None:
    relative = store.write_progress(
        repo_full_name="my-org/my.repo-name", day=DAY, content=PROGRESS
    )
    assert relative == "daily/2026/11/my-org__my.repo-name/2026-11-23.md"


def test_daily_root_outside_data_dir_returns_absolute_path(tmp_path) -> None:
    store = FileDailyStore(daily_root=tmp_path / "elsewhere", data_dir=tmp_path / "data")
    relative = store.write_progress(repo_full_name="a/b", day=DAY, content=PROGRESS)
    assert relative.startswith(str((tmp_path / "elsewhere").resolve()).replace("\\", "/"))


def test_stem_overrides_only_the_file_name(store) -> None:
    """目录仍按报告日归档，文件名交给调用方——两套口径才不会抢同一个文件。"""
    relative = store.write_progress(
        repo_full_name="fastapi/fastapi",
        day=DAY,
        content=PROGRESS,
        stem="2026-11-01_to_2026-11-15",
    )
    assert relative == "daily/2026/11/fastapi__fastapi/2026-11-01_to_2026-11-15.md"
    doc = store.read_progress("fastapi/fastapi", DAY, stem="2026-11-01_to_2026-11-15")
    assert doc is not None
    assert doc.relative_path == relative


def test_report_stem_keeps_default_suffix(store) -> None:
    relative = store.write_report(
        repo_full_name="fastapi/fastapi",
        day=DAY,
        content="---\nrepo: fastapi/fastapi\ndegraded: false\n---\n\n# 报告\n",
        stem="2026-11-01_to_2026-11-15.merged",
    )
    assert relative.endswith("2026-11-01_to_2026-11-15.merged-report.md")
    assert (
        store.read_report("fastapi/fastapi", DAY, stem="2026-11-01_to_2026-11-15.merged")
        is not None
    )


def test_read_progress_backfills_period_from_front_matter(store) -> None:
    content = PROGRESS.replace("date: 2026-11-23", "date: 2026-11-15\ndate_from: 2026-11-01")
    store.write_progress(repo_full_name="fastapi/fastapi", day=DAY, content=content, stem="p")
    doc = store.read_progress("fastapi/fastapi", DAY, stem="p")
    assert doc is not None
    assert doc.date_from == date(2026, 11, 1)
    assert doc.date_to == date(2026, 11, 15)
