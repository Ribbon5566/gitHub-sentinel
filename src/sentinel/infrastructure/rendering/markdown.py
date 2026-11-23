"""极简 Markdown → HTML。

只支持我们自己提示词里会出现的语法：标题、无序列表、粗体、行内代码、段落。
不引 markdown 库的原因：多一个依赖、多一套 XSS 面，而我们只需要这五种。
所有文本先做 HTML 转义，再拼结构，避免模型输出里夹带标签。
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _inline(text: str) -> Markup:
    escaped = str(escape(text))
    escaped = _CODE.sub(r"<code>\1</code>", escaped)
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    return Markup(escaped)


def render_markdown(text: str | None) -> Markup:
    if not text:
        return Markup("")

    blocks: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            blocks.append("</ul>")
            in_list = False

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            close_list()
            continue

        stripped = line.lstrip()
        if stripped.startswith(("- ", "* ")):
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            blocks.append(f"<li>{_inline(stripped[2:])}</li>")
            continue

        close_list()
        heading = _HEADING.match(stripped)
        if heading:
            level = min(len(heading.group(1)) + 2, 6)
            blocks.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue

        if stripped.startswith(">"):
            blocks.append(f"<blockquote>{_inline(stripped.lstrip('> ').strip())}</blockquote>")
            continue

        blocks.append(f"<p>{_inline(stripped)}</p>")

    close_list()
    return Markup("\n".join(blocks))
