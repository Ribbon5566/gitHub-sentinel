from __future__ import annotations

import logging

from ...domain.enums import NotifyStatus
from ...ports.notifier import NotifyMessage, NotifyResult

logger = logging.getLogger(__name__)


class NullNotifier:
    """干跑通知：只记录，不发送。

    默认用它，是为了让「采集 → 报告」链路在没有 SMTP 配置时也能完整跑通，
    并且 notify 的幂等记录照样写入数据库——上线切到 smtp 时不会出现
    「历史报告全被判为未发送」的补发风暴。
    """

    channel = "null"

    async def send(self, message: NotifyMessage) -> NotifyResult:
        logger.info(
            "notify.dry_run report_id=%s target=%s subject=%s",
            message.report_id,
            message.target or "-",
            message.subject,
        )
        return NotifyResult(status=NotifyStatus.SKIPPED, detail="null channel: 未实际发送")

    async def aclose(self) -> None:
        return None
