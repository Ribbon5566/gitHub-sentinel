"""SMTP 邮件通知。

要点：
  * 必须发 multipart/alternative（纯文本 + HTML 两份），只发 HTML 会被判垃圾邮件；
  * 465 用 SSL 直连，587 用 STARTTLS，两者互斥，配置里别同时开；
  * 失败抛 NotifyError，由上层记录到 notifications 表，不影响报告本身已生成的事实。
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from email.utils import formataddr, formatdate

import aiosmtplib

from ...config import Settings
from ...domain.enums import NotifyStatus
from ...domain.errors import NotifyError
from ...ports.notifier import NotifyMessage, NotifyResult

logger = logging.getLogger(__name__)


class SmtpNotifier:
    channel = "smtp"

    def __init__(self, settings: Settings) -> None:
        if not settings.smtp_host:
            raise NotifyError("SENTINEL_SMTP_HOST 未配置，无法启用 smtp 通知")
        if settings.smtp_ssl and settings.smtp_starttls:
            raise NotifyError("SMTP_SSL 与 STARTTLS 只能开一个")
        self._settings = settings

    def _build_message(self, message: NotifyMessage) -> EmailMessage:
        settings = self._settings
        mime = EmailMessage()
        mime["From"] = formataddr(("GitHub Sentinel", settings.smtp_from or settings.smtp_user or ""))
        mime["To"] = ", ".join(settings.smtp_recipients) or message.target
        mime["Subject"] = message.subject
        mime["Date"] = formatdate(localtime=True)
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")
        return mime

    async def send(self, message: NotifyMessage) -> NotifyResult:
        settings = self._settings
        recipients = settings.smtp_recipients or ((message.target,) if message.target else ())
        if not recipients:
            raise NotifyError("没有收件人：请配置 SENTINEL_SMTP_TO 或订阅的 notify_target")

        mime = self._build_message(message)
        try:
            await aiosmtplib.send(
                mime,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user or None,
                password=settings.smtp_password or None,
                use_tls=settings.smtp_ssl,
                start_tls=settings.smtp_starttls,
                timeout=30,
            )
        except (aiosmtplib.SMTPException, OSError) as exc:
            raise NotifyError(f"SMTP 发送失败: {exc}") from exc

        logger.info("notify.sent report_id=%s to=%s", message.report_id, recipients)
        return NotifyResult(status=NotifyStatus.SENT, detail=f"已发送至 {len(recipients)} 个收件人")

    async def aclose(self) -> None:
        return None
