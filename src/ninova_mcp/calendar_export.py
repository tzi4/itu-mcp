"""Build an iCalendar (.ics) file of a student's classes, exams and deadlines.

Phones and calendar apps import the file or open it directly. Weekly classes
repeat until the end of the term, final exams and assignment deadlines are
single events (deadlines with a reminder the day before), and academic calendar
dates are all-day events. Times are local to Istanbul, which has used UTC+3
all year since 2016, so the file carries one fixed VTIMEZONE.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable

TZID = "Europe/Istanbul"
ISTANBUL = timezone(timedelta(hours=3), TZID)
PRODID = "-//ITU MCP + PaperBridge//Calendar export//EN"
WEEKDAYS = {"M": 0, "T": 1, "W": 2, "R": 3, "F": 4, "S": 5, "U": 6}
RRULE_DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_VTIMEZONE = (
    "BEGIN:VTIMEZONE",
    f"TZID:{TZID}",
    "BEGIN:STANDARD",
    "DTSTART:19700101T000000",
    "TZOFFSETFROM:+0300",
    "TZOFFSETTO:+0300",
    "TZNAME:+03",
    "END:STANDARD",
    "END:VTIMEZONE",
)
_DATE = re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})|(\d{4})-(\d{2})-(\d{2})")
_TIME = re.compile(r"(\d{1,2})[:.](\d{2})")


@dataclass
class Event:
    uid: str
    summary: str
    start: datetime | date
    end: datetime | date
    location: str = ""
    description: str = ""
    rrule: str = ""
    alarm_minutes: int | None = None
    category: str = ""


def escape(text: str) -> str:
    """Escape a TEXT value (RFC 5545 section 3.3.11)."""
    return (str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def fold(line: str) -> list[str]:
    """Split a content line into 75-octet pieces without breaking UTF-8 characters."""
    pieces, current, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > (75 if not pieces else 74):
            pieces.append(current)
            current, size = "", 0
        current += char
        size += width
    pieces.append(current)
    return [pieces[0]] + [" " + piece for piece in pieces[1:]]


def _stamp(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return value.astimezone(ISTANBUL).strftime("%Y%m%dT%H%M%S")
    return value.strftime("%Y%m%d")


def build_calendar(events: Iterable[Event], *, name: str = "ITU", now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{escape(name)}", f"X-WR-TIMEZONE:{TZID}", *_VTIMEZONE]
    for event in events:
        timed = isinstance(event.start, datetime)
        lines += ["BEGIN:VEVENT", f"UID:{event.uid}", f"DTSTAMP:{stamp}",
                  f"SUMMARY:{escape(event.summary)}"]
        if timed:
            lines += [f"DTSTART;TZID={TZID}:{_stamp(event.start)}", f"DTEND;TZID={TZID}:{_stamp(event.end)}"]
        else:
            lines += [f"DTSTART;VALUE=DATE:{_stamp(event.start)}", f"DTEND;VALUE=DATE:{_stamp(event.end)}"]
        if event.rrule:
            lines.append(f"RRULE:{event.rrule}")
        if event.location:
            lines.append(f"LOCATION:{escape(event.location)}")
        if event.description:
            lines.append(f"DESCRIPTION:{escape(event.description)}")
        if event.category:
            lines.append(f"CATEGORIES:{escape(event.category)}")
        if event.alarm_minutes is not None:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{escape(event.summary)}",
                      f"TRIGGER:-PT{int(event.alarm_minutes)}M", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "".join(piece + "\r\n" for line in lines for piece in fold(line))


def _uid(*parts: Any) -> str:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:24]
    return f"{digest}@itu-mcp"


def _term_date(value: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def class_events(items: Iterable[dict[str, Any]]) -> tuple[list[Event], list[str]]:
    """Weekly sessions from the OBS schedule (``kayitSinifResultList``)."""
    events, warnings = [], []
    for item in items:
        code = f"{item.get('bransKodu') or ''} {item.get('dersKodu') or ''}".strip()
        title = f"{code} {item.get('dersAdiEN') or item.get('dersAdiTR') or ''}".strip()
        first, last = _term_date(item.get("donemBaslangicTarihi")), _term_date(item.get("donemBitisTarihi"))
        sessions = item.get("sinifYerZaman") or []
        if not sessions:
            warnings.append(f"{code}: no weekly sessions in the schedule.")
            continue
        if not first or not last:
            warnings.append(f"{code}: the term dates are missing.")
            continue
        for session in sessions:
            day = WEEKDAYS.get(str(session.get("gunKodu") or "").upper())
            try:
                begin, finish = int(session["baslangicSaati"]), int(session["bitisSaati"])
            except (KeyError, TypeError, ValueError):
                day = None
            if day is None or not 0 <= begin < finish <= 24 * 60:
                warnings.append(f"{code}: a session has no usable day or time.")
                continue
            start_day = first + timedelta(days=(day - first.weekday()) % 7)
            start = datetime.combine(start_day, time(begin // 60, begin % 60), ISTANBUL)
            # OBS ends sessions at :29 or :59; round up to the next minute.
            end = datetime.combine(start_day, time(0), ISTANBUL) + timedelta(minutes=finish + 1)
            room = " ".join(part for part in (session.get("binaKodu"), session.get("mekanAdi")) if part)
            until = datetime.combine(last, time(23, 59, 59), ISTANBUL).astimezone(timezone.utc)
            events.append(Event(
                uid=_uid("class", item.get("crn"), session.get("id"), start_day),
                summary=title, start=start, end=end, location=room or "",
                description=f"CRN {item.get('crn')}" + (f", {session.get('binaAdi')}" if session.get("binaAdi") else ""),
                rrule=f"FREQ=WEEKLY;BYDAY={RRULE_DAYS[day]};UNTIL={until.strftime('%Y%m%dT%H%M%SZ')}",
                category="Class",
            ))
    return events, warnings


def _parse_when(date_text: Any, time_text: Any) -> tuple[datetime | None, datetime | None]:
    match = _DATE.search(str(date_text or ""))
    if not match:
        return None, None
    if match.group(1):
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    else:
        year, month, day = int(match.group(4)), int(match.group(5)), int(match.group(6))
    try:
        when = date(year, month, day)
    except ValueError:
        return None, None
    times = _TIME.findall(str(time_text or "")) or _TIME.findall(str(date_text or ""))
    if not times:
        return None, None
    start = datetime.combine(when, time(int(times[0][0]) % 24, int(times[0][1]) % 60), ISTANBUL)
    if len(times) > 1:
        end = datetime.combine(when, time(int(times[1][0]) % 24, int(times[1][1]) % 60), ISTANBUL)
        if end <= start:
            end = start + timedelta(hours=2)
    else:
        end = start + timedelta(hours=2)
    return start, end


def exam_events(rows: Iterable[dict[str, Any]], *, wanted: set[str] | None = None) -> tuple[list[Event], list[str]]:
    """Final exams from the public or personal OBS final calendar.

    ``wanted`` holds CRNs and course codes; rows that match neither are skipped.
    """
    events, warnings = [], []
    for row in rows:
        lowered = {str(key).lower(): value for key, value in row.items()}
        code = str(lowered.get("course_code") or lowered.get("derskodu") or "").strip()
        crn = str(lowered.get("crn") or "").strip()
        if wanted is not None and crn not in wanted and " ".join(code.split()).upper() not in wanted:
            continue
        date_text = next((value for key, value in lowered.items() if "date" in key or "tarih" in key), None)
        time_text = next((value for key, value in lowered.items() if "time" in key or "saat" in key), None)
        start, end = _parse_when(date_text, time_text)
        if start is None:
            warnings.append(f"{code or crn or 'An exam'}: the date or time could not be read.")
            continue
        room = " ".join(str(lowered.get(key) or "") for key in ("building", "room")).strip()
        name = lowered.get("course_name") or lowered.get("dersadi") or ""
        events.append(Event(uid=_uid("exam", crn or code, start), summary=f"Final: {code} {name}".strip(),
                            start=start, end=end, location=room, description=f"CRN {crn}" if crn else "",
                            alarm_minutes=24 * 60, category="Exam"))
    return events, warnings


def deadline_events(items: Iterable[dict[str, Any]]) -> tuple[list[Event], list[str]]:
    """Ninova assignment deadlines, as short events with a reminder a day before."""
    events, warnings = [], []
    for item in items:
        course = item.get("course")
        course_name = (course.get("code") or course.get("name")) if isinstance(course, dict) else course
        try:
            due = datetime.fromisoformat(str(item["submission_end_iso"])).astimezone(ISTANBUL)
        except (KeyError, TypeError, ValueError):
            warnings.append(f"{item.get('title') or 'An assignment'}: the deadline could not be read.")
            continue
        events.append(Event(uid=_uid("deadline", item.get("url") or item.get("title"), due),
                            summary=f"Due: {item.get('title') or 'Assignment'}" + (f" ({course_name})" if course_name else ""),
                            start=due - timedelta(minutes=30), end=due, description=str(item.get("url") or ""),
                            alarm_minutes=24 * 60, category="Deadline"))
    return events, warnings


def academic_events(items: Iterable[dict[str, Any]]) -> tuple[list[Event], list[str]]:
    """All-day academic calendar dates (holidays, registration, exam weeks)."""
    events, warnings = [], []
    for item in items:
        first, last = _term_date(item.get("start_date")), _term_date(item.get("end_date") or item.get("start_date"))
        if not first or not last:
            continue
        events.append(Event(uid=_uid("academic", item.get("description"), first),
                            summary=str(item.get("description") or "Academic calendar"),
                            start=first, end=last + timedelta(days=1), category="Academic calendar"))
    return events, warnings
