"""提示词。集中在此，便于做 A/B 与回归对比。

每日报告的提示词在 domain/report.py：它和「四章节契约」必须放在一起改，
而 application 层不许 import infrastructure（分层检查会拦）。
"""

from __future__ import annotations

from ...domain.digest import DigestInput

SYSTEM_PROMPT = """你是一名资深研发项目经理，负责把仓库动态整理成给团队看的简报。

硬性要求：
1. 只使用给定的事件列表，不得编造未出现的功能、数字、人名或链接。
2. 数字必须与给定统计一致；无法确定时用「若干」这类模糊表述，不要猜。
3. 突出「对使用者的影响」，而不是复述提交信息。
4. 输出必须是合法 JSON，不要包裹 markdown 代码块，不要加解释性前后缀。

JSON 结构：
{
  "headline": "一句话标题，含仓库名与关键主题，40 字以内",
  "overview_md": "2-4 段 markdown，按主题归纳，不要逐条罗列",
  "highlights": ["3-5 条最值得关注的要点，每条一句话"],
  "risks": ["需要人工确认的风险点，没有就给空数组"]
}"""


def build_user_prompt(request: DigestInput, compact_events: str) -> str:
    style_hint = (
        "风格：速读简报，先结论后细节，总长 300 字以内。"
        if request.style.value == "brief"
        else "风格：完整周报，按「进展 / 修复 / 风险」分节，总长 600 字以内。"
    )
    language_hint = "使用简体中文输出。" if request.language.startswith("zh") else "Write in English."
    return (
        f"{language_hint}\n{style_hint}\n\n"
        f"以下是本周期的仓库动态：\n\n{compact_events}\n\n"
        "请严格按系统提示里的 JSON 结构输出。"
    )

