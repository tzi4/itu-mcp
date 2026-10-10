from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from paperbridge.browser import RetrievalUnavailable
from paperbridge.itu_login import fill_itu_form, ieee_login_url, trusted_idp_url, itu_username


LOGIN = "https://yetkim.itu.edu.tr/idp/profile/SAML2/Redirect/SSO?execution=synthetic"


class ItuLoginTests(unittest.TestCase):
    def test_itu_mail_address_maps_to_short_account_without_changing_host_credentials(self):
        self.assertEqual(itu_username("synthetic-user"), "synthetic-user")
        self.assertEqual(itu_username("synthetic-user@itu.edu.tr"), "synthetic-user")
        self.assertEqual(itu_username("synthetic-user@ITU.EDU.TR"), "synthetic-user")

    def test_unknown_email_domains_and_empty_usernames_are_rejected(self):
        for value in ["", "@itu.edu.tr", "user@other.test", "user@itu.edu.tr.evil.test",
                      "user@other@itu.edu.tr", "user name", None]:
            with self.subTest(value=value), self.assertRaises(RetrievalUnavailable):
                itu_username(value)

    def test_only_exact_itu_https_login_target_is_accepted(self):
        self.assertTrue(trusted_idp_url(LOGIN, login_form=True))
        for url in [LOGIN.replace("https:", "http:"), LOGIN.replace(".tr/", ".tr.evil.test/"),
                    LOGIN.replace("yetkim.", "user:secret@yetkim."),
                    LOGIN.replace(".tr/", ".tr:8443/"),
                    "https://yetkim.itu.edu.tr/other-form"]:
            with self.subTest(url=url):
                self.assertFalse(trusted_idp_url(url, login_form=True))

    def test_credentials_are_not_loaded_for_an_untrusted_page(self):
        page = Mock(url="https://evil.test/login")
        source = Mock()
        with self.assertRaises(RetrievalUnavailable):
            fill_itu_form(page, source)
        source.assert_not_called()
        page.locator.assert_not_called()

    def test_credentials_are_not_loaded_for_a_cross_origin_form(self):
        page = Mock(url=LOGIN)
        password = page.locator.return_value
        password.count.return_value = 1
        password.evaluate.return_value = {"action": "https://evil.test/capture", "method": "post"}
        source = Mock()
        with self.assertRaises(RetrievalUnavailable):
            fill_itu_form(page, source)
        source.assert_not_called()

    def test_credentials_are_not_submitted_with_get(self):
        page = Mock(url=LOGIN)
        password = page.locator.return_value
        password.count.return_value = 1
        password.evaluate.return_value = {"action": LOGIN, "method": "get"}
        source = Mock()
        with self.assertRaises(RetrievalUnavailable):
            fill_itu_form(page, source)
        source.assert_not_called()

    def test_ieee_entry_uses_verified_entity_not_a_borrowed_execution(self):
        url = ieee_login_url("https://ieeexplore.ieee.org/document/9019105/")
        self.assertIn("entityId=https%3A%2F%2Fyetkim.itu.edu.tr%2Fidp%2Fshibboleth", url)
        self.assertNotIn("execution", url)
        with self.assertRaises(ValueError):
            ieee_login_url("https://evil.test/return")

    @unittest.skipUnless(os.environ.get("PAPERBRIDGE_BROWSER_TESTS") == "1", "Opt-in real Chromium test")
    def test_real_form_uses_runtime_credentials_and_posts_only_to_itu(self):
        from playwright.sync_api import sync_playwright

        source = Mock(return_value=SimpleNamespace(username="synthetic-user@itu.edu.tr", password="synthetic-password"))
        form = '''<form method="post"><input type="text" name="j_username">
        <input type="password" name="j_password"><button name="_eventId_proceed" type="submit">Login</button></form>'''
        seen = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = browser.new_page()
                def route(r):
                    if r.request.method == "POST":
                        seen.append((r.request.url, r.request.post_data))
                        r.fulfill(status=200, body="Synthetic authentication response")
                    else:
                        r.fulfill(status=200, content_type="text/html", body=form)
                page.route("**/*", route)
                page.goto(LOGIN)
                fill_itu_form(page, source)
            finally:
                browser.close()
        source.assert_called_once_with()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], LOGIN)
        self.assertIn("j_username=synthetic-user", seen[0][1])
        self.assertNotIn("%40", seen[0][1])
        self.assertEqual(source.return_value.username, "synthetic-user@itu.edu.tr")
        self.assertIn("j_password=synthetic-password", seen[0][1])


if __name__ == "__main__":
    unittest.main()
