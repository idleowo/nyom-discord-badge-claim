from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit


LOGIN_URL = "https://discord.com/channels/@me"
ME_URL = "https://discord.com/api/v9/users/@me?with_analytics_token=true"
SCIENCE_URL = "https://discord.com/api/v9/science"


@dataclass(frozen=True)
class BrowserAuth:
    token: str = field(repr=False)
    cookie: str = field(repr=False)
    analytics_token: str = field(repr=False)
    user_agent: str
    account_id: str


def is_discord_api(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "discord.com"
        and parsed.path.startswith("/api/")
    )


def capture_auth(timeout: float = 300) -> BrowserAuth:
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ImportError as exc:
        raise RuntimeError("Install browser login with: python -m pip install -r requirements.txt") from exc

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(
                "chrome", no_defaults=True, timeout=45000,
            )
        except Error as exc:
            raise RuntimeError(
                "Could not connect to your open Chrome. In Chrome 144+, open "
                "chrome://inspect/#remote-debugging and enable 'Allow remote debugging "
                "for this browser instance', then retry and click Allow when Chrome asks."
            ) from exc

        captured: dict[str, str] = {}
        page = None

        def collect(request) -> None:
            if not is_discord_api(request.url):
                return
            try:
                headers = request.all_headers()
            except Error:
                return
            token = headers.get("authorization", "").strip()
            if token and not token.lower().startswith(("bot ", "bearer ")):
                captured["token"] = token

        try:
            if not browser.contexts:
                raise RuntimeError("Chrome has no available profile; open a normal Chrome window and retry")
            context = browser.contexts[0]
            page = context.new_page()
            page.on("request", collect)
            page.set_default_timeout(15000)
            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            except TimeoutError:
                pass

            deadline = time.monotonic() + timeout
            rejected = ""
            retry_at = 0.0
            while time.monotonic() < deadline:
                if page.is_closed():
                    raise RuntimeError("Login browser closed before authentication finished")
                token = captured.get("token", "")
                if (
                    token and token != rejected and time.monotonic() >= retry_at
                    and urlsplit(page.url).netloc == "discord.com"
                ):
                    try:
                        result = page.evaluate(
                            """async ({url, token}) => {
                                const response = await fetch(url, {
                                    headers: {authorization: token},
                                    credentials: "include",
                                    signal: AbortSignal.timeout(15000)
                                });
                                return {
                                    status: response.status,
                                    data: response.ok ? await response.json() : null
                                };
                            }""",
                            {"url": ME_URL, "token": token},
                        )
                    except Error:
                        retry_at = time.monotonic() + 2
                        page.wait_for_timeout(500)
                        continue
                    data = result.get("data") or {}
                    if result["status"] == 401:
                        rejected = token
                    elif result["status"] == 200 and data.get("id") and data.get("analytics_token"):
                        cookies = context.cookies([SCIENCE_URL])
                        cookie = "; ".join(f"{item['name']}={item['value']}" for item in cookies)
                        if not cookie:
                            raise RuntimeError("Discord did not provide cookies; complete login and retry")
                        return BrowserAuth(
                            token=token,
                            cookie=cookie,
                            analytics_token=data["analytics_token"],
                            user_agent=page.evaluate("navigator.userAgent"),
                            account_id=str(data["id"]),
                        )
                    retry_at = time.monotonic() + 2
                page.wait_for_timeout(500)
            raise RuntimeError("Login timed out; finish signing in within five minutes and retry")
        except Error as exc:
            raise RuntimeError("Browser login interrupted; reopen it with refresh auth") from exc
        finally:
            try:
                if page is not None and not page.is_closed():
                    page.close()
            except Error:
                pass
            try:
                browser.close()
            except Error:
                pass
