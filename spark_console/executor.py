from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from core.web_chat import (
    CHAT_EDITOR_SELECTOR,
    WEB_CHAT_URL,
    TargetNotFoundError,
    UserInfoCollector,
    select_web_chat_target,
)
from spark_console.credentials import CredentialError, CredentialPayload


logger = logging.getLogger(__name__)


class ExecutionStage(StrEnum):
    STARTING = "starting"
    AUTHENTICATING = "authenticating"
    SELECTING_TARGET = "selecting_target"
    SENDING = "sending"
    CONFIRMING = "confirming"
    SUBMITTED = "submitted"
    COMPLETE = "complete"


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    stage: str
    error_code: str | None = None
    error_summary: str | None = None
    retryable: bool = False
    # 本次实际在聊天页看到的好友名 / 抓到的身份，交给 worker 刷新好友快照。
    # 失败路径也会带上，这样「没找到目标」时也能顺便把名单更新掉。
    discovered_names: tuple[str, ...] = ()
    discovered_identities: tuple = ()


class DouyinExecutor:
    async def execute(
        self,
        cookie_payload: bytes | bytearray,
        target: str,
        message: str,
        credential_version: int = 1,
        target_sec_uid: str | None = None,
        refresh_targets: bool = False,
        target_aliases: tuple[str, ...] = (),
    ) -> ExecutionResult:
        from playwright.async_api import async_playwright
        from core.tasks import confirm_message_sent

        stage = ExecutionStage.AUTHENTICATING
        message_submitted = False
        discovered: list[str] = []
        identities: tuple = ()
        try:
            payload = CredentialPayload.parse(bytes(cookie_payload), credential_version)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = None
                try:
                    context = await browser.new_context(**payload.context_options())
                    legacy_cookies = payload.cookies_to_add()
                    if legacy_cookies:
                        await context.add_cookies(legacy_cookies)
                    page = await context.new_page()
                    user_info = UserInfoCollector()
                    if target_sec_uid or refresh_targets:
                        page.on("response", user_info.capture)
                    await page.goto(WEB_CHAT_URL, wait_until="domcontentloaded", timeout=120000)
                    stage = ExecutionStage.SELECTING_TARGET
                    identity = (
                        await user_info.wait_for(target_sec_uid)
                        if target_sec_uid
                        else None
                    )
                    # 页面只会为「已经在会话列表里的会话」请求 user/info，不在列表里的
                    # 目标永远抓不到别名 —— 而恰恰只有这类目标要靠别名去搜索。
                    # 所以与库里存的历史身份（worker 查好传进来）合并，别让兜底落空，
                    # 这样对方改了昵称也还能用抖音号搜到。
                    aliases = tuple(
                        dict.fromkeys(
                            (*(identity.aliases if identity else ()), *target_aliases)
                        )
                    )
                    await select_web_chat_target(
                        page,
                        target,
                        timeout=45000,
                        aliases=aliases,
                        scroll=refresh_targets,
                        discovered=discovered,
                    )
                    await page.wait_for_selector(CHAT_EDITOR_SELECTOR, timeout=30000)
                    stage = ExecutionStage.SENDING
                    editor = page.locator(CHAT_EDITOR_SELECTOR).first
                    lines = message.splitlines() or [message]
                    for index, line in enumerate(lines):
                        await editor.type(line)
                        if index < len(lines) - 1:
                            await editor.press("Shift+Enter")
                    message_submitted = True
                    await editor.press("Enter")
                    stage = ExecutionStage.CONFIRMING
                    if refresh_targets:
                        identities = await user_info.drain()
                    try:
                        await confirm_message_sent(page, editor, message, timeout=20000)
                    except Exception:
                        return ExecutionResult(
                            True,
                            ExecutionStage.SUBMITTED,
                            "delivery_confirmation_unavailable",
                            "消息已提交，页面未能二次确认",
                            discovered_names=tuple(discovered),
                            discovered_identities=identities,
                        )
                    return ExecutionResult(
                        True,
                        ExecutionStage.COMPLETE,
                        discovered_names=tuple(discovered),
                        discovered_identities=identities,
                    )
                finally:
                    try:
                        if context is not None:
                            await context.close()
                    finally:
                        await browser.close()
        except TargetNotFoundError:
            return ExecutionResult(
                False,
                ExecutionStage.SELECTING_TARGET,
                "target_not_found",
                "未找到完全匹配的目标好友",
                discovered_names=tuple(discovered),
            )
        except CredentialError:
            return ExecutionResult(False, ExecutionStage.AUTHENTICATING, "cookie_invalid", "账号凭据格式无效")
        except Exception as error:
            logger.warning(
                "douyin execution failed stage=%s exception=%s",
                stage,
                type(error).__name__,
            )
            if stage == ExecutionStage.SELECTING_TARGET:
                return ExecutionResult(
                    False,
                    ExecutionStage.SELECTING_TARGET,
                    "conversation_not_opened",
                    "已找到好友，但聊天窗口没有打开",
                    retryable=True,
                    discovered_names=tuple(discovered),
                )
            return ExecutionResult(
                False,
                stage,
                "automation_failed",
                "页面操作或发送确认失败",
                retryable=not message_submitted,
                discovered_names=tuple(discovered),
            )
