"""One memory-only browser session owned by the importing PB process."""
from __future__ import annotations

import atexit
import os
from pathlib import Path
from concurrent.futures import Future, TimeoutError
from queue import Empty, Queue
from threading import Lock, Thread

from .browser import RetrievalUnavailable, browser_environment
from .network import publisher_proxy


def browser_idle_seconds() -> int:
    """Read the operator's idle lifetime, without allowing an unbounded session."""
    try:
        seconds = int(os.environ.get("PAPERBRIDGE_BROWSER_IDLE_SECONDS", "90"))
    except ValueError:
        raise ValueError("PAPERBRIDGE_BROWSER_IDLE_SECONDS must be an integer from 1 to 86400.") from None
    if not 1 <= seconds <= 86400:
        raise ValueError("PAPERBRIDGE_BROWSER_IDLE_SECONDS must be an integer from 1 to 86400.")
    return seconds


class _DiscardBrowserSession(RetrievalUnavailable):
    """Return this rejection only after the managed owner discards its browser."""


def browser_headless() -> bool:
    """Read the operator's explicit browser display mode."""
    value = os.environ.get("PAPERBRIDGE_BROWSER_HEADLESS", "1")
    if value not in ("0", "1"):
        raise ValueError("PAPERBRIDGE_BROWSER_HEADLESS must be 0 or 1.")
    return value == "1"


class ManagedBrowser:
    """Serialize work on one owning thread and inherit its configured engine.

    A provider can explicitly override Firefox or sandboxed Chromium. Engine or
    network changes release the old browser before opening another. No CDP port,
    credential export or persistent profile is required. Timed-out work remains
    busy until it finishes, preventing overlapping sessions and login loops.
    """

    handles_session_discard = True

    def __init__(self, *, browser_engine="firefox", headless=None, idle_seconds=None):
        if browser_engine not in ("firefox", "chromium"):
            raise ValueError("Managed browser engine must be firefox or chromium.")
        self.browser_engine = browser_engine
        self.headless = browser_headless() if headless is None else headless
        if type(self.headless) is not bool:
            raise ValueError("Browser headless selection must be a boolean.")
        self._idle_seconds = browser_idle_seconds() if idle_seconds is None else idle_seconds
        if not isinstance(self._idle_seconds, (int, float)) or not 0 < self._idle_seconds <= 86400:
            raise ValueError("Browser idle lifetime must be positive and at most one day.")
        self._queue = Queue()
        self._lock = Lock()
        self._thread = None
        self._busy = None
        self._closed = False
        atexit.register(self.close)

    def run(self, operation, *, use_publisher_proxy=True, browser_engine=None):
        browser_engine = self.browser_engine if browser_engine is None else browser_engine
        if type(use_publisher_proxy) is not bool:
            raise ValueError("Browser network selection must be a boolean.")
        if browser_engine not in ("firefox", "chromium"):
            raise ValueError("Managed browser engine must be firefox or chromium.")
        with self._lock:
            if self._closed:
                raise RetrievalUnavailable("browser_session_closed")
            if self._busy is not None and not self._busy.done():
                raise RetrievalUnavailable("browser_busy")
            future = self._busy = Future()
            if self._thread is None:
                self._thread = Thread(target=self._worker, name="paperbridge-browser", daemon=True)
                self._thread.start()
            self._queue.put((future, operation, use_publisher_proxy, browser_engine))
        try:
            return future.result(timeout=120)
        except TimeoutError:
            raise RetrievalUnavailable("browser_operation_timeout") from None
        finally:
            with self._lock:
                if future.done() and self._busy is future:
                    self._busy = None

    def _worker(self):
        from playwright.sync_api import Error, sync_playwright

        playwright = browser = context = None
        session_mode = None

        def release_session():
            nonlocal playwright, browser, context
            old_browser, old_playwright = browser, playwright
            playwright = browser = context = None
            # Cleanup runs on the owning thread, never during a retrieval. Even a
            # disconnected browser must not prevent stopping its Playwright driver.
            try:
                if old_browser:
                    old_browser.close()
            except Error:
                pass
            finally:
                if old_playwright:
                    try:
                        old_playwright.stop()
                    except Error:
                        pass

        try:
            while True:
                try:
                    task = self._queue.get(timeout=self._idle_seconds if playwright else None)
                except Empty:
                    release_session()
                    continue
                if task is None:
                    break
                future, operation, use_publisher_proxy, browser_engine = task
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    # One browser at a time across engines and publisher routes.
                    # Close on the owner thread before switching; never retain two
                    # idle browser trees or mutate the machine's network settings.
                    requested_mode = (browser_engine, use_publisher_proxy)
                    if playwright is not None and session_mode != requested_mode:
                        release_session()
                    session_mode = requested_mode
                    if playwright is None:
                        playwright = sync_playwright().start()
                    if context is None:
                        proxy = publisher_proxy() if use_publisher_proxy else None
                        options = dict(headless=self.headless, env=browser_environment(),
                                       proxy={"server": proxy} if proxy else None)
                        if browser_engine == "chromium":
                            executable = os.environ.get("PAPERBRIDGE_CHROMIUM_EXECUTABLE")
                            if executable is not None and not Path(executable).is_absolute():
                                raise ValueError("Chromium executable must be an absolute path.")
                            # Select the matching full browser, not the separate
                            # headless-shell binary. Keep Chromium's sandbox on.
                            options.update(chromium_sandbox=True, executable_path=(
                                executable or playwright.chromium.executable_path))
                        browser = getattr(playwright, browser_engine).launch(**options)
                        context = browser.new_context(accept_downloads=False, service_workers="block")
                    future.set_result(operation(context))
                except _DiscardBrowserSession as exc:
                    # Closing the owned browser uses Playwright's process-level
                    # graceful-close/kill path, avoiding a stalled page.close().
                    # Keep the operation busy until browser and driver cleanup ends.
                    release_session()
                    future.set_exception(RetrievalUnavailable(exc.reason))
                except Error:
                    release_session()
                    future.set_exception(RetrievalUnavailable("browser_interaction_required"))
                except Exception as exc:
                    future.set_exception(exc)
        finally:
            release_session()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(None)
            thread = self._thread
        if thread:
            thread.join(timeout=5)


# Existing integrations may keep importing the historical name.
ManagedFirefox = ManagedBrowser
