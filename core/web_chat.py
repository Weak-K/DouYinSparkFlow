import asyncio
import logging
import os
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from utils.logger import setup_logger


WEB_CHAT_URL = "https://www.douyin.com/chat"
CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"
SEARCH_INPUT_SELECTORS = (
    'input[placeholder="搜索"]',
    'input[placeholder*="搜索"]',
)

# 零宽字符在页面上看不见，却会让 `==` 失配（历史上出现过 \u00a0 与普通空格并存）。
_INVISIBLE_CHARACTERS = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"), None
)

CONVERSATION_SCROLL_MAX_ITEMS = 300
# 滚动刷新好友快照的硬预算：宁可少读几个，也不能把任务的 180 秒执行超时顶掉。
CONVERSATION_SCROLL_BUDGET_SECONDS = 20.0

# 搜索面板冷启动：同一个页面上的第一次输入常常不出结果，清空重搜一轮即可恢复。
# （2026-09-24 线上实测：第一次 fill 等满 3 秒仍是 0 条，紧接着的第二次 0.5 秒就出。）
SEARCH_COLD_START_RETRIES = 1
SEARCH_COLD_START_SETTLE_SECONDS = 0.3

logger = setup_logger(level=logging.DEBUG)


def normalize_target_name(value) -> str:
    """把「人眼无法区分、但字符串比较会失配」的差异折叠掉。

    只做字符层的宽松化（NFKC + 去掉零宽字符 + 空白折叠），**不做大小写折叠**：
    匹配失败是安全失败，误发到另一个人才是事故。
    """

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(_INVISIBLE_CHARACTERS)
    # str.split() 会把 \xa0 / \u3000 等一并当空白处理。
    return " ".join(text.split())


@dataclass(frozen=True)
class DouyinUserIdentity:
    sec_uid: str
    short_id: str | None = None
    unique_id: str | None = None
    nickname: str | None = None
    remark_name: str | None = None

    @property
    def aliases(self) -> tuple[str, ...]:
        values = (
            self.remark_name,
            self.nickname,
            self.unique_id,
            self.short_id,
        )
        return tuple(dict.fromkeys(value.strip() for value in values if value and value.strip()))


class UserInfoCollector:
    PATH = "/aweme/v1/web/im/user/info"

    def __init__(self):
        self.identities: dict[str, DouyinUserIdentity] = {}
        self._changed = asyncio.Event()
        self._pending: set[asyncio.Task] = set()

    def capture(self, response) -> None:
        if self.PATH not in urlparse(response.url).path:
            return
        task = asyncio.create_task(self.handle_response(response))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def handle_response(self, response) -> None:
        if self.PATH not in urlparse(response.url).path or response.status != 200:
            return
        try:
            body = await response.json()
        except Exception:
            return
        data = body.get("data", ()) if isinstance(body, dict) else ()
        for item in data if isinstance(data, list) else ():
            if not isinstance(item, dict):
                continue
            sec_uid = str(item.get("sec_uid") or "").strip()
            if not sec_uid:
                continue
            self.identities[sec_uid] = DouyinUserIdentity(
                sec_uid=sec_uid,
                short_id=_optional_string(item.get("short_id")),
                unique_id=_optional_string(item.get("unique_id")),
                nickname=_optional_string(item.get("nickname")),
                remark_name=_optional_string(item.get("remark_name")),
            )
            self._changed.set()

    def get(self, sec_uid: str) -> DouyinUserIdentity | None:
        return self.identities.get(sec_uid)

    async def wait_for(self, sec_uid: str, timeout: float = 15.0) -> DouyinUserIdentity | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            identity = self.get(sec_uid)
            if identity is not None:
                return identity
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), deadline - loop.time())
            except TimeoutError:
                break
        return self.get(sec_uid)

    async def drain(self) -> tuple[DouyinUserIdentity, ...]:
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)
        return tuple(self.identities.values())


def _optional_string(value) -> str | None:
    text = str(value or "").strip()
    return text or None


class TargetNotFoundError(RuntimeError):
    """Raised when the requested friend is absent from the web chat list."""


async def list_visible_web_chat_targets(page, timeout=30000):
    """Return unique, visible conversation titles from the signed-in chat list."""
    await page.wait_for_selector(CONVERSATION_ITEM_SELECTOR, timeout=timeout)
    targets = []
    seen = set()
    for item in await page.locator(CONVERSATION_ITEM_SELECTOR).all():
        if hasattr(item, "is_visible") and not await item.is_visible():
            continue
        title = (
            await item.locator(CONVERSATION_TITLE_SELECTOR).inner_text()
        ).strip()
        if title and title not in seen:
            seen.add(title)
            targets.append(title)
    return targets


@dataclass(frozen=True)
class ConversationRow:
    title: str
    locator: object


async def _scroll_conversation_list(page, last_row) -> bool:
    """把左侧会话列表往下推一屏；无法滚动时返回 False 让调用方收工。"""

    try:
        container = page.locator(CONVERSATION_LIST_SELECTOR)
        if await container.count() > 0:
            await container.first.evaluate(
                "(element) => { element.scrollTop = element.scrollHeight; }"
            )
        elif last_row is not None:
            await last_row.scroll_into_view_if_needed()
        else:
            return False
    except Exception:
        return False
    try:
        await page.wait_for_timeout(600)
    except Exception:
        pass
    return True


async def _conversation_rows(
    page,
    *,
    scroll: bool = False,
    max_items: int = CONVERSATION_SCROLL_MAX_ITEMS,
    budget_seconds: float = CONVERSATION_SCROLL_BUDGET_SECONDS,
) -> list[ConversationRow]:
    """读出会话列表；`scroll=True` 时一直往下滚到不再新增为止。

    抖音左栏是懒加载的，登录那一刻只渲染首屏十来条——这正是控制台好友列表
    只有十几条的根因。滚动会拉长执行时间，所以只有需要刷新快照时才打开，
    而且有「条数上限 + 秒数预算」两道闸，超了就拿已读到的部分收工。
    这里不等待列表出现（列表尚未渲染时返回空列表），等待由调用方负责。
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    collected: dict[str, ConversationRow] = {}
    idle_rounds = 0
    while True:
        before = len(collected)
        items = await page.locator(CONVERSATION_ITEM_SELECTOR).all()
        visible_items = []
        for item in items:
            if hasattr(item, "is_visible") and not await item.is_visible():
                continue
            visible_items.append(item)
            title = (
                await item.locator(CONVERSATION_TITLE_SELECTOR).inner_text()
            ).strip()
            if title and title not in collected:
                collected[title] = ConversationRow(title, item)
        if not scroll or len(collected) >= max_items or loop.time() >= deadline:
            break
        if len(collected) == before:
            idle_rounds += 1
        else:
            idle_rounds = 0
        if idle_rounds >= 2:
            break
        if not await _scroll_conversation_list(
            page, visible_items[-1] if visible_items else None
        ):
            break
    return list(collected.values())


async def collect_web_chat_conversation_names(
    page,
    *,
    scroll: bool = True,
    max_items: int = CONVERSATION_SCROLL_MAX_ITEMS,
    budget_seconds: float = CONVERSATION_SCROLL_BUDGET_SECONDS,
) -> tuple[str, ...]:
    """返回会话列表里全部（尽可能多）的显示名，用于刷新好友快照。"""

    rows = await _conversation_rows(
        page, scroll=scroll, max_items=max_items, budget_seconds=budget_seconds
    )
    return tuple(row.title for row in rows)


async def _click_search_result(result) -> None:
    try:
        send_button = result.locator(
            "xpath=ancestor::*[.//*[normalize-space()='发消息']][1]"
            "//*[normalize-space()='发消息' and "
            "not(.//*[normalize-space()='发消息'])]"
        )
        if await send_button.count() > 0 and await send_button.is_visible():
            await send_button.click()
            return
    except (AttributeError, TypeError):
        pass
    await result.click()


async def _wait_for_visible_search_results(page, candidate, timeout_ms):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(3.0, max(0.2, timeout_ms / 1000))
    while True:
        exact = page.get_by_text(candidate, exact=True)
        if await exact.count() > 0:
            results = await exact.all() if hasattr(exact, "all") else [exact.first]
            visible = []
            for result in results:
                if hasattr(result, "is_visible") and not await result.is_visible():
                    continue
                visible.append(result)
            if visible:
                return visible
        remaining = deadline - loop.time()
        if remaining <= 0:
            return []
        await asyncio.sleep(min(0.2, remaining))


async def _search_in_panel(page, field, candidate, timeout_ms):
    """在搜索框里搜一个候选，返回可见的精确文本结果（可能为空列表）。

    抖音搜索面板存在**冷启动不出结果**的行为：同一个页面上的第一次输入，结果
    列表一直是空的（线上实测等满 3 秒仍为 0 条），紧接着的第二次输入 0.5 秒就
    出结果。控制台每条任务都新起一个浏览器，于是「每条任务的第一次搜索」必然
    撞上这个行为 —— 凡是不在左侧会话列表里的好友都会被误判成「找不到目标」。

    这里用「清空后重搜一轮」把冷启动那一跳让过去。第一次就出结果时不会多花时间。
    """

    for attempt in range(SEARCH_COLD_START_RETRIES + 1):
        if attempt:
            try:
                await field.fill("")
            except (AttributeError, TypeError):
                pass
            await asyncio.sleep(SEARCH_COLD_START_SETTLE_SECONDS)
        await field.fill(candidate)
        results = await _wait_for_visible_search_results(page, candidate, timeout_ms)
        if results:
            return results
    return []


async def select_web_chat_target(
    page, target, timeout=30000, aliases=(), scroll=False, discovered=None
):
    """Select one exact target, preferring real conversation rows over page text.

    `scroll=True` 时先把左侧会话列表滚动加载完整再匹配（首屏只有十来条，
    更靠下的好友根本扫不到）。`discovered` 是可选的列表出参：把本次真正看到的
    会话显示名写回去，调用方据此刷新好友快照。
    """

    normalized_target = target.strip()
    raw_candidates = tuple(
        dict.fromkeys(
            value.strip() for value in (*aliases, normalized_target) if value and value.strip()
        )
    )
    folded_candidates = {
        normalize_target_name(value): value for value in raw_candidates
    }

    def remember(rows):
        if discovered is None:
            return
        for row in rows:
            if row.title and row.title not in discovered:
                discovered.append(row.title)

    rows = await _conversation_rows(page, scroll=scroll)
    remember(rows)
    for row in rows:
        if row.title in raw_candidates:
            await row.locator.click()
            return row.title

    for selector in SEARCH_INPUT_SELECTORS:
        try:
            search = page.locator(selector)
            if await search.count() == 0:
                continue
            field = search.first
            for candidate in raw_candidates:
                results = await _search_in_panel(page, field, candidate, timeout)
                for result in results:
                    await _click_search_result(result)
                    return candidate
        except (AttributeError, TypeError):
            # Older page doubles and older layouts have no global search surface.
            break

    await page.wait_for_selector(CONVERSATION_ITEM_SELECTOR, timeout=timeout)

    known = {row.title for row in rows}
    retry_rows = await _conversation_rows(page, scroll=False)
    remember(retry_rows)
    for row in retry_rows:
        if row.title not in known:
            known.add(row.title)
            rows.append(row)

    for row in rows:
        if row.title in raw_candidates:
            await row.locator.click()
            return row.title

    # 精确名全没命中时，只放宽「零宽字符/空白差异」，绝不放宽大小写，
    # 免得把消息发给另一个同名不同大小写的人。
    for row in rows:
        if normalize_target_name(row.title) in folded_candidates:
            await row.locator.click()
            return row.title

    raise TargetNotFoundError(f"未在抖音聊天列表中找到好友 {normalized_target}")


async def run_wz_web_chat_probe():
    """Send and verify one WZ message through https://www.douyin.com/chat."""
    from core.browser import get_browser
    from core.msg_builder import build_message
    from core.tasks import confirm_message_sent
    from utils.config import get_userData

    users = get_userData()
    if len(users) != 1:
        raise RuntimeError("WZ 单向测试必须且只能包含一个账号")

    user = users[0]
    targets = user.get("targets", [])
    if len(targets) != 1:
        raise RuntimeError("WZ 单向测试必须且只能包含一个目标好友")

    username = user.get("username", "未知用户")
    target = targets[0]
    playwright, browser = await get_browser()
    context = None

    try:
        context = await browser.new_context()
        context.set_default_navigation_timeout(120000)
        context.set_default_timeout(120000)
        await context.add_cookies(user["cookies"])
        page = await context.new_page()
        await page.goto(WEB_CHAT_URL)

        try:
            await select_web_chat_target(page, target)
            await page.wait_for_selector(CHAT_EDITOR_SELECTOR, timeout=30000)
        except Exception:
            os.makedirs(os.path.join("logs", "diagnostics"), exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            await page.screenshot(
                path=os.path.join(
                    "logs", "diagnostics", f"web_chat_probe_{timestamp}.png"
                ),
                full_page=True,
            )
            raise

        chat_input = page.locator(CHAT_EDITOR_SELECTOR).first
        message = build_message()
        lines = message.split("\n")
        for index, line in enumerate(lines):
            await chat_input.type(line)
            if index < len(lines) - 1:
                await chat_input.press("Shift+Enter")

        logger.info(f"账号 {username} 准备通过抖音网页聊天发送消息给 {target}")
        await chat_input.press("Enter")
        await confirm_message_sent(page, chat_input, message)
        logger.info(f"账号 {username} 给好友 {target} 发送消息并确认送达完成")
    finally:
        if context is not None:
            await context.close()
        await browser.close()
        await playwright.stop()
