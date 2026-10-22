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
        await self.page.set_content(
            '<div class="SearchPanelitembox"><div class="SearchPanelitemtitle" id="results"></div></div>'
        )
        await self.page.evaluate("""() => {
          setTimeout(() => {
            document.querySelector('#results').textContent = 'Delayed target';
          }, 3300);
        }""")
        results = await chat._wait_for_visible_search_results(
            self.page, "Delayed target", 4500
        )
        self.assertEqual(1, len(results))

    async def test_search_skips_hidden_duplicate_input_and_uses_visible_one(self):
        await self.page.set_content('''
          <input placeholder="搜索" style="display:none">
          <input placeholder="搜索">
          <div class="SearchPanelitembox">
            <div class="SearchPanelitemtitle">Target</div>
          </div>
          <script>window.selected = false;</script>
        ''')
        await self.page.locator(chat.SEARCH_RESULT_SELECTOR).evaluate(
            "el => el.onclick = () => { window.selected = true; }"
        )

        await chat.select_web_chat_target(self.page, "Target", timeout=2000)

        self.assertTrue(await self.page.evaluate("window.selected"))

    async def test_search_waits_for_chat_hydration_to_render_input(self):
        await self.page.set_content('''
          <div id="app"></div>
          <script>
            window.selected = false;
            setTimeout(() => {
              const input = document.createElement('input');
              input.placeholder = '搜索';
              input.addEventListener('input', () => {
                document.querySelector('#app').innerHTML =
                  '<div class="SearchPanelitembox" onclick="window.selected=true">' +
                  '<div class="SearchPanelitemtitle">Target</div></div>';
              });
              document.querySelector('#app').append(input);
            }, 250);
          </script>
        ''')

        await chat.select_web_chat_target(self.page, "Target", timeout=2000)

        self.assertTrue(await self.page.evaluate("window.selected"))

    async def test_search_waits_more_than_three_seconds_for_input(self):
        await self.page.set_content('''
          <div id="app"></div>
          <script>
            window.selected = false;
            setTimeout(() => {
              document.querySelector('#app').innerHTML =
                '<div class="conversationConversationItemwrapper">' +
                '<span class="conversationConversationItemtitle">Other</span></div>' +
                '<input placeholder="搜索">' +
                '<div class="SearchPanelitembox" onclick="window.selected=true">' +
                '<div class="SearchPanelitemtitle">Target</div></div>';
            }, 3200);
          </script>
        ''')

        await chat.select_web_chat_target(self.page, "Target", timeout=8000)

        self.assertEqual(1, await self.page.locator('input[placeholder="搜索"]').count())
        self.assertTrue(await self.page.evaluate("window.selected"))

    async def test_search_does_not_start_before_conversation_hydration(self):
        await self.page.set_content('''
          <div id="app"></div>
          <script>
            window.selected = false;
            setTimeout(() => {
              document.querySelector('#app').innerHTML =
                '<div class="conversationConversationItemwrapper">' +
                '<span class="conversationConversationItemtitle">Other</span></div>' +
                '<input placeholder="搜索">';
            }, 250);
          </script>
        ''')

        with patch.object(chat, "_search_in_panel", return_value=[]):
            with self.assertRaises(chat.TargetNotFoundError):
                await chat.select_web_chat_target(self.page, "Absent", timeout=2000)

        self.assertEqual(1, await self.page.locator(chat.CONVERSATION_ITEM_SELECTOR).count())

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

        async def no_results(_page, field, candidate, _timeout, **_kwargs):
            await field.fill(candidate)
            return []

        with patch.object(chat, "SEARCH_INPUT_SELECTORS", ('input[placeholder="search"]',)), \
                patch.object(chat, "_search_in_panel", side_effect=no_results):
            with self.assertRaises(chat.TargetNotFoundError):
                await chat.select_web_chat_target(self.page, "Absent", timeout=1000)
        self.assertEqual("", await self.page.locator("input").input_value())

    async def search_fixture(self, items):
        await self.page.set_content('''
          <input placeholder="搜索"><div id="results"></div>
          <script>window.selected = null;</script>
        ''')
        for title_html, chosen in items:
            await self.page.locator("#results").evaluate("""(el, item) => {
              const row = document.createElement('div');
              row.className = 'SearchPanelitembox';
              row.innerHTML = '<div class="SearchPanelitemtitle">' + item[0] + '</div>';
              row.onclick = () => { window.selected = item[1]; };
              el.append(row);
            }""", [title_html, chosen])

    async def test_ignores_highlight_substring_and_clicks_only_full_title(self):
        await self.search_fixture([
            ('<span class="SearchPanelitemsearch_highlight">Target</span>-Other', 'wrong'),
            ('<span class="SearchPanelitemsearch_highlight">Target</span>', 'right'),
        ])
        self.assertEqual("Target", await chat.select_web_chat_target(self.page, "Target"))
        self.assertEqual("right", await self.page.evaluate("window.selected"))

    async def test_partial_name_or_unrelated_page_text_never_counts_as_result(self):
        await self.search_fixture([
            ('<span class="SearchPanelitemsearch_highlight">Target</span>-Other', 'wrong'),
        ])
        await self.page.evaluate("""() => {
          document.body.insertAdjacentHTML('beforeend',
            '<div class="RightPanelHeadertitle">Target</div>' +
            '<div class="SearchPanelitemtitle">Target</div>' +
            '<div class="messageMessageBoxmessageBox">Target</div>');
        }""")
        self.assertEqual([], await chat._wait_for_visible_search_results(self.page, "Target", 50))
        with self.assertRaises(TimeoutError):
            await chat.select_web_chat_target(self.page, "Target", timeout=250)
        self.assertIsNone(await self.page.evaluate("window.selected"))

    async def test_hidden_duplicate_does_not_block_unique_visible_result(self):
        await self.search_fixture([('Target', 'hidden'), ('Target', 'right')])
        await self.page.locator(chat.SEARCH_RESULT_SELECTOR).first.evaluate(
            "el => { el.style.display = 'none'; }"
        )
        await chat.select_web_chat_target(self.page, "Target")
        self.assertEqual("right", await self.page.evaluate("window.selected"))

    async def test_duplicate_full_titles_abort_without_clicking(self):
        await self.search_fixture([('Target', 'first'), ('Target', 'second')])
        with self.assertRaises(chat.AmbiguousTargetError):
            await chat.select_web_chat_target(self.page, "Target")
        self.assertIsNone(await self.page.evaluate("window.selected"))

    async def test_multiple_alias_matches_abort_without_clicking(self):
        await self.search_fixture([('Old name', 'first'), ('Current name', 'second')])
        with self.assertRaises(chat.AmbiguousTargetError):
            await chat.select_web_chat_target(self.page, "Old name", aliases=("Current name",))
        self.assertIsNone(await self.page.evaluate("window.selected"))

    async def test_result_reorder_cannot_change_clicked_recipient(self):
        await self.search_fixture([('Target', 'right'), ('Other', 'wrong')])
        result = (await chat._wait_for_visible_search_results(self.page, "Target", 50))[0]
        await self.page.locator("#results").evaluate("el => el.prepend(el.lastElementChild)")
        self.assertTrue(await chat._click_search_result(self.page, result, ("Target",)))
        self.assertEqual("right", await self.page.evaluate("window.selected"))

    async def test_renamed_result_is_not_clicked_using_its_old_position(self):
        await self.search_fixture([('Target', 'wrong')])
        result = (await chat._wait_for_visible_search_results(self.page, "Target", 50))[0]
        await self.page.locator(chat.SEARCH_RESULT_TITLE_SELECTOR).evaluate(
            "el => { el.textContent = 'Other'; }"
        )
        self.assertFalse(await chat._click_search_result(self.page, result, ("Target",)))
        self.assertIsNone(await self.page.evaluate("window.selected"))

    async def test_new_duplicate_before_click_is_rejected(self):
        await self.search_fixture([('Target', 'wrong')])
        result = (await chat._wait_for_visible_search_results(self.page, "Target", 50))[0]
        await self.page.locator("#results").evaluate("el => el.append(el.firstChild.cloneNode(true))")
        with self.assertRaises(chat.AmbiguousTargetError):
            await chat._click_search_result(self.page, result, ("Target",))
        self.assertIsNone(await self.page.evaluate("window.selected"))

    async def test_send_message_button_is_scoped_to_matched_item(self):
        await self.search_fixture([('Target', 'right'), ('Other', 'wrong')])
        await self.page.locator(chat.SEARCH_RESULT_SELECTOR).last.evaluate("""el => {
          el.insertAdjacentHTML('beforeend', '<button>发消息</button>');
        }""")
        await chat.select_web_chat_target(self.page, "Target")
        self.assertEqual("right", await self.page.evaluate("window.selected"))

    async def test_nested_send_message_button_in_matched_item_is_clicked(self):
        await self.search_fixture([('Target', 'wrong-row')])
        await self.page.locator(chat.SEARCH_RESULT_SELECTOR).evaluate("""el => {
          el.insertAdjacentHTML('beforeend', '<button><span>发消息</span></button>');
          el.querySelector('button').onclick = event => {
            event.stopPropagation(); window.selected = 'right-button';
          };
        }""")
        await chat.select_web_chat_target(self.page, "Target")
        self.assertEqual("right-button", await self.page.evaluate("window.selected"))

    async def test_known_full_nickname_is_accepted_for_identifier_search(self):
        await self.search_fixture([('Current name', 'right')])
        await chat.select_web_chat_target(
            self.page, "Old name", aliases=("stable-handle", "Current name")
        )
        self.assertEqual("stable-handle", await self.page.locator("input").input_value())
        self.assertEqual("right", await self.page.evaluate("window.selected"))

    async def test_search_normalizes_whitespace_but_not_case(self):
        await self.search_fixture([('target', 'wrong'), ('Target&nbsp;Name', 'right')])
        self.assertEqual([], await chat._wait_for_visible_search_results(self.page, "Target", 50))
        await chat.select_web_chat_target(self.page, "Target Name")
        self.assertEqual("right", await self.page.evaluate("window.selected"))

    async def test_header_switch_after_two_seconds_is_waited_for(self):
        await self.page.set_content('<div class="RightPanelHeadertitle">Previous</div>')
        await self.page.evaluate("""() => {
          setTimeout(() => {
            document.querySelector('.RightPanelHeadertitle').textContent = 'Target';
          }, 2300);
        }""")
        self.assertEqual("Target", await chat.wait_for_opened_conversation(self.page, ("Target",)))

    async def test_hidden_or_ambiguous_headers_are_not_accepted(self):
        await self.page.set_content('''
          <div class="RightPanelHeadertitle" style="display:none">Target</div>
          <div class="RightPanelHeadertitle">Other</div>
        ''')
        self.assertEqual("Other", await chat.opened_conversation_title(self.page))
        await self.page.locator(chat.CONVERSATION_HEADER_SELECTOR).first.evaluate(
            "el => { el.style.display = 'block'; }"
        )
        self.assertEqual("", await chat.opened_conversation_title(self.page))


if __name__ == "__main__":
    unittest.main()
