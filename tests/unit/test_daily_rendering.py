from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from sentinel.config import Settings
from sentinel.domain.progress import (
    CommitRecord,
    IssueRecord,
    PullRecord,
    ReleaseRecord,
    build_daily_progress,
    detect_risks,
    rank_highlights,
    summary_line,
)
from sentinel.infrastructure.rendering.daily_renderer import JinjaDailyRenderer
from sentinel.ports.renderer import DailyProgressContext, DailyReportContext

DAY = date(2026, 11, 23)
NOON = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def renderer(tmp_path) -> JinjaDailyRenderer:
    return JinjaDailyRenderer(Settings(data_dir=tmp_path, timezone="Asia/Shanghai"))


def make_context(*, title: str = "偶发 502", with_commits: bool = False) -> DailyProgressContext:
    start = datetime(2026, 11, 22, 16, 0, tzinfo=UTC)
    end = datetime(2026, 11, 23, 16, 0, tzinfo=UTC)
    issues = [
        IssueRecord(
            number=1,
            title=title,
            url="https://github.com/acme/widgets/issues/1",
            author="alice",
            state="closed",
            labels=("bug",),
            created_at=NOON,
            updated_at=NOON,
            closed_at=NOON,
            comments=2,
            body_excerpt="现象：偶发中断。",
        )
    ]
    pulls = [
        PullRecord(
            number=10,
            title="feat: 增加周报",
            url="https://github.com/acme/widgets/pull/10",
            author="bob",
            state="closed",
            merged_at=NOON,
            created_at=NOON,
            updated_at=NOON,
            additions=120,
            deletions=30,
            changed_files=5,
        )
    ]
    commits = [CommitRecord(sha="a" * 40, title="fix: 修正", author="carol", committed_at=NOON)]
    releases = [ReleaseRecord(tag="v2.1.0", name="Release v2.1.0", published_at=NOON)]
    progress = build_daily_progress(
        repo_full_name="acme/widgets",
        repo_title="Widgets",
        day=DAY,
        timezone="Asia/Shanghai",
        window_start=start,
        window_end=end,
        generated_at=end,
        issues=issues,
        pulls=pulls,
        commits=commits if with_commits else (),
        releases=releases,
    )
    return DailyProgressContext(
        repo_full_name=progress.repo_full_name,
        repo_title=progress.repo_title,
        day=progress.day,
        timezone=progress.timezone,
        window_start=progress.window_start,
        window_end=progress.window_end,
        generated_at=progress.generated_at,
        fingerprint=progress.fingerprint,
        resources=("issues", "pulls", "commits") if with_commits else ("issues", "pulls"),
        issues=progress.issues,
        pulls=progress.pulls,
        commits=progress.commits,
        releases=progress.releases,
        stats=progress.stats,
        truncated=progress.truncated,
        highlights=rank_highlights(progress),
        risks=detect_risks(progress),
        summary=summary_line(progress),
        max_items=200,
    )


def test_progress_markdown_has_front_matter_and_sections(renderer) -> None:
    text = renderer.render_progress(make_context())
    assert text.startswith("---\nrepo: acme/widgets\n")
    assert "fingerprint:" in text
    assert "# Widgets 每日进展 · 2026-11-23" in text
    assert "| 议题（当日有更新） | 1 |" in text
    assert "<!-- section:issues:begin -->" in text
    assert "<!-- section:pulls:end -->" in text
    assert "## 数据口径" in text


def test_pipe_in_title_is_escaped(renderer) -> None:
    text = renderer.render_progress(make_context(title="a | b"))
    assert r"a \| b" in text
    # 表格列数不能被破坏：转义后的竖线不计入分隔
    row = next(line for line in text.splitlines() if line.startswith("| [#1]"))
    assert row.count(" | ") >= 6


def test_commits_section_only_when_requested(renderer) -> None:
    without = renderer.render_progress(make_context())
    assert "## 代码提交" not in without
    with_commits = renderer.render_progress(make_context(with_commits=True))
    assert "## 代码提交（1 次）" in with_commits


def test_empty_resource_renders_placeholder_row(renderer) -> None:
    context = make_context()
    empty = DailyProgressContext(
        repo_full_name=context.repo_full_name,
        repo_title=context.repo_title,
        day=context.day,
        timezone=context.timezone,
        window_start=context.window_start,
        window_end=context.window_end,
        generated_at=context.generated_at,
        fingerprint=context.fingerprint,
        resources=("issues",),
        issues=(),
        pulls=(),
        commits=(),
        releases=(),
        stats=context.stats,
        truncated=frozenset(),
        highlights=(),
        risks=(),
        summary="本日无更新",
        max_items=200,
    )
    text = renderer.render_progress(empty)
    assert "本日无议题更新" in text


def test_report_markdown_reuses_sections_as_appendix(renderer) -> None:
    progress = renderer.render_progress(make_context())
    body = "## 一句话结论\n\n稳。\n\n## 关键进展\n\n- 合并了 PR。\n\n## 风险与阻塞\n\n无。\n\n## 下步计划\n\n- 继续。"
    sections = {
        "issues": "| # | 标题 |\n|---:|---|\n| [#1](https://x) | 偶发 502 |",
        "pulls": "| # | 标题 |\n|---:|---|\n| [#10](https://y) | 周报 |",
    }
    meta = {
        "repo": "acme/widgets",
        "repo_title": "Widgets",
        "date": "2026-11-23",
        "model": "deepseek-flash",
        "degraded": "false",
    }
    text = renderer.render_report(meta=meta, body_md=body, sections=sections)
    assert text.startswith("---\nrepo: acme/widgets\n")
    assert "# Widgets 项目每日报告 · 2026-11-23" in text
    assert "## 附录：明细数据" in text
    assert "### 议题" in text and "### 合并请求" in text
    assert "<!-- section:" not in text
    assert "偶发 502" in text
    # 进度文档本身仍然是独立可读的
    assert "每日进展" in progress


def test_report_without_sections_has_no_appendix(renderer) -> None:
    body = "## 一句话结论\n\n稳。\n\n## 关键进展\n\n- x\n\n## 风险与阻塞\n\n无。\n\n## 下步计划\n\n- y"
    text = renderer.render_report(meta={"repo": "a/b", "date": "2026-11-23"}, body_md=body, sections={})
    assert "附录" not in text


def test_fallback_body_has_four_sections(renderer) -> None:
    body = renderer.render_fallback_body(
        DailyReportContext(
            repo_full_name="acme/widgets",
            repo_title="Widgets",
            day=DAY,
            timezone="Asia/Shanghai",
            stats={"issues_total": 3, "issues_open": 2, "pulls_total": 2, "pulls_merged": 1},
            highlights=(),
            risks=(),
        )
    )
    for name in ("一句话结论", "关键进展", "风险与阻塞", "下步计划"):
        assert f"## {name}" in body
    assert "无。" in body
    assert "跟进 2 个未关闭议题" in body
