import base64
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import browser_auth
import sys
try:
    import main as spoofer
    sys.modules["spoofer"] = spoofer
except ImportError:
    import spoofer
from playwright.sync_api import Error


def account_token(user_id: str) -> str:
    prefix = base64.b64encode(user_id.encode()).decode().rstrip("=")
    return f"{prefix}.test.signature"


class BrowserLoginTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        self.token = account_token("123456789012345678")
        self.listener = None
        self.context = MagicMock()
        self.page = MagicMock()
        self.context.pages = [self.page]
        self.context.new_page.return_value = self.page
        self.page.on.side_effect = self.listen
        self.context.cookies.return_value = [{"name": "session", "value": "test-cookie"}]
        self.page.url = browser_auth.LOGIN_URL
        self.page.is_closed.return_value = False
        self.page.goto.side_effect = lambda *args, **kwargs: self.request(self.token)
        self.page.wait_for_timeout.side_effect = self.advance
        self.page.evaluate.side_effect = self.evaluate
        self.result = {
            "status": 200,
            "data": {"id": "123456789012345678", "analytics_token": "test-analytics"},
        }
        self.playwright = MagicMock()
        self.browser = MagicMock()
        self.browser.contexts = [self.context]
        self.playwright.chromium.connect_over_cdp.return_value = self.browser
        self.manager = MagicMock()
        self.manager.__enter__.return_value = self.playwright
        self.addCleanup(patch.stopall)
        patch("playwright.sync_api.sync_playwright", return_value=self.manager).start()
        patch("browser_auth.time.monotonic", side_effect=lambda: self.clock).start()

    def listen(self, event, callback):
        self.listener = callback

    def request(self, token, url=browser_auth.ME_URL):
        self.listener(SimpleNamespace(url=url, all_headers=lambda: {"authorization": token}))

    def advance(self, milliseconds):
        self.clock += milliseconds / 1000

    def evaluate(self, expression, *args):
        return "Test Browser" if expression == "navigator.userAgent" else self.result

    def capture(self):
        return browser_auth.capture_auth(timeout=1)

    def test_captures_verified_session_without_closing_chrome(self):
        auth = self.capture()
        self.assertEqual(auth.token, self.token)
        self.assertEqual(auth.cookie, "session=test-cookie")
        self.assertEqual(auth.analytics_token, "test-analytics")
        self.assertEqual(auth.user_agent, "Test Browser")
        self.context.cookies.assert_called_once_with([browser_auth.SCIENCE_URL])
        self.playwright.chromium.connect_over_cdp.assert_called_once_with(
            "chrome", no_defaults=True, timeout=45000,
        )
        self.context.close.assert_not_called()
        self.context.on.assert_not_called()
        self.context.new_page.assert_called_once()
        self.page.close.assert_called_once()
        self.browser.close.assert_called_once()
        self.playwright.chromium.launch_persistent_context.assert_not_called()
        self.assertNotIn(self.token, repr(auth))
        self.assertNotIn("test-cookie", repr(auth))
        self.assertNotIn("test-analytics", repr(auth))

    def test_rejects_expired_token_without_saving(self):
        self.result = {"status": 401, "data": None}
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self.capture()
        self.context.cookies.assert_not_called()
        self.context.close.assert_not_called()
        self.page.close.assert_called_once()
        self.browser.close.assert_called_once()

    def test_external_and_bot_tokens_are_ignored(self):
        for url, token in (
            ("https://discord.com.evil.example/api/v9/users/@me", self.token),
            ("https://evil.example/api/v9/users/@me", self.token),
            (browser_auth.ME_URL, "Bot test"),
            (browser_auth.ME_URL, "Bearer test"),
        ):
            with self.subTest(url=url, token=token):
                self.clock = 0
                self.page.evaluate.reset_mock()
                self.page.goto.side_effect = lambda *a, **kw: self.request(token, url)
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    self.capture()
                self.page.evaluate.assert_not_called()

    def test_closed_window_cancels_login(self):
        self.page.is_closed.return_value = True
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.capture()
        self.context.close.assert_not_called()
        self.page.close.assert_not_called()
        self.browser.close.assert_called_once()

    def test_missing_cookies_does_not_return_partial_auth(self):
        self.context.cookies.return_value = []
        with self.assertRaisesRegex(RuntimeError, "did not provide cookies"):
            self.capture()
        self.context.close.assert_not_called()
        self.page.close.assert_called_once()
        self.browser.close.assert_called_once()

    def test_connection_failure_explains_required_chrome_setting(self):
        self.playwright.chromium.connect_over_cdp.side_effect = Error("not enabled")
        with self.assertRaisesRegex(RuntimeError, "chrome://inspect/#remote-debugging"):
            self.capture()
        self.playwright.chromium.launch_persistent_context.assert_not_called()
        self.browser.close.assert_not_called()

    def test_no_browser_context_disconnects_cleanly(self):
        self.browser.contexts = []
        with self.assertRaisesRegex(RuntimeError, "no available profile"):
            self.capture()
        self.context.close.assert_not_called()
        self.browser.close.assert_called_once()

    def test_capture_preserves_all_existing_tabs(self):
        existing = MagicMock()
        self.context.pages = [existing]
        self.capture()
        existing.close.assert_not_called()
        existing.goto.assert_not_called()
        existing.reload.assert_not_called()


class AppAuthTests(unittest.TestCase):
    def setUp(self):
        self.app = spoofer.App.__new__(spoofer.App)
        self.app.console = MagicMock()
        self.app.state = spoofer.State(
            token=account_token("123456789012345678"), cookie="old-cookie",
            analytics_token="old-analytics", fingerprint="old-fingerprint",
            used_games=["1"], total_games_claimed=1, total_hours_claimed=2,
        )
        self.app.games = [spoofer.Game("1", "Test Game", "game.exe")]
        self.auth = browser_auth.BrowserAuth(
            token=account_token("123456789012345678"), cookie="new-cookie",
            analytics_token="new-analytics", user_agent="Test Browser",
            account_id="123456789012345678",
        )

    def test_refresh_preserves_same_accounts_fingerprint(self):
        with patch("spoofer.capture_auth", return_value=self.auth), patch.object(spoofer.State, "save"):
            self.assertTrue(self.app._browser_auth())
        self.assertEqual(self.app.state.fingerprint, "old-fingerprint")
        self.assertEqual(self.app.state.cookie, "new-cookie")
        self.assertEqual(self.app.state.analytics_token, "new-analytics")
        self.assertEqual(self.app.state.user_agent, "Test Browser")
        self.assertFalse(self.app.state.is_stale)

    def test_switching_accounts_clears_account_bound_state(self):
        self.app.state.token = account_token("999999999999999999")
        with patch("spoofer.capture_auth", return_value=self.auth), patch.object(spoofer.State, "save"):
            self.assertTrue(self.app._browser_auth())
        self.assertEqual(self.app.state.fingerprint, "")
        self.assertEqual(self.app.state.used_games, [])
        self.assertEqual(self.app.state.total_games_claimed, 0)
        self.assertEqual(self.app.state.total_hours_claimed, 0)

    def test_failed_login_preserves_saved_credentials(self):
        before = vars(self.app.state).copy()
        with patch("spoofer.capture_auth", side_effect=RuntimeError("cancelled")), \
                patch.object(spoofer.State, "save") as save:
            self.assertFalse(self.app._browser_auth())
        self.assertEqual(vars(self.app.state), before)
        save.assert_not_called()

    def test_old_state_files_keep_default_browser_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps({"token": "test", "cookie": "cookie"}), encoding="utf-8")
            with patch("spoofer.STATE_FILE", path):
                state = spoofer.State.load()
        self.assertEqual(state.user_agent, spoofer.USER_AGENT)
        self.assertEqual(state.used_games, [])

    def test_refresh_passes_cookie_and_browser_identity(self):
        self.app.state.fetched_at = 0
        with patch("spoofer._read_analytics_token", return_value="fresh") as fetch, \
                patch.object(spoofer.State, "save"):
            spoofer.ensure_token(self.app.state, "props")
        fetch.assert_called_once_with(
            self.app.state.token, "props", self.app.state.cookie, self.app.state.user_agent,
        )

    def test_startup_can_skip_fingerprint_without_scanning(self):
        self.app.state = spoofer.State(subscribed=True, joined_discord=True)
        self.app.menu = MagicMock()
        with patch("spoofer.capture_auth", return_value=self.auth), \
                patch("spoofer.load_games", return_value=self.app.games), \
                patch("spoofer.Session.new", return_value=spoofer.Session("hb", "sig", "props")), \
                patch("spoofer.generate_fingerprint") as generate, \
                patch("spoofer.time.sleep"), patch.object(spoofer.State, "save"), \
                patch("builtins.input", return_value="") as prompt, redirect_stdout(io.StringIO()):
            self.app.start()
        self.app.menu.assert_called_once()
        self.assertEqual(prompt.call_count, 2)
        generate.assert_not_called()
        self.assertEqual(self.app.state.token, self.auth.token)
        self.assertEqual(self.app.state.fingerprint, "")

    def test_startup_recovers_from_expired_saved_auth(self):
        self.app.state.subscribed = True
        self.app.state.joined_discord = True
        self.app.menu = MagicMock()
        with patch("spoofer.capture_auth", return_value=self.auth), \
                patch("spoofer.load_games", return_value=self.app.games), \
                patch("spoofer.Session.new", return_value=spoofer.Session("hb", "sig", "props")), \
                patch("spoofer._read_analytics_token", side_effect=RuntimeError("expired")), \
                patch("spoofer.time.sleep"), patch.object(spoofer.State, "save"), \
                patch("builtins.input", return_value="1"):
            self.app.start()
        self.app.menu.assert_called_once()
        self.assertEqual(self.app.state.analytics_token, self.auth.analytics_token)

    def test_skip_disables_saved_fingerprint_and_omits_it_from_events(self):
        with patch("builtins.input", return_value="0"), \
                patch.object(spoofer.State, "save") as save:
            self.app.set_fingerprint()
        self.assertEqual(self.app.state.fingerprint, "")
        save.assert_called_once()
        client = spoofer.ScienceClient(self.app.state, spoofer.Session("hb", "sig", "props"))
        self.assertNotIn("executable_fingerprint", client.build_launch(self.app.games[0])["properties"])

    def test_direct_exe(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "game.exe"
            exe.write_bytes(b"test fixture")
            with patch("builtins.input", side_effect=["1", f'"{exe}"']), \
                    patch("spoofer.generate_fingerprint", return_value="direct-fingerprint") as generate, \
                    patch("spoofer.time.sleep"), patch.object(spoofer.State, "save"):
                self.app.set_fingerprint()
            generate.assert_called_once_with("123456789012345678", path=str(exe), pid=0)
        self.assertEqual(self.app.state.fingerprint, "direct-fingerprint")

    def test_direct_pid(self):
        with patch("builtins.input", side_effect=["1", "4321"]), \
                    patch("spoofer.generate_fingerprint", return_value="pid-fingerprint") as generate, \
                    patch("spoofer.time.sleep"), patch.object(spoofer.State, "save"):
            self.app.set_fingerprint()
        generate.assert_called_once_with("123456789012345678", path="", pid=4321)
        self.assertEqual(self.app.state.fingerprint, "pid-fingerprint")

    def test_auth_refresh_does_not_prompt_for_fingerprint(self):
        self.app.state.fingerprint = ""
        with patch("spoofer.capture_auth", return_value=self.auth), \
                patch.object(self.app, "_new_session"), \
                patch("builtins.input", return_value="1"), \
                patch("spoofer.time.sleep"), patch.object(spoofer.State, "save"):
            self.app._refresh()
        self.assertEqual(self.app.state.fingerprint, "")

    def test_choose_auth_manual_prompts_for_token_and_cookie(self):
        with patch("builtins.input", side_effect=["2", "manual.token.123", "manual-cookie"]), \
                patch("spoofer.capture_auth") as capture, \
                patch("spoofer.time.sleep"), patch.object(spoofer.State, "save"):
            self.app._choose_auth()
        capture.assert_not_called()
        self.assertEqual(self.app.state.token, "manual.token.123")
        self.assertEqual(self.app.state.cookie, "manual-cookie")

    def test_refresh_manual_prompts_for_token_and_cookie(self):
        self.app.session = spoofer.Session("hb", "sig", "props")
        with patch("builtins.input", side_effect=["2", "manual.token.456", "manual-cookie-2"]), \
                patch("spoofer.capture_auth") as capture, \
                patch("spoofer.ensure_token") as ensure, \
                patch.object(self.app, "_new_session", side_effect=lambda: setattr(self.app, "session", spoofer.Session("hb", "sig", "props"))), \
                patch("spoofer.time.sleep"), patch.object(spoofer.State, "save"):
            self.app._refresh()
        capture.assert_not_called()
        ensure.assert_called_once()
        self.assertEqual(self.app.state.token, "manual.token.456")
        self.assertEqual(self.app.state.cookie, "manual-cookie-2")

    def test_debug_output_redacts_credentials(self):
        client = spoofer.ScienceClient(self.app.state, spoofer.Session("hb", "sig", "props"))
        response = MagicMock()
        response.__enter__.return_value.status = 204
        output = io.StringIO()
        with patch("spoofer.DEBUG", True), patch("spoofer.urllib.request.urlopen", return_value=response), \
                redirect_stdout(output):
            self.assertEqual(client.post([]), 204)
        for secret in (self.app.state.token, self.app.state.cookie, self.app.state.analytics_token):
            self.assertNotIn(secret, output.getvalue())


if __name__ == "__main__":
    unittest.main()
