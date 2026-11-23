"""按条件筛选报告对象：口径、谓词、排序轴、产物后缀。

这里只有「选什么」，不含 `max_items` / `skip_bots` 这类运维旋钮——它们归
Settings，放到两处必然出现「传参 A、配置 B」的双真相。

**刻意不 import progress**：谓词用 Protocol 描述结构化类型，由 progress
单向 import 本模块。否则 `progress ↔ selection` 会在运行时 ImportError。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

ACTIVITY_SCOPE_NOTE = "本期口径为活动时间：对象在区间内有过更新即计入。"
COMPLETION_SCOPE_NOTE = (
    "本期口径为完成时间：只统计区间内合并的 PR 与关闭的 Issue；"
    "区间之外发生的评论、推送等新活动不属于本期交付，不要写进关键进展。"
)


class IssueScope(StrEnum):
    """议题的入选口径。

    ACTIVITY：`updated_at` 落在区间内（v2.0 单日语义）
    CLOSED：`closed_at` 落在区间内（研发周期交付口径）
    NONE：本次不请求议题
    """

    ACTIVITY = "activity"
    CLOSED = "closed"
    NONE = "none"


class PullScope(StrEnum):
    ACTIVITY = "activity"
    MERGED = "merged"
    NONE = "none"


class _IssueLike(Protocol):
    state: str
    updated_at: datetime
    closed_at: datetime | None


class _PullLike(Protocol):
    state: str
    updated_at: datetime
    merged_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReportSelection:
    """一次报告要「哪些对象」，以及按哪个时间轴判断与排序。

    两个 scope 都必填：默认值会让 `ReportSelection()` 悄悄变成「什么都不选」，
    那种错误应该在构造期就暴露，而不是跑完一次 GitHub 请求才发现报告是空的。
    """

    issue_scope: IssueScope
    pull_scope: PullScope

    def __post_init__(self) -> None:
        if not self.wants_issues and not self.wants_pulls:
            raise ValueError("ReportSelection 至少要选中一种资源")

    # ------------------------------------------------------------- 构造器
    @classmethod
    def completion(cls) -> ReportSelection:
        """区间报告的默认口径：区间内关闭的议题 + 区间内合并的 PR。"""
        return cls(IssueScope.CLOSED, PullScope.MERGED)

    @classmethod
    def activity(cls) -> ReportSelection:
        """退回 v2.0 的活动口径：`updated_at` 落在区间即可。"""
        return cls(IssueScope.ACTIVITY, PullScope.ACTIVITY)

    @classmethod
    def merged_only(cls) -> ReportSelection:
        return cls(IssueScope.NONE, PullScope.MERGED)

    @classmethod
    def closed_only(cls) -> ReportSelection:
        return cls(IssueScope.CLOSED, PullScope.NONE)

    # --------------------------------------------------------------- 能力
    @property
    def wants_issues(self) -> bool:
        return self.issue_scope is not IssueScope.NONE

    @property
    def wants_pulls(self) -> bool:
        return self.pull_scope is not PullScope.NONE

    @property
    def issue_axis(self) -> str | None:
        """下推给 ProgressQuery 的口径名；None 表示不请求。"""
        if self.issue_scope is IssueScope.CLOSED:
            return "closed"
        if self.issue_scope is IssueScope.ACTIVITY:
            return "updated"
        return None

    @property
    def pull_axis(self) -> str | None:
        if self.pull_scope is PullScope.MERGED:
            return "merged"
        if self.pull_scope is PullScope.ACTIVITY:
            return "updated"
        return None

    @property
    def issue_time_attr(self) -> str | None:
        """展示与排序用的字段名。必须与谓词同源，否则 md 的列和指纹会各说各话。"""
        if self.issue_axis is None:
            return None
        return "closed_at" if self.issue_axis == "closed" else "updated_at"

    @property
    def pull_time_attr(self) -> str | None:
        if self.pull_axis is None:
            return None
        return "merged_at" if self.pull_axis == "merged" else "updated_at"

    @property
    def resources(self) -> tuple[str, ...]:
        names: list[str] = []
        if self.wants_issues:
            names.append("issues")
        if self.wants_pulls:
            names.append("pulls")
        return tuple(names)

    @property
    def completion_mode(self) -> bool:
        """是否按「完成时间」而非「活动时间」筛选。"""
        return (
            self.issue_scope is not IssueScope.ACTIVITY
            and self.pull_scope is not PullScope.ACTIVITY
        )

    # --------------------------------------------------------------- 谓词
    def keeps_issue(self, item: _IssueLike, start: datetime, end: datetime) -> bool:
        if self.issue_scope is IssueScope.NONE:
            return False
        moment = item.updated_at if self.issue_scope is IssueScope.ACTIVITY else item.closed_at
        if moment is None:
            return False
        return start <= moment < end

    def keeps_pull(self, item: _PullLike, start: datetime, end: datetime) -> bool:
        if self.pull_scope is PullScope.NONE:
            return False
        moment = item.updated_at if self.pull_scope is PullScope.ACTIVITY else item.merged_at
        if moment is None:
            return False
        return start <= moment < end

    def issue_time(self, item: _IssueLike) -> datetime:
        if self.issue_scope is IssueScope.CLOSED and item.closed_at is not None:
            return item.closed_at
        return item.updated_at

    def pull_time(self, item: _PullLike) -> datetime:
        if self.pull_scope is PullScope.MERGED and item.merged_at is not None:
            return item.merged_at
        return item.updated_at

    # --------------------------------------------------------------- 文案
    def scope_label(self) -> str:
        if not self.completion_mode:
            return "活动口径（`updated_at`）"
        parts: list[str] = []
        if self.wants_issues:
            parts.append("Issue `closed_at`")
        if self.wants_pulls:
            parts.append("PR `merged_at`")
        return f"完成口径（{' / '.join(parts)}）"

    def scope_note(self) -> str:
        """喂给模型的硬约束，避免把区间外的新活动写成本期进展。"""
        return ACTIVITY_SCOPE_NOTE if not self.completion_mode else COMPLETION_SCOPE_NOTE

    def criteria(self, *, period_label: str, timezone: str) -> tuple[str, ...]:
        """「数据口径」段落，模板直接迭代。与谓词同源，不允许模板另写一套。"""
        lines: list[str] = []
        if self.issue_scope is IssueScope.CLOSED:
            lines.append(
                f"议题：`closed_at` 落在 {period_label}（{timezone}），只收录当前仍处于已关闭的对象。"
            )
        elif self.issue_scope is IssueScope.ACTIVITY:
            lines.append(
                f"议题：`updated_at` 落在 {period_label}（{timezone}），"
                "包含区间内新建、有新活动、状态变为 `closed` 的对象。"
            )
        if self.pull_scope is PullScope.MERGED:
            lines.append(
                f"合并请求：`merged_at` 落在 {period_label}（{timezone}），只收录已合并的对象。"
            )
        elif self.pull_scope is PullScope.ACTIVITY:
            lines.append(
                f"合并请求：`updated_at` 落在 {period_label}（{timezone}），"
                "包含区间内新建、有新活动、状态变为 `merged` 的对象。"
            )
        if self.completion_mode:
            lines.append(
                "取数按 `updated_at` 倒序翻页到区间起点为止，"
                "因此「区间内完成、之后又被评论」的对象同样会被收录。"
            )
        return tuple(lines)

    def stem_suffix(self) -> str:
        """产物文件名后缀：默认口径用裸名，非默认口径才加后缀。

        同一区间的 `--merged` 与 `--closed` 是两份不同的交付清单，
        必须落到不同文件，否则会永久互相覆盖并导致报告反复重算。
        """
        pair = (self.issue_scope, self.pull_scope)
        if pair == (IssueScope.CLOSED, PullScope.MERGED):
            return ""
        if pair == (IssueScope.CLOSED, PullScope.NONE):
            return ".closed"
        if pair == (IssueScope.NONE, PullScope.MERGED):
            return ".merged"
        return ".activity"

    def cli_flags(self) -> str:
        """对应的 CLI 口径参数（含前导空格），用于「请先运行…」这类提示。

        与 `stem_suffix()` 用同一套 pair 判断：提示里给的命令如果漏了口径参数，
        用户照着跑仍会找不到文件，这类循环式报错比不给提示更糟。
        """
        pair = (self.issue_scope, self.pull_scope)
        if pair == (IssueScope.CLOSED, PullScope.MERGED):
            return ""
        if pair == (IssueScope.CLOSED, PullScope.NONE):
            return " --closed"
        if pair == (IssueScope.NONE, PullScope.MERGED):
            return " --merged"
        return " --activity"

    def fingerprint_payload(self) -> dict[str, str]:
        return {
            "issue_scope": self.issue_scope.value,
            "pull_scope": self.pull_scope.value,
        }
