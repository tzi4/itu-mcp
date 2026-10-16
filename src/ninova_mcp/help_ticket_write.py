"""Open an İTÜ help desk ticket (yardim.itu.edu.tr) after the user reviews it.

The help desk is an ASP.NET WebForms site: a unit's form lists categories,
choosing a category posts back to load its subcategories, and choosing a
subcategory may add extra fields. Selecting options only navigates; only the
"Yardım Biletini Oluştur" button creates a ticket.

Like sending mail, opening a ticket takes two steps. ``prepare`` checks the
unit, category, subcategory and extra fields against the live form, reads any
attachments and keeps the ticket in memory under a one-time draft ID.
``submit`` takes that ID and ``confirm=True``, fills a fresh form and presses
the button once.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlsplit

from .client import NinovaError
from .mail_client import ItuMailError
from .mail_send import read_attachment
from .parsing import clean_text, make_soup

HELP_ROOT = "https://yardim.itu.edu.tr/"
UNITS_URL = HELP_ROOT + "birimler.aspx"
DRAFT_TTL_SECONDS = 30 * 60
MAX_PENDING_DRAFTS = 10
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 8_000
MAX_FIELD_CHARS = 500
MAX_ATTACHMENTS = 5
# The help desk rejects 10 MiB or more in one file input.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024 - 1
CATEGORY = "CtrlTalepKonusuId"
SUBCATEGORY = "CtrlTalepAltKonusuId"
TITLE = "CtrlTitle"
DESCRIPTION = "CtrlDescription"
FILE_UPLOAD = "FileUpload1"
SUBMIT = "btnSubmit"
_EXTRA_ID = re.compile(r"rptDataParameter\$ctl\d+\$hfObjectId$")


def form_url(unit_id: int) -> str:
    # The login page passes through the central SSO and lands on the unit's form.
    return (HELP_ROOT + "login.aspx?ReturnUrl=%7e%2fbiletolustur.aspx%3fbirim%3d"
            f"{int(unit_id)}%26kid%3d0%26akid%3d0")


def parse_units(html: str) -> list[dict[str, Any]]:
    """Help desk units from birimler.aspx."""
    units: dict[int, str] = {}
    for anchor in make_soup(html).find_all("a", href=True):
        match = re.match(r"birim\.aspx\?birim=(\d+)", anchor["href"].strip(), re.I)
        name = clean_text(anchor.get_text(" ", strip=True))
        if match and name:
            units.setdefault(int(match[1]), name)
    return [{"id": unit_id, "name": name} for unit_id, name in units.items()]


def _control(soup: Any, suffix: str) -> Any:
    return soup.find(attrs={"name": re.compile(r"\$" + re.escape(suffix) + "$")})


def _options(select: Any) -> list[dict[str, Any]]:
    if select is None:
        return []
    return [{"id": int(option["value"]), "name": clean_text(option.get_text(" ", strip=True))}
            for option in select.find_all("option")
            if (option.get("value") or "").isdigit() and option["value"] != "0"]


def _selected(select: Any) -> int | None:
    option = select.find("option", selected=True) if select is not None else None
    value = option.get("value") if option is not None else None
    return int(value) if value and value.isdigit() and value != "0" else None


def _validator_enabled(html: str, control_id: str) -> bool:
    """Whether the RequiredFieldValidator bound to ``control_id`` is enabled."""
    pattern = r'(\w+)\.controltovalidate = "' + re.escape(control_id) + '"'
    for validator in re.findall(pattern, html):
        if not validator.endswith("rvData"):
            continue
        return not re.search(re.escape(validator) + r'\.enabled = "False"', html)
    return False


def parse_form(html: str) -> dict[str, Any]:
    """Read the ticket form: choices, extra fields, the requester and listed tickets."""
    soup = make_soup(html)
    if _control(soup, SUBMIT) is None or _control(soup, TITLE) is None:
        raise NinovaError("The help desk did not return the ticket form; sign-in may have failed.")
    category, subcategory = _control(soup, CATEGORY), _control(soup, SUBCATEGORY)
    extras: list[dict[str, Any]] = []
    for hidden in soup.find_all("input", attrs={"name": _EXTRA_ID}):
        prefix = hidden["name"].rsplit("$", 1)[0] + "$"
        control = next((element for element in soup.find_all(["input", "select", "textarea"])
                        if (element.get("name") or "").startswith(prefix)
                        and not element["name"].endswith("hfObjectId")), None)
        if control is None:
            continue
        kind = "file" if control.get("type") == "file" else (
            "text" if control.name in ("input", "textarea") and control.get("type") in (None, "text")
            else "unsupported")
        label_node = control.find_previous_sibling("span")
        label = clean_text(control.get("placeholder") or (label_node.get_text(" ", strip=True) if label_node else ""))
        extras.append({
            "key": str(hidden.get("value") or ""),
            "label": label or f"Field {hidden.get('value')}",
            "type": kind,
            "required": _validator_enabled(html, control.get("id") or ""),
            "_name": control["name"],
        })
    text = clean_text(soup.get_text(" ", strip=True))
    requester = re.search(r"Sayın\s+(.+?),\s+(\S+@\S+)\s+e-posta adresinizle", text)
    tickets = {int(match) for match in re.findall(r"bilet\.aspx\?id=(\d+)", html, re.I)}
    return {
        "categories": _options(category),
        "category": _selected(category),
        "subcategories": _options(subcategory) if subcategory is not None else None,
        "subcategory": _selected(subcategory),
        "extra_fields": extras,
        "requester": {"name": requester[1], "email": requester[2]} if requester else None,
        "listed_ticket_ids": tickets,
    }


def form_values(html: str) -> tuple[str, dict[str, str]]:
    """The form's action and the values a browser would post back."""
    soup = make_soup(html)
    form = soup.find("form")
    if form is None:
        raise NinovaError("The help desk page has no form.")
    values: dict[str, str] = {}
    for element in form.find_all("input"):
        name, kind = element.get("name"), (element.get("type") or "text").lower()
        if name and kind in ("hidden", "text"):
            values[name] = element.get("value") or ""
    for element in form.find_all("textarea"):
        if element.get("name"):
            values[element["name"]] = element.get_text()
    for select in form.find_all("select"):
        option = select.find("option", selected=True) or select.find("option")
        if select.get("name"):
            values[select["name"]] = option.get("value", "") if option else ""
    return form.get("action") or "", values


def _read(path: Any) -> tuple[str, str, bytes]:
    try:
        return read_attachment(Path(str(path)).expanduser())
    except ItuMailError as exc:
        raise NinovaError(str(exc)) from exc


def public_extra(field_info: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in field_info.items() if not key.startswith("_")}


def _clean_text(value: str, label: str, limit: int, multiline: bool = False) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not multiline:
        text = " ".join(text.split())
    if "<" in text or ">" in text:
        raise NinovaError(f"{label} must not contain < or >; the help desk rejects them.")
    if "\x00" in text:
        raise NinovaError(f"{label} must not contain NUL characters.")
    if len(text) > limit:
        raise NinovaError(f"{label} must be at most {limit} characters.")
    return text


@dataclass
class _Ticket:
    unit_id: int
    category_id: int
    subcategory_id: int | None
    title: str
    description: str
    extras: dict[str, str]
    files: list[tuple[str, str, bytes]]
    extra_files: dict[str, list[tuple[str, str, bytes]]] = field(default_factory=dict)
    expires: float = 0.0


class HelpTicketWriter:
    """Navigate the help desk form, then open a reviewed ticket once."""

    def __init__(self, client: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self._clock = clock
        self._drafts: dict[str, _Ticket] = {}
        self._lock = threading.Lock()

    # -- navigation -------------------------------------------------------

    def _open(self, unit_id: int) -> tuple[str, str]:
        html, response = self.client.get_html(form_url(unit_id))
        if urlsplit(response.url).path.lower() != "/biletolustur.aspx":
            raise NinovaError("The help desk did not open the ticket form for that unit.")
        return html, response.url

    def _post(self, html: str, url: str, values: dict[str, str],
              files: list[tuple[str, tuple[str, bytes, str]]] | None = None) -> tuple[str, str]:
        action, posted = form_values(html)
        posted.update(values)
        target = urljoin(url, action)
        # An empty file part keeps the post multipart, as a browser sends it.
        parts = files or [(self._name(html, FILE_UPLOAD), ("", b"", "application/octet-stream"))]
        response = self.client.post_multipart(target, data=posted, files=parts, referer=url, timeout=90)
        return response.text, response.url

    @staticmethod
    def _name(html: str, suffix: str) -> str:
        control = _control(make_soup(html), suffix)
        if control is None:
            raise NinovaError(f"The help desk form has no {suffix} field.")
        return control["name"]

    def _choose(self, html: str, url: str, suffix: str, value: int) -> tuple[str, str]:
        name = self._name(html, suffix)
        return self._post(html, url, {name: str(value), "__EVENTTARGET": name, "__EVENTARGUMENT": ""})

    def _navigate(self, unit_id: int, category_id: int | None,
                  subcategory_id: int | None) -> tuple[str, str, dict[str, Any]]:
        html, url = self._open(unit_id)
        form = parse_form(html)
        if category_id is None:
            return html, url, form
        if category_id not in {item["id"] for item in form["categories"]}:
            raise NinovaError("That category is not offered by this unit. List the categories first.")
        html, url = self._choose(html, url, CATEGORY, category_id)
        form = parse_form(html)
        if form["category"] != category_id:
            raise NinovaError("The help desk did not accept that category.")
        if subcategory_id is None:
            return html, url, form
        if form["subcategories"] is None:
            raise NinovaError("This category has no subcategories; omit subcategory_id.")
        if subcategory_id not in {item["id"] for item in form["subcategories"]}:
            raise NinovaError("That subcategory is not offered by this category. List them first.")
        html, url = self._choose(html, url, SUBCATEGORY, subcategory_id)
        form = parse_form(html)
        if form["subcategory"] != subcategory_id:
            raise NinovaError("The help desk did not accept that subcategory.")
        return html, url, form

    def options(self, unit_id: int | None = None, category_id: int | None = None,
                subcategory_id: int | None = None) -> dict[str, Any]:
        if unit_id is None:
            html, _ = self.client.get_html(UNITS_URL)
            units = parse_units(html)
            if not units:
                raise NinovaError("Could not read the help desk units.")
            return {"units": units, "next_step": "Pass unit_id to list its categories."}
        _, _, form = self._navigate(unit_id, category_id, subcategory_id)
        result: dict[str, Any] = {"unit_id": unit_id, "requester": form["requester"]}
        if category_id is None:
            result["categories"] = form["categories"]
            result["next_step"] = "Pass category_id to see its subcategories."
            return result
        result["category_id"] = category_id
        result["subcategories"] = form["subcategories"]
        if subcategory_id is not None or form["subcategories"] is None:
            result["subcategory_id"] = subcategory_id
            result["extra_fields"] = [public_extra(item) for item in form["extra_fields"]]
            result["next_step"] = "Write the ticket with help_prepare_ticket."
        else:
            result["next_step"] = "Pass subcategory_id to see its extra fields."
        return result

    # -- drafts -----------------------------------------------------------

    def prepare(self, unit_id: int, category_id: int, title: str, description: str,
                subcategory_id: int | None = None, extra_fields: dict[str, str] | None = None,
                attachments: list[str | Path] | None = None) -> dict[str, Any]:
        title = _clean_text(title, "title", MAX_TITLE_CHARS)
        description = _clean_text(description, "description", MAX_DESCRIPTION_CHARS, multiline=True)
        if not title or not description:
            raise NinovaError("Give both a title and a description.")
        _, _, form = self._navigate(unit_id, category_id, subcategory_id)
        if form["subcategories"] and subcategory_id is None:
            raise NinovaError("This category requires subcategory_id. List its subcategories first.")

        supplied = {str(key): value for key, value in (extra_fields or {}).items()}
        known = {item["key"]: item for item in form["extra_fields"]}
        unknown = sorted(set(supplied) - set(known))
        if unknown:
            raise NinovaError("Unknown extra field keys: " + ", ".join(unknown) + ".")
        extras: dict[str, str] = {}
        extra_files: dict[str, list[tuple[str, str, bytes]]] = {}
        for key, item in known.items():
            value = supplied.get(key)
            if item["type"] == "unsupported":
                if item["required"]:
                    raise NinovaError(f"'{item['label']}' cannot be filled here; open this ticket on the website.")
                continue
            if item["type"] == "file":
                if value is not None and not isinstance(value, list):
                    raise NinovaError(f"'{item['label']}' is a file field; pass a list of file paths.")
                paths = value or []
                extra_files[key] = [_read(path) for path in paths]
                self._check_size(extra_files[key], item["label"])
                if item["required"] and not extra_files[key]:
                    raise NinovaError(f"'{item['label']}' requires a file.")
                continue
            if isinstance(value, list):
                raise NinovaError(f"'{item['label']}' is a text field; pass a string.")
            text = _clean_text(value or "", item["label"], MAX_FIELD_CHARS)
            if item["required"] and not text:
                raise NinovaError(f"'{item['label']}' is required.")
            extras[key] = text
        if len(attachments or []) > MAX_ATTACHMENTS:
            raise NinovaError(f"Attach at most {MAX_ATTACHMENTS} files.")
        files = [_read(item) for item in attachments or []]
        self._check_size(files, "attachments")

        category = next(item["name"] for item in form["categories"] if item["id"] == category_id)
        subcategory = next((item["name"] for item in form["subcategories"] or []
                            if item["id"] == subcategory_id), None)
        now = self._clock()
        ticket = _Ticket(unit_id, category_id, subcategory_id, title, description, extras, files,
                         extra_files, now + DRAFT_TTL_SECONDS)
        with self._lock:
            for key in [key for key, draft in self._drafts.items() if draft.expires <= now]:
                del self._drafts[key]
            if len(self._drafts) >= MAX_PENDING_DRAFTS:
                del self._drafts[min(self._drafts, key=lambda key: self._drafts[key].expires)]
            draft_id = secrets.token_urlsafe(16)
            self._drafts[draft_id] = ticket

        def listed(items: list[tuple[str, str, bytes]]) -> list[dict[str, Any]]:
            return [{"name": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    for name, _, data in items]

        return {
            "draft_id": draft_id,
            "submitted": False,
            "preview": {
                "requester": form["requester"],
                "unit_id": unit_id,
                "category": category,
                "subcategory": subcategory,
                "title": title,
                "description": description,
                "extra_fields": [
                    {**public_extra(item), "value": extras.get(key) if item["type"] == "text"
                     else listed(extra_files.get(key, []))}
                    for key, item in known.items() if item["type"] != "unsupported"
                ],
                "attachments": listed(files),
            },
            "next_step": (
                "Show the user this exact preview. Only after they explicitly approve it, call "
                "help_submit_ticket with this draft_id and confirm=true. Replies go to the "
                "requester email. To change anything, prepare a new draft."
            ),
        }

    @staticmethod
    def _check_size(files: list[tuple[str, str, bytes]], label: str) -> None:
        if sum(len(data) for _, _, data in files) > MAX_UPLOAD_BYTES:
            raise NinovaError(f"{label}: files must total less than 10 MiB.")

    def submit(self, draft_id: str, confirm: bool) -> dict[str, Any]:
        if confirm is not True:
            raise NinovaError("Opening a ticket requires the user's explicit approval (confirm=true).")
        with self._lock:
            ticket = self._drafts.pop(str(draft_id), None)
        if ticket is None or ticket.expires <= self._clock():
            raise NinovaError("Unknown or expired draft_id. Prepare the ticket again.")
        try:
            html, url, form = self._navigate(ticket.unit_id, ticket.category_id, ticket.subcategory_id)
            values = {self._name(html, TITLE): ticket.title,
                      self._name(html, DESCRIPTION): ticket.description,
                      self._name(html, SUBMIT): "Yardım Biletini Oluştur",
                      "__EVENTTARGET": "", "__EVENTARGUMENT": ""}
            extras = {item["key"]: item for item in form["extra_fields"]}
            for key, text in ticket.extras.items():
                values[extras[key]["_name"]] = text
            parts = [(self._name(html, FILE_UPLOAD), (name, data, content_type))
                     for name, content_type, data in ticket.files]
            for key, files in ticket.extra_files.items():
                parts += [(extras[key]["_name"], (name, data, content_type)) for name, content_type, data in files]
        except Exception:
            # Nothing was submitted yet, so the draft can be used again.
            with self._lock:
                self._drafts[str(draft_id)] = ticket
            raise
        try:
            result_html, result_url = self._post(html, url, values, parts or None)
        except Exception as exc:
            raise NinovaError(
                "Submitting failed partway. The ticket may or may not exist; check your tickets "
                "with obs_get_help_tickets before trying again."
            ) from exc
        return self._outcome(result_html, result_url, form["listed_ticket_ids"])

    def _outcome(self, html: str, url: str, before: set[int]) -> dict[str, Any]:
        path = urlsplit(url).path.lower()
        ids = [value for key, values in parse_qs(urlsplit(url).query).items()
               if key.lower() == "id" for value in values if value.isdigit()]
        if path in ("/bilet.aspx", "/itubilet.aspx") and ids:
            ticket_id = int(ids[0])
        else:
            new = sorted({int(match) for match in re.findall(r"bilet\.aspx\?id=(\d+)", html, re.I)} - before)
            ticket_id = new[-1] if len(new) == 1 else None
        text = clean_text(make_soup(html).get_text(" ", strip=True))
        if ticket_id is None:
            return {
                "submitted": "uncertain",
                "message": "The help desk answered without a new ticket number. Check your tickets with "
                           "obs_get_help_tickets before trying again.",
                "page_excerpt": text[:400],
                "untrusted_external_content": True,
            }
        return {"submitted": True, "ticket_id": ticket_id,
                "url": f"{HELP_ROOT}bilet.aspx?id={ticket_id}"}

