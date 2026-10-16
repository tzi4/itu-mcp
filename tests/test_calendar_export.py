from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from ninova_mcp.calendar_export import (
    ISTANBUL, Event, academic_events, build_calendar, class_events, deadline_events, escape, exam_events, fold,
)

SCHEDULE = [
    {"crn": "11731", "bransKodu": "UZB", "dersKodu": "419E", "dersAdiEN": "Spacecraft Design",
     "donemBaslangicTarihi": "2026-09-21T00:00:00", "donemBitisTarihi": "2027-01-08T00:00:00",
     "sinifYerZaman": [
         {"id": 1, "baslangicSaati": 750, "bitisSaati": 869, "gunKodu": "M", "binaKodu": "MEDC", "mekanAdi": "ZA4",
          "binaAdi": "Merkezi Derslik C Binası"},
         {"id": 2, "baslangicSaati": 510, "bitisSaati": 629, "gunKodu": "T", "binaKodu": "", "mekanAdi": ""},
     ]},
    {"crn": "11744", "bransKodu": "UZB", "dersKodu": "4901E", "dersAdiEN": "Design Project",
     "donemBaslangicTarihi": "2026-09-21T00:00:00", "donemBitisTarihi": "2027-01-08T00:00:00", "sinifYerZaman": []},
]


class IcsFormatTests(unittest.TestCase):
    def test_escape_and_fold(self) -> None:
        self.assertEqual(escape("a,b;c\\d\nnext"), "a\\,b\\;c\\\\d\\nnext")
        line = "SUMMARY:" + "ğ" * 60
        pieces = fold(line)
        self.assertTrue(all(len(piece.encode()) <= 75 for piece in pieces))
        self.assertTrue(all(piece.startswith(" ") for piece in pieces[1:]))
        self.assertEqual("".join(piece[1:] if i else piece for i, piece in enumerate(pieces)), line)

    def test_calendar_structure(self) -> None:
        event = Event("x@itu-mcp", "Test, one", datetime(2026, 10, 19, 12, 30, tzinfo=ISTANBUL),
                      datetime(2026, 10, 19, 14, 30, tzinfo=ISTANBUL), alarm_minutes=60)
        text = build_calendar([event], now=datetime(2026, 10, 16, 12, 0, tzinfo=ISTANBUL))
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n"))
        for needed in ("TZID:Europe/Istanbul", "DTSTART;TZID=Europe/Istanbul:20261019T123000",
                       "DTSTAMP:20261016T090000Z", "SUMMARY:Test\\, one", "TRIGGER:-PT60M"):
            self.assertIn(needed, text)
        self.assertNotIn("\n", text.replace("\r\n", ""))


class SourceTests(unittest.TestCase):
    def test_weekly_classes_repeat_until_term_end(self) -> None:
        events, warnings = class_events(SCHEDULE)
        self.assertEqual(len(events), 2)
        monday = events[0]
        self.assertEqual(monday.start, datetime(2026, 9, 21, 12, 30, tzinfo=ISTANBUL))
        self.assertEqual(monday.end, datetime(2026, 9, 21, 14, 30, tzinfo=ISTANBUL))
        self.assertEqual(monday.rrule, "FREQ=WEEKLY;BYDAY=MO;UNTIL=20270108T205959Z")
        self.assertEqual(monday.location, "MEDC ZA4")
        tuesday = events[1]
        self.assertEqual(tuesday.start, datetime(2026, 9, 22, 8, 30, tzinfo=ISTANBUL))
        self.assertEqual(tuesday.location, "")
        self.assertEqual(warnings, ["UZB 4901E: no weekly sessions in the schedule."])

    def test_exam_rows_in_several_formats(self) -> None:
        rows = [
            {"crn": "11731", "course_code": "UZB 419E", "date": "12.01.2027", "time": "09:30-11:30", "building": "MED",
             "room": "A14"},
            {"crn": "99999", "course_code": "MAT 103E", "date": "13.01.2027", "time": "13:00"},
            {"CRN": "11744", "dersKodu": "UZB 4901E", "SinavTarihi": "2027-01-14 15:00"},
            {"crn": "11745", "course_code": "UZB 441E", "date": "not set"},
        ]
        events, warnings = exam_events(rows, wanted={"11731", "11744", "11745"})
        self.assertEqual([event.start for event in events],
                         [datetime(2027, 1, 12, 9, 30, tzinfo=ISTANBUL), datetime(2027, 1, 14, 15, 0, tzinfo=ISTANBUL)])
        self.assertEqual(events[0].end, datetime(2027, 1, 12, 11, 30, tzinfo=ISTANBUL))
        self.assertEqual(events[0].location, "MED A14")
        self.assertEqual(len(warnings), 1)

    def test_deadlines_and_academic_dates(self) -> None:
        events, warnings = deadline_events([
            {"title": "HW 3", "course": {"code": "UZB 419E"}, "url": "https://ninova.itu.edu.tr/x",
             "submission_end_iso": "2026-10-20T20:59:00+00:00"},
            {"title": "Broken", "submission_end_iso": "soon"},
        ])
        self.assertEqual(events[0].end, datetime(2026, 10, 20, 23, 59, tzinfo=ISTANBUL))
        self.assertEqual(events[0].summary, "Due: HW 3 (UZB 419E)")
        self.assertEqual(events[0].alarm_minutes, 1440)
        self.assertEqual(len(warnings), 1)
        dates, _ = academic_events([{"description": "Holiday", "start_date": "2026-10-29", "end_date": "2026-10-29"}])
        self.assertEqual((dates[0].start, dates[0].end), (date(2026, 10, 29), date(2026, 10, 30)))


class FakeObs:
    def resolve_semester(self, semester):
        return {"akademikDonemId": 806}

    def get_schedule(self, semester_id):
        return {"kayitSinifResultList": SCHEDULE}

    def get_final_calendar(self, semester_id):
        return {"finalBilgiList": []}


class FakePublic:
    def __init__(self):
        self.branches = []

    def get_final_exam_schedule(self, branch):
        self.branches.append(branch)
        return {"exams": [{"crn": "11731", "course_code": "UZB 419E", "date": "12.01.2027", "time": "09:30-11:30"},
                          {"crn": "55555", "course_code": "UZB 211", "date": "12.01.2027", "time": "13:30"}]}


class ExportToolTests(unittest.TestCase):
    def test_export_writes_one_file_with_every_source(self) -> None:
        from ninova_mcp.server import NinovaMcpApp

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"NINOVA_STATE_DIR": folder}):
            app = NinovaMcpApp()
            app._obs = FakeObs()
            app._itu_public = FakePublic()
            with patch.object(NinovaMcpApp, "get_upcoming_deadlines", return_value={"deadlines": [
                    {"title": "HW 3", "submission_end_iso": "2026-10-20T20:59:00+00:00"}]}):
                result = app.export_calendar()
            self.assertEqual(result["event_counts"], {"classes": 2, "exams": 1, "deadlines": 1})
            self.assertEqual(app._itu_public.branches, ["UZB"])
            text = Path(result["path"]).read_bytes().decode("utf-8")
            self.assertEqual(text, result["ics"])
            self.assertEqual(text.count("BEGIN:VEVENT"), 4)
            self.assertEqual(Path(result["path"]).stat().st_mode & 0o777, 0o600)

    def test_one_failing_source_becomes_a_warning(self) -> None:
        from ninova_mcp.server import NinovaMcpApp

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"NINOVA_STATE_DIR": folder}):
            app = NinovaMcpApp()
            app._obs = FakeObs()
            with patch.object(NinovaMcpApp, "get_upcoming_deadlines", side_effect=RuntimeError("Ninova down")):
                result = app.export_calendar(include=["classes", "deadlines"])
            self.assertEqual(result["event_counts"], {"classes": 2})
            self.assertTrue(any("Ninova down" in warning for warning in result["warnings"]))
            with self.assertRaises(ValueError):
                app.export_calendar(include=["grades"])
            with self.assertRaises(ValueError):
                app.export_calendar(include=["classes"], output_path=str(Path(folder) / "x.txt"))

    def test_remote_saves_in_managed_downloads(self) -> None:
        from ninova_mcp.remote_files import RemoteNinovaMcpApp

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            with patch.dict(os.environ, {"NINOVA_REMOTE_FILE_ROOT": str(root), "NINOVA_STATE_DIR": str(root / "state")}):
                os.environ.pop("NINOVA_REMOTE_REQUIRED_MOUNT", None)
                app = RemoteNinovaMcpApp()
                app._obs = FakeObs()
                result = app.export_calendar(include=["classes"], output_path="/etc/evil.ics")
            path = Path(result["path"])
            self.assertTrue(path.is_relative_to(root / "downloads" / "calendar"))
            self.assertTrue(path.name.endswith("itu-calendar.ics"))


if __name__ == "__main__":
    unittest.main()
