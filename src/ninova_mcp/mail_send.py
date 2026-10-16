"""Send a reviewed plain-text message from the user's İTÜ Mail account.

Sending is a two-step flow. ``prepare`` validates the message, builds it and
keeps it in memory under a random draft ID; it returns the exact preview the
user should approve. ``send`` takes only that draft ID and ``confirm=True`` and
submits the stored message unchanged, once. The client never passes the
content twice, so what is sent is what was previewed.

Recipients are limited to the configured domains (``itu.edu.tr`` and its
subdomains by default), which keeps a prompt-injected request from mailing
data to an outside address.
"""
from __future__ import annotations

import hashlib
import imaplib
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from pathlib import Path
from typing import Any, Callable

from .env import env_switch
from .mail_client import ItuMailClient, ItuMailError

DEFAULT_SMTP_HOST = "smtp.itu.edu.tr"
DEFAULT_SMTP_PORT = 587
DEFAULT_ALLOWED_DOMAINS = ("itu.edu.tr",)
DEFAULT_DAILY_LIMIT = 20
MAX_RECIPIENTS = 10
MAX_SUBJECT_CHARS = 250
MAX_BODY_CHARS = 20_000
DRAFT_TTL_SECONDS = 30 * 60
MAX_PENDING_DRAFTS = 20
SEND_ENV = "NINOVA_MAIL_SEND"
MIB = 1024 * 1024
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 15 * MIB
MAX_TOTAL_ATTACHMENT_BYTES = 20 * MIB
_ZIP = (b"PK\x03\x04", b"PK\x05\x06")
_OFFICE = "application/vnd.openxmlformats-officedocument."
# Extension -> MIME type and accepted leading bytes (None: plain text, checked separately).
ATTACHMENT_TYPES: dict[str, tuple[str, tuple[bytes, ...] | None]] = {
    ".pdf": ("application/pdf", (b"%PDF-",)),
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ".zip": ("application/zip", _ZIP),
    ".docx": (_OFFICE + "wordprocessingml.document", _ZIP),
    ".xlsx": (_OFFICE + "spreadsheetml.sheet", _ZIP),
    ".pptx": (_OFFICE + "presentationml.presentation", _ZIP),
    ".txt": ("text/plain", None),
}

_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")


def mail_send_enabled() -> bool:
    """Sending is on unless the operator sets NINOVA_MAIL_SEND=0."""
    return env_switch(SEND_ENV)


def allowed_domains() -> tuple[str, ...]:
    """Recipient domains from NINOVA_MAIL_SEND_ALLOWED_DOMAINS; ``*`` allows any."""
    value = os.getenv("NINOVA_MAIL_SEND_ALLOWED_DOMAINS") or ""
    domains = tuple(item.strip().lower().lstrip("@") for item in value.split(",") if item.strip())
    return domains or DEFAULT_ALLOWED_DOMAINS


def _domain_allowed(address: str, domains: tuple[str, ...]) -> bool:
    if "*" in domains:
        return True
    domain = address.rsplit("@", 1)[1]
    return any(domain == item or domain.endswith("." + item) for item in domains)


def _addresses(value: str | list[str] | None, field: str) -> list[str]:
    """Plain addresses only: no display names, groups or header syntax."""
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    result: list[str] = []
    for item in items:
        address = str(item).strip()
        if not address:
            continue
        if not _ADDRESS.fullmatch(address):
            raise ItuMailError(f"{field} must contain plain email addresses such as name@itu.edu.tr.")
        local, domain = address.rsplit("@", 1)
        address = f"{local}@{domain.lower()}"
        if address.lower() not in {existing.lower() for existing in result}:
            result.append(address)
    return result


def _single_line(value: str, field: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if any(char in str(value or "") for char in "\r\n\x00"):
        raise ItuMailError(f"{field} must be a single line.")
    if len(text) > limit:
        raise ItuMailError(f"{field} must be at most {limit} characters.")
    return text


def _message_ids(value: str) -> list[str]:
    return re.findall(r"<[^<>\s]{1,500}>", value or "")


def read_attachment(path: Path) -> tuple[str, str, bytes]:
    """Read one allowlisted file whose content matches its extension."""
    name = path.name
    kind = ATTACHMENT_TYPES.get(path.suffix.lower())
    if kind is None:
        raise ItuMailError(
            f"{name}: only " + ", ".join(sorted(ATTACHMENT_TYPES)) + " files can be attached."
        )
    if not path.is_file():
        raise ItuMailError(f"{name}: attachment is not an existing file.")
    if path.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise ItuMailError(f"{name}: each attachment must be at most {MAX_ATTACHMENT_BYTES // MIB} MiB.")
    data = path.read_bytes()
    content_type, magic = kind
    if magic is None:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ItuMailError(f"{name}: a .txt attachment must be UTF-8 text.") from exc
    elif not data.startswith(magic):
        raise ItuMailError(f"{name}: the file content does not match its {path.suffix} extension.")
    return name, content_type, data


@dataclass
class _Draft:
    message: EmailMessage
    recipients: list[str]
    expires: float


class ItuMailSender:
    """Prepare and send reviewed messages over verified STARTTLS SMTP."""

    def __init__(
        self,
        reader: ItuMailClient,
        *,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.reader = reader
        self.host = (os.getenv("NINOVA_MAIL_SMTP_HOST") or DEFAULT_SMTP_HOST).strip()
        self.port = int(os.getenv("NINOVA_MAIL_SMTP_PORT") or DEFAULT_SMTP_PORT)
        self.daily_limit = int(os.getenv("NINOVA_MAIL_SEND_DAILY_LIMIT") or DEFAULT_DAILY_LIMIT)
        self._smtp_factory = smtp_factory
        self._clock = clock
        self._drafts: dict[str, _Draft] = {}
        self._sent_at: deque[float] = deque()
        self._lock = threading.Lock()
        if not re.fullmatch(r"[A-Za-z0-9.-]+", self.host):
            raise ItuMailError("NINOVA_MAIL_SMTP_HOST must be a DNS hostname.")
        if not 1 <= self.port <= 65_535:
            raise ItuMailError("NINOVA_MAIL_SMTP_PORT must be between 1 and 65535.")
        if not 0 <= self.daily_limit <= 500:
            raise ItuMailError("NINOVA_MAIL_SEND_DAILY_LIMIT must be between 0 and 500.")

    @property
    def sender(self) -> str:
        username = self.reader.username
        address = username if "@" in username else f"{username}@itu.edu.tr"
        if not _ADDRESS.fullmatch(address):
            raise ItuMailError("Set NINOVA_MAIL_USERNAME or NINOVA_USERNAME to your İTÜ email address.")
        return address

    def _require_enabled(self) -> None:
        if not mail_send_enabled():
            raise ItuMailError(f"Sending mail is disabled by the operator ({SEND_ENV}=0).")

    def _expire(self, now: float) -> None:
        for draft_id in [key for key, draft in self._drafts.items() if draft.expires <= now]:
            del self._drafts[draft_id]
        while self._sent_at and self._sent_at[0] <= now - 86_400:
            self._sent_at.popleft()

    def prepare(
        self,
        to: str | list[str] | None,
        subject: str,
        body: str,
        cc: str | list[str] | None = None,
        reply_to_uid: str | None = None,
        attachments: list[str | Path] | None = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        sender = self.sender
        to_list = _addresses(to, "to")
        cc_list = [item for item in _addresses(cc, "cc")
                   if item.lower() not in {address.lower() for address in to_list}]
        subject = _single_line(subject, "subject", MAX_SUBJECT_CHARS)
        body = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
        if "\x00" in body:
            raise ItuMailError("body must not contain NUL characters.")
        if len(body) > MAX_BODY_CHARS:
            raise ItuMailError(f"body must be at most {MAX_BODY_CHARS} characters.")
        if not body.strip():
            raise ItuMailError("body must not be empty.")

        thread: dict[str, str] = {}
        if reply_to_uid:
            original = self.reader.reply_headers(str(reply_to_uid))
            if not to_list:
                source = original.get("reply_to") or original.get("from") or ""
                to_list = _addresses([address for _, address in getaddresses([source]) if address], "to")
            original_subject = original.get("subject") or ""
            if not subject:
                prefix = "" if re.match(r"(?i)\s*(re|ynt|yanıt)\s*:", original_subject) else "Re: "
                subject = _single_line(prefix + original_subject, "subject", MAX_SUBJECT_CHARS)
            parent = _message_ids(original.get("message_id") or "")[:1]
            if parent:
                references = (_message_ids(original.get("references") or "") + parent)[-20:]
                thread = {"In-Reply-To": parent[0], "References": " ".join(references)}

        recipients = to_list + cc_list
        if not to_list:
            raise ItuMailError("Give at least one recipient in to.")
        if len(recipients) > MAX_RECIPIENTS:
            raise ItuMailError(f"A message may have at most {MAX_RECIPIENTS} recipients.")
        domains = allowed_domains()
        blocked = [address for address in recipients if not _domain_allowed(address, domains)]
        if blocked:
            raise ItuMailError(
                "Recipients outside the allowed domains (" + ", ".join(domains) + ") were rejected: "
                + ", ".join(blocked) + ". The operator can widen NINOVA_MAIL_SEND_ALLOWED_DOMAINS."
            )
        if not subject:
            raise ItuMailError("subject must not be empty.")
        if len(attachments or []) > MAX_ATTACHMENTS:
            raise ItuMailError(f"Attach at most {MAX_ATTACHMENTS} files.")
        files = [read_attachment(Path(item).expanduser()) for item in attachments or []]
        if sum(len(data) for _, _, data in files) > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ItuMailError(f"Attachments must total at most {MAX_TOTAL_ATTACHMENT_BYTES // MIB} MiB.")

        message = EmailMessage()
        message["From"] = sender
        message["To"] = ", ".join(to_list)
        if cc_list:
            message["Cc"] = ", ".join(cc_list)
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[1])
        for header, value in thread.items():
            message[header] = value
        message.set_content(body)
        for name, content_type, data in files:
            maintype, subtype = content_type.split("/", 1)
            message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

        now = self._clock()
        with self._lock:
            self._expire(now)
            if len(self._drafts) >= MAX_PENDING_DRAFTS:
                oldest = min(self._drafts, key=lambda key: self._drafts[key].expires)
                del self._drafts[oldest]
            draft_id = secrets.token_urlsafe(16)
            self._drafts[draft_id] = _Draft(message, recipients, now + DRAFT_TTL_SECONDS)
        return {
            "draft_id": draft_id,
            "sent": False,
            "expires_at": datetime.fromtimestamp(now + DRAFT_TTL_SECONDS, UTC).isoformat(timespec="seconds"),
            "preview": {
                "from": sender,
                "to": to_list,
                "cc": cc_list,
                "subject": subject,
                "body": body,
                "in_reply_to_uid": str(reply_to_uid) if thread else None,
                "attachments": [
                    {"name": name, "content_type": content_type, "size_bytes": len(data),
                     "sha256": hashlib.sha256(data).hexdigest()}
                    for name, content_type, data in files
                ],
            },
            "next_step": (
                "Show the user this exact preview as plain text, not in an email compose card: "
                "a client's own Send button does not use İTÜ Mail. Only after they explicitly "
                "approve it, call mail_send_message with this draft_id and confirm=true. To "
                "change anything, prepare a new draft instead."
            ),
        }

    def send(self, draft_id: str, confirm: bool) -> dict[str, Any]:
        self._require_enabled()
        if confirm is not True:
            raise ItuMailError("Sending requires the user's explicit approval (confirm=true).")
        now = self._clock()
        with self._lock:
            self._expire(now)
            draft = self._drafts.pop(str(draft_id), None)
            if draft is None:
                raise ItuMailError("Unknown or expired draft_id. Prepare the message again.")
            if len(self._sent_at) >= self.daily_limit:
                self._drafts[str(draft_id)] = draft
                raise ItuMailError(
                    f"The daily sending limit ({self.daily_limit}) was reached. "
                    "The draft was kept; try again later."
                )
            self._sent_at.append(now)

        try:
            smtp = self._smtp_factory(self.host, self.port, timeout=self.reader.timeout)
        except OSError as exc:
            self._restore(draft_id, draft, now)
            raise ItuMailError(f"Could not reach {self.host}:{self.port}. Nothing was sent.") from exc
        try:
            try:
                smtp.ehlo()
                if not smtp.has_extn("starttls"):
                    raise ItuMailError("The mail server did not offer STARTTLS. Nothing was sent.")
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(self.reader.username, self.reader.password)
            except smtplib.SMTPAuthenticationError as exc:
                self._restore(draft_id, draft, now)
                raise ItuMailError("İTÜ Mail rejected the configured credentials. Nothing was sent.") from exc
            except (smtplib.SMTPException, ssl.SSLError, OSError, ItuMailError) as exc:
                self._restore(draft_id, draft, now)
                if isinstance(exc, ItuMailError):
                    raise
                raise ItuMailError("Could not open a verified TLS session. Nothing was sent.") from exc
            try:
                refused = smtp.send_message(draft.message, from_addr=self.sender, to_addrs=draft.recipients)
            except smtplib.SMTPRecipientsRefused as exc:
                raise ItuMailError("The mail server refused every recipient. Nothing was sent.") from exc
            except (smtplib.SMTPException, OSError) as exc:
                raise ItuMailError(
                    "Sending failed partway. The message may or may not have been sent; check the "
                    "sent folder before trying again."
                ) from exc
        finally:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                pass

        accepted = [address for address in draft.recipients if address not in refused]
        return {
            "sent": True,
            "message_id": draft.message["Message-ID"],
            "accepted": accepted,
            "refused": sorted(refused),
            "subject": draft.message["Subject"],
            "saved_to_sent": self._save_copy(draft.message),
        }

    def _restore(self, draft_id: str, draft: _Draft, sent_at: float) -> None:
        with self._lock:
            self._drafts[str(draft_id)] = draft
            try:
                self._sent_at.remove(sent_at)
            except ValueError:
                pass

    def _save_copy(self, message: EmailMessage) -> bool:
        """Append the sent message to the sent folder; SMTP alone does not keep a copy."""
        if not env_switch("NINOVA_MAIL_SAVE_SENT"):
            return False
        try:
            with self.reader._connection() as connection:
                mailbox = self.reader._sent_mailbox(connection)
                status, _ = connection.append(
                    mailbox, r"(\Seen)", imaplib.Time2Internaldate(time.time()), message.as_bytes(),
                )
                return status == "OK"
        except (ItuMailError, imaplib.IMAP4.error, OSError):
            return False
