"""Bounded PDF identity checks; successful download is not sufficient evidence."""
from __future__ import annotations

import io
import re
from .browser import RetrievalUnavailable
from .identifiers import normalize_doi


def verify_pdf_identity(data: bytes, paper: dict, *, max_bytes: int = 20_000_000,
                        retrieved_article_id: str | None = None,
                        expected_article_id: str | None = None) -> str:
    from pypdf import PdfReader

    if not data.startswith(b"%PDF-") or len(data) > max_bytes:
        raise RetrievalUnavailable("publisher_did_not_return_pdf")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 200:
            raise RetrievalUnavailable("unsupported_pdf")
        first_page = reader.pages[0].extract_text() or ""
    except RetrievalUnavailable:
        raise
    except Exception:
        raise RetrievalUnavailable("invalid_pdf") from None
    compact = re.sub(r"\s+", "", first_page).casefold()
    title_key = lambda value: "".join(c for c in value.casefold() if c.isalnum())
    title = title_key(paper["title"])
    if len(title) >= 8 and title in title_key(first_page):
        # Permit PDF line wrapping, but do not accept a DOI that is merely a
        # prefix of a different printed DOI sharing the same paper title.
        doi_pattern = r"\s*".join(re.escape(c) for c in normalize_doi(paper["doi"]))
        if re.search(doi_pattern + r"(?![a-z0-9._;()/:-])", first_page.casefold()):
            return "first_page_doi_and_title"
        # IEEE conference PDFs do not always print their DOI. Require the
        # Crossref article number to match the validated retrieval link, plus
        # the full title and every supplied author on the PDF's first page.
        # A conflicting printed DOI must never fall back to this alternative.
        if retrieved_article_id and not re.search(r"10\.\d{4,9}/", compact):
            authors = paper.get("authors") or []
            if (retrieved_article_id == expected_article_id and authors
                    and all(isinstance(a, str) and len(title_key(a)) >= 4
                            and title_key(a) in title_key(first_page) for a in authors)):
                return "publisher_article_id_and_first_page_title_authors"
    raise RetrievalUnavailable("pdf_identity_not_verified")
