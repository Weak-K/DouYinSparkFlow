import unittest

from core.tasks import (
    LoginRequiredError,
    click_first_match,
    confirm_message_sent,
    page_has_login_prompt,
    read_message_baseline,
    visible_message_fragments,
    wait_for_authenticated_selector,
    wait_for_first_visible,
)


class FakePage:
    def __init__(self, visible_selector):
        self.visible_selector = visible_selector
        self.attempts = []

    async def wait_for_selector(self, selector, *, state, timeout):
        self.attempts.append((selector, state, timeout))
        if selector != self.visible_selector:
            raise TimeoutError(selector)
        return selector


class FakeLocator:
    def __init__(self, count):
        self._count = count

    async def count(self):
        return self._count


class FakeLoginPage:
    def __init__(self, visible_text, url="https://creator.douyin.com/creator-micro/data/following/chat"):
        self.visible_text = visible_text
        self.url = url

    def locator(self, selector):
        return FakeLocator(1 if selector == f"text={self.visible_text}" else 0)


class FakeDelayedLoginPage(FakeLoginPage):
    def __init__(self):
        super().__init__(None)

    async def wait_for_selector(self, selector, *, state, timeout):
        self.visible_text = "扫码登录"
        raise TimeoutError(selector)


class FakeClickableLocator:
    def __init__(self):
        self.first = self
        self.clicked = False

    async def click(self):
        self.clicked = True


class FakeClickablePage:
    def __init__(self):
        self.result = FakeClickableLocator()

    def locator(self, selector):
        return self.result


class FakeMessageLocator:
    def __init__(self):
        self.last = self
        self.wait_calls = []

    async def wait_for(self, *, state, timeout):
        self.wait_calls.append((state, timeout))


class FakeChatInput:
    async def element_handle(self):
        return "chat-input-handle"


class FakeDeliveryPage:
    def __init__(self, bubble_states=None):
        self.wait_function_calls = []
        self.message_locator = FakeMessageLocator()
        self.requested_text = None
        self.evaluated = []
        self.bubble_states = list(
            bubble_states or [["old message", "oldest message"]]
        )

    async def wait_for_function(self, expression, *, arg, timeout):
        self.wait_function_calls.append((expression, arg, timeout))

    def get_by_text(self, text, *, exact):
        self.requested_text = (text, exact)
        return self.message_locator

    async def evaluate(self, expression, arg):
        self.evaluated.append((expression, arg))
        if len(self.bubble_states) > 1:
            return self.bubble_states.pop(0)
        return self.bubble_states[0]


class WaitForFirstVisibleTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_the_first_visible_selector(self):
        page = FakePage("new-selector")

        selected = await wait_for_first_visible(
            page,
            ["old-selector", "new-selector", "unused-selector"],
            timeout=250,
        )

        self.assertEqual("new-selector", selected)
        self.assertEqual(
            [
                ("old-selector", "visible", 250),
                ("new-selector", "visible", 250),
            ],
            page.attempts,
        )


class LoginPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_detects_a_scan_login_prompt(self):
        page = FakeLoginPage("扫码登录")

        self.assertTrue(await page_has_login_prompt(page))

    async def test_stops_waiting_when_login_prompt_appears_late(self):
        page = FakeDelayedLoginPage()

        with self.assertRaises(LoginRequiredError):
            await wait_for_authenticated_selector(
                page,
                ["friends-tab"],
                timeout=1000,
                poll_timeout=25,
            )


class ClickFirstMatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_clicks_only_the_first_matching_element(self):
        page = FakeClickablePage()

        await click_first_match(page, "many-matches")

        self.assertTrue(page.result.clicked)

    async def test_detects_redirect_back_to_the_login_homepage(self):
        page = FakeLoginPage(None, url="https://creator.douyin.com/")

        self.assertTrue(await page_has_login_prompt(page))


class VisibleMessageFragmentsTests(unittest.TestCase):
    def test_strips_douyin_emoji_tags(self):
        self.assertEqual(
            ("今日火花",), visible_message_fragments("[盖瑞]今日火花[加一]")
        )

    def test_keeps_each_visible_line(self):
        self.assertEqual(
            ("第一行", "第二行"),
            visible_message_fragments("第一行\n[笑脸]第二行"),
        )


class MessageBaselineTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_both_ends_of_the_bubble_list(self):
        page = FakeDeliveryPage()

        self.assertEqual(
            ("old message", "oldest message"),
            await read_message_baseline(page),
        )
        self.assertEqual(1, len(page.evaluated))
        self.assertEqual(
            ".messageMessageBoxmessageBox", page.evaluated[0][1]
        )

    async def test_returns_none_when_the_page_cannot_be_read(self):
        class UnreadablePage:
            async def evaluate(self, *_args):
                raise RuntimeError("page closed")

        self.assertIsNone(await read_message_baseline(UnreadablePage()))


class ConfirmMessageSentTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_last_message_line_without_baseline(self):
        page = FakeDeliveryPage()
        chat_input = FakeChatInput()

        await confirm_message_sent(
            page,
            chat_input,
            "first line\nunique verification line",
            timeout=1200,
        )

        self.assertEqual([], page.wait_function_calls)
        self.assertEqual(("unique verification line", False), page.requested_text)
        self.assertEqual([("visible", 1200)], page.message_locator.wait_calls)

    async def test_waits_for_a_new_bubble_with_visible_text_when_baseline_is_known(self):
        page = FakeDeliveryPage(
            bubble_states=[["刚刚\n今日火花", "oldest message"]]
        )
        chat_input = FakeChatInput()

        await confirm_message_sent(
            page,
            chat_input,
            "[盖瑞]今日火花[加一]",
            timeout=1200,
            baseline=("old message", "oldest message"),
        )

        self.assertEqual([], page.wait_function_calls)
        self.assertEqual(1, len(page.evaluated))
        self.assertEqual(".messageMessageBoxmessageBox", page.evaluated[0][1])
        self.assertIsNone(page.requested_text)

    async def test_keeps_polling_until_a_new_bubble_appears(self):
        page = FakeDeliveryPage(
            bubble_states=[
                ["old message", "oldest message"],
                ["刚刚\n今日火花", "oldest message"],
            ]
        )
        chat_input = FakeChatInput()

        await confirm_message_sent(
            page,
            chat_input,
            "[盖瑞]今日火花[加一]",
            timeout=1200,
            baseline=("old message", "oldest message"),
        )

        self.assertEqual(2, len(page.evaluated))

    async def test_times_out_when_the_sent_message_never_appears(self):
        page = FakeDeliveryPage()
        chat_input = FakeChatInput()

        with self.assertRaises(TimeoutError):
            await confirm_message_sent(
                page,
                chat_input,
                "[盖瑞]今日火花[加一]",
                timeout=600,
                baseline=("old message", "oldest message"),
            )


if __name__ == "__main__":
    unittest.main()
