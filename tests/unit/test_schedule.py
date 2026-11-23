from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from sentinel.domain.errors import CronError
from sentinel.domain.schedule import next_fire, normalize_cron, parse_cron, previous_fire

TZ = ZoneInfo("Asia/Shanghai")


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def test_rejects_numeric_weekday() -> None:
    """0/7 在 crontab 和 APScheduler 里含义不同，入口直接拒绝数字。"""
    with pytest.raises(CronError, match="不接受数字"):
        parse_cron("0 9 * * 1")


def test_rejects_six_fields_with_seconds() -> None:
    with pytest.raises(CronError, match="5 段"):
        parse_cron("0 0 9 * * *")


def test_rejects_out_of_range() -> None:
    with pytest.raises(CronError):
        parse_cron("0 25 * * *")


def test_alias_is_expanded() -> None:
    assert normalize_cron("@daily") == "0 0 * * *"
    assert normalize_cron("  0   9  *  *  mon ") == "0 9 * * mon"


def test_next_fire_same_day() -> None:
    spec = parse_cron("0 9 * * *")
    assert next_fire(spec, at(2026, 11, 23, 8, 30), TZ) == at(2026, 11, 23, 9, 0)


def test_next_fire_rolls_to_tomorrow() -> None:
    spec = parse_cron("0 9 * * *")
    assert next_fire(spec, at(2026, 11, 23, 9, 30), TZ) == at(2026, 11, 24, 9, 0)


def test_weekday_only_skips_weekend() -> None:
    # 2026-11-23 是周一，从周二找下一个周一应跨到下周一
    spec = parse_cron("0 9 * * mon")
    assert next_fire(spec, at(2026, 11, 24, 10, 0), TZ) == at(2026, 11, 30, 9, 0)


def test_dom_and_dow_are_or_semantics() -> None:
    """标准 crontab：日与星期都受限时取「或」。

    `0 9 1 * mon` 意味着「每月 1 号 或 每周一」，所以从 11-25 往后第一个
    触发点是 11-30（周一），而不是 12-01。
    """
    spec = parse_cron("0 9 1 * mon")
    assert next_fire(spec, at(2026, 11, 25, 0, 0), TZ) == at(2026, 11, 30, 9, 0)
    # 周一过后，下一个月初同样会触发，证明 dom 分支并未被吞掉
    assert next_fire(spec, at(2026, 11, 30, 10, 0), TZ) == at(2026, 12, 1, 9, 0)


def test_step_field() -> None:
    spec = parse_cron("*/15 * * * *")
    assert spec.minutes_sorted == (0, 15, 30, 45)
    assert next_fire(spec, at(2026, 11, 23, 10, 16), TZ) == at(2026, 11, 23, 10, 30)


def test_previous_fire_today() -> None:
    spec = parse_cron("0 9 * * *")
    assert previous_fire(spec, at(2026, 11, 23, 12, 40), TZ) == at(2026, 11, 23, 9, 0)


def test_previous_fire_before_today_returns_yesterday() -> None:
    spec = parse_cron("0 9 * * *")
    assert previous_fire(spec, at(2026, 11, 23, 8, 0), TZ) == at(2026, 11, 22, 9, 0)


def test_daily_window_is_consistent() -> None:
    """next_fire 与 previous_fire 应互为逆运算。"""
    spec = parse_cron("30 9 * * *")
    moment = at(2026, 11, 23, 10, 0)
    prev = previous_fire(spec, moment, TZ)
    assert next_fire(spec, prev, TZ) == at(2026, 11, 24, 9, 30)
