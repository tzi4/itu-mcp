from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit


def normalize_doi(value: str) -> str:
    raw = value.strip()
    if raw.lower().startswith("doi:"):
        raw = raw[4:].strip()
    elif raw.lower().startswith(("https://", "http://")):
        url = urlsplit(raw)
        if url.hostname not in {"doi.org", "dx.doi.org"} or url.username or url.password:
            raise ValueError("Expected a DOI or doi.org URL.")
        if url.query or url.fragment:
            raise ValueError("DOI URLs must not contain query parameters or fragments.")
        raw = unquote(url.path.lstrip("/"))
    raw = raw.lower()
    if len(raw) > 512 or not re.fullmatch(r"10\.\d{4,9}/[^\s\x00-\x1f\x7f]+", raw):
        raise ValueError("Invalid DOI.")
    return raw


def doi_url(doi: str) -> str:
    return "https://doi.org/" + quote(normalize_doi(doi), safe="/")


def public_https_url(value: str) -> str:
    """Validate a citation link. These links are not downloaded by this version."""
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("Expected an HTTPS citation URL without credentials.")
    if any(ord(c) < 33 for c in value):
        raise ValueError("Invalid citation URL.")
    return value
