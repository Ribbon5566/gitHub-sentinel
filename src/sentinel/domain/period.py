"""报告周期：把「一段时间」建模成一等公民。

为什么不塞进 progress：progress 已经承担了快照聚合、统计、指纹三件事，
再放周期只会让它继续膨胀；而 selection 需要周期但不需要 progress，
分成两个模块才能让 progress 单向依赖它们，避免循环引用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from .errors import ValidationError
from .models import as_utc


@dataclass(frozen=True, slots=True)
class ReportPeriod:
    """左闭右闭的日期区间。

    日期区间用「闭区间」表达（人对「11-01 到 11-15」的理解就是含两端），
    换算成时间窗口时才变成左闭右开 [date_from 00:00, date_to+1d 00:00)，
    这样相邻两段区间不会把边界上的对象数两次。
    """

    date_from: date
    date_to: date

    def __post_init__(self) -> None:
        if self.date_from > self.date_to:
            raise ValidationError(
                f"报告周期起点 {self.date_from} 晚于终点 {self.date_to}"
            )

    @classmethod
    def single(cls, day: date) -> ReportPeriod:
        return cls(date_from=day, date_to=day)

    @property
    def is_single_day(self) -> bool:
        return self.date_from == self.date_to

    @property
    def span_days(self) -> int:
        """含两端的自然日数量：11-01 ~ 11-15 是 15 天。"""
        return (self.date_to - self.date_from).days + 1

    def label(self) -> str:
        if self.is_single_day:
            return f"{self.date_to:%Y-%m-%d}"
        return f"{self.date_from:%Y-%m-%d} ~ {self.date_to:%Y-%m-%d}"

    def file_stem(self) -> str:
        """产物文件名主干，不含口径后缀。

        单日也写成 `A_to_A`：单日区间与 v2.0 的 `YYYY-MM-DD.md` 是两份不同的
        产物（口径不同），文件名必须能区分，否则会互相覆盖。
        """
        return f"{self.date_from:%Y-%m-%d}_to_{self.date_to:%Y-%m-%d}"

    def validate(self, *, today: date, max_days: int) -> None:
        if self.date_to > today:
            raise ValidationError(f"报告周期终点 {self.date_to} 还没到（今天 {today}）")
        if max_days > 0 and self.span_days > max_days:
            raise ValidationError(
                f"报告周期 {self.span_days} 天超过上限 {max_days} 天"
                "（SENTINEL_REPORT_RANGE_MAX_DAYS）"
            )


def resolve_period_window(
    period: ReportPeriod, tz: tzinfo, *, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """返回 [date_from 00:00, date_to+1d 00:00) 的 aware UTC。

    与 resolve_day_window 同构：传 now 时把右端截到「现在」，区间报告不该
    把未来几小时算进去；裁完若为空则返回 start == end，由调用方给出
    「区间尚未开始」这类有针对性的报错。
    """
    start_local = datetime.combine(period.date_from, time.min, tzinfo=tz)
    end_local = datetime.combine(period.date_to + timedelta(days=1), time.min, tzinfo=tz)
    start = start_local.astimezone(UTC)
    end = end_local.astimezone(UTC)
    if now is not None:
        moment = as_utc(now) or start
        end = min(end, moment)
        if end < start:
            end = start
    return start, end
