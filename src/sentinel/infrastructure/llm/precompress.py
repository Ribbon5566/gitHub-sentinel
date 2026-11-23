"""把结构化事件压成「模型友好」的纯文本。

为什么必须先压：一个活跃仓库一天能产生上百条 commit，原始 JSON 动辄几万 token，
既贵又容易让模型抓错重点（它会去编造 diff 内容）。这里只保留归纳必需的信息：
类型、标题、作者、状态、标签、时间、正文摘要。
"""

from __future__ import annotations

from ...domain.digest import DigestInput

_KIND_LABEL = {
    "commit": "提交",
    "issue": "议题",
    "pull_request": "合并请求",
    "release": "发布",
    "discussion": "讨论",
}


def render_compact(request: DigestInput, *, max_chars: int = 12000) -> str:
    header = (
        f"仓库: {request.repo_full_name}\n"
        f"统计: 共 {request.total_events} 条"
        f"（提交 {request.stats.get('commits', 0)}"
        f" / 议题 {request.stats.get('issues', 0)}"
        f" / PR {request.stats.get('pull_requests', 0)}"
        f" / 发布 {request.stats.get('releases', 0)}）"
        f"，贡献者 {request.stats.get('contributors', 0)} 人\n"
        f"时间窗: {request.period_start:%Y-%m-%d %H:%M} ~ {request.period_end:%Y-%m-%d %H:%M}\n"
        f"事件列表:\n"
    )
    lines: list[str] = []
    used = len(header)

    for event in request.events:
        kind = _KIND_LABEL.get(event.kind, event.kind)
        meta = [f"作者={event.author or '未知'}"]
        if event.state:
            meta.append(f"状态={event.state}")
        if event.labels:
            meta.append("标签=" + ",".join(event.labels))
        line = f"- [{kind}] {event.title}（{'；'.join(meta)}；{event.authored_at:%m-%d %H:%M}）"
        if event.excerpt:
            line += f" 摘要: {event.excerpt[:160]}"
        if used + len(line) + 1 > max_chars:
            lines.append("- ...（后续事件已省略，请基于已有信息归纳）")
            break
        lines.append(line)
        used += len(line) + 1

    return header + "\n".join(lines)
