from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from paperbridge import DocumentStore, PaperBridge
from paperbridge.browser import RetrievalUnavailable, browser_port
from paperbridge.ieee import IeeeBrowserProvider, article_url, download_link, verify_pdf
from paperbridge_tests.test_paperbridge import sample_pdf


PAPER = {"doi": "10.1109/demo.123456", "title": "Rocket landing demo",
         "publisher_url": "https://ieeexplore.ieee.org/document/123456/"}
PDF = sample_pdf("Rocket landing demo DOI: 10.1109/demo.123456")
PDF_URL = "https://ieeexplore.ieee.org/iel7/100/200/00123456.pdf"


class BrowserRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DocumentStore(Path(self.temp.name))
        self.provider = IeeeBrowserProvider(self.store, 9222)

    def allow_fixture(self):
        (self.store.root / "publisher-rights.json").write_text(json.dumps({PAPER["doi"]: {
            "local_processing": True, "external_model": True, "basis": "Original synthetic test fixture"}}))

    def test_metadata_url_must_identify_ieee_article(self):
        self.assertEqual(article_url(PAPER)[1], "123456")
        for url in ["https://ieeexplore.ieee.org.evil.test/document/123456/",
                    "http://ieeexplore.ieee.org:443/document/123456/",
                    "https://user:secret@ieeexplore.ieee.org/document/123456/",
                    "https://ieeexplore.ieee.org:8443/document/123456/",
                    "https://ieeexplore.ieee.org/servlet/login"]:
            with self.subTest(url=url), self.assertRaises(RetrievalUnavailable):
                article_url(dict(PAPER, publisher_url=url))

    def test_legacy_metadata_is_upgraded_without_allowing_http_downloads(self):
        for url in ["http://ieeexplore.ieee.org/document/123456/",
                    "http://ieeexplore.ieee.org:80/document/123456"]:
            self.assertEqual(article_url(dict(PAPER, publisher_url=url)),
                             ("https://ieeexplore.ieee.org/document/123456/", "123456"))
        for url in ["http://ieeexplore.ieee.org.evil.test/document/123456/",
                    "http://user@ieeexplore.ieee.org/document/123456/",
                    "http://ieeexplore.ieee.org/document/123456/?redirect=other",
                    "http://ieeexplore.ieee.org/document/123456/#fragment",
                    "http://ieeexplore.ieee.org/document/１２３４５６/"]:
            with self.subTest(url=url), self.assertRaises(RetrievalUnavailable):
                article_url(dict(PAPER, publisher_url=url))
        self.assertFalse(download_link(PDF_URL.replace("https:", "http:"), "123456"))

    def test_download_links_cannot_select_another_article_or_host(self):
        self.assertTrue(download_link(PDF_URL, "123456"))
        self.assertTrue(download_link("https://ieeexplore.ieee.org/stamp/stamp.jsp?arnumber=123456", "123456"))
        for url in [PDF_URL.replace("00123456", "654321"), "https://127.0.0.1/file.pdf",
                    "https://ieeexplore.ieee.org/stamp/stamp.jsp?arnumber=123456&arnumber=9",
                    "https://ieeexplore.ieee.org/logout"]:
            with self.subTest(url=url):
                self.assertFalse(download_link(url, "123456"))

    def test_port_cannot_be_a_remote_endpoint(self):
        self.assertEqual(browser_port("9222"), 9222)
        for value in ["http://evil.test", "9222/path", "0", "65536", "１２３４"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                browser_port(value)

    def test_pdf_identity_and_html_rejection(self):
        verify_pdf(PDF, PAPER)
        for data in [b"<html>Sign in</html>", sample_pdf(),
                     sample_pdf("Unrelated paper DOI: 10.1109/demo.123456")]:
            with self.subTest(data=data[:20]), self.assertRaises(RetrievalUnavailable):
                verify_pdf(data, PAPER)

    def test_no_permission_means_no_browser_or_metadata_request(self):
        metadata = Mock()
        bridge = PaperBridge(self.store, metadata, provider=self.provider)
        with patch("paperbridge.ieee.connected_session") as browser:
            result = bridge.get_text(PAPER["doi"])
        self.assertEqual(result["reason"], "document_permission_required")
        self.assertIsNone(result["text"])
        metadata.lookup.assert_not_called()
        browser.assert_not_called()

    def test_pdf_without_printed_doi_requires_matching_article_and_all_authors(self):
        paper = dict(PAPER, authors=["Synthetic Author", "Second Author"])
        data = sample_pdf("Rocket landing demo Synthetic Author Second Author")
        self.assertEqual(verify_pdf(data, paper, retrieved_article_id="123456"),
                         "publisher_article_id_and_first_page_title_authors")
        for article_id, metadata in [(None, paper), ("999", paper),
                ("123456", dict(paper, authors=[])),
                ("123456", dict(paper, authors=["Unrelated Author"])),
                ("123456", dict(paper, publisher_url="https://evil.test/document/123456/"))]:
            with self.subTest(article_id=article_id, metadata=metadata), self.assertRaises(RetrievalUnavailable):
                verify_pdf(data, metadata, retrieved_article_id=article_id)

    def test_conflicting_printed_doi_does_not_use_author_fallback(self):
        data = sample_pdf("Rocket landing demo Synthetic Author DOI: 10.1109/other.654321")
        with self.assertRaises(RetrievalUnavailable):
            verify_pdf(data, dict(PAPER, authors=["Synthetic Author"]), retrieved_article_id="123456")

    def test_download_failure_is_not_metadata_success(self):
        self.allow_fixture()
        metadata = Mock()
        metadata.lookup.return_value = PAPER
        with patch("paperbridge.ieee.connected_session", side_effect=RetrievalUnavailable("browser_session_required")):
            result = PaperBridge(self.store, metadata, provider=self.provider).get_text(PAPER["doi"])
        self.assertEqual(result["reason"], "browser_session_required")
        self.assertFalse(result["full_text_read"])
        self.assertEqual(self.store.search("rocket"), [])

    def test_operator_provider_policy_allows_new_doi_but_document_denial_wins(self):
        (self.store.root / "publisher-policy.json").write_text(json.dumps({"ieee": {
            "local_processing": True, "external_model": True, "basis": "Synthetic operator policy"}}))
        self.assertTrue(self.provider.rights(PAPER["doi"]).external_model)
        (self.store.root / "publisher-rights.json").write_text(json.dumps({PAPER["doi"]: {
            "local_processing": True, "external_model": False, "basis": "Synthetic explicit denial"}}))
        with self.assertRaises(RetrievalUnavailable):
            self.provider.rights(PAPER["doi"])

    def test_provider_text_is_ingested_and_retrievable(self):
        self.allow_fixture()
        with patch("paperbridge.ieee.connected_session"), patch.object(self.provider, "_read_page", return_value=PDF):
            result = self.provider.retrieve(PAPER)
        self.assertIn("Rocket landing demo", result["text"])
        self.assertEqual(result["metadata"]["institutional_entitlement"], "not_verified")
        self.assertEqual(PaperBridge(self.store).get_text(PAPER["doi"])["text"], result["text"])
        self.assertEqual(list(self.store.root.glob("*/article.pdf")), [])

    def test_non_1109_doi_routes_by_validated_xplore_metadata(self):
        doi = "10.23919/synthetic.123456"
        paper = dict(PAPER, doi=doi, publisher="Synthetic conference publisher")
        data = sample_pdf("Rocket landing demo DOI: " + doi)
        (self.store.root / "publisher-rights.json").write_text(json.dumps({doi: {
            "local_processing": True, "external_model": True, "basis": "Original synthetic fixture"}}))
        metadata = Mock()
        metadata.lookup.return_value = paper
        bridge = PaperBridge(self.store, metadata, provider=self.provider)
        with patch("paperbridge.ieee.connected_session"), patch.object(self.provider, "_read_page", return_value=data):
            result = bridge.get_text(doi)
        self.assertTrue(result["id"].startswith("pb:"))
        self.assertEqual(result["metadata"]["pdf_identity"], "first_page_doi_and_title")
        self.assertEqual(bridge.resolve(doi)["provider"], "ieee")

    def test_non_1109_doi_still_checks_permission_before_metadata(self):
        metadata = Mock()
        result = PaperBridge(self.store, metadata, provider=self.provider).get_text("10.23919/synthetic.123456")
        self.assertEqual(result["reason"], "document_permission_required")
        metadata.lookup.assert_not_called()

    def test_metadata_host_cannot_be_selected_by_ieee_publisher_name_or_prefix(self):
        self.allow_fixture()
        metadata = Mock()
        for url in ["https://elsevier.example/document/123456", "https://ieeexplore.ieee.org.evil.test/document/123456"]:
            metadata.lookup.return_value = dict(PAPER, publisher="IEEE", publisher_url=url)
            with self.subTest(url=url), patch("paperbridge.ieee.connected_session") as browser:
                result = PaperBridge(self.store, metadata, provider=self.provider).get_text(PAPER["doi"])
                self.assertEqual(result["reason"], "publisher_not_supported")
                browser.assert_not_called()

    def test_lookup_for_different_doi_never_starts_browser(self):
        self.allow_fixture()
        metadata = Mock()
        metadata.lookup.return_value = dict(PAPER, doi="10.1109/other.654321")
        with patch("paperbridge.ieee.connected_session") as browser:
            result = PaperBridge(self.store, metadata, provider=self.provider).get_text(PAPER["doi"])
        self.assertEqual(result["reason"], "metadata_identity_not_verified")
        browser.assert_not_called()

    def test_fetch_alias_reaches_provider(self):
        self.allow_fixture()
        metadata = Mock()
        metadata.lookup.return_value = PAPER
        bridge = PaperBridge(self.store, metadata, provider=self.provider)
        with patch("paperbridge.ieee.connected_session"), patch.object(self.provider, "_read_page", return_value=PDF):
            result = bridge.fetch("doi:" + PAPER["doi"])
        self.assertIn("Rocket landing demo", result["text"])

    def test_missing_access_uses_institution_callback_once_then_retries_pdf(self):
        self.allow_fixture()
        self.provider.login = Mock()
        with patch("paperbridge.ieee.connected_session"), patch.object(self.provider, "_read_page", side_effect=[
                RetrievalUnavailable("publisher_pdf_link_unavailable"), PDF]):
            self.assertIn("Rocket landing demo", self.provider.retrieve(PAPER)["text"])
        self.provider.login.assert_called_once()

    def test_failed_login_or_browser_challenge_does_not_loop(self):
        self.allow_fixture()
        for reason, expected_calls in [("publisher_pdf_link_unavailable", 1), ("publisher_interaction_required", 0)]:
            self.provider.login = Mock()
            with patch("paperbridge.ieee.connected_session"), patch.object(self.provider, "_read_page",
                    side_effect=RetrievalUnavailable(reason)), self.assertRaises(RetrievalUnavailable):
                self.provider.retrieve(PAPER)
            self.assertEqual(self.provider.login.call_count, expected_calls)

    def test_redirect_never_forwards_cookies_to_another_host(self):
        context = Mock()
        context.cookies.return_value = []
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.status_code = 302
        response.headers = {"Location": "https://evil.test/steal"}
        with patch("paperbridge.ieee.requests.Session") as factory:
            factory.return_value.__enter__.return_value = session
            with self.assertRaises(RetrievalUnavailable) as error:
                self.provider._download(context, PDF_URL, "123456")
        self.assertEqual(error.exception.reason, "publisher_redirect_not_allowed")
        self.assertEqual(session.get.call_count, 1)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_stream_size_is_bounded_even_without_content_length(self):
        context = Mock()
        context.cookies.return_value = []
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.status_code = 200
        response.iter_content.return_value = [b"%PDF-", b"x" * 100]
        with patch("paperbridge.ieee.requests.Session") as factory, patch("paperbridge.ieee.MAX_PDF_BYTES", 64):
            factory.return_value.__enter__.return_value = session
            with self.assertRaises(RetrievalUnavailable) as error:
                self.provider._download(context, PDF_URL, "123456")
        self.assertEqual(error.exception.reason, "pdf_size_limit_exceeded")


@unittest.skipUnless(os.environ.get("PAPERBRIDGE_BROWSER_TESTS") == "1", "Opt-in real Chromium test")
class ChromiumTests(unittest.TestCase):
    setUp = BrowserRetrievalTests.setUp
    allow_fixture = BrowserRetrievalTests.allow_fixture

    def test_real_chromium_session_pdf_and_disconnect(self):
        """Real CDP, incognito cookies, HTML/stamp discovery; no publisher network."""
        import socket
        import subprocess
        import sys
        from contextlib import contextmanager
        from paperbridge.browser import connected_session

        self.allow_fixture()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        owner_code = '''
import sys
from playwright.sync_api import sync_playwright
from paperbridge.browser import SESSION_MARKER
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, chromium_sandbox=True,
        args=["--remote-debugging-address=127.0.0.1", "--remote-debugging-port="+sys.argv[1]])
    c = b.new_context(service_workers="block")
    c.add_cookies([{"name":"pb_fixture", "value":"synthetic", "url":"https://ieeexplore.ieee.org/"}])
    marker = c.new_page()
    marker.goto(SESSION_MARKER)
    print("ready", flush=True)
    while b.is_connected():
        marker.wait_for_timeout(500)
'''
        owner = subprocess.Popen([sys.executable, "-c", owner_code, str(port)], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True)
        def cleanup():
            owner.terminate()
            try:
                owner.wait(timeout=10)
            except subprocess.TimeoutExpired:
                owner.kill()
                owner.wait()
            owner.stdout.close()
        self.addCleanup(cleanup)
        self.assertEqual(owner.stdout.readline().strip(), "ready")
        self.provider.port = port
        @contextmanager
        def fixture_session(port):
            with connected_session(port) as context:
                def route(r):
                    if "/document/" in r.request.url:
                        self.assertIn("pb_fixture=synthetic", r.request.all_headers().get("cookie", ""))
                        body = '<a href="/stamp/stamp.jsp?arnumber=123456">PDF</a>'
                    elif "/stamp/" in r.request.url:
                        body = f'<iframe src="{PDF_URL}"></iframe>'
                    else:
                        body = "Synthetic PDF frame"
                    r.fulfill(status=200, content_type="text/html", body=body)
                context.context.route("**/*", route)
                yield context
        with patch("paperbridge.ieee.connected_session", fixture_session):
            session = MagicMock()
            response = session.get.return_value.__enter__.return_value
            response.status_code = 200
            response.iter_content.return_value = [PDF]
            with patch("paperbridge.ieee.requests.Session") as factory:
                factory.return_value.__enter__.return_value = session
                result = self.provider.retrieve(PAPER)
            self.assertIn("Rocket landing demo", result["text"])
            self.assertEqual(session.cookies.set.call_args.args, ("pb_fixture", "synthetic"))
        self.assertIsNone(owner.poll())
        with connected_session(port) as context:
            self.assertTrue(any(c["name"] == "pb_fixture" for c in context.cookies([PDF_URL])))


if __name__ == "__main__":
    unittest.main()
