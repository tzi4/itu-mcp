from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from paperbridge.browser import run_session
from paperbridge.ieee import IeeeBrowserProvider
from paperbridge.network import publisher_proxy


class PublisherNetworkTests(unittest.TestCase):
    def test_ambient_proxy_credentials_are_not_inherited(self):
        with patch.dict(os.environ, {"PAPERBRIDGE_PUBLISHER_PROXY": "",
                                    "HTTPS_PROXY": "http://user:synthetic-secret@remote.test:8080"}):
            self.assertIsNone(publisher_proxy())

    def test_only_explicit_loopback_connect_endpoints_are_accepted(self):
        for url in ["http://127.0.0.1:18888", "http://[::1]:18888/"]:
            with self.subTest(url=url), patch.dict(os.environ, {"PAPERBRIDGE_PUBLISHER_PROXY": url}):
                self.assertEqual(publisher_proxy(), url.rstrip("/"))
        for url in ["http://remote.test:8080", "http://localhost:8080", "https://127.0.0.1:8080",
                    "http://127.0.0.1", "http://127.0.0.1:80", "http://127.0.0.1:99999",
                    "http://127.0.0.1:8080/path", "http://127.0.0.1:8080?key=synthetic-secret",
                    "http://user:synthetic-secret@127.0.0.1:8080", "http://127.0.0.1:8080\n"]:
            with self.subTest(url=url), patch.dict(os.environ, {"PAPERBRIDGE_PUBLISHER_PROXY": url}):
                with self.assertRaises(ValueError) as error:
                    publisher_proxy()
                self.assertNotIn("synthetic-secret", str(error.exception))

    def test_pdf_request_and_browser_launch_share_operator_proxy(self):
        proxy = "http://127.0.0.1:18888"
        context = Mock()
        context.cookies.return_value = []
        with patch.dict(os.environ, {"PAPERBRIDGE_PUBLISHER_PROXY": proxy}):
            with patch("paperbridge.ieee.requests.Session") as factory:
                session = factory.return_value.__enter__.return_value
                response = session.get.return_value.__enter__.return_value
                response.status_code = 200
                response.iter_content.return_value = [b"%PDF-synthetic-transport"]
                IeeeBrowserProvider._download(context, "https://ieeexplore.ieee.org/iel7/1/2/123.pdf", "123")
                session.proxies.update.assert_called_once_with({"http": proxy, "https": proxy})
                self.assertFalse(session.trust_env)
            with patch("playwright.sync_api.sync_playwright") as factory, patch("paperbridge.browser.socket.socket"):
                playwright = factory.return_value.__enter__.return_value
                browser = playwright.chromium.launch.return_value
                marker = browser.new_context.return_value.new_page.return_value
                marker.wait_for_timeout.side_effect = KeyboardInterrupt
                run_session("https://ieeexplore.ieee.org/Xplore/home.jsp")
                self.assertEqual(playwright.chromium.launch.call_args.kwargs["proxy"], {"server": proxy})
                browser.close.assert_called()


if __name__ == "__main__":
    unittest.main()
