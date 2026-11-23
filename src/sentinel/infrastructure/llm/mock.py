"""确定性摘要：没有 API Key 时的兜底，也是测试里的基准。

它不是「空壳」：统计、要点排序、风险提示全部来自 domain 的规则计算，
只是少了语言润色。这样离线演示看到的报告结构与接了模型后一致。
"""

from __future__ import annotations

from ...domain.completion import CompletionRequest, CompletionResult
from ...domain.digest import DigestInput, DigestSummary
from ...domain.enums import EventKind
from ...domain.errors import LLMError
from ...domain.schedule import resolve_tz

_KIND_LABEL = {
    "commit": "提交",
    "issue": "议题",
    "pull_request": "合并请求",
    "release": "发布",
    "discussion": "讨论",
}

_RISK_LABELS = ("breaking", "breaking-change", "security", "critical", "regression")


class MockLLM:
    name = "mock"

    async def summarize(self, request: DigestInput) -> DigestSummary:
        stats = request.stats
        total = request.total_events or stats.get("total", 0)
        if total == 0:
            return DigestSummary(
                headline=f"{request.repo_title} 本期没有新动态",
                overview_md="本周期内该仓库没有产生被订阅的事件。",
                model="mock-rules",
            )

        headline = (
            f"{request.repo_title}：{total} 条更新，"
            f"{stats.get('contributors', 0)} 位贡献者参与"
        )

        # 时间一律按订阅时区展示；DigestInput 里是 UTC
        tz = resolve_tz(request.timezone)
        start = request.period_start.astimezone(tz)
        end = request.period_end.astimezone(tz)
        lines = [
            f"本周期（{start:%m-%d %H:%M} ~ {end:%m-%d %H:%M}）共捕获 **{total}** 条动态：",
            "",
            f"- 代码提交 {stats.get('commits', 0)} 次",
            f"- 议题 {stats.get('issues', 0)} 条，其中 {stats.get('closed', 0)} 条已关闭",
            f"- 合并请求 {stats.get('pull_requests', 0)} 条，其中 {stats.get('merged', 0)} 条已合并",
            f"- 新版本发布 {stats.get('releases', 0)} 个",
            f"- 参与贡献者 {stats.get('contributors', 0)} 位",
        ]
        if request.truncated:
            lines.append("")
            lines.append("> 事件量较大，摘要仅覆盖权重最高的部分，完整明细见报告末尾列表。")

        highlights = tuple(
            f"{_KIND_LABEL.get(event.kind, event.kind)}：{event.title}"
            + (f"（{event.author}）" if event.author else "")
            for event in sorted(
                request.events,
                key=lambda e: (_score_hint(e), e.authored_at),
                reverse=True,
            )[:5]
        )

        risks = []
        for event in request.events:
            labels = " ".join(event.labels).lower()
            if any(label in labels for label in _RISK_LABELS):
                risks.append(f"「{event.title}」带有高风险标签（{', '.join(event.labels)}），建议确认影响范围")
            if len(risks) >= 3:
                break

        return DigestSummary(
            headline=headline,
            overview_md="\n".join(lines),
            highlights=highlights,
            risks=tuple(risks),
            model="mock-rules",
        )

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """刻意抛错而不是返回伪文本。

        每日报告需要一个「没有模型也要有产出」的兜底，但那个兜底必须由
        application 层的规则模板负责（它知道统计数字与风险清单）；
        这里如果拼一段像模像样的文字，反而会掩盖「其实没调通模型」这件事。
        """
        raise LLMError("MockLLM 不支持自由文本生成，请使用规则兜底")

    async def aclose(self) -> None:
        return None


def _score_hint(event: object) -> int:
    """DigestInput.events 是 EventBrief，不是 RepoEvent，这里用一个轻量映射。"""
    weight = {
        EventKind.RELEASE: 100,
        EventKind.PULL_REQUEST: 60,
        EventKind.ISSUE: 50,
        EventKind.DISCUSSION: 40,
        EventKind.COMMIT: 20,
    }
    kind = getattr(event, "kind", "commit")
    state = (getattr(event, "state", "") or "").lower()
    return weight.get(EventKind(kind), 10) + (15 if state in {"closed", "merged"} else 0)
