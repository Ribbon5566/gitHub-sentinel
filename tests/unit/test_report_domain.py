from __future__ import annotations

from datetime import date

from sentinel.domain.report import (
    APPENDIX_TITLE,
    REPORT_SECTIONS,
    REPORT_SYSTEM_PROMPT,
    build_report_user_prompt,
    extract_sections,
    normalize_report_body,
    progress_headings,
    report_system_prompt,
    source_sha256,
    stat_labels,
    strip_section_markers,
    truncate_for_llm,
)

GOOD = """## 一句话结论

本日交付平稳，2 个 PR 合并、1 个版本发布。

## 关键进展

### 稳定性
- 修复采集偶发中断。

## 风险与阻塞

无。

## 下步计划

1. 跟进未关闭议题。
"""


def test_normalize_accepts_four_sections() -> None:
    body = normalize_report_body(GOOD)
    assert body is not None
    for name in REPORT_SECTIONS:
        assert f"## {name}" in body
    assert "### 稳定性" in body


def test_normalize_rewrites_heading_levels_and_order() -> None:
    shuffled = """**风险与阻塞**

暂无。

### 一句话结论

一句话。

### 关键进展

- 做了点事。

### 下步计划

- 继续。
"""
    body = normalize_report_body(shuffled)
    assert body is not None
    assert body.index("## 一句话结论") < body.index("## 关键进展") < body.index("## 风险与阻塞")


def test_normalize_rejects_missing_section() -> None:
    """空着的「风险与阻塞」比缺报告更危险，必须判定失败。"""
    assert normalize_report_body("## 一句话结论\n\n好。\n\n## 关键进展\n\n- x\n") is None


def test_normalize_rejects_empty_section_content() -> None:
    body = GOOD.replace("无。", "")
    assert normalize_report_body(body) is None


def test_normalize_strips_code_fence_and_drops_model_appendix() -> None:
    wrapped = "```markdown\n" + GOOD + "\n## 附录\n\n| a | b |\n```\n"
    body = normalize_report_body(wrapped)
    assert body is not None
    assert APPENDIX_TITLE not in body
    assert "| a | b |" not in body
    assert "```" not in body


def test_normalize_returns_none_on_garbage() -> None:
    assert normalize_report_body("") is None
    assert normalize_report_body("抱歉，我无法完成。") is None


def test_truncate_keeps_whole_lines_and_adds_note() -> None:
    markdown = "\n".join(f"- 第 {index} 行" for index in range(200))
    text, truncated = truncate_for_llm(markdown, 200)
    assert truncated is True
    assert len(text) < len(markdown)
    assert "截断" in text
    # 截断点落在行边界上：最后保留的行是完整的，不会出现「- 第 2」这种半行
    assert "- 第 22 行" in text
    assert "- 第 23 行" not in text


def test_truncate_is_noop_when_short_enough() -> None:
    text, truncated = truncate_for_llm("短文本", 100)
    assert (text, truncated) == ("短文本", False)


def test_section_extraction_ignores_unclosed_block() -> None:
    markdown = (
        "# 进展\n\n<!-- section:issues:begin -->\n| # | 标题 |\n<!-- section:issues:end -->\n\n"
        "<!-- section:pulls:begin -->\n| # | 标题 |\n"
    )
    sections = extract_sections(markdown)
    assert sections == {"issues": "| # | 标题 |"}


def test_strip_markers_keeps_table_rows() -> None:
    markdown = "<!-- section:issues:begin -->\n| # | 标题 |\n<!-- section:issues:end -->"
    stripped = strip_section_markers(markdown)
    assert stripped.strip() == "| # | 标题 |"


def test_source_hash_reacts_to_edits() -> None:
    assert source_sha256(GOOD) == source_sha256(GOOD)
    assert source_sha256(GOOD) != source_sha256(GOOD + "\n手工加一行")


# --------------------------------------------------- v3.0 周期报告提示词
def test_system_prompt_is_untouched_for_single_day() -> None:
    """单日必须逐字复用 v2.0 的系统提示：改一个字都会让历史报告口径漂移。"""
    assert report_system_prompt() == REPORT_SYSTEM_PROMPT


def test_system_prompt_switches_lead_for_period() -> None:
    text = report_system_prompt(period=True)
    assert "项目研发周期报告" in text
    assert "项目每日报告" not in text
    # 章节契约的其余部分原样保留
    assert REPORT_SYSTEM_PROMPT.split("\n", 1)[1] in text


def test_system_prompt_appends_scope_constraint_last() -> None:
    note = "本期口径为完成时间：只统计区间内合并的 PR 与关闭的 Issue。"
    text = report_system_prompt(period=True, scope_note=note)
    assert text.rstrip().endswith(note)
    assert "\n8. " in text


def test_completion_stat_labels_drop_open_metrics() -> None:
    keys = [key for key, _ in stat_labels(True)]
    assert keys == ["issues_closed", "pulls_merged", "commits_total", "releases_total", "contributors"]
    # 活动口径仍是 v2.0 的八项，且含「草稿/总数」这类只有活动口径才有意义的指标
    live_keys = [key for key, _ in stat_labels(False)]
    assert live_keys[:2] == ["issues_total", "issues_closed"]
    assert "pulls_draft" in live_keys


def test_progress_headings_switch_time_column() -> None:
    stats = {"issues_closed": 2, "pulls_merged": 1}
    done = progress_headings(True, stats)
    assert done["issue_time_header"] == "关闭"
    assert done["pull_time_header"] == "合并"
    assert done["issue_heading"] == "议题（2 条已关闭）"
    live = progress_headings(False, {"issues_total": 5, "pulls_total": 3})
    assert live["issue_time_header"] == "更新"
    assert live["issue_heading"] == "议题（5 条有更新）"


def test_user_prompt_uses_period_wording_when_range_given() -> None:
    text = build_report_user_prompt(
        repo_full_name="acme/widgets",
        repo_title="Widgets",
        day=date(2026, 11, 15),
        timezone="Asia/Shanghai",
        stats={"issues_closed": 3, "pulls_merged": 2, "commits_total": 0, "releases_total": 1},
        highlights=(),
        risks=(),
        progress_md="# 进展",
        truncated=False,
        period_label="2026-11-01 ~ 2026-11-15",
        scope_label="完成口径（Issue `closed_at` / PR `merged_at`）",
        completion=True,
    )
    assert "报告周期：2026-11-01 ~ 2026-11-15（Asia/Shanghai）" in text
    assert "口径：完成口径" in text
    assert "项目进展 Markdown" in text
    # 完成口径下不能再出现「议题已关闭 N」这类双重计数的指标
    assert "议题（区间内关闭） 3" in text
    assert "议题已关闭" not in text


def test_user_prompt_stays_daily_without_range() -> None:
    text = build_report_user_prompt(
        repo_full_name="acme/widgets",
        repo_title="Widgets",
        day=date(2026, 11, 23),
        timezone="Asia/Shanghai",
        stats={"issues_total": 4, "issues_open": 1, "pulls_total": 2, "pulls_merged": 1},
        highlights=(),
        risks=(),
        progress_md="# 进展",
        truncated=False,
    )
    assert "报告日期：2026-11-23（Asia/Shanghai）" in text
    assert "每日进展 Markdown" in text
    assert "口径：" not in text
