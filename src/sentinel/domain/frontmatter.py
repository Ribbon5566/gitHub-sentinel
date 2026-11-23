"""极简 front matter 读写（只服务我们自己生成的进度/报告文档）。

不引 PyYAML：这里写的永远是 `key: value` 与 JSON 值，解析规则不到 40 行；
引一个完整 YAML 解析器只会多一个依赖、多一套隐式类型转换（`2026-11-23` 会变成 date，
`no` 会变成 False），反而更难排查。

放在文档头而不是 sidecar json 的理由：元信息与正文同文件，
手工复制一份 md 给同事时统计与指纹不会丢。
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from .errors import ValidationError

DELIMITER = "---"


def dump_front_matter(fields: Mapping[str, object]) -> str:
    """输出 `---` 包裹的元信息块，末尾不带空行（调用方自己控制与正文的间隔）。"""
    lines = [DELIMITER]
    for key, value in fields.items():
        lines.append(f"{key}: {_dump_value(value)}")
    lines.append(DELIMITER)
    return "\n".join(lines)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """返回 (meta, body)。

    只认文件首行的 `---`；没有就当作无元信息。缺少闭合 `---` 视为文件损坏，
    宁可抛错让上层重跑，也不要读进半份元信息后生成一份数字对不上的报告。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != DELIMITER:
        return {}, text

    meta: dict[str, str] = {}
    closing: int | None = None
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == DELIMITER:
            closing = index
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, raw = line.partition(":")
        if not sep:
            raise ValidationError(f"front matter 第 {index + 1} 行缺少冒号：{line!r}")
        meta[key.strip()] = raw.strip()

    if closing is None:
        raise ValidationError("front matter 缺少闭合的 ---")
    body = "\n".join(lines[closing + 1 :]).lstrip("\n")
    return meta, body


def meta_int(meta: Mapping[str, str], key: str, default: int = 0) -> int:
    raw = meta.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValidationError(f"front matter 字段 {key} 不是整数：{raw!r}") from exc


def meta_bool(meta: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = meta.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def meta_list(meta: Mapping[str, str], key: str) -> tuple[str, ...]:
    """读 JSON 数组字段；不是合法 JSON 时按逗号切分，兼容手工编辑。"""
    raw = meta.get(key, "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if isinstance(parsed, list):
        return tuple(str(item) for item in parsed)
    return (str(parsed),)


def meta_frozenset(meta: Mapping[str, str], key: str) -> frozenset[str]:
    return frozenset(meta_list(meta, key))


def _dump_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (set, frozenset)):
        return json.dumps(sorted(value), ensure_ascii=False, separators=(",", ":"))
    # 换行会破坏「一行一个字段」的结构，直接压平
    return " ".join(str(value).split())
