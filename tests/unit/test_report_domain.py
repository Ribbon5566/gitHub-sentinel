from __future__ import annotations

from sentinel.domain.report import (
    APPENDIX_TITLE,
    REPORT_SECTIONS,
    extract_sections,
    normalize_report_body,
    source_sha256,
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
