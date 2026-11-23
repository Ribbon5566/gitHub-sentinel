from __future__ import annotations

from dataclasses import replace
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
from sentinel.domain.selection import ReportSelection
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


# ------------------------------------------------------ v3.0 区间口径渲染
PERIOD_START = date(2026, 11, 1)
PERIOD_END = date(2026, 11, 15)


def make_period_context() -> DailyProgressContext:
    """完成口径的区间进展上下文：closed_at / merged_at 落在 11-01 ~ 11-15。"""
    window_start = datetime(2026, 10, 31, 16, 0, tzinfo=UTC)
    window_end = datetime(2026, 11, 15, 16, 0, tzinfo=UTC)
    moment = datetime(2026, 11, 10, 4, 0, tzinfo=UTC)
    issues = [
        IssueRecord(
            number=7,
            title="按标签过滤",
            url="https://github.com/acme/widgets/issues/7",
            author="alice",
            state="closed",
            labels=("bug",),
            created_at=datetime(2026, 11, 8, 4, 0, tzinfo=UTC),
            updated_at=moment,
            closed_at=moment,
        )
    ]
    pulls = [
        PullRecord(
            number=11,
            title="feat: 周报汇总",
            url="https://github.com/acme/widgets/pull/11",
            author="bob",
            state="closed",
            merged_at=moment,
            created_at=datetime(2026, 11, 9, 4, 0, tzinfo=UTC),
            updated_at=datetime(2026, 11, 20, 4, 0, tzinfo=UTC),
            additions=40,
            deletions=8,
            changed_files=2,
        )
    ]
    selection = ReportSelection.completion()
    progress = build_daily_progress(
        repo_full_name="acme/widgets",
        repo_title="Widgets",
        day=PERIOD_END,
        date_from=PERIOD_START,
        timezone="Asia/Shanghai",
        window_start=window_start,
        window_end=window_end,
        generated_at=window_end,
        issues=issues,
        pulls=pulls,
        selection=selection,
        max_items=200,
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
        resources=("issues", "pulls"),
        issues=progress.issues,
        pulls=progress.pulls,
        commits=(),
        releases=(),
        stats=progress.stats,
        truncated=progress.truncated,
        highlights=rank_highlights(progress),
        risks=detect_risks(progress),
        summary=summary_line(progress),
        max_items=200,
        date_from=progress.date_from,
        period_label=progress.period_label,
        report_kind_label="区间项目进展",
        scope_completion=progress.completion_mode,
        issue_axis_attr="closed_at",
        pull_axis_attr="merged_at",
        criteria=selection.criteria(
            period_label=progress.period_label, timezone=progress.timezone
        ),
        trimmed=progress.trimmed,
    )


def test_period_progress_uses_completion_vocabulary(renderer) -> None:
    text = renderer.render_progress(make_period_context())
    assert "# Widgets 区间项目进展 · 2026-11-01 ~ 2026-11-15" in text
    assert "| 议题（区间内关闭） | 1 |" in text
    assert "| 合并请求（区间内合并） | 1 |" in text
    # 完成口径不统计 open / draft，这两行必须消失
    assert "议题已关闭" not in text
    assert "合并请求草稿" not in text
    assert "## 议题（1 条已关闭）" in text
    assert "## 合并请求（1 条已合并）" in text


def test_period_progress_columns_read_completion_time(renderer) -> None:
    text = renderer.render_progress(make_period_context())
    # 表头改成「关闭 / 合并」，取值走 closed_at / merged_at
    assert "| # | 标题 | 状态 | 作者 | 标签 | 关闭 | 摘要 |" in text
    assert "| # | 标题 | 状态 | 作者 | 变更 | 合并 | 摘要 |" in text
    assert "2026-11-10 12:00" in text
    # PR 的 updated_at 是 11-20（区间之后），不该出现在合并列
    assert "2026-11-20 12:00" not in text


def test_period_progress_front_matter_carries_period_and_axis(renderer) -> None:
    text = renderer.render_progress(make_period_context())
    head = text.split("---")[1]
    assert "date_from: 2026-11-01" in head
    assert "period_label: 2026-11-01 ~ 2026-11-15" in head
    assert "issue_axis: closed_at" in head
    assert "pull_axis: merged_at" in head


def test_single_day_front_matter_stays_legacy(renderer) -> None:
    """单日产物必须与 v2.0 逐字节兼容：多写一个键就会让既有文件全体变形。"""
    head = renderer.render_progress(make_context()).split("---")[1]
    for key in ("date_from:", "period_label:", "issue_axis:", "pull_axis:", "trimmed:"):
        assert key not in head


def test_period_criteria_lines_replace_legacy_wording(renderer) -> None:
    text = renderer.render_progress(make_period_context())
    assert "- 议题：`closed_at` 落在 2026-11-01 ~ 2026-11-15（Asia/Shanghai）" in text
    assert "- 合并请求：`merged_at` 落在 2026-11-01 ~ 2026-11-15（Asia/Shanghai）" in text
    # v2.0 的单日口径描述不能残留，否则读者会以为统计的是 updated_at
    assert "包含当日新建、有新活动" not in text
    assert "落在区间内" in text


def test_period_progress_reports_trimmed_and_truncated_separately(renderer) -> None:
    # 这两个字段都不在同一个「截断」概念里：一个数据是全的，一个是下界
    context = replace(
        make_period_context(),
        trimmed=frozenset({"issues"}),
        truncated=frozenset({"pulls"}),
    )
    text = renderer.render_progress(context)
    assert "本次触及翻页预算的资源：pulls（统计为下界）" in text
    assert "因展示上限未列出的资源：issues（统计仍按全量）" in text


def test_period_report_title_switches_to_cycle_wording(renderer) -> None:
    body = "## 一句话结论\n\n稳。\n\n## 关键进展\n\n- x\n\n## 风险与阻塞\n\n无。\n\n## 下步计划\n\n- y"
    meta = {
        "repo": "acme/widgets",
        "repo_title": "Widgets",
        "date": "2026-11-15",
        "period_label": "2026-11-01 ~ 2026-11-15",
    }
    text = renderer.render_report(meta=meta, body_md=body, sections={})
    assert "# Widgets 项目研发周期报告 · 2026-11-01 ~ 2026-11-15" in text


def test_fallback_body_in_completion_mode_counts_deliverables(renderer) -> None:
    body = renderer.render_fallback_body(
        DailyReportContext(
            repo_full_name="acme/widgets",
            repo_title="Widgets",
            day=PERIOD_END,
            timezone="Asia/Shanghai",
            stats={
                "issues_total": 4,
                "issues_closed": 4,
                "issues_open": 0,
                "pulls_total": 3,
                "pulls_merged": 3,
                "pulls_open": 0,
                "releases_total": 1,
            },
            highlights=(),
            risks=(),
            scope_completion=True,
            period_label="2026-11-01 ~ 2026-11-15",
        )
    )
    assert "本期 4 个议题关闭、3 个 PR 合并、1 个版本发布。" in body
    assert "核对本期交付清单" in body
    # 完成口径没有「未关闭议题」这个概念，这类计划项必须消失
    assert "跟进 0 个未关闭议题" not in body

