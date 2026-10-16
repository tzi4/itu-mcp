"""Read-only client for HocaMetre, the instructor ratings on notkutusu.com.

notkutusu.com is a student-run site where İTÜ students rate instructors on five
fixed criteria (1-5 each) and leave anonymous comments. The site is a React
single-page app whose data comes from a separate JSON API. This client reads
three GET endpoints of that API without an account, token or cookie:

- ``/instructors?filter=name:like:<text>`` for name search,
- ``/instructors/ratings/by-slug/<slug>`` for one profile's ratings,
- ``/instructor-comments/instructor/<id>`` for its newest comments.

Nothing is ever written: rating and commenting need a site login plus a
browser reCAPTCHA token and are deliberately not implemented.

The idea and the endpoint knowledge come from Ninova MCP by Hikmet Gultekin
(https://github.com/hikmedit/ninova-mcp, MIT License, Copyright (c) 2026
Hikmet Gultekin), whose ``notkutusu.py`` first wired HocaMetre into an MCP
server. This module is a separate implementation in this codebase's style; the
project LICENSE carries the same MIT notice.

Two properties of the source shape the design:

- The site's instructor table has duplicate and misspelled profiles, and votes
  fragment across them. Only profiles whose normalized name equals the
  requested name are merged into ``combined``; shorter or longer variants
  ("Ayşe Tosun" for "Ayşe Tosun Kühn") are returned as related candidates and
  never merged silently.
- The data is anonymous student opinion, not an İTÜ source. Every result is
  marked as untrusted external content.

The tools are on by default and ``ITU_MCP_HOCAMETRE=0`` turns them off. Reads
are cached (6 hours by default) and spaced at least
``MIN_REQUEST_INTERVAL_SECONDS`` apart.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import quote, urlparse

import requests

from .cache import TtlCache, parse_ttl_seconds
from .client import DEFAULT_HEADERS, _request_delay_seconds
from .env import env_switch
from .http_security import request_with_safe_redirects
from .parsing import clean_text, normalize_lookup_text

ENABLE_ENV = "ITU_MCP_HOCAMETRE"
CACHE_TTL_ENV = "ITU_MCP_HOCAMETRE_CACHE_TTL_SECONDS"
API_BASE_URL = "https://mordor-api-production.notkutusu.com"
SITE_URL = "https://notkutusu.com"
DEFAULT_CACHE_TTL_SECONDS = 6 * 60 * 60.0
MIN_REQUEST_INTERVAL_SECONDS = 0.3
SEARCH_PAGE_SIZE = 20
MAX_PROFILES_PER_NAME = 5
MAX_COMMENTS = 20
MAX_COMMENT_CHARS = 1000

# The site's own labels (locales/{en,tr}/instructor.json). Every criterion is
# scored so that a higher number is better for the student.
RATING_CRITERIA: dict[str, dict[str, str]] = {
    "GIVES_NOTES": {
        "label": "Grading generosity",
        "label_tr": "Not Cömertliği",
        "meaning": "Higher means more generous grades (more AA, fewer FF).",
    },
    "IS_HELPFUL": {
        "label": "Helpfulness",
        "label_tr": "Yardımseverlik",
        "meaning": "Availability and willingness to help outside class.",
    },
    "ASSIGNS_HOMEWORK": {
        "label": "Homework load",
        "label_tr": "Ödev Yükü",
        "meaning": "Higher means less homework.",
    },
    "TAKES_ATTENDANCE": {
        "label": "Attendance policy",
        "label_tr": "Yoklama Politikası",
        "meaning": "Higher means a more lenient attendance policy.",
    },
    "TEACHING_SKILLS": {
        "label": "Teaching skills",
        "label_tr": "Öğretme Becerileri",
        "meaning": "Overall teaching quality and clarity.",
    },
}

SOURCE_NOTE = (
    "Anonymous student votes from notkutusu.com HocaMetre, not an official İTÜ source. "
    "Scores run from 1 to 5 and higher is better for the student on every criterion. "
    "Small vote counts are unreliable, and the same instructor can appear under several "
    "profiles. Only profiles whose name matches exactly are merged into 'combined'."
)

# Leading academic titles, compared after Turkish folding and without dots.
_TITLE_TOKENS = frozenset({
    "prof", "doc", "dr", "yrd", "yard", "ogr", "uyesi", "uye", "gor", "ars", "aras",
    "okt", "uzm", "assoc", "asst", "assist", "lect", "lecturer", "instructor",
})
_INSTRUCTOR_SEPARATORS = re.compile(r"\s*(?:,|;|/|&|\bve\b|\band\b)\s*", re.IGNORECASE)
_SLUG_PATTERN = re.compile(r"[\w-][\w.-]{0,159}")
_ID_PATTERN = re.compile(r"[A-Za-z0-9-]{1,64}")
_EMPTY_INSTRUCTOR_VALUES = frozenset({"", "-", "--", "---", "yok", "none", "tba"})


class HocametreError(RuntimeError):
    """HocaMetre is off, unreachable, or returned an unexpected shape."""


def hocametre_enabled() -> bool:
    """HocaMetre reads are on unless the operator sets ITU_MCP_HOCAMETRE=0."""
    return env_switch(ENABLE_ENV)


def strip_academic_titles(name: str | None) -> str:
    """Drop leading titles: ``"Dr. Öğr. Üyesi Ayşe Tosun"`` -> ``"Ayşe Tosun"``."""
    tokens = clean_text(name).split()
    while tokens:
        # A token may glue several titles and even the name: "Doç.Dr.Ali".
        pieces = tokens[0].split(".")
        index = 0
        while index < len(pieces) and (
            not pieces[index] or normalize_lookup_text(pieces[index]) in _TITLE_TOKENS
        ):
            index += 1
        if index == len(pieces):
            tokens.pop(0)
        elif index:
            tokens[0] = ".".join(pieces[index:])
            break
        else:
            break
    return " ".join(tokens)


def name_tokens(name: str | None) -> tuple[str, ...]:
    """Case-, accent- and title-insensitive tokens used to compare names."""
    folded = normalize_lookup_text(strip_academic_titles(name))
    return tuple(re.findall(r"[a-z0-9]+", folded))


def split_instructors(value: str | None) -> list[str]:
    """Split an OBS instructor cell (``"A B, C D"``) into single names."""
    names: list[str] = []
    for part in _INSTRUCTOR_SEPARATORS.split(value or ""):
        name = strip_academic_titles(part)
        if normalize_lookup_text(name) in _EMPTY_INSTRUCTOR_VALUES:
            continue
        if name and name not in names:
            names.append(name)
    return names


def _vote_count(ratings: dict[str, dict[str, Any]]) -> int:
    """Largest per-criterion count, a close proxy for the number of raters."""
    return max((row.get("count") or 0 for row in ratings.values()), default=0)


def combine_ratings(profiles: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Vote-weighted average per criterion across profiles of one instructor."""
    totals = {key: [0.0, 0] for key in RATING_CRITERIA}
    for profile in profiles:
        for key, row in (profile.get("ratings") or {}).items():
            if key not in totals or row.get("average") is None or not row.get("count"):
                continue
            totals[key][0] += row["average"] * row["count"]
            totals[key][1] += row["count"]
    ratings = {
        key: {"average": round(weighted / count, 2) if count else None, "count": count}
        for key, (weighted, count) in totals.items()
    }
    votes = _vote_count(ratings)
    if votes == 0:
        return None
    return {
        "vote_count": votes,
        "profiles_merged": sum(1 for profile in profiles if profile.get("vote_count")),
        "ratings": ratings,
    }


class HocametreClient:
    """Cached, throttled reader for the public notkutusu.com JSON API."""

    ALLOWED_HOSTS = frozenset({urlparse(API_BASE_URL).hostname or ""})

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        cache_ttl: float | None = None,
        min_request_interval: float | None = None,
    ) -> None:
        self._session = session
        ttl = cache_ttl if cache_ttl is not None else parse_ttl_seconds(
            os.getenv(CACHE_TTL_ENV), DEFAULT_CACHE_TTL_SECONDS
        )
        self._cache: TtlCache[Any] = TtlCache(ttl)
        self._min_request_interval = (
            max(MIN_REQUEST_INTERVAL_SECONDS, _request_delay_seconds())
            if min_request_interval is None
            else max(0.0, min_request_interval)
        )
        self._last_request_at = 0.0

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            # A fresh session: no İTÜ cookie or credential ever reaches this host.
            self._session = requests.Session()
            self._session.headers.update(DEFAULT_HEADERS)
            self._session.headers["Accept"] = "application/json"
        return self._session

    # -- HTTP -------------------------------------------------------------

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in self.ALLOWED_HOSTS:
            raise HocametreError(f"HocaMetre request destination is not allowlisted: {url}")

    def _throttle(self) -> None:
        if self._min_request_interval <= 0:
            return
        remaining = self._min_request_interval - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        cache_key = f"{path}?{sorted((params or {}).items())}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        url = API_BASE_URL + path
        self._throttle()
        try:
            response = request_with_safe_redirects(
                self.session,
                "GET",
                url,
                validate_url=self._validate_url,
                params=params,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise HocametreError(f"notkutusu.com could not be reached: {exc}") from exc
        if response.status_code >= 400:
            raise HocametreError(f"notkutusu.com returned HTTP {response.status_code} for {path}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise HocametreError(f"notkutusu.com returned non-JSON data for {path}.") from exc
        self._cache.set(cache_key, payload)
        return payload

    # -- endpoints --------------------------------------------------------

    def search(self, text: str, *, size: int = SEARCH_PAGE_SIZE) -> list[dict[str, Any]]:
        """Substring search over instructor names."""
        # ":" separates the filter's fields, so it cannot be part of the value.
        query = clean_text(text.replace(":", " "))
        if not query:
            return []
        payload = self._get_json(
            "/instructors",
            {"page": 1, "size": max(1, min(int(size), 50)), "filter": f"name:like:{query}"},
        )
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise HocametreError("Unexpected response shape from the instructor search.")
        return [_profile_summary(item) for item in items if isinstance(item, dict)]

    def ratings(self, slug: str) -> dict[str, Any]:
        """Per-criterion averages and vote counts for one profile."""
        if not isinstance(slug, str) or not _SLUG_PATTERN.fullmatch(slug) or slug.startswith("."):
            raise HocametreError("The profile slug has an unexpected format.")
        payload = self._get_json(
            f"/instructors/ratings/by-slug/{quote(slug, safe='')}",
            # The relation set the site itself requests; Ninova MCP found that a
            # smaller set makes the API answer HTTP 500 for rated profiles.
            {"relations": "faculty|instructorRatings|instructorRatings.user"},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("ratings"), list):
            raise HocametreError("Unexpected response shape from the ratings endpoint.")
        ratings = _normalize_ratings(payload["ratings"])
        profile = _profile_summary(payload.get("instructor") or {})
        profile["slug"] = profile.get("slug") or slug
        profile["profile_url"] = f"{SITE_URL}/instructors/{quote(profile['slug'], safe='')}"
        return {**profile, "vote_count": _vote_count(ratings), "ratings": ratings}

    def comments(self, instructor_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Newest anonymous comments for one profile."""
        identifier = str(instructor_id)
        if not _ID_PATTERN.fullmatch(identifier):
            raise HocametreError("The profile id has an unexpected format.")
        payload = self._get_json(
            f"/instructor-comments/instructor/{identifier}",
            # The API rejects an upper-case sort direction.
            {"page": 1, "size": max(1, min(int(limit), MAX_COMMENTS)), "sort": "createdAt:desc"},
        )
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise HocametreError("Unexpected response shape from the comments endpoint.")
        comments = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = clean_text(str(item.get("comment") or ""))
            if text:
                comments.append({
                    "comment": text[:MAX_COMMENT_CHARS],
                    "truncated": len(text) > MAX_COMMENT_CHARS,
                    "created_at": item.get("createdAt"),
                })
        return comments

    # -- name resolution ----------------------------------------------------

    def lookup(
        self,
        name: str,
        *,
        include_comments: bool = False,
        comment_limit: int = 5,
    ) -> dict[str, Any]:
        """Resolve one instructor name to rated profiles without guessing.

        ``status`` is ``matched`` when at least one profile has exactly the
        requested name (all such duplicates are merged into ``combined``),
        ``candidates`` when only partial-name profiles exist (returned with
        their ratings, never merged), and ``not_found`` otherwise.
        """
        display = strip_academic_titles(name)
        wanted = name_tokens(display)
        if not wanted:
            raise HocametreError("Provide an instructor name.")
        hits = self._collect_hits(display)

        exact: list[dict[str, Any]] = []
        near: list[dict[str, Any]] = []
        wanted_set = set(wanted)
        for hit in hits:
            have = set(name_tokens(hit.get("name")))
            if not have:
                continue
            if have == wanted_set and len(wanted) > 1:
                exact.append(hit)
            elif wanted_set <= have or (len(have) > 1 and have <= wanted_set):
                near.append(hit)

        result: dict[str, Any] = {"query": name, "name": display}
        if exact:
            profiles = self._rate(exact)
            result.update({
                "status": "matched",
                "combined": combine_ratings(profiles),
                "profiles": profiles,
            })
            if result["combined"] is None:
                result["message"] = "The matching profile has no votes yet."
            if near:
                result["related_profiles"] = self._rate(near)
                result["related_note"] = (
                    "Profiles with a shorter or longer version of this name. They may be the "
                    "same instructor, but they are not merged into 'combined'."
                )
        elif near:
            result.update({
                "status": "candidates",
                "candidates": self._rate(near),
                "message": (
                    "No profile has exactly this name. These partial matches may or may not be "
                    "the same instructor; ask the user, or call again with a candidate's exact name."
                ),
            })
        else:
            result.update({
                "status": "not_found",
                "message": "No HocaMetre profile matches this name.",
            })

        omitted = max(0, len(exact) - MAX_PROFILES_PER_NAME) + max(0, len(near) - MAX_PROFILES_PER_NAME)
        if omitted:
            result["omitted_profile_count"] = omitted
        if include_comments and exact:
            result["comments"] = self._comments_for(result["profiles"], comment_limit)
        return result

    def _collect_hits(self, display: str) -> list[dict[str, Any]]:
        """Search the full name, then each later name part on its own.

        Votes often sit on a shortened duplicate ("Ayşe Tosun" for "Ayşe
        Tosun Kühn"), which a full-name substring search cannot find.
        """
        parts = display.split()
        attempts = [display] + [part for part in parts[1:] if len(name_tokens(part)) == 1
                                and len(name_tokens(part)[0]) >= 3]
        seen_queries: set[tuple[str, ...]] = set()
        found: dict[str, dict[str, Any]] = {}
        for attempt in attempts:
            key = name_tokens(attempt)
            if key in seen_queries:
                continue
            seen_queries.add(key)
            for hit in self.search(attempt):
                ident = str(hit.get("slug") or hit.get("id") or "")
                if ident:
                    found.setdefault(ident, hit)
        return list(found.values())

    def _rate(self, profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rated: list[dict[str, Any]] = []
        for profile in profiles[:MAX_PROFILES_PER_NAME]:
            try:
                detail = self.ratings(str(profile.get("slug") or ""))
            except HocametreError as exc:
                rated.append({**profile, "error": str(exc)})
                continue
            merged = {**profile, **{key: value for key, value in detail.items() if value is not None}}
            rated.append(merged)
        rated.sort(key=lambda item: item.get("vote_count") or 0, reverse=True)
        return rated

    def _comments_for(self, profiles: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_COMMENTS))
        comments: list[dict[str, Any]] = []
        for profile in profiles:
            if profile.get("id") is None or profile.get("error"):
                continue
            try:
                for comment in self.comments(str(profile["id"]), limit=limit):
                    comments.append({**comment, "profile": profile.get("name")})
            except HocametreError as exc:
                comments.append({"profile": profile.get("name"), "error": str(exc)})
        dated = [comment for comment in comments if "comment" in comment]
        dated.sort(key=lambda comment: str(comment.get("created_at") or ""), reverse=True)
        return dated[:limit] + [comment for comment in comments if "comment" not in comment]


def _profile_summary(item: dict[str, Any]) -> dict[str, Any]:
    faculty = item.get("faculty")
    if isinstance(faculty, dict):
        faculty = faculty.get("name")
    return {
        "id": item.get("id"),
        "name": clean_text(str(item.get("name") or "")) or None,
        "slug": item.get("slug") if isinstance(item.get("slug"), str) else None,
        "faculty": clean_text(str(faculty)) if faculty else None,
    }


def _normalize_ratings(raw: list[Any]) -> dict[str, dict[str, Any]]:
    by_key = {item.get("name"): item for item in raw if isinstance(item, dict)}
    ratings: dict[str, dict[str, Any]] = {}
    for key in RATING_CRITERIA:
        item = by_key.get(key) or {}
        try:
            count = max(0, int(item.get("count") or 0))
            average = float(item.get("averageRating")) if item.get("averageRating") is not None else None
        except (TypeError, ValueError):
            count, average = 0, None
        # The API reports 0 for an unrated criterion: that is "no data", not a score.
        if not count or average is None or not 1.0 <= average <= 5.0:
            ratings[key] = {"average": None, "count": 0}
        else:
            ratings[key] = {"average": round(average, 2), "count": count}
    return ratings


def criteria_legend() -> dict[str, dict[str, str]]:
    """Criterion labels for a result, so per-profile rows can stay compact."""
    return {key: dict(labels, scale="1-5") for key, labels in RATING_CRITERIA.items()}
