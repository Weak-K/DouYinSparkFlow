"""控制台告警邮件：Cookie 失效与任务失败通知。

只使用标准库 smtplib/email，不引入额外依赖。SMTP 未配置时**静默跳过**，
发送失败也只记一条日志，绝不影响任务执行与重试。

两个入口，职责分开，互不重复：

- `notify_cookie_expired()`：某个抖音号 Cookie 失效时，**只通知该号所属用户本人**
  （收件人取 `users.email`）。每个控制台账号名下的抖音号不同，所以不做全局群发；
  该用户尚未填写邮箱时，兜底发给 `SPARK_ALERT_EMAIL_TO`，避免旧账号静默漏提醒。
- `alert_task_failure()`：其他任务失败时通知运维收件人，带**冷却窗口**（默认 60 分钟）
  且在**守护线程**里发送，不阻塞调用方事务。两类失败不走这条路：`cookie_invalid`
  （已按用户发给本人）和 `retry_scheduled_*`（只是「已安排自动重试」的中间态，不是最终失败）。

环境变量（写在 .env.console）
    SPARK_SMTP_HOST / _PORT / _SECURITY / _USER / _PASSWORD / _FROM
    SPARK_ALERT_EMAIL_TO          全局兜底收件人，多个用逗号分隔
    SPARK_ALERT_COOLDOWN_MINUTES  全局告警最小间隔（默认 60）
    SPARK_DATA_DIR                冷却状态文件所在目录（默认 /data）
"""

from __future__ import annotations

import html
import json
import logging
import os
import smtplib
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from typing import Mapping, Sequence

logger = logging.getLogger("spark.notify")

DEFAULT_SMTP_HOST = "smtp.qq.com"
DEFAULT_SMTP_PORT = 465
SMTP_TIMEOUT_SECONDS = 20
DEFAULT_COOLDOWN_MINUTES = 60
STATE_FILENAME = "alert-state.json"
SUPPORTED_SECURITY = {"ssl", "starttls", "plain"}
CONSOLE_URL = "https://lgnsl.com/spark/"

# 这些错误码通常意味着账号未登录 / Cookie 失效，全局邮件里给出对应提示
COOKIE_RELATED_CODES = {"cookie_invalid", "conversation_not_opened", "target_not_found"}

# 已由「按用户」的失效提醒覆盖，全局告警不再重复发
USER_SCOPED_CODES = {"cookie_invalid"}

# `retry_scheduled_<n>m` 是 Worker 自己安排的自动重试，属于中间态而非最终失败：
# 它一定还会再试一次，此时发告警只会把正常重试变成误报噪音。
# （2026-09-24 线上实测：一次临时故障就会发出一封「任务失败：retry_scheduled_1m」。）
RETRY_SCHEDULED_PREFIX = "retry_scheduled"


def parse_recipients(raw: str) -> tuple[str, ...]:
    return tuple(
        address.strip()
        for address in (raw or "").replace(";", ",").split(",")
        if address.strip()
    )


@dataclass(frozen=True)
class MailSettings:
    host: str = DEFAULT_SMTP_HOST
    port: int = DEFAULT_SMTP_PORT
    username: str = ""
    password: str = ""
    sender: str = ""
    security: str = "ssl"
    timeout: int = SMTP_TIMEOUT_SECONDS
    alert_recipients: tuple[str, ...] = ()
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES
    data_dir: str = "/data"

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "MailSettings":
        username = environ.get("SPARK_SMTP_USER", "").strip()
        security = environ.get("SPARK_SMTP_SECURITY", "ssl").strip().lower()
        if security not in SUPPORTED_SECURITY:
            security = "ssl"
        try:
            port = int(environ.get("SPARK_SMTP_PORT", str(DEFAULT_SMTP_PORT)))
        except ValueError:
            port = DEFAULT_SMTP_PORT
        try:
            cooldown = max(1, int(environ.get("SPARK_ALERT_COOLDOWN_MINUTES", "")))
        except ValueError:
            cooldown = DEFAULT_COOLDOWN_MINUTES
        return cls(
            host=environ.get("SPARK_SMTP_HOST", "").strip() or DEFAULT_SMTP_HOST,
            port=port,
            username=username,
            password=environ.get("SPARK_SMTP_PASSWORD", ""),
            sender=environ.get("SPARK_SMTP_FROM", "").strip() or username,
            security=security,
            alert_recipients=parse_recipients(environ.get("SPARK_ALERT_EMAIL_TO", "")),
            cooldown_minutes=cooldown,
            data_dir=environ.get("SPARK_DATA_DIR", "").strip() or "/data",
        )

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.password and self.sender)


def _footnote() -> str:
    return (
        "---\n"
        f"控制台：{CONSOLE_URL}\n"
        f"发送主机：{socket.gethostname()}\n"
        "此邮件由「抖音火花控制台」自动发送，请勿回复。"
    )


def send_mail(
    settings: MailSettings,
    recipients: str | Sequence[str],
    subject: str,
    body: str,
) -> bool:
    """同步发送一封纯文本 + HTML 邮件，返回是否发送成功。不抛出异常。"""

    targets = [recipients] if isinstance(recipients, str) else list(recipients)
    targets = [address for address in targets if address]
    if not targets:
        logger.warning("收件人为空，跳过邮件发送")
        return False
    if not settings.configured:
        logger.warning("邮件通知未配置（SPARK_SMTP_USER/SPARK_SMTP_PASSWORD 缺失），跳过发送")
        return False

    message = MIMEMultipart("alternative")
    message["From"] = settings.sender
    message["To"] = ", ".join(targets)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)

    text_body = f"{body}\n\n{_footnote()}"
    html_body = (
        "<html><body>"
        f"<pre style='font-family:monospace;white-space:pre-wrap;'>{html.escape(body)}</pre>"
        f"<hr><p style='color:#888;font-size:12px;'>{html.escape(_footnote())}</p>"
        "</body></html>"
    )
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if settings.security == "ssl":
            with smtplib.SMTP_SSL(
                settings.host, settings.port, timeout=settings.timeout
            ) as server:
                server.login(settings.username, settings.password)
                server.sendmail(settings.sender, targets, message.as_string())
        else:
            with smtplib.SMTP(
                settings.host, settings.port, timeout=settings.timeout
            ) as server:
                if settings.security == "starttls":
                    server.starttls()
                server.login(settings.username, settings.password)
                server.sendmail(settings.sender, targets, message.as_string())
    except Exception:
        logger.warning("通知邮件发送失败（不影响任务）")
        return False
    logger.info("通知邮件已发送（收件人已隐去）")
    return True


def notify_cookie_expired(
    settings: MailSettings,
    recipient: str | None,
    *,
    owner_username: str,
    account_name: str,
    occurred_at: str,
    detail: str = "",
) -> bool:
    """某个抖音号 Cookie 失效：通知该号所属用户本人，没填邮箱时兜底给运维收件人。"""

    fallback = not recipient
    targets: list[str] = list(settings.alert_recipients) if fallback else [recipient]
    if not targets:
        logger.warning("该用户未填写通知邮箱且未配置兜底收件人，跳过发送")
        return False

    subject = f"【火花控制台】抖音号「{account_name}」Cookie 已失效"
    body = (
        f"控制台账号：{owner_username}\n"
        f"抖音号：{account_name}\n"
        "状态：Cookie 已失效，该号下的续火任务将无法发送消息。\n"
        f"时间：{occurred_at}\n"
        "\n请登录控制台，在「抖音账号」页面重新扫码绑定该号。\n"
    )
    if fallback:
        body += "\n（该抖音号所属用户尚未填写通知邮箱，本邮件改发给运维收件人。）\n"
    if detail:
        body += f"\n详情：{detail}\n"
    return send_mail(settings, targets, subject, body)


def _state_path(settings: MailSettings) -> Path:
    return Path(settings.data_dir) / STATE_FILENAME


def _read_state(settings: MailSettings) -> dict:
    try:
        return json.loads(_state_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(settings: MailSettings, state: dict) -> None:
    path = _state_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        logger.warning("告警冷却状态写入失败（不影响任务）")


def _build_failure_body(
    stage: str, error_code: str, error_summary: str, suppressed: int, task_id: str = ""
) -> str:
    lines = [
        "抖音火花控制台：任务执行失败",
        "",
        f"时间：{datetime.now(timezone.utc).astimezone():%Y-%m-%d %H:%M:%S %Z}",
        f"阶段：{stage or '未知'}",
        f"错误码：{error_code or '未提供'}",
        f"摘要：{error_summary or '无'}",
    ]
    if task_id:
        lines.append(f"任务：{task_id}")
    if error_code in COOKIE_RELATED_CODES:
        lines += [
            "",
            "这通常意味着该账号未登录或 Cookie 已失效。",
            "请登录控制台，在「抖音账号」页面重新扫码绑定。",
        ]
    if suppressed:
        lines += ["", f"（本次冷却窗口内另有 {suppressed} 条同类失败告警已被抑制）"]
    return "\n".join(lines)


def _send_async(
    settings: MailSettings, recipients: Sequence[str], subject: str, body: str
) -> None:
    threading.Thread(
        target=send_mail,
        args=(settings, list(recipients), subject, body),
        name="alert-mail",
        daemon=True,
    ).start()


def alert_task_failure(
    stage: str = "",
    error_code: str = "",
    error_summary: str = "",
    task_id: str = "",
    settings: MailSettings | None = None,
) -> bool:
    """任务失败的全局兜底告警：带冷却窗口、异步发送。任何异常都不向上冒泡。

    两类失败不在这里发：

    - `cookie_invalid`：由 Worker 按用户发给该号归属人，再走一遍全局收件人只会重复打扰。
    - `retry_scheduled_*`：Worker 已自行安排了 1 分钟 / 5 分钟的重试，这是中间态；
      此时告警会把「正常的自动重试」变成误报。真正重试完仍失败时，最后那次
      会带真实错误码走到这里，该发的告警一封都不会少。
    """

    try:
        if error_code in USER_SCOPED_CODES:
            return False
        if error_code.startswith(RETRY_SCHEDULED_PREFIX):
            return False
        settings = settings or MailSettings.from_environ(os.environ)
        if not settings.configured or not settings.alert_recipients:
            return False

        state = _read_state(settings)
        now = datetime.now(timezone.utc)
        suppressed = int(state.get("suppressed") or 0)
        last_raw = state.get("last_sent_at")

        should_send = True
        if last_raw:
            try:
                last = datetime.fromisoformat(last_raw)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                should_send = (
                    now - last
                ).total_seconds() >= settings.cooldown_minutes * 60
            except ValueError:
                should_send = True

        if not should_send:
            state["suppressed"] = suppressed + 1
            _write_state(settings, state)
            return False

        subject = f"【火花控制台】任务失败：{error_code or stage or '未知原因'}"
        body = _build_failure_body(stage, error_code, error_summary, suppressed, task_id)
        state["last_sent_at"] = now.isoformat()
        state["suppressed"] = 0
        _write_state(settings, state)

        _send_async(settings, settings.alert_recipients, subject, body)
        return True
    except Exception:  # noqa: BLE001 —— 通知是旁路，任何情况下都不能影响主流程
        logger.warning("告警通知流程异常（已忽略）")
        return False
