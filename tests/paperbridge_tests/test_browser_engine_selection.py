"""Original fixtures for isolated per-provider browser selection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from paperbridge import DocumentStore, PaperBridge
from paperbridge.browser import RetrievalUnavailable
from paperbridge.managed_browser import ManagedFirefox
from paperbridge.springer import SpringerBrowserProvider
from paperbridge_tests.test_paperbridge import sample_pdf


PAPER = {
    'doi': '10.1007/synthetic-engine-selection-91',
    'title': 'Original browser selection and landing fixture',
    'publisher_url': 'https://link.springer.com/article/10.1007/synthetic-engine-selection-91',
}
PDF_URL = 'https://link.springer.com/content/pdf/' + PAPER['doi'] + '.pdf'
PDF = sample_pdf(PAPER['title'] + ' DOI: ' + PAPER['doi'])


class EngineLifecycleTests(unittest.TestCase):
    def driver(self):
        driver = Mock()
        driver.chromium.executable_path = '/synthetic/playwright/chromium/chrome'
        return driver

    def test_engine_switch_closes_browser_and_driver_before_launch_in_both_directions(self):
        events, live = [], set()
        drivers = [self.driver() for _ in range(3)]
        engines = ['firefox', 'chromium', 'firefox']
        for index, (driver, engine) in enumerate(zip(drivers, engines)):
            browser = getattr(driver, engine).launch.return_value
            def launch(*, index=index, browser=browser, **kwargs):
                self.assertFalse(live, 'A previous browser is still alive at launch')
                if index:
                    self.assertIn(('stop', index - 1), events)
                live.add(index)
                events.append(('launch', index))
                return browser
            def close(index=index):
                self.assertIn(index, live)
                live.remove(index)
                events.append(('close', index))
            def stop(index=index):
                self.assertNotIn(index, live)
                events.append(('stop', index))
            getattr(driver, engine).launch.side_effect = launch
            browser.close.side_effect = close
            driver.stop.side_effect = stop
        with patch.dict(os.environ, {}, clear=True), patch('playwright.sync_api.sync_playwright') as factory:
            factory.return_value.start.side_effect = drivers
            runner = ManagedFirefox()
            try:
                contexts = []
                for engine in engines:
                    context = runner.run(lambda context: context, use_publisher_proxy=False, browser_engine=engine)
                    contexts.append(context)
                    self.assertIs(context, runner.run(lambda context: context,
                                                      use_publisher_proxy=False, browser_engine=engine))
                self.assertEqual(len({id(context) for context in contexts}), 3)
            finally:
                runner.close()
        self.assertFalse(live)
        self.assertEqual(events, [(action, index) for index in range(3)
                                  for action in ('launch', 'close', 'stop')])

    def test_network_switch_within_chromium_releases_session_and_reuses_new_context(self):
        first, second = self.driver(), self.driver()
        with patch.dict(os.environ, {}, clear=True), patch('playwright.sync_api.sync_playwright') as factory, \
                patch('paperbridge.managed_browser.publisher_proxy', return_value='http://127.0.0.1:18888'):
            factory.return_value.start.side_effect = [first, second]
            def next_launch(**kwargs):
                first.chromium.launch.return_value.close.assert_called_once()
                first.stop.assert_called_once()
                self.assertIsNone(kwargs['proxy'])
                return second.chromium.launch.return_value
            second.chromium.launch.side_effect = next_launch
            runner = ManagedFirefox()
            try:
                old = runner.run(lambda context: context, browser_engine='chromium')
                self.assertEqual(first.chromium.launch.call_args.kwargs['proxy'], {'server': 'http://127.0.0.1:18888'})
                new = runner.run(lambda context: context, browser_engine='chromium', use_publisher_proxy=False)
                self.assertIsNot(old, new)
                self.assertIs(new, runner.run(lambda context: context, browser_engine='chromium', use_publisher_proxy=False))
                second.chromium.launch.assert_called_once()
            finally:
                runner.close()

    def test_chromium_uses_full_executable_sandbox_and_stripped_environment(self):
        for override in (None, '/operator/owned-browser/chrome'):
            driver = self.driver()
            environment = {'NINOVA_PASSWORD': 'synthetic-password', 'NINOVA_USERNAME': 'synthetic-user',
                           'HTTP_PROXY': 'http://synthetic-user:synthetic-password@proxy.invalid',
                           'HOME': '/synthetic/home', 'LANG': 'C.UTF-8'}
            if override:
                environment['PAPERBRIDGE_CHROMIUM_EXECUTABLE'] = override
            with self.subTest(override=override), patch.dict(os.environ, environment, clear=True), \
                    patch('playwright.sync_api.sync_playwright') as factory:
                factory.return_value.start.return_value = driver
                runner = ManagedFirefox()
                try:
                    runner.run(lambda context: None, browser_engine='chromium', use_publisher_proxy=False)
                    options = driver.chromium.launch.call_args.kwargs
                    self.assertEqual(options['executable_path'], override or driver.chromium.executable_path)
                    self.assertIs(options['chromium_sandbox'], True)
                    self.assertIs(options['headless'], True)
                    self.assertIsNone(options['proxy'])
                    self.assertNotIn('synthetic-password', repr(options))
                    self.assertNotIn('NINOVA_USERNAME', options['env'])
                    self.assertNotIn('HTTP_PROXY', options['env'])
                    self.assertNotIn('PAPERBRIDGE_CHROMIUM_EXECUTABLE', options['env'])
                    driver.firefox.launch.assert_not_called()
                finally:
                    runner.close()

    def test_invalid_engine_is_rejected_before_thread_or_driver_start(self):
        for value in ('chrome', 'Firefox', '', True, [], {}):
            with self.subTest(value=value), patch('playwright.sync_api.sync_playwright') as factory, \
                    patch('paperbridge.managed_browser.Thread') as thread:
                runner = ManagedFirefox()
                try:
                    with self.assertRaises(ValueError):
                        runner.run(lambda context: self.fail('Operation must not run'), browser_engine=value)
                    thread.assert_not_called()
                    factory.assert_not_called()
                finally:
                    runner.close()

    def test_relative_chromium_override_never_launches_a_browser(self):
        with patch.dict(os.environ, {'PAPERBRIDGE_CHROMIUM_EXECUTABLE': 'relative/chrome'}, clear=True), \
                patch('playwright.sync_api.sync_playwright') as factory:
            driver = self.driver()
            factory.return_value.start.return_value = driver
            runner = ManagedFirefox()
            try:
                with self.assertRaises(ValueError):
                    runner.run(lambda context: None, browser_engine='chromium')
                driver.chromium.launch.assert_not_called()
            finally:
                runner.close()

    def test_busy_operation_cannot_start_a_second_engine(self):
        entered, release = threading.Event(), threading.Event()
        output, errors = [], []
        with patch.dict(os.environ, {}, clear=True), patch('playwright.sync_api.sync_playwright') as factory:
            driver = self.driver()
            factory.return_value.start.return_value = driver
            runner = ManagedFirefox()
            def operation(context):
                entered.set()
                self.assertTrue(release.wait(timeout=5))
                return 'finished'
            def call():
                try:
                    output.append(runner.run(operation))
                except Exception as exc:
                    errors.append(exc)
            caller = threading.Thread(target=call)
            caller.start()
            try:
                self.assertTrue(entered.wait(timeout=3))
                with self.assertRaises(RetrievalUnavailable) as error:
                    runner.run(lambda context: self.fail('Busy operation ran'), browser_engine='chromium')
                self.assertEqual(error.exception.reason, 'browser_busy')
                driver.chromium.launch.assert_not_called()
                driver.firefox.launch.return_value.close.assert_not_called()
            finally:
                release.set()
                caller.join(timeout=5)
                runner.close()
            self.assertFalse(caller.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(output, ['finished'])


class ProviderSelectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = DocumentStore(Path(directory.name))
        (self.store.root / 'publisher-policy.json').write_text(json.dumps({'springer': {
            'local_processing': True, 'external_model': True, 'basis': 'Original engine-selection fixture'}}))

    def test_default_springer_remains_compatible_with_legacy_runner_signature(self):
        calls = []
        class LegacyRunner:
            def run(self, operation, *, use_publisher_proxy=True):
                calls.append(use_publisher_proxy)
                return operation(Mock())
        provider = SpringerBrowserProvider(self.store, runner=LegacyRunner())
        with patch.object(provider, '_read_page', return_value=PDF):
            result = provider.retrieve(PAPER)
        self.assertEqual(calls, [False])
        self.assertEqual(result['metadata']['pdf_identity'], 'first_page_doi_and_title')

    def test_chromium_provider_passes_only_its_explicit_engine_and_default_network(self):
        runner = Mock()
        runner.run.side_effect = lambda operation, **kwargs: operation(Mock())
        provider = SpringerBrowserProvider(self.store, runner=runner, browser_engine='chromium')
        with patch.object(provider, '_read_page', return_value=PDF):
            result = provider.retrieve(PAPER)
        self.assertEqual(runner.run.call_args.kwargs, {'use_publisher_proxy': False, 'browser_engine': 'chromium'})
        self.assertEqual(result['metadata']['pdf_identity'], 'first_page_doi_and_title')

    def test_invalid_provider_engine_is_rejected_without_running(self):
        for engine in ('Chrome', False, [], {}):
            runner = Mock()
            with self.subTest(engine=engine), self.assertRaises(ValueError):
                SpringerBrowserProvider(self.store, runner=runner, browser_engine=engine)
            runner.run.assert_not_called()

    def test_springer_setting_does_not_switch_ieee_or_create_a_second_runner(self):
        for requested in (None, 'chromium'):
            env = {'PAPERBRIDGE_BROWSER_ENGINE': 'firefox'}
            if requested:
                env['PAPERBRIDGE_SPRINGER_BROWSER_ENGINE'] = requested
            with self.subTest(requested=requested), patch.dict(os.environ, env, clear=True), \
                    patch('paperbridge.managed_browser.ManagedBrowser') as manager, \
                    patch('paperbridge.ieee.IeeeBrowserProvider') as ieee, \
                    patch('paperbridge.springer.SpringerBrowserProvider') as springer:
                bridge = PaperBridge(self.store, Mock())
                manager.assert_called_once()
                self.assertIs(ieee.call_args.kwargs['runner'], manager.return_value)
                self.assertNotIn('browser_engine', ieee.call_args.kwargs)
                self.assertIs(springer.call_args.kwargs['runner'], manager.return_value)
                self.assertEqual(springer.call_args.kwargs['browser_engine'], requested)
                self.assertEqual(bridge.providers, [ieee.return_value, springer.return_value])

    def test_invalid_springer_setting_fails_before_a_browser_or_discovery(self):
        for engine in ('chrome', '', 'FIREFOX'):
            discovery = Mock()
            with self.subTest(engine=engine), patch.dict(os.environ, {
                    'PAPERBRIDGE_BROWSER_ENGINE': 'firefox', 'PAPERBRIDGE_SPRINGER_BROWSER_ENGINE': engine}, clear=True), \
                    patch('playwright.sync_api.sync_playwright') as browser, self.assertRaises(ValueError):
                PaperBridge(self.store, discovery)
            browser.assert_not_called()
            discovery.lookup.assert_not_called()


@unittest.skipUnless(os.environ.get('PAPERBRIDGE_BROWSER_TESTS') == '1', 'Opt-in real Chromium fixture')
class ManagedChromiumSpringerFixtureTests(unittest.TestCase):
    def test_locked_article_uses_one_synthetic_login_and_downloads_original_fixture_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DocumentStore(Path(directory))
            (store.root / 'publisher-policy.json').write_text(json.dumps({'springer': {
                'local_processing': True, 'external_model': True, 'basis': 'Original local Chromium fixture'}}))
            runner = ManagedFirefox()
            state = {'logins': 0, 'downloads': 0, 'unexpected_urls': []}
            try:
                def setup(context):
                    def fixture(route):
                        url = route.request.url
                        cookie = route.request.all_headers().get('cookie', '')
                        if url == PAPER['publisher_url']:
                            body = (f'<a href="{PDF_URL}">Original PDF</a>' if 'pb_engine_fixture=allowed' in cookie
                                    else '<h1>Original locked article fixture</h1>')
                            route.fulfill(status=200, content_type='text/html', body=body)
                        elif url == PDF_URL:
                            self.assertIn('pb_engine_fixture=allowed', cookie)
                            state['downloads'] += 1
                            route.fulfill(status=200, content_type='application/pdf', body=PDF)
                        else:
                            state['unexpected_urls'].append(url)
                            route.abort()
                    context.route('**/*', fixture)
                runner.run(setup, use_publisher_proxy=False, browser_engine='chromium')
                def login(context, article):
                    self.assertEqual(article, PAPER['publisher_url'])
                    state['logins'] += 1
                    context.add_cookies([{'name': 'pb_engine_fixture', 'value': 'allowed',
                                         'url': 'https://link.springer.com/'}])
                provider = SpringerBrowserProvider(store, runner=runner, login=login, browser_engine='chromium')
                with patch('requests.Session') as http_client:
                    result = provider.retrieve(PAPER)
                http_client.assert_not_called()
                self.assertEqual(state, {'logins': 1, 'downloads': 1, 'unexpected_urls': []})
                self.assertIn(PAPER['title'], result['text'])
                self.assertEqual(result['metadata']['pdf_identity'], 'first_page_doi_and_title')
                cookies = runner.run(lambda context: context.cookies([PAPER['publisher_url']]),
                                     use_publisher_proxy=False, browser_engine='chromium')
                self.assertTrue(any(row['name'] == 'pb_engine_fixture' for row in cookies))
            finally:
                runner.close()


if __name__ == '__main__':
    unittest.main()
