"""Springer Nature Link retrieval with permission checks and ITU session support."""
from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from .browser import RetrievalUnavailable
from .identifiers import doi_url, normalize_doi
from .pdf_identity import verify_pdf_identity
from .pdf_transfer import browser_pdf
from .permissions import read_rights


ARTICLE_HOSTS = frozenset({'link.springer.com', 'link.springernature.com'})
LOGIN_HOSTS = ARTICLE_HOSTS | {'sp.springer.com', 'fsso.springer.com', 'idp.springer.com', 'wayf.springernature.com'}


def springer_url(url: str, *, login=False) -> bool:
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and p.hostname in (LOGIN_HOSTS if login else ARTICLE_HOSTS)
                and p.port in (None, 443) and not p.username and not p.password
                and not any(ord(c) < 33 for c in url) and '\\' not in url)
    except (TypeError, ValueError):
        return False


def article_url(paper: dict) -> str:
    """Accept only a canonical DOI path on a supported publisher host."""
    url = paper.get('publisher_url') or ''
    # Older Crossref records advertise HTTP landing pages. Upgrade only this
    # metadata form before any request; PDF and login URL guards remain HTTPS-only.
    p = urlsplit(url)
    if (p.scheme == 'http' and p.hostname in ARTICLE_HOSTS
            and p.port in (None, 80) and not p.username and not p.password
            and not p.query and not p.fragment and '\\' not in url
            and not any(ord(c) < 33 for c in url)):
        url = 'https://' + p.hostname + p.path
    if not springer_url(url):
        raise RetrievalUnavailable('publisher_article_url_unavailable')
    p = urlsplit(url)
    path = unquote(p.path).lstrip('/')
    if path.startswith(('article/', 'chapter/')):
        path = path.split('/', 1)[1]
    try:
        matches = normalize_doi(path) == normalize_doi(paper['doi'])
    except (ValueError, KeyError, AttributeError):
        matches = False
    if not matches or p.query or p.fragment:
        raise RetrievalUnavailable('publisher_article_url_unavailable')
    return url


def pdf_link(url: str, doi: str) -> bool:
    if not springer_url(url):
        return False
    p = urlsplit(url)
    return (unquote(p.path).casefold() == '/content/pdf/' + normalize_doi(doi) + '.pdf'
            and not p.query and not p.fragment)


class SpringerBrowserProvider:
    name = 'springer'

    def __init__(self, store, *, runner, login=None, browser_engine=None):
        if browser_engine not in (None, "firefox", "chromium"):
            raise ValueError("Springer browser engine must be firefox, chromium or None.")
        self.store = store
        self.runner = runner
        self.login = login
        self.browser_engine = browser_engine

    @staticmethod
    def supports(paper: dict) -> bool:
        try:
            article_url(paper)
            return True
        except (RetrievalUnavailable, TypeError, ValueError):
            return False

    def rights(self, doi):
        return read_rights(self.store, doi, self.name)

    def _read_page(self, context, paper):
        page = context.new_page()
        page.route('**/*', lambda route: route.fallback() if springer_url(route.request.url) else route.abort())
        try:
            response = page.goto(article_url(paper), wait_until='domcontentloaded', timeout=30000)
            if not response or response.status != 200:
                raise RetrievalUnavailable('publisher_interaction_required')
            if page.title().strip().casefold() in {'client challenge', 'just a moment...', 'just a moment…', 'attention required! | cloudflare'}:
                # A 200 HTML response can still be a publisher verification page.
                # It is not evidence of missing subscription and must not trigger login.
                raise RetrievalUnavailable('publisher_interaction_required')
            # The DOI redirect can change the article/chapter path, never the paper.
            article_url(dict(paper, publisher_url=page.url))
            candidates = page.locator('a[href],meta[name="citation_pdf_url"]').evaluate_all(
                'nodes => nodes.map(n => n.href || n.content)')
            for candidate in candidates:
                url = urljoin(page.url, candidate or '')
                if pdf_link(url, paper['doi']):
                    return browser_pdf(page, url, lambda value: pdf_link(value, paper['doi']))
            raise RetrievalUnavailable('publisher_pdf_link_unavailable')
        finally:
            page.close()

    def retrieve(self, paper):
        from filelock import FileLock, Timeout
        rights = self.rights(paper['doi'])
        article = article_url(paper)
        try:
            with FileLock(str(self.store.root / '.browser.lock'), timeout=0):
                def acquire(context):
                    try:
                        return self._read_page(context, paper)
                    except RetrievalUnavailable as exc:
                        if not self.login or exc.reason not in {
                            'publisher_pdf_link_unavailable', 'publisher_did_not_return_pdf',
                            'publisher_access_unavailable',
                        }:
                            raise
                        self.login(context, article)
                        return self._read_page(context, paper)
                # The existing dedicated IEEE proxy does not serve Springer.
                # Keep its configuration intact and use the current default route.
                options = {"use_publisher_proxy": False}
                if self.browser_engine is not None:
                    options["browser_engine"] = self.browser_engine
                data = self.runner.run(acquire, **options)
                identity = verify_pdf_identity(data, paper)
                with tempfile.TemporaryDirectory(dir=self.store.root) as directory:
                    path = Path(directory) / 'article.pdf'
                    with path.open('xb') as stream:
                        path.chmod(0o600)
                        stream.write(data)
                    document = self.store.ingest(path, title=paper['title'],
                        url=doi_url(paper['doi']), rights=rights)
                result = self.store.fetch(document['first_chunk_id'])
                result['metadata'].update(retrieval='springer_browser_session',
                    institutional_entitlement='not_verified', pdf_identity=identity)
                return result
        except Timeout:
            raise RetrievalUnavailable('browser_busy') from None
