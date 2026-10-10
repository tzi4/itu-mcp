from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import re
from urllib.parse import quote, urlsplit

import requests

from .identifiers import doi_url, normalize_doi, public_https_url


class DiscoveryError(RuntimeError):
    pass


def _safe_link(value) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or "\\" in value or "\x7f" in value:
        return None
    try:
        value = public_https_url(value)
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.port not in (None, 443) or host == "localhost" or host.endswith((".localhost", ".local")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if "." not in host:
                return None
        return value
    except (TypeError, ValueError):
        return None


def _open_license(value) -> str | None:
    """Recognize explicit CC reading licenses, not arbitrary publisher TDM terms."""
    if (not isinstance(value, str) or len(value) > 256 or "\\" in value
            or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in
                {"creativecommons.org", "www.creativecommons.org"} or parsed.username or
                parsed.password or parsed.port is not None or parsed.query or parsed.fragment):
            return None
        path = parsed.path.rstrip("/")
        if path.endswith("/legalcode"):
            path = path.removesuffix("/legalcode")
        if not re.fullmatch(r"/(?:licenses/(?:by|by-sa|by-nd|by-nc|by-nc-sa|by-nc-nd)/(?:1\.0|2\.0|2\.5|3\.0|4\.0)|publicdomain/zero/1\.0)", path):
            return None
        return "https://creativecommons.org" + path + "/"
    except ValueError:
        return None


def _license_start(entry) -> tuple[bool, str | None]:
    # Crossref permits an omitted start for a license applying for the work's lifetime.
    if "start" not in entry:
        return True, None
    start = entry["start"]
    if not isinstance(start, dict) or not start:
        return False, None
    dates = []
    try:
        if "date-time" in start:
            value = start["date-time"]
            if not isinstance(value, str) or len(value) > 64:
                return False, None
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return False, None
            dates.append(parsed.astimezone(timezone.utc))
        if "timestamp" in start:
            value = start["timestamp"]
            if type(value) not in (int, float):
                return False, None
            dates.append(datetime.fromtimestamp(value / 1000, timezone.utc))
        if "date-parts" in start:
            parts = start["date-parts"]
            if (not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], list)
                    or len(parts[0]) != 3 or not all(type(n) is int for n in parts[0])):
                return False, None
            dates.append(datetime(*parts[0], tzinfo=timezone.utc))
        # The latest supplied start wins if upstream representations disagree.
        return (True, max(dates).isoformat()) if dates else (False, None)
    except (ValueError, TypeError, OverflowError, OSError):
        return False, None


def _licenses(work) -> list[dict]:
    values = work.get("license")
    if not isinstance(values, list):
        return []
    result = []
    for value in values[:32]:
        if not isinstance(value, dict):
            continue
        license_url = _open_license(value.get("URL"))
        valid_start, effective_from = _license_start(value)
        if license_url and value.get("content-version") == "vor" and valid_start:
            result.append({"url": license_url, "content_version": "vor", "effective_from": effective_from})
    return result


def public_access(paper: dict) -> dict | None:
    """Return positive metadata evidence and reading links, never downloaded text."""
    if not isinstance(paper, dict):
        return None
    try:
        doi = normalize_doi(paper["doi"])
        if paper.get("id") not in (None, "doi:" + doi) or paper.get("url") not in (None, doi_url(doi)):
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    landing = _safe_link(paper.get("publisher_url"))
    link = landing or doi_url(doi)
    evidence = license_url = None
    values = paper.get("licenses", [])
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict) or item.get("content_version") != "vor":
            continue
        candidate = _open_license(item.get("url"))
        if not candidate or "effective_from" not in item:
            continue
        start = item["effective_from"]
        if start is not None:
            try:
                date = datetime.fromisoformat(start)
                if date.tzinfo is None or date > datetime.now(timezone.utc):
                    continue
            except (TypeError, ValueError):
                continue
        evidence, license_url = "crossref_current_vor_open_license", candidate
        break
    if evidence is None and landing and doi.startswith("10.3390/"):
        parsed = urlsplit(landing)
        if parsed.hostname == "www.mdpi.com" and re.fullmatch(r"/\d{4}-\d{3}[\dX]/[1-9]\d*/[1-9]\d*/[1-9]\d*/?", parsed.path):
            evidence = "mdpi_journal_open_access_policy"
    if evidence is None:
        return None
    return {"status": "found", "links": {"url": link, "url_for_landing_page": link},
            "evidence": evidence, "reported_license": license_url, "version": "publishedVersion",
            "downloaded": False, "external_model_permission": "not_evaluated"}


class MetadataClient:
    """Fixed API origins only. No credentials, general URL fetch or redirects."""

    def __init__(self, email: str | None = None, session=None):
        self.email = email
        self.session = session or requests.Session()
        self.session.trust_env = False  # Do not silently inherit netrc authentication.

    def _get(self, url: str, params: dict) -> dict:
        try:
            with self.session.get(
                url, params=params, timeout=(5, 20), allow_redirects=False,
                headers={"User-Agent": "PaperBridge/0.1 (personal research prototype)",
                         "Accept": "application/json"}, stream=True,
            ) as response:
                if response.status_code != 200:
                    raise DiscoveryError(f"Metadata API returned HTTP {response.status_code}.")
                body = bytearray()
                for part in response.iter_content(65536):
                    body.extend(part)
                    if len(body) > 4_000_000:
                        raise DiscoveryError("Metadata response exceeded 4 MB.")
                import json
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise DiscoveryError("Metadata API returned an invalid response.")
                return result
        except (requests.RequestException, ValueError):
            # Request exception messages may contain email parameters or URLs.
            raise DiscoveryError("Metadata request failed or returned invalid JSON.") from None

    @staticmethod
    def _paper(work: dict) -> dict:
        doi = normalize_doi(work["DOI"])
        authors = [" ".join(filter(None, [a.get("given"), a.get("family")]))
                   for a in work.get("author", [])[:20]]
        dates = work.get("published", {}).get("date-parts", [[]])
        return {
            "id": "doi:" + doi, "doi": doi,
            "title": (work.get("title") or [doi])[0], "url": doi_url(doi),
            "authors": authors, "year": dates[0][0] if dates and dates[0] else None,
            "publisher": work.get("publisher"), "content_level": "metadata_only",
            "publisher_url": work.get("resource", {}).get("primary", {}).get("URL"),
            "full_text_read": False, "licenses": _licenses(work),
        }

    def search(self, query: str, limit: int = 10) -> list[dict]:
        if not query.strip() or len(query) > 1000 or not 1 <= limit <= 20:
            raise ValueError("Use a nonempty query of <=1000 characters and limit 1..20.")
        params = {"query.bibliographic": query, "rows": limit}
        if self.email:
            params["mailto"] = self.email
        data = self._get("https://api.crossref.org/works", params)
        return [self._paper(w) for w in data["message"]["items"]]

    def lookup(self, doi: str) -> dict:
        doi = normalize_doi(doi)
        encoded = quote(doi, safe="")
        params = {"mailto": self.email} if self.email else {}
        paper = self._paper(self._get("https://api.crossref.org/works/" + encoded, params)["message"])
        if paper["doi"] != doi:
            raise DiscoveryError("Metadata identity did not match the requested DOI.")
        return paper

    def open_access(self, doi: str) -> dict:
        if not self.email:
            return {"status": "not_configured", "reason": "Unpaywall requires a contact email."}
        doi = normalize_doi(doi)
        data = self._get("https://api.unpaywall.org/v2/" + quote(doi, safe=""),
                         {"email": self.email})
        try:
            matches = isinstance(data.get("doi"), str) and normalize_doi(data["doi"]) == doi
        except ValueError:
            matches = False
        if not matches:
            raise DiscoveryError("Open-access metadata identity did not match the requested DOI.")
        if type(data.get("is_oa")) is not bool:
            raise DiscoveryError("Open-access metadata returned an invalid availability flag.")
        location = data.get("best_oa_location")
        location = location if isinstance(location, dict) else {}
        links = {}
        if data["is_oa"] is True:
            for name in ("url", "url_for_pdf", "url_for_landing_page"):
                link = _safe_link(location.get(name))
                if link:
                    links[name] = link
        return {"status": "found" if links else "unavailable" if data["is_oa"] else "not_found",
                "links": links, "evidence": "unpaywall",
                "reported_license": location.get("license") if isinstance(location.get("license"), str) else None,
                "version": location.get("version") if isinstance(location.get("version"), str) else None,
                "downloaded": False, "external_model_permission": "not_evaluated"}
