from __future__ import annotations

import os
import smtplib
import tempfile
import unittest
from pathlib import Path
from email.message import EmailMessage
from unittest.mock import patch

from ninova_mcp.mail_client import ItuMailClient, ItuMailError
from ninova_mcp.mail_send import DRAFT_TTL_SECONDS, ItuMailSender


def _original() -> bytes:
    message = EmailMessage()
    message["From"] = "Lecturer <lecturer@itu.edu.tr>"
    message["Reply-To"] = "office@ce.itu.edu.tr"
    message["Subject"] = "Project deadline"
    message["Message-ID"] = "<parent@itu.edu.tr>"
    message["References"] = "<root@itu.edu.tr>"
    return message.as_bytes().split(b"\n\n", 1)[0] + b"\n\n"


class FakeImap:
    instances: list["FakeImap"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.appended: list[tuple[bytes, str, bytes]] = []
        self.selected: list[tuple[str, bool]] = []
        FakeImap.instances.append(self)

    def login(self, username, password):
        return "OK", [b"logged in"]

    def select(self, mailbox, readonly=False):
        self.selected.append((mailbox, readonly))
        return "OK", [b"3"]

    capabilities = ("IMAP4REV1", "LITERAL+")

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searches = getattr(self, "searches", []) + [(self.selected[-1][0], args)]
            return "OK", [b"3 12 7" if self.selected[-1][0] == "INBOX" else b""]
        uid = args[0]
        return "OK", [(b"1 (UID " + uid.encode() + b")", _original())]

    def list(self, *args):
        return "OK", [b'(\\HasNoChildren \\Sent) "/" "Sent"']

    def append(self, mailbox, flags, date, data):
        self.appended.append((mailbox, flags, data))
        return "OK", [b"appended"]

    def logout(self):
        return "BYE", []


class FakeSmtp:
    instances: list["FakeSmtp"] = []

    def __init__(self, host, port, timeout=None, *, starttls=True, refuse=(), fail=None) -> None:
        self.host, self.port = host, port
        self.offers_starttls = starttls
        self.refuse = set(refuse)
        self.fail = fail
        self.calls: list[str] = []
        self.sent: list[tuple[EmailMessage, str, list[str]]] = []
        FakeSmtp.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def has_extn(self, name):
        return self.offers_starttls and name == "starttls"

    def starttls(self, context=None):
        self.calls.append("starttls")
        self.tls_context = context

    def login(self, username, password):
        self.calls.append("login")
        if self.fail == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, message, from_addr=None, to_addrs=None):
        if self.fail == "data":
            raise smtplib.SMTPDataError(451, b"later")
        if self.refuse >= set(to_addrs):
            raise smtplib.SMTPRecipientsRefused({address: (550, b"no") for address in to_addrs})
        self.sent.append((message, from_addr, list(to_addrs)))
        return {address: (550, b"no") for address in to_addrs if address in self.refuse}

    def quit(self):
        self.calls.append("quit")


class MailSendTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeImap.instances.clear()
        FakeSmtp.instances.clear()
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("NINOVA_MAIL_SEND", "NINOVA_MAIL_SEND_ALLOWED_DOMAINS",
                     "NINOVA_MAIL_SEND_DAILY_LIMIT", "NINOVA_MAIL_SAVE_SENT"):
            os.environ.pop(name, None)
        self.now = 1_000_000.0

    def sender(self, username: str = "student", **smtp_options) -> ItuMailSender:
        reader = ItuMailClient(username=username, password="pw", imap_factory=FakeImap)
        return ItuMailSender(
            reader,
            smtp_factory=lambda host, port, timeout=None: FakeSmtp(host, port, timeout, **smtp_options),
            clock=lambda: self.now,
        )

    def prepare(self, sender: ItuMailSender, **overrides):
        values = {"to": ["lecturer@itu.edu.tr"], "subject": "Question", "body": "Hello,\nThanks."}
        values.update(overrides)
        return sender.prepare(values.pop("to"), values.pop("subject"), values.pop("body"), **values)

    def test_prepare_previews_without_sending(self) -> None:
        sender = self.sender()
        draft = self.prepare(sender, cc="ta@itu.edu.tr, lecturer@itu.edu.tr")
        self.assertFalse(draft["sent"])
        self.assertEqual(draft["preview"]["from"], "student@itu.edu.tr")
        self.assertEqual(draft["preview"]["to"], ["lecturer@itu.edu.tr"])
        self.assertEqual(draft["preview"]["cc"], ["ta@itu.edu.tr"])
        self.assertEqual(draft["preview"]["body"], "Hello,\nThanks.")
        self.assertEqual(FakeSmtp.instances, [])

    def test_send_submits_the_previewed_message_once_over_starttls(self) -> None:
        sender = self.sender()
        draft = self.prepare(sender)
        result = sender.send(draft["draft_id"], True)
        smtp = FakeSmtp.instances[0]
        self.assertEqual((smtp.host, smtp.port), ("smtp.itu.edu.tr", 587))
        self.assertEqual(smtp.calls[:4], ["ehlo", "starttls", "ehlo", "login"])
        message, from_addr, recipients = smtp.sent[0]
        self.assertEqual(from_addr, "student@itu.edu.tr")
        self.assertEqual(recipients, ["lecturer@itu.edu.tr"])
        self.assertEqual(message.get_content().rstrip("\n"), "Hello,\nThanks.")
        self.assertTrue(result["sent"])
        self.assertEqual(result["accepted"], ["lecturer@itu.edu.tr"])
        self.assertTrue(result["saved_to_sent"])
        self.assertEqual(FakeImap.instances[-1].appended[0][0], b'"Sent"')
        with self.assertRaisesRegex(ItuMailError, "Unknown or expired"):
            sender.send(draft["draft_id"], True)

    def test_send_requires_explicit_confirmation(self) -> None:
        sender = self.sender()
        draft = self.prepare(sender)
        for value in (False, "true", 1):
            with self.assertRaisesRegex(ItuMailError, "confirm=true"):
                sender.send(draft["draft_id"], value)  # type: ignore[arg-type]
        self.assertEqual(FakeSmtp.instances, [])
        sender.send(draft["draft_id"], True)

    def test_outside_domains_are_rejected(self) -> None:
        sender = self.sender()
        for to in (["someone@gmail.com"], ["a@itu.edu.tr.evil.com"], ["a@notitu.edu.tr"]):
            with self.subTest(to=to), self.assertRaisesRegex(ItuMailError, "allowed domains"):
                self.prepare(sender, to=to)
        self.assertEqual(self.prepare(sender, to=["ta@ce.itu.edu.tr"])["preview"]["to"], ["ta@ce.itu.edu.tr"])

    def test_operator_can_widen_domains(self) -> None:
        os.environ["NINOVA_MAIL_SEND_ALLOWED_DOMAINS"] = "itu.edu.tr, example.org"
        self.assertTrue(self.prepare(self.sender(), to=["x@example.org"])["draft_id"])
        os.environ["NINOVA_MAIL_SEND_ALLOWED_DOMAINS"] = "*"
        self.assertTrue(self.prepare(self.sender(), to=["x@gmail.com"])["draft_id"])

    def test_header_injection_and_display_names_are_rejected(self) -> None:
        sender = self.sender()
        for to in (["Lecturer <lecturer@itu.edu.tr>"], ["a@itu.edu.tr\nBcc: x@gmail.com"], ["not-an-address"]):
            with self.subTest(to=to), self.assertRaisesRegex(ItuMailError, "plain email addresses"):
                self.prepare(sender, to=to)
        with self.assertRaisesRegex(ItuMailError, "single line"):
            self.prepare(sender, subject="Hi\r\nBcc: x@gmail.com")

    def test_limits(self) -> None:
        sender = self.sender()
        with self.assertRaisesRegex(ItuMailError, "at most 10 recipients"):
            self.prepare(sender, to=[f"p{i}@itu.edu.tr" for i in range(11)])
        with self.assertRaisesRegex(ItuMailError, "body must not be empty"):
            self.prepare(sender, body="  ")
        with self.assertRaisesRegex(ItuMailError, "subject must not be empty"):
            self.prepare(sender, subject="")
        with self.assertRaisesRegex(ItuMailError, "at least one recipient"):
            self.prepare(sender, to=[])

    def test_reply_fills_recipient_subject_and_thread(self) -> None:
        sender = self.sender()
        draft = sender.prepare(None, "", "Thank you.", reply_to_uid="42")
        self.assertEqual(draft["preview"]["to"], ["office@ce.itu.edu.tr"])
        self.assertEqual(draft["preview"]["subject"], "Re: Project deadline")
        self.assertTrue(all(readonly for _, readonly in FakeImap.instances[0].selected))
        sender.send(draft["draft_id"], True)
        message = FakeSmtp.instances[0].sent[0][0]
        self.assertEqual(message["In-Reply-To"], "<parent@itu.edu.tr>")
        self.assertEqual(message["References"], "<root@itu.edu.tr> <parent@itu.edu.tr>")

    def test_drafts_expire(self) -> None:
        sender = self.sender()
        draft = self.prepare(sender)
        self.now += DRAFT_TTL_SECONDS + 1
        with self.assertRaisesRegex(ItuMailError, "Unknown or expired"):
            sender.send(draft["draft_id"], True)

    def test_daily_limit_keeps_the_draft(self) -> None:
        os.environ["NINOVA_MAIL_SEND_DAILY_LIMIT"] = "1"
        sender = self.sender()
        sender.send(self.prepare(sender)["draft_id"], True)
        second = self.prepare(sender)
        with self.assertRaisesRegex(ItuMailError, "daily sending limit"):
            sender.send(second["draft_id"], True)
        self.assertIn(second["draft_id"], sender._drafts)
        self.now += 86_401
        self.assertTrue(sender.send(self.prepare(sender)["draft_id"], True)["sent"])

    def test_failures_before_data_keep_the_draft(self) -> None:
        sender = self.sender(fail="auth")
        draft = self.prepare(sender)
        with self.assertRaisesRegex(ItuMailError, "Nothing was sent"):
            sender.send(draft["draft_id"], True)
        sender._smtp_factory = lambda host, port, timeout=None: FakeSmtp(host, port, timeout)
        self.assertTrue(sender.send(draft["draft_id"], True)["sent"])

    def test_missing_starttls_does_not_log_in(self) -> None:
        sender = self.sender(starttls=False)
        with self.assertRaisesRegex(ItuMailError, "STARTTLS"):
            sender.send(self.prepare(sender)["draft_id"], True)
        self.assertNotIn("login", FakeSmtp.instances[0].calls)

    def test_failure_during_data_is_reported_as_uncertain(self) -> None:
        sender = self.sender(fail="data")
        draft = self.prepare(sender)
        with self.assertRaisesRegex(ItuMailError, "may or may not"):
            sender.send(draft["draft_id"], True)
        with self.assertRaisesRegex(ItuMailError, "Unknown or expired"):
            sender.send(draft["draft_id"], True)

    def test_partial_refusal_is_reported(self) -> None:
        sender = self.sender(refuse={"ta@itu.edu.tr"})
        result = sender.send(self.prepare(sender, cc=["ta@itu.edu.tr"])["draft_id"], True)
        self.assertEqual(result["accepted"], ["lecturer@itu.edu.tr"])
        self.assertEqual(result["refused"], ["ta@itu.edu.tr"])

    def test_operator_switches(self) -> None:
        os.environ["NINOVA_MAIL_SAVE_SENT"] = "0"
        sender = self.sender()
        self.assertFalse(sender.send(self.prepare(sender)["draft_id"], True)["saved_to_sent"])
        draft = self.prepare(sender)
        os.environ["NINOVA_MAIL_SEND"] = "0"
        with self.assertRaisesRegex(ItuMailError, "disabled"):
            self.prepare(sender)
        with self.assertRaisesRegex(ItuMailError, "disabled"):
            sender.send(draft["draft_id"], True)

    def test_full_username_is_used_as_sender(self) -> None:
        draft = self.prepare(self.sender("student@itu.edu.tr"))
        self.assertEqual(draft["preview"]["from"], "student@itu.edu.tr")


class MailAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        FakeSmtp.instances.clear()

    def sender(self) -> ItuMailSender:
        reader = ItuMailClient(username="student", password="pw", imap_factory=FakeImap)
        return ItuMailSender(reader, smtp_factory=lambda host, port, timeout=None: FakeSmtp(host, port))

    def write(self, name: str, data: bytes) -> str:
        path = self.root / name
        path.write_bytes(data)
        return str(path)

    def test_allowed_files_are_attached_and_listed(self) -> None:
        files = [self.write("report.pdf", b"%PDF-1.7 body"), self.write("photo.JPG", b"\xff\xd8\xff\xe0 x"),
                 self.write("code.zip", b"PK\x03\x04 zip"), self.write("notes.txt", "Not: ödev".encode())]
        sender = self.sender()
        draft = sender.prepare(["lecturer@itu.edu.tr"], "Homework", "Attached.", attachments=files)
        listed = draft["preview"]["attachments"]
        self.assertEqual([item["name"] for item in listed], ["report.pdf", "photo.JPG", "code.zip", "notes.txt"])
        self.assertEqual(listed[0]["content_type"], "application/pdf")
        self.assertEqual(listed[2]["content_type"], "application/zip")
        self.assertEqual(len(listed[0]["sha256"]), 64)
        sender.send(draft["draft_id"], True)
        message = FakeSmtp.instances[0].sent[0][0]
        parts = list(message.iter_attachments())
        self.assertEqual([part.get_filename() for part in parts], ["report.pdf", "photo.JPG", "code.zip", "notes.txt"])
        self.assertEqual(parts[0].get_content(), b"%PDF-1.7 body")

    def test_the_previewed_bytes_are_sent_even_if_the_file_changes(self) -> None:
        path = self.write("report.pdf", b"%PDF- first")
        sender = self.sender()
        draft = sender.prepare(["lecturer@itu.edu.tr"], "Homework", "Attached.", attachments=[path])
        Path(path).write_bytes(b"%PDF- second")
        sender.send(draft["draft_id"], True)
        part = next(FakeSmtp.instances[0].sent[0][0].iter_attachments())
        self.assertEqual(part.get_content(), b"%PDF- first")

    def test_disallowed_or_mismatched_files_are_rejected(self) -> None:
        sender = self.sender()
        cases = {
            "id_rsa": (b"-----BEGIN", "can be attached"),
            "run.exe": (b"MZ", "can be attached"),
            "fake.pdf": (b"MZ not a pdf", "does not match"),
            "fake.zip": (b"%PDF-", "does not match"),
            "binary.txt": (b"\xff\xfe\x00", "UTF-8"),
        }
        for name, (data, message) in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(ItuMailError, message):
                sender.prepare(["a@itu.edu.tr"], "s", "b", attachments=[self.write(name, data)])
        with self.assertRaisesRegex(ItuMailError, "not an existing file"):
            sender.prepare(["a@itu.edu.tr"], "s", "b", attachments=[str(self.root / "missing.pdf")])

    def test_size_and_count_limits(self) -> None:
        sender = self.sender()
        with patch("ninova_mcp.mail_send.MAX_ATTACHMENT_BYTES", 8):
            with self.assertRaisesRegex(ItuMailError, "each attachment"):
                sender.prepare(["a@itu.edu.tr"], "s", "b", attachments=[self.write("a.pdf", b"%PDF-123456")])
        with patch("ninova_mcp.mail_send.MAX_TOTAL_ATTACHMENT_BYTES", 15):
            files = [self.write(f"{i}.pdf", b"%PDF-1234") for i in range(2)]
            with self.assertRaisesRegex(ItuMailError, "total"):
                sender.prepare(["a@itu.edu.tr"], "s", "b", attachments=files)
        with self.assertRaisesRegex(ItuMailError, "at most 10 files"):
            sender.prepare(["a@itu.edu.tr"], "s", "b", attachments=["x.pdf"] * 11)


class MailSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeImap.instances.clear()
        self.client = ItuMailClient(username="student", password="pw", imap_factory=FakeImap)

    def test_search_runs_on_the_server_in_both_mailboxes(self) -> None:
        result = self.client.search(query='lineer "MAT 261"', sender="lecturer", since="2023-01-31",
                                    unread_only=True, limit=2)
        imap = FakeImap.instances[0]
        self.assertTrue(all(readonly for _, readonly in imap.selected))
        self.assertEqual([mailbox for mailbox, _ in imap.searches], ["INBOX", b'"Sent"'])
        criteria = imap.searches[0][1]
        self.assertEqual(criteria[:2], ("CHARSET", "UTF-8"))
        self.assertEqual(criteria[2:5], ("UNSEEN", "SINCE", "31-Jan-2023"))
        self.assertEqual(criteria[5:14], ("OR", "SUBJECT", b'"lineer"', "OR", "FROM", b'"lineer"',
                                          "OR", "TO", b'"lineer"'))
        self.assertIn(b'"MAT 261"', criteria)
        self.assertEqual(criteria[-2:], ("FROM", b'"lecturer"'))
        self.assertEqual(result["matched_by_mailbox"], {"INBOX": 3, "SENT": 0})
        self.assertEqual([item["uid"] for item in result["messages"]], ["12", "7"])

    def test_non_ascii_terms_use_utf8_literals(self) -> None:
        self.client.search(query="Özdikililer", mailbox="INBOX")
        criteria = FakeImap.instances[0].searches[0][1]
        encoded = "Özdikililer".encode()
        self.assertIn(b"{" + str(len(encoded)).encode() + b"+}\r\n" + encoded, criteria)

    def test_body_search_uses_text(self) -> None:
        self.client.search(query="cebir", in_body=True, mailbox="SENT")
        criteria = FakeImap.instances[0].searches[0][1]
        self.assertEqual(criteria[2:], ("TEXT", b'"cebir"'))

    def test_quotes_are_escaped_and_input_validated(self) -> None:
        self.client.search(subject='say \\"hi', mailbox="INBOX")
        self.assertIn(b'"say"', FakeImap.instances[0].searches[0][1])
        with self.assertRaisesRegex(ItuMailError, "Give query"):
            self.client.search()
        with self.assertRaisesRegex(ItuMailError, "date"):
            self.client.search(query="x", since="31.01.2023")
        with self.assertRaisesRegex(ItuMailError, "mailbox"):
            self.client.search(query="x", mailbox="Trash")
        with self.assertRaisesRegex(ItuMailError, "at most 8"):
            self.client.search(query=" ".join("abcdefghi"))


class MailSendRegistrationTests(unittest.TestCase):
    def test_send_tool_is_destructive_and_not_routed(self) -> None:
        from ninova_mcp.server import NinovaMcpApp, register_tools

        class FakeMcp:
            def __init__(self) -> None:
                self.registered: dict[str, object] = {}

            def add_tool(self, fn: object, **kwargs: object) -> None:
                self.registered[str(kwargs["name"])] = kwargs["annotations"]

        fake = FakeMcp()
        register_tools(fake, NinovaMcpApp(), ["mail_prepare_message", "mail_send_message"], profile="full")
        self.assertFalse(fake.registered["mail_prepare_message"].readOnlyHint)
        self.assertFalse(fake.registered["mail_prepare_message"].destructiveHint)
        self.assertTrue(fake.registered["mail_send_message"].destructiveHint)

    def test_remote_needs_mail_and_writes(self) -> None:
        from ninova_mcp import remote

        def names(**env: str) -> set[str]:
            with patch.dict(os.environ, env, clear=False), \
                    patch.object(remote, "remote_file_capabilities", return_value=(True, env.get("W") == "1")):
                return set(remote._selected_remote_tool_names())

        send = {"mail_prepare_message", "mail_send_message"}
        self.assertTrue(send <= names(W="1", NINOVA_REMOTE_ENABLE_MAIL="1", NINOVA_MAIL_SEND="1"))
        self.assertFalse(send & names(W="0", NINOVA_REMOTE_ENABLE_MAIL="1", NINOVA_MAIL_SEND="1"))
        self.assertFalse(send & names(W="1", NINOVA_REMOTE_ENABLE_MAIL="0", NINOVA_MAIL_SEND="1"))
        self.assertFalse(send & names(W="1", NINOVA_REMOTE_ENABLE_MAIL="1", NINOVA_MAIL_SEND="0"))

    def test_remote_attachments_must_be_managed_files(self) -> None:
        from ninova_mcp.remote_files import RemoteNinovaMcpApp

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            with patch.dict(os.environ, {"NINOVA_REMOTE_FILE_ROOT": str(root)}):
                os.environ.pop("NINOVA_REMOTE_REQUIRED_MOUNT", None)
                app = RemoteNinovaMcpApp()
            (root / "downloads" / "notes.pdf").write_bytes(b"%PDF- notes")
            (root / "secret.pdf").write_bytes(b"%PDF- secret")
            app._mail = ItuMailClient(username="student", password="pw", imap_factory=FakeImap)
            draft = app.mail_prepare_message("s", "b", to=["a@itu.edu.tr"], attachments=["downloads/notes.pdf"])
            self.assertEqual(draft["preview"]["attachments"][0]["name"], "notes.pdf")
            for path in (str(root / "secret.pdf"), "downloads/../secret.pdf", "/etc/hostname"):
                with self.subTest(path=path), self.assertRaises(Exception):
                    app.mail_prepare_message("s", "b", to=["a@itu.edu.tr"], attachments=[path])


if __name__ == "__main__":
    unittest.main()
