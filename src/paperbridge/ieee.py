"""Experimental IEEE retrieval using an operator's existing browser session.

Only observed IEEE PDF links are followed. An authenticated download alone is
not evidence that institutional entitlement, rather than open access, was used.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

from .browser import RetrievalUnavailable, connected_session
from .identifiers import doi_url
from .network import publisher_proxy
from .storage import Rights


MAX_PDF_BYTES = 20_000_000
IEEE_HOST = "ieeexplore.ieee.org"


def ieee_url(url: str) -> bool:
    try:
        value = urlsplit(url)
        return (value.scheme == "https" and value.hostname == IEEE_HOST
                and value.port in (None, 443) and not value.username and not value.password
                and not any(c in url for c in ("\\", "\r", "\n", "\t")))
    except ValueError:
        return False


def article_url(paper: dict) -> tuple[str, str]:
    url = paper.get("publisher_url") or ""
    parsed = urlsplit(url)
    match = re.fullmatch(r"/document/([0-9]{1,12})/?", parsed.path)
    # Older Crossref records still advertise a plain-HTTP Xplore landing URL.
    # Upgrade this exact metadata form locally; never make an HTTP request or
    # extend the accepted download/login origins to insecure transport.
    legacy = (parsed.scheme == "http" and parsed.hostname == IEEE_HOST
              and parsed.port in (None, 80) and not parsed.username and not parsed.password
              and not parsed.query and not parsed.fragment and "\\" not in url
              and not any(ord(c) < 33 for c in url))
    if not (ieee_url(url) or legacy) or not match:
        raise RetrievalUnavailable("publisher_article_url_unavailable")
    return f"https://{IEEE_HOST}/document/{match[1]}/", match[1]


def download_link(url: str, article_id: str) -> bool:
    if not ieee_url(url):
        return False
    parsed = urlsplit(url)
    if parsed.path in {"/stamp/stamp.jsp", "/stampPDF/getPDF.jsp"}:
        return parse_qs(parsed.query).get("arnumber") == [article_id]
    # IEEE may use zero-padded numeric PDF names in its /iel... content tree.
    match = re.fullmatch(r"/iel[^/]*/(?:[^/]+/)*(\d+)\.pdf", parsed.path)
    return bool(match and int(match[1]) == int(article_id))


def article_request_allowed(request) -> bool:
    if ieee_url(request.url):
        return True
    # Allow the publisher's normal AWS browser verification subresources to run.
    # This does not solve CAPTCHAs, fabricate tokens, or allow off-site navigation.
    try:
        parsed = urlsplit(request.url)
        return (not request.is_navigation_request() and parsed.scheme == "https"
                and (parsed.hostname or "").endswith(".awswaf.com")
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not any(ord(c) < 33 for c in request.url) and "\\" not in request.url)
    except ValueError:
        return False


def verify_pdf(data: bytes, paper: dict, *, retrieved_article_id: str | None = None) -> str:
    from .pdf_identity import verify_pdf_identity
    expected_id = article_url(paper)[1] if retrieved_article_id else None
    return verify_pdf_identity(data, paper, max_bytes=MAX_PDF_BYTES,
        retrieved_article_id=retrieved_article_id, expected_article_id=expected_id)


class IeeeBrowserProvider:
    name = "ieee"

    @staticmethod
    def supports(paper: dict) -> bool:
        """Route by validated Xplore article metadata, not a DOI prefix."""
        try:
            article_url(paper)
            return True
        except (RetrievalUnavailable, ValueError, TypeError):
            return False

    def __init__(self, store, port: int | None, *, login=None, runner=None):
        self.store = store
        self.port = port
        self.login = login
        self.runner = runner

    def rights(self, doi: str) -> Rights:
        from .permissions import read_rights
        return read_rights(self.store, doi, self.name)

    @staticmethod
    def _download(context, url: str, article_id: str) -> bytes:
        with requests.Session() as session:
            session.trust_env = False
            proxy = publisher_proxy()
            if proxy:
                session.proxies.update({"http": proxy, "https": proxy})
            for _ in range(4):
                if not download_link(url, article_id):
                    raise RetrievalUnavailable("publisher_redirect_not_allowed")
                # Scoped cookies stay in memory. No cookies or signed URLs are logged.
                session.cookies.clear()
                for cookie in context.cookies([url]):
                    session.cookies.set(cookie["name"], cookie["value"],
                                        domain=cookie["domain"], path=cookie["path"],
                                        secure=cookie["secure"])
                try:
                    with session.get(url, stream=True, allow_redirects=False, timeout=(5, 25)) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            url = urljoin(url, response.headers.get("Location", ""))
                            continue
                        if response.status_code != 200:
                            raise RetrievalUnavailable("publisher_access_unavailable")
                        data = bytearray()
                        for chunk in response.iter_content(65536):
                            data.extend(chunk)
                            if len(data) > MAX_PDF_BYTES:
                                raise RetrievalUnavailable("pdf_size_limit_exceeded")
                        if not data.startswith(b"%PDF-"):
                            raise RetrievalUnavailable("publisher_did_not_return_pdf")
                        return bytes(data)
                except requests.RequestException:
                    raise RetrievalUnavailable("publisher_download_failed") from None
        raise RetrievalUnavailable("publisher_redirect_limit_exceeded")

    def retrieve(self, paper: dict) -> dict:
        from filelock import FileLock, Timeout

        rights = self.rights(paper["doi"])
        url, article_id = article_url(paper)
        try:
            with FileLock(str(self.store.root / ".browser.lock"), timeout=0):
                def acquire(context):
                    try:
                        return self._read_page(context, url, article_id)
                    except RetrievalUnavailable as exc:
                        if not self.login or exc.reason not in {
                            "publisher_pdf_link_unavailable", "publisher_did_not_return_pdf",
                            "publisher_access_unavailable", "publisher_redirect_not_allowed"
                        }:
                            raise
                        self.login(context, url)
                        return self._read_page(context, url, article_id)
                if self.runner:
                    data = self.runner.run(acquire)
                else:
                    with connected_session(self.port) as context:
                        data = acquire(context)
                identity = verify_pdf(data, paper, retrieved_article_id=article_id)
                # The temporary PDF is private and removed after ingestion, even on failure.
                with tempfile.TemporaryDirectory(dir=self.store.root) as directory:
                    path = Path(directory) / "article.pdf"
                    with path.open("xb") as stream:
                        path.chmod(0o600)
                        stream.write(data)
                    result = self.store.ingest(path, title=paper["title"],
                                              url=doi_url(paper["doi"]), rights=rights)
                chunk = self.store.fetch(result["first_chunk_id"])
                chunk["metadata"].update({"retrieval": "ieee_browser_session",
                                          "institutional_entitlement": "not_verified",
                                          "pdf_identity": identity})
                return chunk
        except Timeout:
            raise RetrievalUnavailable("browser_busy") from None

    def _download_for_page(self, context, page, url: str, article_id: str) -> bytes:
        if not download_link(url, article_id):
            raise RetrievalUnavailable("publisher_redirect_not_allowed")
        if not self.runner:
            return self._download(context, url, article_id)
        # Keep authenticated PDF transfer in the same Firefox network session.
        # A separate HTTP client can be rejected even with copied session cookies.
        from .pdf_transfer import browser_pdf
        return browser_pdf(page, url, lambda candidate: download_link(candidate, article_id),
                           max_bytes=MAX_PDF_BYTES)

    def _read_page(self, context, url: str, article_id: str) -> bytes:
        from playwright.sync_api import TimeoutError as BrowserTimeout

        page = context.new_page()
        discard_session = False
        # Article retrieval may read IEEE only; institution login has its own guard.
        page.route("**/*", lambda route: route.fallback() if article_request_allowed(route.request) else route.abort())
        try:
            # A normal first visit may receive 202 and then navigate after running
            # the site's own JavaScript. Wait for its result instead of failing early.
            with page.expect_response(lambda r: r.request.is_navigation_request()
                    and r.frame == page.main_frame and ieee_url(r.url)
                    and (r.status == 200 or r.status >= 400),
                    timeout=20000) as loaded:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
            response = loaded.value
            if not response or response.status != 200:
                raise RetrievalUnavailable("publisher_interaction_required")
            try:
                page.locator('meta[name="citation_pdf_url"], a[href*="stamp"], iframe[src]').first.wait_for(
                    state="attached", timeout=10000)
            except BrowserTimeout:
                # Locked pages may expose no PDF link until institutional login.
                # Continue discovery so the caller can invoke that login once.
                pass
            candidates = page.locator('meta[name="citation_pdf_url"], a[href], iframe[src]').evaluate_all(
                "nodes => nodes.map(n => n.getAttribute('content') || n.getAttribute('href') || n.getAttribute('src'))")
            links = list(dict.fromkeys(urljoin(page.url, link) for link in candidates if link))
            for link in links:
                if not download_link(link, article_id):
                    continue
                if urlsplit(link).path == "/stamp/stamp.jsp":
                    stamp = page.goto(link, timeout=30000, wait_until="domcontentloaded")
                    if not stamp or stamp.status != 200:
                        raise RetrievalUnavailable("publisher_interaction_required")
                    frames = page.locator("iframe[src]").evaluate_all("nodes => nodes.map(n => n.src)")
                    for frame in frames:
                        if download_link(frame, article_id) and urlsplit(frame).path != "/stamp/stamp.jsp":
                            return self._download_for_page(context, page, frame, article_id)
                else:
                    return self._download_for_page(context, page, link, article_id)
            raise RetrievalUnavailable("publisher_pdf_link_unavailable")
        except RetrievalUnavailable as exc:
            if (exc.reason == "publisher_interaction_required"
                    and getattr(self.runner, "handles_session_discard", False) is True):
                from .managed_browser import _DiscardBrowserSession
                # Only an owned managed browser may be discarded. Keep page routes
                # installed and leave legacy attached/CDP browser ownership intact.
                discard_session = True
                raise _DiscardBrowserSession(exc.reason) from None
            raise
        finally:
            if not discard_session:
                page.close()
