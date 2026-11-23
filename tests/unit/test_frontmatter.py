from __future__ import annotations

import pytest

from sentinel.domain.errors import ValidationError
from sentinel.domain.frontmatter import (
    dump_front_matter,
    meta_bool,
    meta_int,
    meta_list,
    parse_front_matter,
)


def test_roundtrip_keeps_colon_in_value() -> None:
    """时间戳里全是冒号，不能按冒号切分成 key/value 两半。"""
    block = dump_front_matter({"window_start": "2026-11-22T16:00:00Z", "repo": "a/b"})
    meta, body = parse_front_matter(block + "\n# 标题\n")
    assert meta["window_start"] == "2026-11-22T16:00:00Z"
    assert meta["repo"] == "a/b"
    assert body == "# 标题"


def test_list_values_are_json_and_chinese_is_not_escaped() -> None:
    block = dump_front_matter({"highlights_json": ["已合并 PR #1", "发布 v2.1.0"]})
    meta, _ = parse_front_matter(block)
    assert "\\u" not in block
    assert meta_list(meta, "highlights_json") == ("已合并 PR #1", "发布 v2.1.0")


def test_bool_and_int_helpers_tolerate_empty_values() -> None:
    meta = {"degraded": "false", "issues_total": "12", "empty": ""}
    assert meta_bool(meta, "degraded", default=True) is False
    assert meta_int(meta, "issues_total") == 12
    assert meta_int(meta, "missing", default=7) == 7
    assert meta_bool(meta, "empty", default=True) is True


def test_missing_closing_delimiter_is_an_error() -> None:
    with pytest.raises(ValidationError):
        parse_front_matter("---\nrepo: a/b\n# 正文\n")


def test_text_without_front_matter_is_returned_as_body() -> None:
    meta, body = parse_front_matter("# 标题\n正文\n")
    assert meta == {}
    assert body == "# 标题\n正文\n"


def test_multiline_value_is_flattened() -> None:
    block = dump_front_matter({"note": "第一行\n第二行"})
    assert block.count("\n") == 2
    meta, _ = parse_front_matter(block)
    assert meta["note"] == "第一行 第二行"
