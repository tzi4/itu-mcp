"""Original deterministic fixtures for the observed rejected-page close stall."""
import concurrent.futures
from contextlib import contextmanager
import sys
import threading
import types
import unittest
from unittest.mock import patch

from paperbridge.browser import RetrievalUnavailable
from paperbridge.ieee import IeeeBrowserProvider
from paperbridge.managed_browser import ManagedFirefox


class BrowserError(Exception):
    pass


class BrowserTimeout(BrowserError):
    pass


class FastFuture(concurrent.futures.Future):
    def result(self, timeout=None):
        return super().result(timeout=min(timeout, 1.0) if timeout is not None else None)


class FakePage:
    def __init__(self, status=418, block_close=False, close_error=False):
        self.status=status
        self.block_close=block_close
        self.close_error=close_error
        self.close_calls=0
        self.close_entered=threading.Event()
        self.close_release=threading.Event()
        self.pending_reason=None
        self.routes=[]
        self.main_frame=object()
        request=types.SimpleNamespace(is_navigation_request=lambda:True)
        self.response=types.SimpleNamespace(status=status,frame=self.main_frame,
            request=request,url='https://ieeexplore.ieee.org/document/123456/')

    def route(self, pattern, handler):
        self.routes.append((pattern,handler))

    @contextmanager
    def expect_response(self, predicate, timeout):
        assert predicate(self.response)
        yield types.SimpleNamespace(value=self.response)

    def goto(self, *args, **kwargs):
        return self.response

    def locator(self, selector):
        return types.SimpleNamespace(first=types.SimpleNamespace(wait_for=lambda **kw:None),
                                     evaluate_all=lambda code:[])

    def close(self):
        self.close_calls+=1
        self.pending_reason=getattr(sys.exception(),'reason',None)
        self.close_entered.set()
        if self.block_close:
            assert self.close_release.wait(2), 'Fixture close was not released'
        if self.close_error:
            raise BrowserError('Original synthetic close failure')


class FakeBrowser:
    def __init__(self, page):
        self.close_calls=0
        self.close_error=False
        self.close_entered=threading.Event()
        self.close_release=None
        self.closed=False
        self.context=types.SimpleNamespace(new_page=lambda:page,browser=self)
        self.owner_threads=[]

    def new_context(self, **kwargs):
        self.owner_threads.append(threading.get_ident())
        return self.context

    def close(self):
        self.owner_threads.append(threading.get_ident())
        self.close_calls+=1
        self.close_entered.set()
        if self.close_release is not None:
            assert self.close_release.wait(2), 'Fixture browser was not released'
        if self.close_error:
            raise BrowserError('Original synthetic browser close failure')
        self.closed=True


class FakeDriver:
    def __init__(self, browser):
        self.browser=browser
        self.launches=0
        self.stops=0
        self.owner_threads=[]
        self.firefox=types.SimpleNamespace(launch=self.launch)
        self.chromium=types.SimpleNamespace(launch=self.launch,executable_path='/synthetic/chrome')

    def launch(self, **kwargs):
        self.owner_threads.append(threading.get_ident())
        self.launches+=1
        return self.browser

    def stop(self):
        self.owner_threads.append(threading.get_ident())
        self.stops+=1
        self.browser.closed=True


@contextmanager
def fake_playwright(drivers):
    pending=iter(drivers)
    sync=types.ModuleType('playwright.sync_api')
    sync.Error=BrowserError;sync.TimeoutError=BrowserTimeout
    sync.sync_playwright=lambda:types.SimpleNamespace(start=lambda:next(pending))
    package=types.ModuleType('playwright');package.__path__=[];package.sync_api=sync
    with patch.dict(sys.modules,{'playwright':package,'playwright.sync_api':sync}), \
            patch('paperbridge.managed_browser.Future',FastFuture), \
            patch('paperbridge.managed_browser.publisher_proxy',return_value=None):
        yield


class RejectedBrowserCleanupTests(unittest.TestCase):
    def rejection(self, runner):
        provider=IeeeBrowserProvider(None,None,runner=runner)
        return runner.run(lambda context:provider._read_page(context,
            'https://ieeexplore.ieee.org/document/123456/','123456'))

    def test_hanging_page_close_does_not_replace_known_rejection_and_next_call_reopens(self):
        page=FakePage(block_close=True)
        old,new=FakeDriver(FakeBrowser(page)),FakeDriver(FakeBrowser(FakePage()))
        with fake_playwright([old,new]):
            runner=ManagedFirefox()
            try:
                with self.assertRaises(RetrievalUnavailable) as caught:
                    self.rejection(runner)
                self.assertEqual(caught.exception.reason,'publisher_interaction_required')
                self.assertEqual(page.close_calls,0)
                self.assertEqual(old.browser.close_calls,1)
                self.assertEqual(old.stops,1)
                self.assertEqual(len(page.routes),1,'The allowlist must remain installed')
                self.assertIs(runner.run(lambda context:context),new.browser.context)
                self.assertTrue(old.browser.closed)
                self.assertEqual(new.launches,1)
            finally:
                page.close_release.set();runner.close()

    def test_browser_close_error_does_not_mask_rejection_and_driver_is_stopped(self):
        page=FakePage(close_error=True)
        old,new=FakeDriver(FakeBrowser(page)),FakeDriver(FakeBrowser(FakePage()))
        old.browser.close_error=True
        with fake_playwright([old,new]):
            runner=ManagedFirefox()
            try:
                with self.assertRaises(RetrievalUnavailable) as caught:self.rejection(runner)
                self.assertEqual(caught.exception.reason,'publisher_interaction_required')
                self.assertEqual(old.stops,1)
                self.assertTrue(old.browser.closed)
                self.assertIs(runner.run(lambda context:context),new.browser.context)
            finally:runner.close()

    def test_busy_and_owner_thread_are_preserved_until_browser_discard_finishes(self):
        page=FakePage();driver=FakeDriver(FakeBrowser(page))
        driver.browser.close_release=threading.Event()
        errors=[]
        with fake_playwright([driver]):
            runner=ManagedFirefox()
            def request():
                try:self.rejection(runner)
                except RetrievalUnavailable as error:errors.append(error.reason)
            caller=threading.Thread(target=request)
            caller.start()
            try:
                self.assertTrue(driver.browser.close_entered.wait(.5),'Owned browser cleanup never began')
                self.assertTrue(caller.is_alive(),'Failure was published before cleanup finished')
                with self.assertRaises(RetrievalUnavailable) as caught:runner.run(lambda context:self.fail('Concurrent work ran'))
                self.assertEqual(caught.exception.reason,'browser_busy')
                self.assertEqual(driver.launches,1)
            finally:
                driver.browser.close_release.set();caller.join(2);runner.close()
            self.assertEqual(errors,['publisher_interaction_required'])
            self.assertFalse(caller.is_alive())
            owner_threads=driver.owner_threads+driver.browser.owner_threads
            self.assertEqual(len(set(owner_threads)),1)
            self.assertNotEqual(owner_threads[0],threading.get_ident())

    def test_missing_pdf_keeps_page_cleanup_and_the_reusable_session(self):
        page=FakePage(status=200);driver=FakeDriver(FakeBrowser(page))
        with fake_playwright([driver]):
            runner=ManagedFirefox()
            try:
                with self.assertRaises(RetrievalUnavailable) as caught:self.rejection(runner)
                self.assertEqual(caught.exception.reason,'publisher_pdf_link_unavailable')
                self.assertEqual(page.close_calls,1)
                self.assertEqual(driver.browser.close_calls,0)
                self.assertIs(runner.run(lambda context:context),driver.browser.context)
                self.assertEqual(driver.launches,1)
            finally:runner.close()

    def test_legacy_cdp_and_custom_runner_do_not_gain_browser_ownership(self):
        for legacy_runner in (None,types.SimpleNamespace(run=lambda operation:None)):
            with self.subTest(legacy=legacy_runner),fake_playwright([]):
                page=FakePage();browser=FakeBrowser(page)
                provider=IeeeBrowserProvider(None,9222,runner=legacy_runner)
                with self.assertRaises(RetrievalUnavailable) as caught:
                    provider._read_page(browser.context,'https://ieeexplore.ieee.org/document/123456/','123456')
                self.assertEqual(caught.exception.reason,'publisher_interaction_required')
                self.assertEqual(page.close_calls,1)
                self.assertEqual(browser.close_calls,0)


if __name__=='__main__':unittest.main()
