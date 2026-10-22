import asyncio
import logging
import os
import re
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
# 聊天区顶部「当前打开的会话」标题（发送前校验的依据）
CONVERSATION_HEADER_SELECTOR = ".RightPanelHeadertitle"
SEARCH_INPUT_SELECTORS = (
    'input[placeholder="搜索"]',
    'input[placeholder*="搜索"]',
)
# 只在搜索条目的完整标题上匹配；高亮 span 的文字可能只是另一个人名字的一部分。
SEARCH_RESULT_SELECTOR = ".SearchPanelitembox"
SEARCH_RESULT_TITLE_SELECTOR = ".SearchPanelitemtitle"
OPENED_CONVERSATION_TIMEOUT_MS = 5000

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
    """Raised when the requested friend is absent from conversations and exact search."""


class TargetSearchUnavailableError(RuntimeError):
    """Raised when no usable search control was found to verify a missing target."""


class AmbiguousTargetError(RuntimeError):
    """多个完整标题相符时安全失败，不按页面顺序猜收件人。"""


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


def _search_result_locator(page, title):
    # 不保留 all()/nth() 的位置：列表异步重排后仍必须定位同一个完整标题。
    title_locator = page.locator(SEARCH_RESULT_TITLE_SELECTOR).filter(
        has_text=re.compile(r"^\s*" + re.escape(title) + r"\s*$")
    )
    return page.locator(SEARCH_RESULT_SELECTOR + ":visible").filter(has=title_locator)


async def _matching_search_results(page, candidates):
    accepted = {normalize_target_name(value) for value in candidates if value}
    matches = []
    for item in await page.locator(SEARCH_RESULT_SELECTOR + ":visible").all():
        titles = item.locator(SEARCH_RESULT_TITLE_SELECTOR)
        if await titles.count() != 1 or not await titles.is_visible():
            continue
        title = (await titles.inner_text()).strip()
        if title and normalize_target_name(title) in accepted:
            matches.append(ConversationRow(title, _search_result_locator(page, title)))
    if len(matches) > 1:
        logger.warning("target_selection path=search status=ambiguous")
        raise AmbiguousTargetError("ambiguous_search_results")
    return matches


async def _click_search_result(page, result, candidates) -> bool:
    # 点击前重读完整条目；新出现的重名 / 别名歧义不能靠取 first 掩盖。
    current = await _matching_search_results(page, candidates)
    if len(current) != 1 or current[0].title != result.title:
        logger.info("target_selection path=search status=result_changed")
        return False
    item = current[0].locator
    if await item.count() != 1:
        return False
    # 按钮仅能来自当前条目，禁止向上攀到整个结果面板后点击别人的按钮。
    send_button = item.locator(
        "xpath=.//*[normalize-space()='发消息' and "
        "not(.//*[normalize-space()='发消息'])]"
    )
    count = await send_button.count()
    if count > 1:
        raise AmbiguousTargetError("ambiguous_search_action")
    if count == 1 and await send_button.is_visible():
        await send_button.click()
    else:
        await item.click()
    logger.info("target_selection path=search status=clicked")
    return True


async def _wait_for_visible_search_results(page, candidate, timeout_ms, *, candidates=()):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, timeout_ms / 1000)
    accepted = (candidate, *candidates)
    while True:
        results = await _matching_search_results(page, accepted)
        if results:
            return results
        remaining = deadline - loop.time()
        if remaining <= 0:
            return []
        await asyncio.sleep(min(0.2, remaining))


async def _search_in_panel(page, field, candidate, timeout_ms, *, candidates=()):
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
            await asyncio.sleep(min(SEARCH_COLD_START_SETTLE_SECONDS, timeout_ms / 10000))
        await field.fill(candidate)
        # 冷启动先短等一次，重搜后给慢响应更多时间；外层限制整个选人预算。
        wait_ms = min(timeout_ms / (SEARCH_COLD_START_RETRIES + 1), 3000 if attempt == 0 else 8000)
        results = await _wait_for_visible_search_results(
            page, candidate, wait_ms, candidates=candidates
        )
        if results:
            return results
    return []


async def opened_conversation_title(page) -> str:
    """读聊天区顶部「当前打开的会话」标题；读不到时返回空串。

    这是发送前最后一道校验的依据。选人链路（搜索面板的结果、虚拟滚动复用的
    会话行）都存在选错的可能，而消息一旦发出去就撤不回来 —— 宁可认成「读不到」
    让调用方安全失败，也不能默认放行。
    """

    try:
        locator = page.locator(CONVERSATION_HEADER_SELECTOR + ":visible")
        if await locator.count() != 1:
            return ""
        return (await locator.inner_text(timeout=500)).strip()
    except Exception:  # noqa: BLE001 —— 校验读取失败一律按「读不到」处理
        return ""


def matches_opened_conversation(opened_title: str, candidates) -> bool:
    """打开的会话标题是否就是候选之一（用与选人一致的归一化规则比对）。"""

    opened = normalize_target_name(opened_title)
    if not opened:
        return False
    return any(
        opened == normalize_target_name(value) for value in candidates if value
    )


async def wait_for_opened_conversation(
    page, candidates, timeout_ms: int = OPENED_CONVERSATION_TIMEOUT_MS
) -> str:
    """等待聊天区标题切换到本次选中的目标，避免读到上一个会话的旧标题。"""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, timeout_ms / 1000)
    opened = ""
    while True:
        opened = await opened_conversation_title(page)
        if matches_opened_conversation(opened, candidates):
            return opened
        remaining = deadline - loop.time()
        if remaining <= 0:
            return opened
        await asyncio.sleep(min(0.1, remaining))


async def select_web_chat_target(
    page, target, timeout=30000, aliases=(), scroll=False, discovered=None
):
    async with asyncio.timeout(timeout / 1000):
        return await _select_web_chat_target(
            page, target, timeout, aliases, scroll, discovered
        )


async def _click_conversation_row(page, row):
    # all() 返回的是按位置重查的 locator。点击时按标题重新定位，防止列表重排后换人。
    title = page.locator(CONVERSATION_TITLE_SELECTOR).filter(
        has_text=re.compile(r"^\s*" + re.escape(row.title) + r"\s*$")
    )
    current = page.locator(CONVERSATION_ITEM_SELECTOR + ":visible").filter(has=title)
    if await current.count() != 1:
        return False
    await current.click()
    return True


async def _select_web_chat_target(page, target, timeout, aliases, scroll, discovered):
    """Select one exact target, preferring real conversation rows over page text.

    滚动快照只用于记录名字，点击必须重新读取当前列表，不能重用滚动前的行位置。
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

    if scroll:
        remember(await _conversation_rows(page, scroll=True))
        container = page.locator(CONVERSATION_LIST_SELECTOR)
        if await container.count() > 0:
            await container.first.evaluate("element => { element.scrollTop = 0; }")
            await page.wait_for_timeout(150)

    rows = await _conversation_rows(page, scroll=False)
    remember(rows)
    for row in rows:
        if row.title in raw_candidates and await _click_conversation_row(page, row):
            logger.info("target_selection path=conversation status=clicked")
            return row.title

    search_available = False
    for selector in SEARCH_INPUT_SELECTORS:
        try:
            search = page.locator(selector)
            if await search.count() == 0:
                continue
            field = search.first
            if not hasattr(field, "fill"):
                continue
            if hasattr(field, "is_visible") and not await field.is_visible():
                continue
            search_available = True
            for candidate in raw_candidates:
                results = await _search_in_panel(
                    page, field, candidate, timeout, candidates=raw_candidates
                )
                for result in results:
                    if await _click_search_result(page, result, raw_candidates):
                        return result.title
            # 搜索结果会覆盖左栏，必须退出搜索再检查会话，不能在搜索态白等列表。
            await field.fill("")
            break
        except (AttributeError, TypeError):
            # Older page doubles and older layouts have no global search surface.
            search_available = False
            break

    await page.wait_for_selector(CONVERSATION_ITEM_SELECTOR, timeout=timeout)

    rows = await _conversation_rows(page, scroll=False)
    remember(rows)

    for row in rows:
        if row.title in raw_candidates and await _click_conversation_row(page, row):
            logger.info("target_selection path=conversation_after_search status=clicked")
            return row.title

    # 精确名全没命中时，只放宽「零宽字符/空白差异」，绝不放宽大小写，
    # 免得把消息发给另一个同名不同大小写的人。
    for row in rows:
        if (
            normalize_target_name(row.title) in folded_candidates
            and await _click_conversation_row(page, row)
        ):
            logger.info("target_selection path=conversation_normalized status=clicked")
            return row.title

    if not search_available:
        logger.warning("target_selection path=search status=unavailable")
        raise TargetSearchUnavailableError("search_control_unavailable")

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
