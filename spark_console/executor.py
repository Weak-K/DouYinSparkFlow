from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from core.web_chat import (
    CHAT_EDITOR_SELECTOR,
    WEB_CHAT_URL,
    AmbiguousTargetError,
    TargetNotFoundError,
    TargetSearchUnavailableError,
    UserInfoCollector,
    matches_opened_conversation,
    select_web_chat_target,
    wait_for_opened_conversation,
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


class DeliveryState(StrEnum):
    """投递状态。与 success 正交：成功与否看业务口径，能否重试看这里。

    Enter 是不可安全重试的副作用边界。一旦发送动作开始，拿不到回执
    也**不能**证明没发出去，只能记为 UNCERTAIN 并禁止自动重试。
    """

    NOT_SENT = "not_sent"
    UNCERTAIN = "uncertain"
    CONFIRMED = "confirmed"


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
    # 投递状态。None 表示本次执行没走到发送阶段，无从判断。
    delivery_state: DeliveryState | None = None

    @property
    def safe_to_retry(self) -> bool:
        """只有「确定没发出去」才允许重试，不确定一律按已送达处理。"""
        return self.delivery_state == DeliveryState.NOT_SENT


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
        # 发送边界标记。Enter 按下之前为 False，之后为 True；只有 False 时
        # 才允许把失误当成「没发出去」来处理。
        send_attempted = False
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
                    # 发送前必须确认打开的就是目标本人。选人链路（搜索面板的结果、
                    # 虚拟滚动里被复用的会话行）都可能把别的会话选中，而消息一旦
                    # 发出去就撤不回来 —— 这里宁可安全失败也不赌。
                    opened = await wait_for_opened_conversation(
                        page, (target, *aliases)
                    )
                    if not matches_opened_conversation(opened, (target, *aliases)):
                        code = (
                            "wrong_conversation_opened"
                            if opened else "conversation_title_unavailable"
                        )
                        logger.warning("conversation_guard stage=selecting_target status=%s", code)
                        return ExecutionResult(
                            False,
                            ExecutionStage.SELECTING_TARGET,
                            code,
                            (
                                "打开的会话与目标不一致，已中止发送"
                                if opened else "未能读取唯一的会话标题，已中止发送"
                            ),
                            retryable=True,
                            discovered_names=tuple(discovered),
                            delivery_state=DeliveryState.NOT_SENT,
                        )
                    stage = ExecutionStage.SENDING
                    editor = page.locator(CHAT_EDITOR_SELECTOR).first
                    lines = message.splitlines() or [message]
                    for index, line in enumerate(lines):
                        await editor.type(line)
                        if index < len(lines) - 1:
                            await editor.press("Shift+Enter")
                    # 不可逆点：Enter 一经按下，页面可能已经在发送，即便随后
                    # 报错也无法证明没发出去。先置位，再触发。
                    send_attempted = True
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
                            delivery_state=DeliveryState.UNCERTAIN,
                        )
                    return ExecutionResult(
                        True,
                        ExecutionStage.COMPLETE,
                        discovered_names=tuple(discovered),
                        discovered_identities=identities,
                        delivery_state=DeliveryState.CONFIRMED,
                    )
                finally:
                    try:
                        if context is not None:
                            await context.close()
                    finally:
                        await browser.close()
        except AmbiguousTargetError:
            return ExecutionResult(
                False,
                ExecutionStage.SELECTING_TARGET,
                "target_ambiguous",
                "多个搜索结果的完整标题与目标相符，已中止发送",
                retryable=False,
                discovered_names=tuple(discovered),
                delivery_state=DeliveryState.NOT_SENT,
            )
        except TargetNotFoundError:
            return ExecutionResult(
                False,
                ExecutionStage.SELECTING_TARGET,
                "target_not_found",
                "会话列表和精确搜索均未找到目标，请检查昵称/抖音号及对方搜索、私信权限",
                retryable=True,
                discovered_names=tuple(discovered),
                delivery_state=DeliveryState.NOT_SENT,
            )
        except TargetSearchUnavailableError:
            return ExecutionResult(
                False,
                ExecutionStage.SELECTING_TARGET,
                "target_search_unavailable",
                "未找到会话中的目标，且抖音聊天搜索控件不可用，尚未完成目标搜索",
                retryable=True,
                discovered_names=tuple(discovered),
                delivery_state=DeliveryState.NOT_SENT,
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
                    "选人或打开聊天窗口失败，尚未发送消息",
                    retryable=True,
                    discovered_names=tuple(discovered),
                    delivery_state=DeliveryState.NOT_SENT,
                )
            # 走到这里说明异常发生在发送阶段之后。若 Enter 已经按过，
            # 结果就是不可确认而非失败，绝不能拿来当重试依据。
            state = (
                DeliveryState.UNCERTAIN if send_attempted else DeliveryState.NOT_SENT
            )
            return ExecutionResult(
                False,
                stage,
                "automation_failed",
                "页面操作或发送确认失败",
                retryable=not send_attempted,
                discovered_names=tuple(discovered),
                delivery_state=state,
            )
