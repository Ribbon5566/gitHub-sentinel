"""通知渠道注册表。

加企微 / 飞书 / 钉钉 / Slack 的完整动作只有两步：
  1. 在本目录新增 xxx.py，实现 Notifier 协议；
  2. 在下面的 _FACTORIES 里加一行。
application / api 层零改动——这就是把通知放进端口的意义。
"""

from __future__ import annotations

from collections.abc import Callable

from ...config import Settings
from ...ports.notifier import Notifier
from .null import NullNotifier
from .smtp import SmtpNotifier

_FACTORIES: dict[str, Callable[[Settings], Notifier]] = {
    "null": lambda settings: NullNotifier(),
    "smtp": lambda settings: SmtpNotifier(settings),
}


def build_notifier(settings: Settings) -> Notifier:
    channel = settings.notify_channel
    factory = _FACTORIES.get(channel)
    if factory is None:
        raise ValueError(f"未知通知渠道: {channel}，可选 {sorted(_FACTORIES)}")
    return factory(settings)


def available_channels() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


__all__ = ["NullNotifier", "SmtpNotifier", "available_channels", "build_notifier"]
