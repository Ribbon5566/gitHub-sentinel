"""日志：单行 KV，便于 grep 和后续接采集端。"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class SecretFilter(logging.Filter):
    """把 token / password 从日志里抹掉。

    不是可选项：GitHub token 和 SMTP 口令一旦进了日志文件，
    排查包里就会带着凭证到处流转。
    """

    def __init__(self, secrets: Iterable[str | None]) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s and len(s) >= 6)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        for secret in self._secrets:
            if secret in message:
                message = message.replace(secret, "***")
        record.msg = message
        record.args = None
        return True


def configure_logging(level: str = "INFO", *, secrets: Iterable[str | None] = ()) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler.addFilter(SecretFilter(secrets))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # 这几个库默认很吵，压到 WARNING
    for noisy in ("httpx", "aiosqlite", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
