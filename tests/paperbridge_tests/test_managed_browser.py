from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from paperbridge import DocumentStore
from paperbridge.browser import RetrievalUnavailable
from paperbridge.ieee import IeeeBrowserProvider, article_request_allowed
from paperbridge.managed_browser import ManagedFirefox, browser_idle_seconds
from paperbridge_tests.test_browser_retrieval import PAPER, PDF, PDF_URL


class ManagedBrowserTests(unittest.TestCase):
    def test_network_switch_releases_old_browser_before_starting_another(self):
        with patch("playwright.sync_api.sync_playwright") as factory, \
                patch("paperbridge.managed_browser.publisher_proxy", return_value="http://127.0.0.1:18888"):
            first, second = Mock(), Mock()
            factory.return_value.start.side_effect = [first, second]
            def direct_launch(**kwargs):
                first.firefox.launch.return_value.close.assert_called_once()
                first.stop.assert_called_once()
                self.assertIsNone(kwargs["proxy"])
                return second.firefox.launch.return_value
            second.firefox.launch.side_effect = direct_launch
            runner = ManagedFirefox()
            try:
                original = runner.run(lambda context: context)
                self.assertIs(original, runner.run(lambda context: context))
                self.assertEqual(first.firefox.launch.call_count, 1)
                changed = runner.run(lambda context: context, use_publisher_proxy=False)
                self.assertIsNot(original, changed)
                self.assertIs(changed, runner.run(lambda context: context, use_publisher_proxy=False))
                self.assertEqual(second.firefox.launch.call_count, 1)
            finally:
                runner.close()
            second.stop.assert_called_once()

    def test_idle_session_releases_driver_and_reopens_on_demand(self):
        stopped = threading.Event()
        with patch("playwright.sync_api.sync_playwright") as factory:
            first, second = Mock(), Mock()
            factory.return_value.start.side_effect = [first, second]
            first.stop.side_effect = stopped.set
            runner = ManagedFirefox(idle_seconds=0.05)
            try:
                context = runner.run(lambda context: context)
                self.assertIs(context, first.firefox.launch.return_value.new_context.return_value)
                self.assertTrue(stopped.wait(timeout=3), "Idle driver was not stopped")
                first.firefox.launch.return_value.close.assert_called_once()
                self.assertIs(runner.run(lambda context: context),
                              second.firefox.launch.return_value.new_context.return_value)
            finally:
                runner.close()
            second.stop.assert_called_once()

    def test_idle_deadline_does_not_interrupt_an_active_operation(self):
        with patch("playwright.sync_api.sync_playwright") as factory:
            driver = factory.return_value.start.return_value
            stopped = threading.Event()
            driver.stop.side_effect = stopped.set
            runner = ManagedFirefox(idle_seconds=0.02)
            try:
                # The operation deliberately lasts longer than the idle timeout.
                self.assertFalse(runner.run(lambda context: stopped.wait(timeout=0.1)))
            finally:
                runner.close()
            self.assertTrue(stopped.is_set())

    def test_invalid_idle_environment_does_not_start_a_browser(self):
        for value in ["0", "-1", "nan", "300.5", "86401"]:
            with self.subTest(value=value), patch.dict(os.environ, {
                    "PAPERBRIDGE_BROWSER_IDLE_SECONDS": value}), self.assertRaises(ValueError):
                browser_idle_seconds()

    def test_closed_session_does_not_restart(self):
        runner = ManagedFirefox()
        runner.close()
        with self.assertRaises(RetrievalUnavailable):
            runner.run(lambda context: None)

    def test_host_credentials_are_not_browser_environment_variables(self):
        with patch.dict(os.environ, {"NINOVA_PASSWORD": "synthetic-private-value",
                                    "PAPERBRIDGE_PUBLISHER_PROXY": ""}), \
                patch("playwright.sync_api.sync_playwright") as factory:
            runner = ManagedFirefox()
            try:
                self.assertEqual(runner.run(lambda context: 123), 123)
                launch = factory.return_value.start.return_value.firefox.launch
                self.assertNotIn("NINOVA_PASSWORD", launch.call_args.kwargs["env"])
                self.assertNotIn("synthetic-private-value", str(launch.call_args.kwargs))
            finally:
                runner.close()

    def test_parallel_operation_is_rejected_while_owner_is_busy(self):
        entered, release = threading.Event(), threading.Event()
        with patch("playwright.sync_api.sync_playwright"):
            runner = ManagedFirefox()
            results = []
            def operation(context):
                entered.set()
                release.wait(timeout=5)
                return "complete"
            caller = threading.Thread(target=lambda: results.append(runner.run(operation)))
            caller.start()
            try:
                self.assertTrue(entered.wait(timeout=3))
                with self.assertRaises(RetrievalUnavailable) as error:
                    runner.run(lambda context: "must not run")
                self.assertEqual(error.exception.reason, "browser_busy")
            finally:
                release.set()
                caller.join(timeout=5)
                runner.close()
            self.assertEqual(results, ["complete"])

    def test_verification_resources_do_not_allow_off_site_navigation(self):
        request = Mock(url="https://fixture.awswaf.com/challenge.js")
        request.is_navigation_request.return_value = False
        self.assertTrue(article_request_allowed(request))
        request.is_navigation_request.return_value = True
        self.assertFalse(article_request_allowed(request))
        request.is_navigation_request.return_value = False
        for url in ["https://fixture.awswaf.com.evil.test/js", "http://fixture.awswaf.com/js",
                    "https://fixture.awswaf.com:8443/js", "https://127.0.0.1/js",
                    "https://user:secret@fixture.awswaf.com/js"]:
            request.url = url
            self.assertFalse(article_request_allowed(request))


@unittest.skipUnless(os.environ.get("PAPERBRIDGE_BROWSER_TESTS") == "1", "Opt-in real Firefox test")
class FirefoxTransportTests(unittest.TestCase):
    def test_native_download_rejects_redirects_html_and_oversized_body(self):
        runner = ManagedFirefox()
        try:
            def check(context):
                page = context.new_page()
                seen = []
                mode = {"value": "redirect"}
                def route(r):
                    seen.append(r.request.url)
                    if r.request.url == PDF_URL:
                        if mode["value"] == "redirect":
                            r.fulfill(status=302, headers={"Location": "https://evil.test/steal"})
                        elif mode["value"] == "html":
                            r.fulfill(status=200, content_type="text/html", body="<html>Login</html>")
                        else:
                            r.fulfill(status=200, content_type="application/pdf", body=b"%PDF-" + b"x" * 100)
                    else:
                        r.fulfill(status=200, content_type="text/html", body="Synthetic page")
                context.route("**/*", route)
                try:
                    page.goto(PAPER["publisher_url"])
                    provider = IeeeBrowserProvider(None, None, runner=runner)
                    for value, reason in [("redirect", "publisher_download_failed"),
                            ("html", "publisher_did_not_return_pdf"),
                            ("size", "pdf_size_limit_exceeded")]:
                        mode["value"] = value
                        with patch("paperbridge.ieee.MAX_PDF_BYTES", 64), self.assertRaises(RetrievalUnavailable) as error:
                            provider._download_for_page(context, page, PDF_URL, "123456")
                        self.assertEqual(error.exception.reason, reason)
                    self.assertFalse(any("evil.test" in url for url in seen))
                finally:
                    page.close()
            runner.run(check)
        finally:
            runner.close()

    def test_memory_session_handles_normal_navigation_then_extracts_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DocumentStore(Path(directory))
            (store.root / "publisher-rights.json").write_text(json.dumps({PAPER["doi"]: {
                "local_processing": True, "external_model": True, "basis": "Original synthetic fixture"}}))
            runner = ManagedFirefox()
            try:
                visited = []
                def prepare(context):
                    context.add_cookies([{"name": "pb_fixture", "value": "synthetic",
                                         "url": "https://ieeexplore.ieee.org/"}])
                    def route(r):
                        url = r.request.url
                        if "fixture.awswaf.com" in url:
                            r.fulfill(status=200, content_type="application/javascript",
                                      body="location.replace('/document/123456/?ready=1')")
                        elif "/document/" in url and "ready=1" not in url:
                            visited.append(202)
                            r.fulfill(status=202, content_type="text/html",
                                      body='<script src="https://fixture.awswaf.com/challenge.js"></script>')
                        elif "/document/" in url:
                            visited.append(200)
                            r.fulfill(status=200, content_type="text/html",
                                      body='<a href="/stamp/stamp.jsp?arnumber=123456">PDF</a>')
                        elif "/stamp/" in url:
                            r.fulfill(status=200, content_type="text/html", body=f'<iframe src="{PDF_URL}"></iframe>')
                        elif url == PDF_URL:
                            self.assertIn("pb_fixture=synthetic", r.request.all_headers().get("cookie", ""))
                            r.fulfill(status=200, content_type="application/pdf", body=PDF)
                        else:
                            r.fulfill(status=200, content_type="text/plain", body="Synthetic PDF frame")
                    context.route("**/*", route)
                    return threading.get_ident()
                owner_thread = runner.run(prepare)
                with patch("paperbridge.ieee.requests.Session") as factory:
                    result = IeeeBrowserProvider(store, None, runner=runner).retrieve(PAPER)
                    self.assertIn("Rocket landing demo", result["text"])
                    factory.assert_not_called()
                self.assertEqual(visited, [202, 200])
                thread, cookies = runner.run(lambda context: (
                    threading.get_ident(), context.cookies([PDF_URL])))
                self.assertEqual(thread, owner_thread)
                self.assertNotEqual(thread, threading.get_ident())
                self.assertTrue(any(c["name"] == "pb_fixture" for c in cookies))
            finally:
                runner.close()


if __name__ == "__main__":
    unittest.main()
