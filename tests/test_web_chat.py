import unittest

from core.web_chat import (
    CONVERSATION_LIST_SELECTOR,
    SEARCH_RESULT_SELECTOR,
    SEARCH_RESULT_TITLE_SELECTOR,
    AmbiguousTargetError,
    TargetNotFoundError,
    UserInfoCollector,
    collect_web_chat_conversation_names,
    list_visible_web_chat_targets,
    matches_opened_conversation,
    normalize_target_name,
    select_web_chat_target,
    wait_for_opened_conversation,
)


class FakeTitle:
    def __init__(self, text):
        self.text = text

    async def inner_text(self):
        return self.text


class FakeConversation:
    def __init__(self, title, visible=True):
        self.title = title
        self.visible = visible
        self.clicked = False

    def locator(self, selector):
        return FakeTitle(self.title)

    async def click(self):
        self.clicked = True

    async def is_visible(self):
        return self.visible


class FakeConversationList:
    def __init__(self, items):
        self.items = items

    async def all(self):
        return self.items

    async def count(self):
        return len(self.items)

    async def click(self):
        assert len(self.items) == 1
        await self.items[0].click()

    def filter(self, *, has=None, has_text=None):
        if has_text is not None:
            return has_text
        return FakeConversationList([
            item for item in self.items if item.visible and has.fullmatch(item.title)
        ])


class FakeWebChatPage:
    def __init__(self, titles, visible=None):
        visible = visible or [True] * len(titles)
        self.items = [
            FakeConversation(title, is_visible)
            for title, is_visible in zip(titles, visible)
        ]
        self.waited_for = None

    async def wait_for_selector(self, selector, *, timeout):
        self.waited_for = (selector, timeout)

    def locator(self, selector):
        return FakeConversationList(self.items)


class FakeSearchField:
    def __init__(self):
        self.first = self
        self.fills = []

    async def count(self):
        return 1

    async def fill(self, value):
        self.fills.append(value)


class FakeSearchTitle(FakeTitle):
    async def count(self):
        return 1

    async def is_visible(self):
        return True


class FakeSendButton:
    def __init__(self, present=False):
        self.present = present
        self.clicked = False

    async def count(self):
        return int(self.present)

    async def is_visible(self):
        return self.present

    async def click(self):
        self.clicked = True


class FakeSearchItem(FakeConversation):
    def __init__(self, title, visible=True, send_button=False):
        super().__init__(title, visible)
        self.send_button = FakeSendButton(send_button)

    def locator(self, selector):
        if selector == SEARCH_RESULT_TITLE_SELECTOR:
            return FakeSearchTitle(self.title)
        assert selector.startswith("xpath=.//*")
        return self.send_button


class FakeSearchResults:
    def __init__(self, page, pattern=None):
        self.page = page
        self.pattern = pattern

    def _items(self):
        if not self.page.ready():
            return []
        return [item for item in self.page.search_items if item.visible and (
            self.pattern is None or self.pattern.fullmatch(item.title)
        )]

    async def all(self):
        self.page.checks += 1
        return self._items()

    async def count(self):
        return len(self._items())

    def filter(self, *, has=None, has_text=None):
        if has_text is not None:
            return has_text
        return FakeSearchResults(self.page, has)

    def locator(self, selector):
        items = self._items()
        assert len(items) == 1
        return items[0].locator(selector)

    async def click(self):
        items = self._items()
        assert len(items) == 1
        await items[0].click()


class FakeSearchPage:
    def __init__(self, items):
        self.search = FakeSearchField()
        self.search_items = items
        self.checks = 0
        self.ready = lambda: True
        self.recent_requested = False

    def locator(self, selector):
        if selector.startswith("input"):
            return self.search
        if selector in (SEARCH_RESULT_SELECTOR + ":visible", SEARCH_RESULT_TITLE_SELECTOR):
            return FakeSearchResults(self)
        self.recent_requested = True
        return FakeConversationList([])

    def get_by_text(self, *_args, **_kwargs):
        raise AssertionError("must never search arbitrary page text")


class WebChatSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_info_collector_maps_stable_id_to_current_aliases(self):
        class Response:
            url = "https://www.douyin.com/aweme/v1/web/im/user/info/?aid=6383"
            status = 200

            async def json(self):
                return {
                    "data": [
                        {
                            "sec_uid": "stable-user-id",
                            "short_id": "123456",
                            "unique_id": "old-search-id",
                            "nickname": "新的昵称",
                            "remark_name": "我的备注",
                        }
                    ]
                }

        collector = UserInfoCollector()
        await collector.handle_response(Response())

        identity = collector.get("stable-user-id")
        self.assertEqual("stable-user-id", identity.sec_uid)
        self.assertEqual(
            ("我的备注", "新的昵称", "old-search-id", "123456"),
            identity.aliases,
        )

    async def test_selects_renamed_friend_using_current_identity_alias(self):
        page = FakeWebChatPage(["新的昵称", "another friend"])

        selected = await select_web_chat_target(
            page,
            "旧的昵称",
            aliases=("我的备注", "新的昵称", "old-search-id"),
        )

        self.assertEqual("新的昵称", selected)
        self.assertTrue(page.items[0].clicked)
        self.assertFalse(page.items[1].clicked)

    async def test_lists_visible_unique_conversations_for_task_picker(self):
        page = FakeWebChatPage(
            [" wzlovegsy ", "gsy", "wzlovegsy", "隐藏"],
            visible=[True, True, True, False],
        )

        targets = await list_visible_web_chat_targets(page, timeout=3200)

        self.assertEqual(["wzlovegsy", "gsy"], targets)
        self.assertEqual((".conversationConversationItemwrapper", 3200), page.waited_for)

    async def test_selects_only_the_requested_web_chat_conversation(self):
        page = FakeWebChatPage(["another friend", " ʚ繁花ɞ🌸 "])

        selected = await select_web_chat_target(page, "ʚ繁花ɞ🌸", timeout=3200)

        self.assertEqual("ʚ繁花ɞ🌸", selected)
        self.assertFalse(page.items[0].clicked)
        self.assertTrue(page.items[1].clicked)

    async def test_prefers_complete_search_title(self):
        item = FakeSearchItem("Target")
        page = FakeSearchPage([FakeSearchItem("Target-Other"), item])
        selected = await select_web_chat_target(page, "Target")
        self.assertEqual("Target", selected)
        self.assertTrue(item.clicked)
        self.assertFalse(page.search_items[0].clicked)
        self.assertTrue(page.recent_requested)

    async def test_search_result_clicks_send_message_button(self):
        item = FakeSearchItem("Target", send_button=True)
        page = FakeSearchPage([item])
        self.assertEqual("Target", await select_web_chat_target(page, "Target"))
        self.assertTrue(item.send_button.clicked)
        self.assertFalse(item.clicked)

    async def test_waits_for_delayed_search_result(self):
        item = FakeSearchItem("Target")
        page = FakeSearchPage([item])
        page.ready = lambda: page.checks >= 3
        selected = await select_web_chat_target(page, "Target", timeout=1000)
        self.assertEqual("Target", selected)
        self.assertGreaterEqual(page.checks, 3)
        self.assertTrue(item.clicked)

    async def test_retries_a_cold_search_panel_that_returns_nothing(self):
        item = FakeSearchItem("Target")
        page = FakeSearchPage([item])
        page.ready = lambda: page.search.fills.count("Target") >= 2
        selected = await select_web_chat_target(page, "Target", timeout=300)
        self.assertEqual("Target", selected)
        self.assertTrue(item.clicked)
        self.assertEqual(["Target", "", "Target"], page.search.fills)

    async def test_search_rejects_duplicate_complete_titles(self):
        page = FakeSearchPage([FakeSearchItem("Target"), FakeSearchItem("Target")])
        with self.assertRaises(AmbiguousTargetError):
            await select_web_chat_target(page, "Target")
        self.assertFalse(any(item.clicked for item in page.search_items))

    async def test_identifier_search_can_match_a_known_full_nickname(self):
        item = FakeSearchItem("Current name")
        page = FakeSearchPage([item])
        selected = await select_web_chat_target(
            page, "Old name", aliases=("stable-handle", "Current name")
        )
        self.assertEqual("Current name", selected)
        self.assertEqual(["stable-handle"], page.search.fills)
        self.assertTrue(item.clicked)

    async def test_ignores_hidden_duplicate_and_clicks_visible_conversation(self):
        page = FakeWebChatPage(
            ["ʚ繁花ɞ🌸", "ʚ繁花ɞ🌸"],
            visible=[False, True],
        )

        selected = await select_web_chat_target(page, "ʚ繁花ɞ🌸")

        self.assertEqual("ʚ繁花ɞ🌸", selected)
        self.assertFalse(page.items[0].clicked)
        self.assertTrue(page.items[1].clicked)

    async def test_search_ignores_hidden_exact_result(self):
        hidden = FakeSearchItem("Target", visible=False)
        visible = FakeSearchItem("Target")
        page = FakeSearchPage([hidden, visible])
        self.assertEqual("Target", await select_web_chat_target(page, "Target"))
        self.assertFalse(hidden.clicked)
        self.assertTrue(visible.clicked)

    async def test_clicks_already_visible_conversation_before_opening_search(self):
        class Page:
            def __init__(self):
                self.item = FakeConversation("wzlovegsy", True)
                self.search_requested = False

            def locator(self, selector):
                if selector.startswith(".conversationConversationItem"):
                    return FakeConversationList([self.item])
                self.search_requested = True
                raise AssertionError("search should not open when the target is already visible")
        page = Page()

        selected = await select_web_chat_target(page, "wzlovegsy")

        self.assertEqual("wzlovegsy", selected)
        self.assertTrue(page.item.clicked)
        self.assertFalse(page.search_requested)

    async def test_ignores_same_name_text_outside_conversation_list(self):
        class Results:
            def __init__(self, item):
                self.items = [item]
                self.first = item

            async def count(self):
                return len(self.items)

            async def all(self):
                return self.items

        class Page(FakeWebChatPage):
            def __init__(self):
                super().__init__(["wzlovegsy"])
                self.decoy = FakeConversation("wzlovegsy")

            def get_by_text(self, _text, exact):
                return Results(self.decoy)

        page = Page()

        selected = await select_web_chat_target(page, "wzlovegsy")

        self.assertEqual("wzlovegsy", selected)
        self.assertTrue(page.items[0].clicked)
        self.assertFalse(page.decoy.clicked)

    async def test_fails_instead_of_sending_to_the_wrong_conversation(self):
        page = FakeWebChatPage(["another friend"])

        with self.assertRaises(TargetNotFoundError):
            await select_web_chat_target(page, "ʚ繁花ɞ🌸")

        self.assertFalse(page.items[0].clicked)


class FakeScrollContainer:
    def __init__(self, conversation_list):
        self.conversation_list = conversation_list

    async def count(self):
        return 1

    @property
    def first(self):
        return self

    async def evaluate(self, _script):
        self.conversation_list.reveal_next_batch()


class FakeLazyConversationList(FakeConversationList):
    """模拟抖音左栏：滚到底才渲染下一屏。"""

    def __init__(self, batches):
        self.batches = [list(batch) for batch in batches]
        self.revealed = 1

    def reveal_next_batch(self):
        if self.revealed < len(self.batches):
            self.revealed += 1

    async def all(self):
        return self.items

    @property
    def items(self):
        return [
            FakeConversation(title)
            for batch in self.batches[: self.revealed]
            for title in batch
        ]


class FakeScrollingPage:
    def __init__(self, batches):
        self.conversations = FakeLazyConversationList(batches)

    async def wait_for_selector(self, _selector, *, timeout):
        return None

    async def wait_for_timeout(self, _milliseconds):
        return None

    def locator(self, selector):
        if selector == CONVERSATION_LIST_SELECTOR:
            return FakeScrollContainer(self.conversations)
        return self.conversations


class WebChatNameNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_the_opened_title_to_switch(self):
        class Header:
            def __init__(self):
                self.reads = 0
                self.first = self

            async def count(self):
                return 1

            async def inner_text(self, **_kwargs):
                self.reads += 1
                return "旧会话" if self.reads < 2 else "目标"

        class Page:
            def locator(self, _selector):
                return HeaderLocator

        HeaderLocator = Header()
        opened = await wait_for_opened_conversation(Page(), ("目标",), timeout_ms=500)

        self.assertEqual("目标", opened)

    async def test_only_sends_when_the_opened_conversation_is_the_target(self):
        """发送前的标题校验：打开的必须是目标本人或它的别名，否则一律拒绝。"""

        self.assertTrue(matches_opened_conversation("我是妹妹", ("我是妹妹", "kwkooy")))
        self.assertTrue(matches_opened_conversation("kwkooy", ("我是妹妹", "kwkooy")))
        # 归一化：零宽字符 / 空白差异不算换人
        self.assertTrue(matches_opened_conversation("punk\xa0LeeLii", ("punk LeeLii",)))
        # 打开的是别人 → 必须拒绝（这就是「消息发错人」的兜底）
        self.assertFalse(matches_opened_conversation("鸡蛋面", ("我是妹妹", "kwkooy")))
        # 读不到标题、候选为空 → 安全失败
        self.assertFalse(matches_opened_conversation("", ("我是妹妹",)))
        self.assertFalse(matches_opened_conversation("我是妹妹", ()))

    async def test_matches_a_name_that_differs_only_by_invisible_characters(self):
        page = FakeWebChatPage(["punk\xa0LeeLii", "别人"])

        selected = await select_web_chat_target(page, "punk LeeLii")

        self.assertEqual("punk\xa0LeeLii", selected)
        self.assertTrue(page.items[0].clicked)
        self.assertFalse(page.items[1].clicked)

    async def test_never_matches_a_differently_cased_name(self):
        page = FakeWebChatPage(["alp"])

        with self.assertRaises(TargetNotFoundError):
            await select_web_chat_target(page, "Alp")

    async def test_normalization_only_folds_invisible_differences(self):
        self.assertEqual("Alp", normalize_target_name("  Alp\u200b "))
        self.assertEqual("punk LeeLii", normalize_target_name("punk\xa0LeeLii"))
        self.assertNotEqual(
            normalize_target_name("Alp"), normalize_target_name("alp")
        )


class WebChatSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_scrolling_reveals_the_whole_conversation_list(self):
        page = FakeScrollingPage([["第一位好友", "Alp"], ["更早的好友"], ["最早的好友"]])

        names = await collect_web_chat_conversation_names(page)

        self.assertEqual(("第一位好友", "Alp", "更早的好友", "最早的好友"), names)

    async def test_selection_can_reach_a_friend_from_a_later_screen(self):
        page = FakeScrollingPage([["最近的好友"], ["沉到下面的 Alp"]])

        discovered = []
        selected = await select_web_chat_target(
            page, "沉到下面的 Alp", scroll=True, discovered=discovered
        )

        self.assertEqual("沉到下面的 Alp", selected)
        self.assertIn("最近的好友", discovered)

    async def test_reports_every_name_it_saw_for_the_snapshot(self):
        page = FakeScrollingPage([["甲", "乙"]])

        discovered = []
        await select_web_chat_target(page, "乙", discovered=discovered)

        self.assertEqual(["甲", "乙"], discovered)


if __name__ == "__main__":
    unittest.main()
