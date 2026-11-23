"""进度文档的区块切分 + 最终报告的章节校验。

两个方向相反的转换都放这里，因为它们是同一份「格式契约」的两端：
  * 进度 md 用 `<!-- section:issues:begin -->` 包住表格，报告附录直接复用这些块，
    不重新请求 GitHub、也不重新拼表格；
  * 模型返回的报告正文必须能切成四个固定章节，切不出来就判定失败、走规则兜底。
    宁可出一份朴素的报告，也不要把半截模型输出落盘。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import date

REPORT_SECTIONS: tuple[str, ...] = ("一句话结论", "关键进展", "风险与阻塞", "下步计划")
APPENDIX_TITLE = "附录：明细数据"

REPORT_SYSTEM_PROMPT = """你是一名研发负责人，正在向管理层提交项目每日报告。

硬性要求：
1. 只依据给定的每日进展 Markdown，不得编造未出现的功能、数字、人名、链接或日期。
2. 数字必须与「统计摘要」一致；无法确定时用「若干」这类表述，不要猜。
3. 按主题或模块归类归纳，不要逐条复述 issue/PR 标题；不要输出表格，附录由系统生成。
4. 输出简体中文 Markdown，严格使用以下四个二级标题，顺序不变：
## 一句话结论
## 关键进展
## 风险与阻塞
## 下步计划
5. 「一句话结论」只写 1 句，不超过 60 字；「关键进展」3-6 条；
   「风险与阻塞」没有就写「无」；「下步计划」2-4 条，必须是可执行动作。
6. 不要输出一级标题、附录、代码块围栏或解释性前后缀。
7. 不要把「规则线索」原样抄成句子，它只是给你定位重点用的。"""

_STAT_LABELS: tuple[tuple[str, str], ...] = (
    ("issues_total", "议题（当日有更新）"),
    ("issues_closed", "议题已关闭"),
    ("pulls_total", "合并请求（当日有更新）"),
    ("pulls_merged", "合并请求已合并"),
    ("pulls_draft", "合并请求草稿"),
    ("commits_total", "代码提交"),
    ("releases_total", "版本发布"),
    ("contributors", "参与贡献者"),
)

# 完成口径只讲「本期交付了什么」，不再罗列未关闭/草稿这类活动口径的指标
_COMPLETION_STAT_LABELS: tuple[tuple[str, str], ...] = (
    ("issues_closed", "议题（区间内关闭）"),
    ("pulls_merged", "合并请求（区间内合并）"),
    ("commits_total", "代码提交"),
    ("releases_total", "版本发布"),
    ("contributors", "参与贡献者"),
)


def stat_labels(completion: bool) -> tuple[tuple[str, str], ...]:
    """统计表与提示词共用一份标签：两处各写一套必然漂移。"""
    return _COMPLETION_STAT_LABELS if completion else _STAT_LABELS


def progress_headings(completion: bool, stats: Mapping[str, int]) -> dict[str, str]:
    """进展文档的小节标题与空表占位文案。

    完成口径不能写「本日 N 个议题有更新」——那些对象本来就已经关闭了。
    """
    if completion:
        return {
            "issue_heading": f"议题（{stats.get('issues_closed', 0)} 条已关闭）",
            "pull_heading": f"合并请求（{stats.get('pulls_merged', 0)} 条已合并）",
            "commit_heading": f"代码提交（{stats.get('commits_total', 0)} 次）",
            "release_heading": f"版本发布（{stats.get('releases_total', 0)} 个）",
            "empty_issue": "区间内没有关闭的议题",
            "empty_pull": "区间内没有合并的 PR",
            "empty_commit": "区间内没有提交",
            "empty_release": "区间内没有发布",
            "issue_time_header": "关闭",
            "pull_time_header": "合并",
        }
    return {
        "issue_heading": f"议题（{stats.get('issues_total', 0)} 条有更新）",
        "pull_heading": f"合并请求（{stats.get('pulls_total', 0)} 条有更新）",
        "commit_heading": f"代码提交（{stats.get('commits_total', 0)} 次）",
        "release_heading": f"版本发布（{stats.get('releases_total', 0)} 个）",
        "empty_issue": "本日无议题更新",
        "empty_pull": "本日无 PR 更新",
        "empty_commit": "本日无提交",
        "empty_release": "本日无发布",
        "issue_time_header": "更新",
        "pull_time_header": "更新",
    }


def report_system_prompt(*, period: bool = False, scope_note: str = "") -> str:
    """按日报 / 周期报告切换首句，并追加口径硬约束。

    章节契约（REPORT_SECTIONS 与 normalize_report_body）完全不动：
    改动它会连带改兜底逻辑，收益远小于风险。
    """
    if not period and not scope_note:
        return REPORT_SYSTEM_PROMPT
    lead = (
        "你是一名研发负责人，正在向管理层提交项目研发周期报告。"
        if period
        else "你是一名研发负责人，正在向管理层提交项目每日报告。"
    )
    rest = REPORT_SYSTEM_PROMPT.split("\n", 1)[1]
    text = f"{lead}\n{rest}"
    if scope_note:
        text += f"\n8. {scope_note}"
    return text

_SECTION_START = re.compile(r"^\s*<!--\s*section:([a-z_]+):begin\s*-->\s*$")
_SECTION_END = re.compile(r"^\s*<!--\s*section:([a-z_]+):end\s*-->\s*$")
_HEADING = re.compile(r"^(#{1,6})\s*(.+?)\s*$")
_BOLD_TITLE = re.compile(r"^\*\*(.+?)\*\*[：:]?\s*$")
_TRUNCATION_NOTE = (
    "\n\n（说明：上面的进展明细因过长被截断，未包含全部条目；"
    "请只依据现有信息归纳，不要推测被截断的部分。）\n"
)


def strip_section_markers(markdown: str) -> str:
    """删掉区块标记：喂给模型的内容不该出现内部结构注释。"""
    kept = [
        line
        for line in markdown.splitlines()
        if not (_SECTION_START.match(line) or _SECTION_END.match(line))
    ]
    return "\n".join(kept)


def extract_sections(markdown: str) -> dict[str, str]:
    """从进度文档里取出 `<!-- section:x -->` 包住的正文。

    未闭合的块直接忽略：宁可附录少一块，也不要把后半篇文档塞进某一个表格下。
    """
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    for line in markdown.splitlines():
        start = _SECTION_START.match(line)
        if start:
            current = start.group(1)
            buffer = []
            continue
        end = _SECTION_END.match(line)
        if end:
            if current and end.group(1) == current:
                sections[current] = "\n".join(buffer).strip()
            current = None
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    return sections


def normalize_report_body(text: str) -> str | None:
    """校验并规范化模型输出；不合格返回 None。

    宽容的地方：`### 一句话结论`、`**一句话结论**` 这类写法统一成 `##`，
    章节顺序被打乱也按固定顺序重排。
    严格的地方：四节必须都有内容，缺一节就返回 None——空着的「风险与阻塞」
    会让读者以为没有风险，这是最危险的误导。
    """
    body = _strip_fences(text or "").strip()
    if not body:
        return None

    collected: dict[str, list[str]] = {}
    current: str | None = None

    for line in body.splitlines():
        heading = _heading_title(line)
        if heading is not None:
            level, title = heading
            if level <= 2 and (title.startswith("附录") or title.lower().startswith("appendix")):
                break  # 附录由系统生成，模型写的直接丢弃
            if title in REPORT_SECTIONS:
                current = title
                collected.setdefault(current, [])
                continue
            if level == 1:
                continue  # 模型自己加的一级标题，丢掉（标题由渲染器统一生成）
        if current is not None:
            collected[current].append(line)

    chunks: list[str] = []
    for name in REPORT_SECTIONS:
        content = "\n".join(collected.get(name, [])).strip()
        if not content:
            return None
        chunks.append(f"## {name}\n\n{content}")
    return "\n\n".join(chunks)


def truncate_for_llm(markdown: str, max_chars: int) -> tuple[str, bool]:
    """按行截断到 max_chars，尽量不把一行表格切成两半。"""
    if max_chars <= 0 or len(markdown) <= max_chars:
        return markdown, False
    head = markdown[:max_chars]
    cut = head.rfind("\n")
    if cut > max_chars * 0.6:
        head = head[:cut]
    return head.rstrip() + _TRUNCATION_NOTE, True


def source_sha256(markdown: str) -> str:
    """进度文档全文指纹：识别「有人手工改过 md」，避免报告与原文脱节。"""
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:32]


def build_report_user_prompt(
    *,
    repo_full_name: str,
    repo_title: str,
    day: date,
    timezone: str,
    stats: Mapping[str, int],
    highlights: tuple[str, ...],
    risks: tuple[str, ...],
    progress_md: str,
    truncated: bool,
    period_label: str = "",
    scope_label: str = "",
    completion: bool = False,
) -> str:
    """报告提示词与四章节契约放在同一个文件：改章节就必须同时改提示词。"""
    stat_line = " · ".join(
        f"{label} {stats[key]}" for key, label in stat_labels(completion) if stats.get(key) is not None
    )
    hints = "\n".join(f"- {item}" for item in highlights) or "- 无"
    risk_hints = "\n".join(f"- {item}" for item in risks) or "- 无"
    truncated_note = (
        "\n注意：进展明细因过长被截断，不要推测未出现的条目。\n" if truncated else ""
    )
    when = (
        f"报告周期：{period_label}（{timezone}）"
        if period_label
        else f"报告日期：{day:%Y-%m-%d}（{timezone}）"
    )
    scope_line = f"\n口径：{scope_label}" if scope_label else ""
    progress_title = "项目进展 Markdown" if period_label else "每日进展 Markdown"
    return (
        f"项目：{repo_title}（{repo_full_name}）\n"
        f"{when}{scope_line}\n"
        f"统计摘要：{stat_line}\n\n"
        f"规则线索（仅用于定位重点，不要直接复制）：\n{hints}\n\n"
        f"规则识别的风险点（请判断是否需要保留，不要机械照抄）：\n{risk_hints}\n\n"
        f"{progress_title}：\n\n{progress_md}\n"
        f"{truncated_note}\n"
        "请按系统提示的四个二级标题输出正文，不要输出附录、表格或一级标题。"
    )


def _heading_title(line: str) -> tuple[int, str] | None:
    match = _HEADING.match(line.strip())
    if match:
        title = match.group(2).strip().strip("*").strip()
        return len(match.group(1)), title
    bold = _BOLD_TITLE.match(line.strip())
    if bold:
        return 2, bold.group(1).strip()
    return None


def _strip_fences(text: str) -> str:
    lines = text.strip().splitlines()
    while lines and lines[0].strip().startswith("```"):
        lines.pop(0)
    while lines and lines[-1].strip().startswith("```"):
        lines.pop()
    return "\n".join(lines)
