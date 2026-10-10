"""Operator-owned, memory-only browser sessions on the importing server.

No passwords, cookie exports, persistent browser profiles, or login MCP tools.
The operator completes institutional login in the browser through SSH/DevTools.
"""
from __future__ import annotations

import os
import socket
from contextlib import contextmanager

from .network import publisher_proxy


SESSION_MARKER = "about:blank#paperbridge-session"


def browser_environment():
    """Do not pass the host's credential environment to web browser processes."""
    return {key: value for key, value in os.environ.items()
            if key in {"PATH", "HOME", "TMPDIR", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
                       "XDG_RUNTIME_DIR", "LANG", "LC_ALL"}}


class AttachedSession:
    """Preserve the real incognito context when attaching through Chromium CDP.

    Playwright's CDP attachment can group existing incognito targets under its
    default context. Create targets using Chromium's actual browserContextId;
    otherwise a new page or a cookie lookup may silently use an empty session.
    """

    def __init__(self, context, marker, cdp, context_id):
        self.context = context
        self.marker = marker
        self.cdp = cdp
        self.context_id = context_id

    def new_page(self):
        with self.context.expect_page(timeout=5000) as created:
            self.cdp.send("Target.createTarget", {"url": "about:blank", "browserContextId": self.context_id})
        return created.value

    def cookies(self, urls):
        session = self.context.new_cdp_session(self.marker)
        try:
            return session.send("Network.getCookies", {"urls": urls})["cookies"]
        finally:
            session.detach()


class RetrievalUnavailable(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def browser_port(value: str | int) -> int:
    raw = str(value)
    if not raw.isascii() or not raw.isdecimal() or not 1024 <= int(raw) <= 65535:
        raise ValueError("Browser port must be an integer between 1024 and 65535.")
    return int(raw)


@contextmanager
def connected_session(port: int):
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as playwright:
        browser = None
        try:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{browser_port(port)}", timeout=5000)
            contexts = [context for context in browser.contexts
                        if any(page.url == SESSION_MARKER for page in context.pages)]
            if len(contexts) != 1:
                raise RetrievalUnavailable("browser_session_required")
            context = contexts[0]
            marker = next(page for page in context.pages if page.url == SESSION_MARKER)
            cdp = browser.new_browser_cdp_session()
            targets = [target for target in cdp.send("Target.getTargets")["targetInfos"]
                       if target["url"] == SESSION_MARKER]
            if len(targets) != 1 or not targets[0].get("browserContextId"):
                raise RetrievalUnavailable("browser_session_required")
            yield AttachedSession(context, marker, cdp, targets[0]["browserContextId"])
        except Error:
            # Browser errors can contain private URLs, headers, and page contents.
            raise RetrievalUnavailable("browser_interaction_required") from None
        finally:
            if browser:
                # For a CDP attachment this disconnects, leaving the owner running.
                browser.close()


def run_session(login_url: str, *, port: int = 9222, headed: bool = False):
    """Keep an incognito context alive; all authentication expires on shutdown."""
    from playwright.sync_api import Error, sync_playwright

    port = browser_port(port)
    proxy = publisher_proxy()
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        raise RetrievalUnavailable("browser_port_unavailable") from None
    # Do not inherit the MCP service's credential environment into Chromium.
    environment = browser_environment()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=not headed, chromium_sandbox=True, env=environment,
                proxy={"server": proxy} if proxy else None,
                args=[f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"],
            )
            context = browser.new_context(accept_downloads=False, service_workers="block")
            marker = context.new_page()
            marker.goto(SESSION_MARKER)
            page = context.new_page()
            print(f"Browser session started on 127.0.0.1:{port}. Keep this process running.", flush=True)
            print("Use an SSH tunnel and Chrome's chrome://inspect to complete login yourself. "
                  "Do not enter credentials in a chat or monitored terminal. "
                  "Closing this process clears the session.", flush=True)
            try:
                page.goto(login_url, timeout=30000, wait_until="domcontentloaded")
            except Error:
                print("Open the institutional login link in the browser if navigation did not finish.", flush=True)
            try:
                while browser.is_connected():
                    marker.wait_for_timeout(1000)
            except KeyboardInterrupt:
                pass
            finally:
                browser.close()
    except Error:
        raise RetrievalUnavailable("browser_session_start_failed") from None
