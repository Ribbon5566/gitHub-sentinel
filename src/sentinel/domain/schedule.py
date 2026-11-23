"""cron 表达式解析与触发时间计算。

为什么自己写而不用 croniter / APScheduler：
  1. domain 层要求零第三方依赖，才能 0 mock 单测；
  2. APScheduler 3.x 的 cron 里 0=周一、crontab 里 0=周日 —— 两套约定混用是本项目
     最容易埋的坑。这里统一用 ISO 约定：**1=周一 … 7=周日**，并且对外只接受
     mon..sun 名称，直接拒绝数字星期，从入口消灭歧义。

只支持 5 段表达式（分 时 日 月 星期），不支持秒级。秒级对本项目没有意义，
且会让「同一分钟内重复触发」的幂等设计失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import CronError

_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * mon",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 jan *",
    "@annually": "0 0 1 jan *",
}

_WEEKDAY_NAMES = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}
_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

MAX_LOOKAHEAD_DAYS = 400


def _token(raw: str, names: dict[str, int] | None, field: str, *, numeric: bool = True) -> int:
    token = raw.strip()
    if not token:
        raise CronError(f"{field} 字段存在空值，检查多余的逗号或连字符")
    if names and token in names:
        return names[token]
    try:
        value = int(token)
    except ValueError:
        allowed = f"（可用 {', '.join(sorted(names))}）" if names else ""
        raise CronError(f"{field} 字段无法识别: {token!r}{allowed}") from None
    if not numeric:
        raise CronError(
            f"{field} 字段不接受数字 {token}：0/7 在不同 cron 实现里含义不同，"
            "请改用 mon/tue/wed/thu/fri/sat/sun"
        )
    return value


def _parse_field(
    expr: str,
    lo: int,
    hi: int,
    *,
    names: dict[str, int] | None = None,
    numeric: bool = True,
    field: str = "",
) -> tuple[frozenset[int], bool]:
    """返回 (取值集合, 是否裸 '*'）。裸 '*' 在「日/星期」里语义特殊，需要单独标记。"""
    text = expr.strip().lower()
    if not text:
        raise CronError(f"{field} 字段为空")
    if text == "*":
        return frozenset(range(lo, hi + 1)), True

    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{field} 字段存在空段: {expr!r}")

        step = 1
        base = part
        if "/" in part:
            base, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) <= 0:
                raise CronError(f"{field} 字段步长非法: {part!r}")
            step = int(step_text)

        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_text, _, end_text = base.partition("-")
            start = _token(start_text, names, field, numeric=numeric)
            end = _token(end_text, names, field, numeric=numeric)
        else:
            start = _token(base, names, field, numeric=numeric)
            # "5/10" 语义：从 5 开始到上限，步长 10
            end = hi if step > 1 else start

        if not (lo <= start <= hi and lo <= end <= hi):
            raise CronError(f"{field} 字段超出范围 [{lo}, {hi}]: {part!r}")
        if start > end:
            raise CronError(f"{field} 字段区间倒置: {part!r}")
        values.update(range(start, end + 1, step))

    if not values:
        raise CronError(f"{field} 字段没有匹配到任何值: {expr!r}")
    return frozenset(values), False


@dataclass(frozen=True, slots=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_star: bool
    weekday_star: bool
    source: str = ""

    @property
    def minutes_sorted(self) -> tuple[int, ...]:
        return tuple(sorted(self.minutes))

    @property
    def hours_sorted(self) -> tuple[int, ...]:
        return tuple(sorted(self.hours))

    def describe(self) -> str:
        return self.source


def parse_cron(expr: str) -> CronSpec:
    # 连续空白压成一个，避免 "0   9  *" 这类输入原样落库导致展示不一致
    text = " ".join((expr or "").split()).lower()
    text = _ALIASES.get(text, text)
    fields = text.split()
    if len(fields) == 6:
        raise CronError("只支持 5 段 cron（分 时 日 月 星期），不接受秒级字段")
    if len(fields) != 5:
        raise CronError(f"cron 需要 5 段（分 时 日 月 星期），实际 {len(fields)} 段: {expr!r}")

    minutes, _ = _parse_field(fields[0], 0, 59, field="分钟")
    hours, _ = _parse_field(fields[1], 0, 23, field="小时")
    days, day_star = _parse_field(fields[2], 1, 31, field="日")
    months, _ = _parse_field(fields[3], 1, 12, names=_MONTH_NAMES, field="月")
    weekdays, weekday_star = _parse_field(
        fields[4], 1, 7, names=_WEEKDAY_NAMES, numeric=False, field="星期"
    )
    return CronSpec(
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        day_star=day_star,
        weekday_star=weekday_star,
        source=text,
    )


def normalize_cron(expr: str) -> str:
    """校验并规范化。返回可直接落库的表达式（别名会被展开）。"""
    return parse_cron(expr).source


def resolve_tz(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise CronError(
            f"未知时区 {name!r}。Windows 上需要安装 tzdata（pip install tzdata），"
            "并使用 IANA 名称，例如 Asia/Shanghai"
        ) from None


def lease_seconds_for(tick_interval_seconds: int) -> int:
    """任务租约时长。

    至少覆盖 3 个 tick：一个 tick 卡住不会让下一轮空转，也不会因为
    一次失败把订阅永久锁死（锁死比重复执行更难排查）。
    """
    return max(180, tick_interval_seconds * 3)


def _day_matches(spec: CronSpec, day: date) -> bool:
    if day.month not in spec.months:
        return False
    dom_ok = day.day in spec.days
    dow_ok = day.isoweekday() in spec.weekdays
    if spec.day_star and spec.weekday_star:
        return True
    if spec.day_star:
        return dow_ok
    if spec.weekday_star:
        return dom_ok
    # 两者都受限时按 crontab 惯例取「或」，而不是「与」
    return dom_ok or dow_ok


def next_fire(spec: CronSpec, after: datetime, tz: tzinfo) -> datetime:
    """严格晚于 after 的下一个触发时刻（返回带 tz 的本地时间）。"""
    start = after.astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    start_day = start.date()
    day = start_day
    minutes = spec.minutes_sorted
    hours = spec.hours_sorted

    for _ in range(MAX_LOOKAHEAD_DAYS):
        if _day_matches(spec, day):
            hour_from = start.hour if day == start_day else 0
            for hour in hours:
                if hour < hour_from:
                    continue
                minute_from = start.minute if (day == start_day and hour == start.hour) else 0
                for minute in minutes:
                    if minute < minute_from:
                        continue
                    return datetime.combine(day, time(hour, minute), tzinfo=tz)
        day += timedelta(days=1)

    raise CronError(f"cron {spec.source!r} 在未来 {MAX_LOOKAHEAD_DAYS} 天内没有触发时间")


def previous_fire(spec: CronSpec, before: datetime, tz: tzinfo) -> datetime:
    """不晚于 before 的最近一次触发时刻。"""
    local = before.astimezone(tz)
    end = local.replace(second=0, microsecond=0)
    end_day = end.date()
    day = end_day
    minutes = spec.minutes_sorted
    hours = spec.hours_sorted

    for _ in range(MAX_LOOKAHEAD_DAYS):
        if _day_matches(spec, day):
            hour_to = end.hour if day == end_day else 23
            for hour in reversed(hours):
                if hour > hour_to:
                    continue
                minute_to = end.minute if (day == end_day and hour == end.hour) else 59
                for minute in reversed(minutes):
                    if minute > minute_to:
                        continue
                    candidate = datetime.combine(day, time(hour, minute), tzinfo=tz)
                    if candidate <= local:
                        return candidate
        day -= timedelta(days=1)

    raise CronError(f"cron {spec.source!r} 在过去 {MAX_LOOKAHEAD_DAYS} 天内没有触发时间")
