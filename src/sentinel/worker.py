"""只跑调度、不跑 HTTP 的进程。

生产上推荐：web 进程设 SENTINEL_ENABLE_SCHEDULER=false，单独起一个 worker。
好处是重活（采集/摘要）不会拖慢看板请求，也不会因为 web 重启打断正在跑的任务。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from .config import load_settings
from .container import Container
from .logging_setup import configure_logging

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    settings = load_settings()
    configure_logging(
        settings.log_level,
        secrets=(settings.api_key, settings.github_token, settings.llm_api_key, settings.smtp_password),
    )
    container = Container(settings)
    await container.startup()
    if container.scheduler is None:
        raise SystemExit("调度器未启动：检查 SENTINEL_ENABLE_SCHEDULER 是否为 true")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            # Windows 不支持 loop.add_signal_handler 的部分信号，忽略即可
            loop.add_signal_handler(sig, stop.set)

    logger.info("worker.ready tick=%ss", settings.tick_interval_seconds)
    try:
        await stop.wait()
    except KeyboardInterrupt:  # pragma: no cover —— 交互式中断
        pass
    finally:
        await container.shutdown()
        logger.info("worker.stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
