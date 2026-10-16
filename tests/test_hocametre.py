"""Offline tests for the HocaMetre (notkutusu.com) client and tools.

Every response is synthetic; no test touches the network.
"""

from __future__ import annotations

import os
import unittest
from typing import Any
from unittest import mock
from urllib.parse import urlparse

import requests

from ninova_mcp.hocametre import (
    API_BASE_URL,
    ENABLE_ENV,
    HocametreClient,
    HocametreError,
    combine_ratings,
    hocametre_enabled,
    name_tokens,
    split_instructors,
    strip_academic_titles,
)
from ninova_mcp.server import NinovaMcpApp

RELATIONS = "faculty|instructorRatings|instructorRatings.user"


class _Response:
    def __init__(self, url: str, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.history: list[Any] = []

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def close(self) -> None:
        pass


def _rating(name: str, average: float, count: int) -> dict[str, Any]:
    return {"name": name, "userRating": None, "averageRating": average, "count": count}


PROFILES = [
    {"id": "1", "name": "Ayşe Tosun Kühn", "slug": "ayse_tosun_kuhn", "email": "x@itu.edu.tr"},
    {"id": "2", "name": "Ayşe Tosun", "slug": "ayse_tosun"},
    {"id": "3", "name": "Mehmet Tosun", "slug": "mehmet_tosun"},
    {"id": "4", "name": "AYŞE TOSUN", "slug": "ayse_tosun_2"},
    {"id": "5", "name": "Yusuf Şahin", "slug": "yusuf_sahin"},
    {"id": "6", "name": "Zeynep Işık", "slug": "zeynep_isik"},
]

RATINGS = {
    "ayse_tosun_kuhn": [_rating("GIVES_NOTES", 0, 0), _rating("TAKES_ATTENDANCE", 0, 0)],
    "ayse_tosun": [_rating("GIVES_NOTES", 2.0, 30), _rating("TEACHING_SKILLS", 4.0, 28)],
    "ayse_tosun_2": [_rating("GIVES_NOTES", 4.0, 10), _rating("TEACHING_SKILLS", 3.0, 12)],
    "mehmet_tosun": [_rating("GIVES_NOTES", 5.0, 10)],
    "yusuf_sahin": [_rating("IS_HELPFUL", 4.5, 8)],
    "zeynep_isik": [_rating("ASSIGNS_HOMEWORK", 3.2, 5)],
}


def _fold(text: str) -> str:
    return " ".join(name_tokens(text))


class FakeSession:
    """Answers the three API routes from the synthetic tables above."""

    def __init__(self, *, fail: bool = False, status: int = 200) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail
        self.status = status
        self.redirect_to: str | None = None

    def request(self, method: str, url: str, *, allow_redirects: bool, params: Any = None,
                timeout: Any = None, **_: Any) -> _Response:
        assert method == "GET" and allow_redirects is False
        params = dict(params or {})
        self.calls.append((url, params))
        if self.fail:
            raise requests.ConnectionError("network down")
        if self.redirect_to:
            return _Response(url, 302, None, {"Location": self.redirect_to})
        if self.status != 200:
            return _Response(url, self.status, {"message": "error"})
        path = urlparse(url).path
        if path == "/instructors":
            term = _fold(params["filter"].split(":", 2)[2])
            items = [item for item in PROFILES if term in _fold(item["name"])]
            return _Response(url, 200, {"totalItems": len(items), "items": items})
        if path.startswith("/instructors/ratings/by-slug/"):
            slug = path.rsplit("/", 1)[1]
            if params.get("relations") != RELATIONS:
                return _Response(url, 500, {"message": "Something went wrong"})
            profile = next(item for item in PROFILES if item["slug"] == slug)
            return _Response(url, 200, {
                "instructor": {**profile, "faculty": {"name": "BBF"}},
                "ratings": RATINGS[slug],
            })
        if path.startswith("/instructor-comments/instructor/"):
            ident = path.rsplit("/", 1)[1]
            return _Response(url, 200, {"totalItems": 2, "items": [
                {"id": f"c{ident}a", "comment": "Anlatımı açık. " * 100, "createdAt": f"2026-0{ident}-02T10:00:00Z"},
                {"id": f"c{ident}b", "comment": "Ödevler zor.", "createdAt": f"2026-0{ident}-01T10:00:00Z"},
            ]})
        return _Response(url, 404, {})


def _client(session: FakeSession | None = None) -> HocametreClient:
    return HocametreClient(session=session or FakeSession(), cache_ttl=3600, min_request_interval=0)


class NameHandlingTests(unittest.TestCase):
    def test_turkish_titles_are_stripped(self) -> None:
        cases = {
            "Prof. Dr. Ali Çakmak": "Ali Çakmak",
            "Doç.Dr. Mehmet Tosun": "Mehmet Tosun",
            "Dr. Öğr. Üyesi Ayşe Tosun": "Ayşe Tosun",
            "Yrd. Doç. Dr. Can Ak": "Can Ak",
            "Arş. Gör. Dr. Zeynep Işık": "Zeynep Işık",
            "Öğr. Gör. Deniz Er": "Deniz Er",
            "Assoc. Prof. Dr. Ece Su": "Ece Su",
            "Ayşe Tosun": "Ayşe Tosun",
            "Prof.Dr.Ali Çakmak": "Ali Çakmak",
            "M. Ali Veli": "M. Ali Veli",
            "Prof. Dr.": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(strip_academic_titles(raw), expected)

    def test_turkish_letters_fold_to_the_same_tokens(self) -> None:
        self.assertEqual(name_tokens("ZEYNEP IŞIK"), ("zeynep", "isik"))
        self.assertEqual(name_tokens("zeynep ışık"), ("zeynep", "isik"))
        self.assertEqual(name_tokens("Zeynep Isik"), ("zeynep", "isik"))
        self.assertEqual(name_tokens("İLKER ÖZ"), name_tokens("ilker öz"))
        self.assertEqual(name_tokens("Prof. Dr. Ayşe Tosun-Kühn"), ("ayse", "tosun", "kuhn"))

    def test_obs_instructor_cells_are_split(self) -> None:
        self.assertEqual(split_instructors("Ali Çakmak, Ayşe Tosun"), ["Ali Çakmak", "Ayşe Tosun"])
        self.assertEqual(split_instructors("Prof. Dr. Ali Çakmak / Ayşe Tosun ve Can Ak"),
                         ["Ali Çakmak", "Ayşe Tosun", "Can Ak"])
        self.assertEqual(split_instructors("Ali Çakmak, Ali Çakmak"), ["Ali Çakmak"])
        for empty in (None, "", "-", "--"):
            self.assertEqual(split_instructors(empty), [])


class LookupTests(unittest.TestCase):
    def test_exact_duplicates_merge_and_partial_names_stay_separate(self) -> None:
        result = _client().lookup("Dr. Öğr. Üyesi Ayşe Tosun")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["name"], "Ayşe Tosun")
        self.assertEqual({p["slug"] for p in result["profiles"]}, {"ayse_tosun", "ayse_tosun_2"})
        # Vote-weighted: (2.0*30 + 4.0*10) / 40 = 2.5; (4.0*28 + 3.0*12) / 40 = 3.7.
        combined = result["combined"]
        self.assertEqual(combined["ratings"]["GIVES_NOTES"], {"average": 2.5, "count": 40})
        self.assertEqual(combined["ratings"]["TEACHING_SKILLS"], {"average": 3.7, "count": 40})
        self.assertIsNone(combined["ratings"]["IS_HELPFUL"]["average"])
        self.assertEqual(combined["profiles_merged"], 2)
        related = [p["slug"] for p in result["related_profiles"]]
        self.assertEqual(related, ["ayse_tosun_kuhn"])
        # A different person who only shares the surname is never included.
        everything = str(result)
        self.assertNotIn("mehmet_tosun", everything)
        self.assertEqual(result["profiles"][0]["profile_url"], "https://notkutusu.com/instructors/ayse_tosun")
        self.assertEqual(result["profiles"][0]["faculty"], "BBF")

    def test_longer_obs_name_returns_candidates_without_guessing(self) -> None:
        result = _client().lookup("Yusuf Hüseyin Şahin")
        self.assertEqual(result["status"], "candidates")
        self.assertNotIn("combined", result)
        self.assertEqual([c["name"] for c in result["candidates"]], ["Yusuf Şahin"])
        self.assertEqual(result["candidates"][0]["ratings"]["IS_HELPFUL"], {"average": 4.5, "count": 8})

    def test_exact_profile_without_votes_still_matches(self) -> None:
        result = _client().lookup("Ayşe Tosun Kühn")
        self.assertEqual(result["status"], "matched")
        self.assertIsNone(result["combined"])
        self.assertIn("no votes", result["message"])
        self.assertEqual(result["profiles"][0]["ratings"]["GIVES_NOTES"], {"average": None, "count": 0})
        self.assertEqual({p["slug"] for p in result["related_profiles"]}, {"ayse_tosun", "ayse_tosun_2"})

    def test_single_name_part_is_never_an_exact_match(self) -> None:
        result = _client().lookup("Tosun")
        self.assertEqual(result["status"], "candidates")
        self.assertEqual(len(result["candidates"]), 4)

    def test_unknown_name_is_not_found(self) -> None:
        result = _client().lookup("Hiç Olmayan")
        self.assertEqual(result["status"], "not_found")

    def test_ascii_spelling_matches_turkish_profile(self) -> None:
        # The fake search folds letters; the comparison after it must fold too.
        self.assertEqual(_client().lookup("ZEYNEP ISIK")["status"], "matched")

    def test_comments_are_newest_first_and_bounded(self) -> None:
        result = _client().lookup("Ayşe Tosun", include_comments=True, comment_limit=3)
        comments = result["comments"]
        self.assertEqual(len(comments), 3)
        dates = [c["created_at"] for c in comments]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertTrue(comments[0]["truncated"])
        self.assertLessEqual(len(comments[0]["comment"]), 1000)

    def test_results_are_cached(self) -> None:
        session = FakeSession()
        client = _client(session)
        client.lookup("Ayşe Tosun")
        first = len(session.calls)
        client.lookup("Ayşe Tosun")
        self.assertEqual(len(session.calls), first)

    def test_filter_separator_is_removed_from_queries(self) -> None:
        session = FakeSession()
        _client(session).search("Ayşe:Tosun")
        self.assertEqual(session.calls[0][1]["filter"], "name:like:Ayşe Tosun")

    def test_combine_ignores_unrated_rows(self) -> None:
        self.assertIsNone(combine_ratings([{"ratings": {"GIVES_NOTES": {"average": None, "count": 0}}}]))


class FailureTests(unittest.TestCase):
    def test_network_failure_raises_a_clear_error(self) -> None:
        with self.assertRaisesRegex(HocametreError, "could not be reached"):
            _client(FakeSession(fail=True)).lookup("Ayşe Tosun")

    def test_http_error_and_bad_json_raise(self) -> None:
        with self.assertRaisesRegex(HocametreError, "HTTP 503"):
            _client(FakeSession(status=503)).search("Ayşe")

        class BadJson(FakeSession):
            def request(self, method: str, url: str, **kwargs: Any) -> _Response:
                return _Response(url, 200, ValueError("not json"))

        with self.assertRaisesRegex(HocametreError, "non-JSON"):
            _client(BadJson()).search("Ayşe")

    def test_redirect_to_another_host_is_refused(self) -> None:
        session = FakeSession()
        session.redirect_to = "https://evil.example/instructors"
        with self.assertRaisesRegex(HocametreError, "not allowlisted"):
            _client(session).search("Ayşe")
        self.assertEqual(len(session.calls), 1)

    def test_unexpected_slug_is_refused_before_any_request(self) -> None:
        session = FakeSession()
        for slug in ("../admin", "a/b", "..", ""):
            with self.subTest(slug=slug), self.assertRaises(HocametreError):
                _client(session).ratings(slug)
        self.assertEqual(session.calls, [])

    def test_requests_stay_on_the_api_host(self) -> None:
        session = FakeSession()
        _client(session).lookup("Ayşe Tosun")
        hosts = {urlparse(url).hostname for url, _ in session.calls}
        self.assertEqual(hosts, {urlparse(API_BASE_URL).hostname})


class _FakeObsPublic:
    def __init__(self, courses: list[dict[str, Any]]) -> None:
        self.courses = courses
        self.calls: list[tuple[str, str]] = []

    def get_course_schedule(self, program_type: str, department: str) -> dict[str, Any]:
        self.calls.append((program_type, department))
        return {"semester": "2026-2027 Güz", "program_type": "LS", "courses": self.courses}


SCHEDULE = [
    {"crn": "101", "code": "BBF 101E", "name": "Intro", "instructor": "Ayşe Tosun",
     "capacity": 50, "enrolled": 40, "sessions": [{"day": "Pazartesi", "time": "08:30/10:29"}]},
    {"crn": "102", "code": "BBF 101E", "name": "Intro", "instructor": "Yusuf Hüseyin Şahin, Ayşe Tosun",
     "capacity": 50, "enrolled": 50, "sessions": []},
    {"crn": "103", "code": "BBF 101E", "name": "Intro", "instructor": "--",
     "capacity": 30, "enrolled": 0, "sessions": []},
    {"crn": "201", "code": "BBF 102E", "name": "Other", "instructor": "Mehmet Tosun",
     "capacity": 50, "enrolled": 1, "sessions": []},
]


class ServerToolTests(unittest.TestCase):
    def _app(self, session: FakeSession | None = None) -> NinovaMcpApp:
        app = NinovaMcpApp()
        app._hocametre = _client(session)
        app._obs_public = _FakeObsPublic(SCHEDULE)  # type: ignore[assignment]
        return app

    def test_tools_are_off_by_default(self) -> None:
        session = FakeSession()
        app = self._app(session)
        with mock.patch.dict(os.environ, {ENABLE_ENV: ""}):
            self.assertFalse(hocametre_enabled())
            with self.assertRaisesRegex(HocametreError, ENABLE_ENV):
                app.hocametre_instructor("Ayşe Tosun")
            with self.assertRaisesRegex(HocametreError, ENABLE_ENV):
                app.hocametre_course_sections("BBF 101E")
        with mock.patch.dict(os.environ, {ENABLE_ENV: "0"}):
            self.assertFalse(hocametre_enabled())
        self.assertEqual(session.calls, [])
        self.assertEqual(app._obs_public.calls, [])  # type: ignore[union-attr]

    def test_instructor_tool_marks_untrusted_content(self) -> None:
        with mock.patch.dict(os.environ, {ENABLE_ENV: "1"}):
            result = self._app().hocametre_instructor("Prof. Dr. Ayşe Tosun")
        self.assertTrue(result["untrusted_external_content"])
        self.assertEqual(result["status"], "matched")
        self.assertIn("GIVES_NOTES", result["criteria"])
        self.assertEqual(result["criteria"]["ASSIGNS_HOMEWORK"]["label"], "Homework load")
        self.assertIn("not an official", result["note"])

    def test_course_sections_attach_ratings_per_instructor(self) -> None:
        session = FakeSession()
        app = self._app(session)
        with mock.patch.dict(os.environ, {ENABLE_ENV: "1"}):
            result = app.hocametre_course_sections("bbf101e")
        self.assertEqual(result["course_code"], "BBF 101E")
        self.assertEqual(result["section_source"], "obs")
        self.assertEqual(app._obs_public.calls, [("LS", "BBF")])  # type: ignore[union-attr]
        self.assertEqual([s["crn"] for s in result["sections"]], ["101", "102", "103"])
        self.assertEqual(set(result["instructors"]), {"Ayşe Tosun", "Yusuf Hüseyin Şahin"})
        first = result["sections"][0]["instructor_ratings"]
        self.assertEqual(first[0]["status"], "matched")
        self.assertEqual(first[0]["vote_count"], 40)
        self.assertEqual(first[0]["averages"]["GIVES_NOTES"], 2.5)
        second = {row["name"]: row for row in result["sections"][1]["instructor_ratings"]}
        self.assertEqual(second["Yusuf Hüseyin Şahin"]["status"], "candidates")
        self.assertEqual(second["Yusuf Hüseyin Şahin"]["candidate_names"], ["Yusuf Şahin"])
        self.assertEqual(result["sections"][2]["instructor_ratings"], [])
        self.assertNotIn("Mehmet Tosun", result["instructors"])

    def test_course_sections_stop_after_a_network_failure(self) -> None:
        session = FakeSession(fail=True)
        with mock.patch.dict(os.environ, {ENABLE_ENV: "1"}):
            result = self._app(session).hocametre_course_sections("BBF 101E")
        statuses = {name: entry["status"] for name, entry in result["instructors"].items()}
        self.assertEqual(statuses, {"Ayşe Tosun": "error", "Yusuf Hüseyin Şahin": "not_looked_up"})
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(result["sections"]), 3)

    def test_course_without_sections_makes_no_rating_requests(self) -> None:
        session = FakeSession()
        with mock.patch.dict(os.environ, {ENABLE_ENV: "1"}):
            result = self._app(session).hocametre_course_sections("BBF 999E")
        self.assertEqual(result["section_count"], 0)
        self.assertIn("No BBF 999E sections", result["message"])
        self.assertEqual(session.calls, [])

    def test_archived_term_sections_can_be_rated(self) -> None:
        app = self._app()
        archived = [
            {"crn": "9", "code": "BBF 101E", "name": "Intro", "instructor": "Ayşe Tosun",
             "capacity": 10, "enrolled": 9, "days": ["Salı"], "times": ["08:30/10:29"]},
            {"crn": "8", "code": "BBF 103E", "name": "Other", "instructor": "Mehmet Tosun"},
        ]
        with mock.patch.dict(os.environ, {ENABLE_ENV: "1"}), mock.patch.object(
            app, "_resolve_archive_term_branch", return_value=("covered", {}, {}, archived)
        ) as resolve:
            result = app.hocametre_course_sections("BBF 101E", term="2026-2027-guz")
        resolve.assert_called_once_with("2026-2027-guz", "BBF")
        self.assertEqual(result["section_source"], "archive")
        self.assertEqual(result["coverage"], "covered")
        self.assertEqual([s["crn"] for s in result["sections"]], ["9"])
        self.assertEqual(result["sections"][0]["sessions"][0]["day"], "Salı")
        self.assertEqual(app._obs_public.calls, [])  # type: ignore[union-attr]

    def test_invalid_course_code_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {ENABLE_ENV: "1"}), self.assertRaises(HocametreError):
            self._app().hocametre_course_sections("not a code")


if __name__ == "__main__":
    unittest.main()
