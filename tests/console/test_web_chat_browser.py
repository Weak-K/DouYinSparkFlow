import unittest
from pathlib import Path
from unittest.mock import patch

import core.web_chat as chat


HTML = """
<style>
.conversationConversationListwrapper { height: 80px; overflow: auto; }
.conversationConversationItemwrapper { height: 50px; }
</style>
<div class="conversationConversationListwrapper"></div>
<script>
const list = document.querySelector('.conversationConversationListwrapper');
window.selected = null;
window.render = names => {
  list.replaceChildren(...names.map(name => {
    const row = document.createElement('div');
    row.className = 'conversationConversationItemwrapper';
    const title = document.createElement('span');
    title.className = 'conversationConversationItemtitle';
    title.textContent = name;
    row.append(title);
    row.onclick = () => { window.selected = name; };
    return row;
  }));
};
render(['Other', 'Target', 'Last']);
</script>
"""


class WebChatBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        if not Path(self.playwright.chromium.executable_path).exists():
            self.skipTest("Playwright Chromium binary is not installed")
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        await self.page.set_content(HTML)

    async def test_reorders_between_read_and_click_without_selecting_someone_else(self):
        original = chat._conversation_rows

        async def reordered(page, **kwargs):
            rows = await original(page, **kwargs)
            await page.evaluate("render(['Last', 'Other', 'Target'])")
            return rows

        with patch.object(chat, "_conversation_rows", side_effect=reordered):
            await chat.select_web_chat_target(self.page, "Target", timeout=2000)
        self.assertEqual("Target", await self.page.evaluate("window.selected"))

    async def test_scroll_snapshot_cannot_replay_recycled_row_positions(self):
        await self.page.evaluate("""() => {
          list.addEventListener('scroll', () => {
            if (list.scrollTop > 0 && !window.recycled) {
              window.recycled = true;
              render(['Older', 'Unrelated', 'Oldest']);
            } else if (list.scrollTop === 0 && window.recycled) {
              window.recycled = false;
              render(['Other', 'Target', 'Last']);
            }
          });
        }""")
        discovered = []
        await chat.select_web_chat_target(self.page, "Target", timeout=8000,
                                          scroll=True, discovered=discovered)
        self.assertEqual("Target", await self.page.evaluate("window.selected"))
        self.assertIn("Older", discovered)
        self.assertIn("Target", discovered)

    async def test_search_response_later_than_three_seconds_is_still_accepted(self):
        await self.page.set_content("<div id='results'></div>")
        await self.page.evaluate("""() => {
          setTimeout(() => {
            document.querySelector('#results').textContent = 'Delayed target';
          }, 3300);
        }""")
        results = await chat._wait_for_visible_search_results(
            self.page, "Delayed target", 4500
        )
        self.assertEqual(1, len(results))

    async def test_total_search_budget_bounds_all_aliases(self):
        await self.page.set_content('<input placeholder="search">')
        with patch.object(chat, "SEARCH_INPUT_SELECTORS", ('input[placeholder="search"]',)):
            with self.assertRaises(TimeoutError):
                await chat.select_web_chat_target(self.page, "Absent", timeout=200,
                                                  aliases=("Alias1", "Alias2"))
        self.assertEqual(0, await self.page.locator(chat.CHAT_EDITOR_SELECTOR).count())

    async def test_search_is_cleared_before_returning_to_conversations(self):
        await self.page.set_content('<input placeholder="search">')
        await self.page.evaluate("""() => {
          document.querySelector('input').addEventListener('input', event => {
            if (!event.target.value) {
              const row = document.createElement('div');
              row.className = 'conversationConversationItemwrapper';
              row.innerHTML = '<span class="conversationConversationItemtitle">Other</span>';
              document.body.append(row);
            }
          });
        }""")

        async def no_results(_page, field, candidate, _timeout):
            await field.fill(candidate)
            return []

        with patch.object(chat, "SEARCH_INPUT_SELECTORS", ('input[placeholder="search"]',)), \
                patch.object(chat, "_search_in_panel", side_effect=no_results):
            with self.assertRaises(chat.TargetNotFoundError):
                await chat.select_web_chat_target(self.page, "Absent", timeout=1000)
        self.assertEqual("", await self.page.locator("input").input_value())


if __name__ == "__main__":
    unittest.main()
