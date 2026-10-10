"""Synthetic coverage for a shared managed engine and operator display mode."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from paperbridge import DocumentStore, PaperBridge
from paperbridge.browser import browser_environment
from paperbridge.managed_browser import ManagedBrowser, ManagedFirefox, browser_headless
from paperbridge.springer import SpringerBrowserProvider
from paperbridge_tests.test_browser_engine_selection import PAPER, PDF


class CommonManagedBrowserTests(unittest.TestCase):
    def test_legacy_name_remains_an_alias_with_firefox_default(self):
        self.assertIs(ManagedFirefox, ManagedBrowser)
        with patch.dict(os.environ, {}, clear=True):
            runner = ManagedFirefox()
            try:
                self.assertEqual(runner.browser_engine, "firefox")
                self.assertTrue(runner.headless)
                self.assertIsNone(runner._thread)
            finally:
                runner.close()

    def test_default_and_none_inherit_chromium_after_explicit_firefox_override(self):
        drivers = [Mock(), Mock(), Mock()]
        for driver in drivers:
            driver.chromium.executable_path = "/synthetic/chrome"
        with patch.dict(os.environ, {}, clear=True), patch("playwright.sync_api.sync_playwright") as factory:
            factory.return_value.start.side_effect = drivers
            runner = ManagedBrowser(browser_engine="chromium")
            try:
                original = runner.run(lambda context: context)
                self.assertIs(original, runner.run(lambda context: context, browser_engine=None))
                changed = runner.run(lambda context: context, browser_engine="firefox")
                self.assertIsNot(original, changed)
                drivers[0].chromium.launch.return_value.close.assert_called_once()
                drivers[0].stop.assert_called_once()
                restored = runner.run(lambda context: context)
                self.assertIsNot(restored, changed)
                drivers[1].firefox.launch.return_value.close.assert_called_once()
                drivers[1].stop.assert_called_once()
                self.assertEqual(runner.browser_engine, "chromium")
                drivers[2].chromium.launch.assert_called_once()
            finally:
                runner.close()

    def test_invalid_constructor_settings_never_start_a_thread_or_browser(self):
        for settings in ({"browser_engine": value} for value in (None, "", "Chrome", True, [], {})):
            with self.subTest(settings=settings), patch("paperbridge.managed_browser.Thread") as thread, \
                    patch("playwright.sync_api.sync_playwright") as factory, self.assertRaises(ValueError):
                ManagedBrowser(**settings)
            thread.assert_not_called()
            factory.assert_not_called()
        for value in (0, 1, "0", "1", [], {}):
            with self.subTest(headless=value), self.assertRaises(ValueError):
                ManagedBrowser(headless=value)

    def test_headless_environment_is_strict_and_explicit_boolean_can_override(self):
        for value, expected in ((None, True), ("1", True), ("0", False)):
            env = {} if value is None else {"PAPERBRIDGE_BROWSER_HEADLESS": value}
            with self.subTest(value=value), patch.dict(os.environ, env, clear=True):
                self.assertIs(browser_headless(), expected)
                for override in (None, True, False):
                    runner = ManagedBrowser(headless=override)
                    try:
                        self.assertIs(runner.headless, expected if override is None else override)
                    finally:
                        runner.close()
        for value in ("", "true", "False", "2", "01", " 0", "1 "):
            with self.subTest(value=value), patch.dict(os.environ, {"PAPERBRIDGE_BROWSER_HEADLESS": value}, clear=True), \
                    patch("playwright.sync_api.sync_playwright") as factory, self.assertRaises(ValueError):
                ManagedBrowser()
            factory.assert_not_called()

    def test_headed_launch_retains_sandbox_and_only_allowed_display_environment(self):
        environment = {"PAPERBRIDGE_BROWSER_HEADLESS": "0", "DISPLAY": ":117",
                       "XAUTHORITY": "/synthetic/display/auth", "HOME": "/synthetic/home",
                       "NINOVA_PASSWORD": "fixture-private-value", "API_TOKEN": "fixture-private-value",
                       "HTTP_PROXY": "http://fixture-private-value@proxy.invalid"}
        for engine in ("firefox", "chromium"):
            with self.subTest(engine=engine), patch.dict(os.environ, environment, clear=True), \
                    patch("playwright.sync_api.sync_playwright") as factory:
                driver = factory.return_value.start.return_value
                driver.chromium.executable_path = "/synthetic/chrome"
                runner = ManagedBrowser(browser_engine=engine)
                try:
                    runner.run(lambda context: None, use_publisher_proxy=False)
                    options = getattr(driver, engine).launch.call_args.kwargs
                    self.assertIs(options["headless"], False)
                    self.assertEqual(options["env"], {key: environment[key] for key in ("DISPLAY", "XAUTHORITY", "HOME")})
                    self.assertNotIn("fixture-private-value", repr(options))
                    self.assertNotIn("--no-sandbox", options.get("args", []))
                    if engine == "chromium":
                        self.assertIs(options["chromium_sandbox"], True)
                    else:
                        self.assertNotIn("chromium_sandbox", options)
                finally:
                    runner.close()

    def test_browser_environment_does_not_export_host_configuration_or_credentials(self):
        with patch.dict(os.environ, {"XAUTHORITY": "/synthetic/auth", "DISPLAY": ":117",
                                    "PAPERBRIDGE_BROWSER_HEADLESS": "0", "COOKIE": "fixture-only"}, clear=True):
            self.assertEqual(browser_environment(), {"XAUTHORITY": "/synthetic/auth", "DISPLAY": ":117"})


class CommonProviderConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = DocumentStore(Path(temporary.name))

    def test_provider_selection_and_status_matrix(self):
        for engine in (None, "chromium", "firefox"):
            for port in (None, "9222"):
                for mdpi in (False, True):
                    env = {}
                    if engine is not None:
                        env["PAPERBRIDGE_BROWSER_ENGINE"] = engine
                    if port is not None:
                        env["PAPERBRIDGE_BROWSER_PORT"] = port
                    if mdpi:
                        env["PAPERBRIDGE_MDPI_PUBLIC_PDF"] = "1"
                    enabled = engine is not None or port is not None
                    managed = enabled and (engine == "firefox" or port is None)
                    expected = (["ieee", "springer"] if managed else ["ieee"] if enabled else [])
                    with self.subTest(engine=engine, port=port, mdpi=mdpi), patch.dict(os.environ, env, clear=True), \
                            patch("playwright.sync_api.sync_playwright") as factory:
                        bridge = PaperBridge(self.store, Mock())
                        try:
                            status = bridge.status()
                            self.assertEqual([provider.name for provider in bridge.providers], expected)
                            self.assertEqual(status["configured_providers"], expected)
                            self.assertIsNone(status["institutional_retrieval_verified"])
                            self.assertEqual(status["institutional_retrieval_verification"],
                                             "not_checked_by_status")
                            self.assertEqual(status["status_scope"],
                                             "configuration_only_no_network_requests")
                            self.assertIs(status["browser_provider_configured"], enabled)
                            self.assertEqual(status["browser_engine"], engine or "chromium")
                            self.assertEqual(status["springer_browser_engine"], (engine or "chromium") if managed else None)
                            self.assertIs(status["managed_browser_headless"], True if managed else None)
                            if managed:
                                runner = bridge.providers[0].runner
                                self.assertIs(runner, bridge.providers[1].runner)
                                self.assertEqual(runner.browser_engine, engine)
                                self.assertIsNone(bridge.providers[1].browser_engine)
                                self.assertIsNone(runner._thread)
                            elif enabled:
                                self.assertIsNone(bridge.provider.runner)
                                self.assertEqual(bridge.provider.port, 9222)
                            factory.assert_not_called()
                        finally:
                            if managed:
                                bridge.provider.runner.close()

    def test_explicit_springer_override_does_not_change_common_engine_or_headless_mode(self):
        for common, override in (("chromium", "firefox"), ("firefox", "chromium")):
            with self.subTest(common=common), patch.dict(os.environ, {
                    "PAPERBRIDGE_BROWSER_ENGINE": common, "PAPERBRIDGE_SPRINGER_BROWSER_ENGINE": override,
                    "PAPERBRIDGE_BROWSER_HEADLESS": "0"}, clear=True):
                bridge = PaperBridge(self.store, Mock())
                try:
                    self.assertEqual(bridge.provider.runner.browser_engine, common)
                    self.assertIs(bridge.provider.runner, bridge.providers[1].runner)
                    self.assertEqual(bridge.providers[1].browser_engine, override)
                    self.assertIs(bridge.provider.runner.headless, False)
                    self.assertEqual(bridge.status()["springer_browser_engine"], override)
                    self.assertIs(bridge.status()["managed_browser_headless"], False)
                finally:
                    bridge.provider.runner.close()

    def test_invalid_environment_fails_before_manager_or_discovery_for_constructor_and_status(self):
        for name, invalid in (("PAPERBRIDGE_BROWSER_ENGINE", ("", "Chrome", "true")),
                              ("PAPERBRIDGE_SPRINGER_BROWSER_ENGINE", ("", "Chrome", "true")),
                              ("PAPERBRIDGE_BROWSER_HEADLESS", ("", "true", "2", "01"))):
            for value in invalid:
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}, clear=True), \
                        patch("paperbridge.managed_browser.ManagedBrowser") as manager:
                    discovery = Mock()
                    with self.assertRaises(ValueError):
                        PaperBridge(self.store, discovery)
                    with self.assertRaises(ValueError):
                        PaperBridge.status()
                    manager.assert_not_called()
                    discovery.lookup.assert_not_called()

    def test_explicit_firefox_provider_override_is_forwarded_to_a_chromium_runner(self):
        (self.store.root / "publisher-policy.json").write_text(json.dumps({"springer": {
            "local_processing": True, "external_model": True, "basis": "Original synthetic fixture"}}))
        runner = Mock()
        runner.run.side_effect = lambda operation, **kwargs: operation(Mock())
        provider = SpringerBrowserProvider(self.store, runner=runner, browser_engine="firefox")
        with patch.object(provider, "_read_page", return_value=PDF):
            provider.retrieve(PAPER)
        self.assertEqual(runner.run.call_args.kwargs, {"use_publisher_proxy": False, "browser_engine": "firefox"})


if __name__ == "__main__":
    unittest.main()
