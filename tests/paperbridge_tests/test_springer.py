from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from paperbridge import DocumentStore, PaperBridge
from paperbridge.browser import RetrievalUnavailable
from paperbridge.ieee import IeeeBrowserProvider
from paperbridge.managed_browser import ManagedFirefox
from paperbridge.pdf_identity import verify_pdf_identity
from paperbridge.springer import SpringerBrowserProvider, article_url, pdf_link, springer_url
from paperbridge.springer_login import springer_login_url
from paperbridge_tests.test_paperbridge import sample_pdf


PAPER = {'doi':'10.1007/synthetic-123', 'title':'Original Springer landing fixture',
         'publisher_url':'https://link.springer.com/10.1007/synthetic-123'}
PDF_URL = 'https://link.springer.com/content/pdf/10.1007/synthetic-123.pdf'
PDF = sample_pdf(PAPER['title']+' DOI: '+PAPER['doi'])


class SpringerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DocumentStore(Path(self.temp.name))
        self.runner = Mock()
        self.runner.run.side_effect = lambda operation, **_: operation(Mock())
        self.provider = SpringerBrowserProvider(self.store, runner=self.runner)

    def allow(self, provider='springer'):
        (self.store.root/'publisher-policy.json').write_text(json.dumps({provider: {
            'local_processing':True,'external_model':True,'basis':'Original synthetic fixture'}}))

    def test_article_routing_requires_doi_host_and_path_identity(self):
        self.assertEqual(article_url(PAPER), PAPER['publisher_url'])
        for path in ['/article/10.1007/synthetic-123','/chapter/10.1007/synthetic-123']:
            self.assertTrue(self.provider.supports(dict(PAPER,publisher_url='https://link.springernature.com'+path)))
        for url in ['https://link.springer.com.evil.test/10.1007/synthetic-123',
                    'https://user:secret@link.springer.com/10.1007/synthetic-123',
                    'https://link.springer.com:444/10.1007/synthetic-123',
                    'https://link.springer.com/10.1007/other',
                    'https://link.springer.com/10.1007/synthetic-123?redirect=https://evil.test',
                    'https://link.springer.com/login',
                    'https://link.springer.com/10.1007/synthetic-123#fragment']:
            with self.subTest(url=url):
                self.assertFalse(self.provider.supports(dict(PAPER,publisher_url=url)))

    def test_legacy_metadata_upgrades_before_read_and_keeps_download_guards(self):
        self.allow()
        for host in ['link.springer.com', 'link.springernature.com']:
            for port in ['', ':80']:
                for path in ['/10.1007/synthetic-123', '/article/10.1007/synthetic-123',
                             '/chapter/10.1007/synthetic-123']:
                    paper = dict(PAPER, publisher_url='http://' + host + port + path)
                    with self.subTest(url=paper['publisher_url']):
                        expected = 'https://' + host + path
                        self.assertEqual(article_url(paper), expected)
                        self.assertTrue(self.provider.supports(paper))
        self.assertFalse(springer_url('http://link.springer.com/10.1007/synthetic-123'))
        self.assertFalse(pdf_link(PDF_URL.replace('https:', 'http:'), PAPER['doi']))

    def test_legacy_metadata_rejects_changed_identity_and_unsafe_authorities(self):
        for url in ['http://link.springer.com.evil.test/10.1007/synthetic-123',
                    'http://user:secret@link.springer.com/10.1007/synthetic-123',
                    'http://link.springer.com:443/10.1007/synthetic-123',
                    'http://link.springer.com:bad/10.1007/synthetic-123',
                    'http://link.springer.com/10.1007/other',
                    'http://link.springer.com/10.1007/synthetic-123?next=other',
                    'http://link.springer.com/10.1007/synthetic-123#other',
                    'http://link.springer.com/10.1007/synthetic-123\n',
                    'http://link.springer.com\\@evil.test/10.1007/synthetic-123']:
            with self.subTest(url=url):
                self.assertFalse(self.provider.supports(dict(PAPER, publisher_url=url)))

    def test_pdf_link_cannot_select_other_paper_origin_or_endpoint(self):
        self.assertTrue(pdf_link(PDF_URL,PAPER['doi']))
        for url in [PDF_URL.replace('synthetic-123','other'), PDF_URL.replace('link.springer.com','evil.test'),
                    PDF_URL+'?redirect=other', PDF_URL.replace('/content/pdf/','/article/')]:
            with self.subTest(url=url):self.assertFalse(pdf_link(url,PAPER['doi']))
        self.assertFalse(springer_url('https://sp.springer.com/saml/login'))
        self.assertTrue(springer_url('https://fsso.springer.com/saml',login=True))
        self.assertTrue(springer_url('https://idp.springer.com/saml',login=True))
        with self.assertRaises(ValueError):springer_login_url('https://evil.test/return')

    def test_no_policy_prevents_lookup_and_browser(self):
        discovery = Mock()
        result = PaperBridge(self.store,discovery,provider=self.provider).get_text(PAPER['doi'])
        self.assertEqual(result['reason'],'document_permission_required')
        discovery.lookup.assert_not_called()
        self.runner.run.assert_not_called()

    def test_ieee_policy_does_not_authorize_springer(self):
        self.allow('ieee')
        discovery = Mock()
        discovery.lookup.return_value = PAPER
        bridge = PaperBridge(self.store,discovery,provider=IeeeBrowserProvider(self.store,None))
        bridge.providers.append(self.provider)
        result = bridge.get_text(PAPER['doi'])
        self.assertEqual(result['reason'],'document_permission_required')
        self.runner.run.assert_not_called()

    def test_document_denial_overrides_publisher_permission_before_browser(self):
        self.allow()
        (self.store.root/'publisher-rights.json').write_text(json.dumps({PAPER['doi']:{
            'local_processing':True,'external_model':False,'basis':'Explicit synthetic denial'}}))
        with patch.object(self.provider,'_read_page') as read, self.assertRaises(RetrievalUnavailable):
            self.provider.retrieve(PAPER)
        read.assert_not_called()
        self.runner.run.assert_not_called()

    def test_pdf_cache_and_identity_provenance(self):
        self.allow()
        with patch.object(self.provider,'_read_page',return_value=PDF):
            result = self.provider.retrieve(PAPER)
        self.assertEqual(result['metadata']['pdf_identity'],'first_page_doi_and_title')
        self.assertEqual(result['metadata']['retrieval'],'springer_browser_session')
        self.assertFalse(self.runner.run.call_args.kwargs['use_publisher_proxy'])
        discovery=Mock()
        self.assertEqual(PaperBridge(self.store,discovery).get_text(PAPER['doi'])['text'],result['text'])
        discovery.lookup.assert_not_called()
        self.assertEqual(list(self.store.root.glob('*/article.pdf')),[])

    def test_login_occurs_once_then_pdf_is_retried(self):
        self.allow()
        self.provider.login=Mock()
        with patch.object(self.provider,'_read_page',side_effect=[
                RetrievalUnavailable('publisher_pdf_link_unavailable'),PDF]) as read:
            result=self.provider.retrieve(PAPER)
        self.assertEqual(read.call_count,2)
        self.provider.login.assert_called_once()
        self.assertTrue(result['id'].startswith('pb:'))

    def test_legacy_metadata_uses_https_for_institution_login(self):
        self.allow()
        self.provider.login = Mock()
        paper = dict(PAPER, publisher_url=PAPER['publisher_url'].replace('https:', 'http:'))
        with patch.object(self.provider, '_read_page', side_effect=[
                RetrievalUnavailable('publisher_pdf_link_unavailable'), PDF]):
            result = self.provider.retrieve(paper)
        self.assertEqual(self.provider.login.call_args.args[1], PAPER['publisher_url'])
        self.assertEqual(result['metadata']['pdf_identity'], 'first_page_doi_and_title')

    def test_challenge_and_repeated_access_failure_never_loop_or_archive(self):
        self.allow()
        for reason, logins in [('publisher_interaction_required',0),('publisher_pdf_link_unavailable',1)]:
            self.provider.login=Mock()
            with self.subTest(reason=reason), patch.object(self.provider,'_read_page',side_effect=RetrievalUnavailable(reason)), self.assertRaises(RetrievalUnavailable):
                self.provider.retrieve(PAPER)
            self.assertEqual(self.provider.login.call_count,logins)
        self.assertEqual(self.store.search('landing'),[])

    def test_same_title_with_longer_doi_is_not_a_verified_identity(self):
        for doi in [PAPER['doi']+'4',PAPER['doi']+'.other',PAPER['doi']+'/other']:
            with self.subTest(doi=doi), self.assertRaises(RetrievalUnavailable):
                verify_pdf_identity(sample_pdf(PAPER['title']+' DOI: '+doi),PAPER)
        self.assertEqual(verify_pdf_identity(PDF,PAPER),'first_page_doi_and_title')


@unittest.skipUnless(os.environ.get('PAPERBRIDGE_BROWSER_TESTS')=='1','Opt-in real Firefox fixture')
class SpringerFirefoxTests(unittest.TestCase):
    def test_saml_callback_posts_once_to_itu_and_accepts_federation_return(self):
        from types import SimpleNamespace
        from playwright.sync_api import sync_playwright
        from paperbridge.springer_login import authenticate_springer
        from test_itu_login import LOGIN
        source=Mock(return_value=SimpleNamespace(username='synthetic-user@itu.edu.tr',password='synthetic-password'))
        # New scripted navigations are routed as separate fixture requests.
        # Playwright does not reliably re-intercept an HTTP redirect chain.
        posts=[]
        with sync_playwright() as p:
            browser=p.firefox.launch(headless=True)
            try:
                context=browser.new_context()
                def fixture(route):
                    request=route.request
                    if request.url.startswith('https://sp.springer.com/'):
                        route.fulfill(status=200,content_type='text/html',body='<script>location.replace('+json.dumps(LOGIN)+')</script>')
                    elif request.url==LOGIN:
                        if request.method=='POST':
                            posts.append(request.url)
                            self.assertIn('j_username=synthetic-user',request.post_data)
                            self.assertIn('j_password=synthetic-password',request.post_data)
                            route.fulfill(status=200,content_type='text/html',body='<script>location.replace("https://fsso.springer.com/return")</script>')
                        else:
                            route.fulfill(status=200,content_type='text/html',body='<form method="post"><input name="j_username"><input type="password" name="j_password"><button name="_eventId_proceed" type="submit">Login</button></form>')
                    elif request.url=='https://fsso.springer.com/return':
                        route.fulfill(status=200,content_type='text/html',body='<script>location.replace("https://idp.springer.com/return")</script>')
                    elif request.url=='https://idp.springer.com/return':
                        route.fulfill(status=200,content_type='text/html',body='<script>location.replace('+json.dumps(PAPER['publisher_url'])+')</script>')
                    elif request.url==PAPER['publisher_url']:
                        route.fulfill(status=200,content_type='text/html',body='Original authenticated fixture')
                    else:
                        self.fail('Unexpected synthetic fixture request: '+request.url)
                context.route('**/*',fixture)
                authenticate_springer(context,PAPER['publisher_url'],source)
            finally:
                browser.close()
        source.assert_called_once_with()
        self.assertEqual(posts,[LOGIN])

    def test_http_200_client_challenge_never_triggers_institution_login(self):
        with tempfile.TemporaryDirectory() as directory:
            store=DocumentStore(directory)
            (store.root/'publisher-policy.json').write_text(json.dumps({'springer':{
                'local_processing':True,'external_model':True,'basis':'Original fixture'}}))
            runner=ManagedFirefox();login=Mock()
            provider=SpringerBrowserProvider(store,runner=runner,login=login)
            try:
                def setup(context):
                    context.route('**/*',lambda route:route.fulfill(status=200,
                        content_type='text/html',body='<title>Client Challenge</title><form><input name="answer"></form>'))
                runner.run(setup,use_publisher_proxy=False)
                with self.assertRaises(RetrievalUnavailable) as error:provider.retrieve(PAPER)
                self.assertEqual(error.exception.reason,'publisher_interaction_required')
                login.assert_not_called()
                self.assertEqual(store.search('fixture'),[])
            finally:runner.close()

    def test_locked_page_uses_one_itu_login_and_browser_native_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            store=DocumentStore(directory)
            (store.root/'publisher-policy.json').write_text(json.dumps({'springer':{
                'local_processing':True,'external_model':True,'basis':'Original browser fixture'}}))
            runner=ManagedFirefox()
            provider=SpringerBrowserProvider(store,runner=runner)
            state={'authenticated':False,'downloads':0,'logins':0}
            try:
                def setup(context):
                    def fixture(route):
                        url=route.request.url
                        if url==PDF_URL:
                            self.assertTrue(state['authenticated'])
                            state['downloads']+=1
                            route.fulfill(status=200,content_type='application/pdf',body=PDF)
                        else:
                            body=f'<a href="{PDF_URL}">PDF</a>' if state['authenticated'] else '<h1>Access through institution</h1>'
                            route.fulfill(status=200,content_type='text/html',body=body)
                    context.route('**/*',fixture)
                runner.run(setup,use_publisher_proxy=False)
                def login(context,article):
                    self.assertEqual(article,PAPER['publisher_url'])
                    state['logins']+=1
                    state['authenticated']=True
                provider.login=login
                with patch('requests.Session') as requests_factory:
                    result=provider.retrieve(PAPER)
                    requests_factory.assert_not_called()
                self.assertEqual(state,{'authenticated':True,'downloads':1,'logins':1})
                self.assertEqual(result['metadata']['pdf_identity'],'first_page_doi_and_title')
            finally:
                runner.close()
