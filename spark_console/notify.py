"""Cookie 失效等事件的邮件通知。

只使用标准库 smtplib/email，不引入额外依赖。SMTP 未配置时静默跳过，
发送失败也只记录日志，绝不影响任务执行。

收件人是抖音号所属用户自己的通知邮箱（users.email），不是全局收件人：
每个控制台用户名下绑定的抖音号不同，Cookie 失效时只提醒该用户本人。
"""

from __future__ import annotations

import html
import logging
import smtplib
import socket
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Mapping

logger = logging.getLogger("spark.notify")

DEFAULT_SMTP_HOST = "smtp.qq.com"
DEFAULT_SMTP_PORT = 465
SMTP_TIMEOUT_SECONDS = 15
_SUPPORTED_SECURITY = {"ssl", "starttls", "plain"}


@dataclass(frozen=True)
class MailSettings:
    host: str = DEFAULT_SMTP_HOST
    port: int = DEFAULT_SMTP_PORT
    username: str = ""
    password: str = ""
    sender: str = ""
    security: str = "ssl"
    timeout: int = SMTP_TIMEOUT_SECONDS

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "MailSettings":
        username = environ.get("SPARK_SMTP_USER", "").strip()
        security = environ.get("SPARK_SMTP_SECURITY", "ssl").strip().lower()
        if security not in _SUPPORTED_SECURITY:
            security = "ssl"
        try:
            port = int(environ.get("SPARK_SMTP_PORT", str(DEFAULT_SMTP_PORT)))
        except ValueError:
            port = DEFAULT_SMTP_PORT
        return cls(
            host=environ.get("SPARK_SMTP_HOST", DEFAULT_SMTP_HOST).strip(),
            port=port,
            username=username,
            password=environ.get("SPARK_SMTP_PASSWORD", ""),
            sender=environ.get("SPARK_SMTP_FROM", "").strip() or username,
            security=security,
        )

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.password and self.sender)


def send_mail(settings: MailSettings, recipient: str, subject: str, body: str) -> bool:
    """给单个收件人发送纯文本 + HTML 邮件，返回是否发送成功。不抛出异常。"""

    if not recipient:
        logger.warning("通知邮箱为空，跳过邮件发送")
        return False
    if not settings.configured:
        logger.warning("邮件通知未配置（SPARK_SMTP_USER/SPARK_SMTP_PASSWORD 缺失），跳过发送")
        return False

    message = MIMEMultipart("alternative")
    message["From"] = settings.sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    hostname = socket.gethostname()

    text_body = (
        f"{body}\n\n"
        "---\n"
        f"发送主机：{hostname}\n"
        "此邮件由火花守护控制台自动发送，请勿回复。"
    )
    html_body = (
        "<html><body>"
        f"<pre style='font-family:monospace;white-space:pre-wrap;'>{html.escape(body)}</pre>"
        "<hr><p style='color:#888;font-size:12px;'>"
        f"发送主机：{html.escape(hostname)}<br>此邮件由火花守护控制台自动发送，请勿回复。"
        "</p></body></html>"
    )
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if settings.security == "ssl":
            with smtplib.SMTP_SSL(settings.host, settings.port, timeout=settings.timeout) as server:
                server.login(settings.username, settings.password)
                server.sendmail(settings.sender, [recipient], message.as_string())
        else:
            with smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout) as server:
                if settings.security == "starttls":
                    server.starttls()
                server.login(settings.username, settings.password)
                server.sendmail(settings.sender, [recipient], message.as_string())
    except Exception:
        logger.exception("通知邮件发送失败")
        return False
    logger.info("通知邮件已发送（收件人已隐去）")
    return True


def notify_cookie_expired(
    settings: MailSettings,
    recipient: str,
    *,
    owner_username: str,
    account_name: str,
    occurred_at: str,
    detail: str = "",
) -> bool:
    """某个抖音号 Cookie 失效时，通知该号所属用户。"""

    subject = f"[火花守护] 抖音号「{account_name}」Cookie 已失效"
    body = (
        f"控制台账号：{owner_username}\n"
        f"抖音号：{account_name}\n"
        f"状态：Cookie 已失效，该号下的续火任务将无法发送消息。\n"
        f"时间：{occurred_at}\n"
        "\n请在控制台「抖音账号」页面重新扫码绑定该号，并确认页面上填写的通知邮箱可以收到本邮件。\n"
    )
    if detail:
        body += f"\n详情：{detail}\n"
    return send_mail(settings, recipient, subject, body)
