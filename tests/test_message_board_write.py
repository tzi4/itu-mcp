"""Two-step message board replies, against a synthetic Ninova that never touches the network."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ninova_mcp.client import NinovaError
from ninova_mcp.message_board_write import DRAFT_TTL_SECONDS, MAX_TEXT_CHARS, MessageBoardWriter, reply_html

from test_ninova_course_pages import BASE, BOARD_HTML, COURSE, thread_html

THREAD = COURSE + "/MesajPanosu/555"
COURSE_ITEM = {"code": "ABC 101", "title": "Example", "url": COURSE}
OPENING = ("Grace Hopper", "09 Haziran 2026 20:08", "<p>Will we get a formula sheet?</p>")


class FakeBoard:
    """Ninova serving one board; a reply post appends to the topic like the live page."""

    base_url = BASE

    def __init__(self, *, reply_form: bool = True, store_reply: bool = True, fail_post: bool = False) -> None:
        self.posts: list[dict] = [OPENING]
        self.reply_form = reply_form
        self.store_reply = store_reply
        self.fail_post = fail_post
        self.submissions: list[dict] = []

    def get_html(self, url: str):
        if url == COURSE + "/MesajPanosu":
            return BOARD_HTML, SimpleNamespace(url=url)
        if url == THREAD:
            return thread_html(list(self.posts), reply_form=self.reply_form), SimpleNamespace(url=url)
        if url == COURSE + "/MesajPanosu/556":
            return thread_html([], thread_id=556), SimpleNamespace(url=url)
        raise NinovaError(f"unexpected request {url}")

    def post_multipart(self, url, *, data, files, referer=None, timeout=120):
        self.submissions.append({"url": url, "data": dict(data), "files": files, "referer": referer})
        if self.fail_post:
            raise NinovaError("connection reset")
        if self.store_reply:
            self.posts.append(("Ada Lovelace", "17 Ekim 2026 12:00", data["ctl00$ContentPlaceHolder1$txtMesaj"]))
        return SimpleNamespace(text=thread_html(list(self.posts)), url=url)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def writer(**options) -> tuple[MessageBoardWriter, FakeBoard, Clock]:
    board, clock = FakeBoard(**options), Clock()
    return MessageBoardWriter(board, clock=clock), board, clock


class ReplyFormattingTests(unittest.TestCase):
    def test_text_is_escaped_into_paragraphs(self) -> None:
        self.assertEqual(reply_html("Hello <b>hocam</b> & all\nline two\n\n second paragraph "),
                         "<p>Hello &lt;b&gt;hocam&lt;/b&gt; &amp; all<br />line two</p><p>second paragraph</p>")


class MessageBoardWriterTests(unittest.TestCase):
    def test_prepare_previews_without_posting(self) -> None:
        reply, board, _ = writer()
        draft = reply.prepare(COURSE_ITEM, THREAD, "Thank you!\nSee you in class.")
        self.assertFalse(draft["posted"])
        preview = draft["preview"]
        self.assertEqual(preview["thread_id"], "555")
        self.assertEqual(preview["thread_title"], "Final exam")
        self.assertEqual(preview["posting_as"], "Ada Lovelace")
        self.assertEqual(preview["text"], "Thank you!\nSee you in class.")
        self.assertEqual(preview["body_html"], "<p>Thank you!<br />See you in class.</p>")
        self.assertEqual(preview["existing_post_count"], 1)
        self.assertEqual(preview["last_post"]["author"], "Grace Hopper")
        self.assertIn("explicitly approve", draft["next_step"])
        self.assertEqual(board.submissions, [])

    def test_prepare_validation(self) -> None:
        reply, board, _ = writer()
        for thread, text in ((THREAD, "  "), (THREAD, "x" * (MAX_TEXT_CHARS + 1)), (THREAD, "bad\x00text"),
                             (COURSE + "/MesajPanosu/999", "hi"), ("https://ninova.itu.edu.tr/Sinif/9.9/MesajPanosu/555", "hi")):
            with self.subTest(thread=thread, text=text[:10]), self.assertRaises(NinovaError):
                reply.prepare(COURSE_ITEM, thread, text)
        closed, _, _ = writer(reply_form=False)
        with self.assertRaisesRegex(NinovaError, "does not accept replies"):
            closed.prepare(COURSE_ITEM, THREAD, "hi")
        self.assertEqual(board.submissions, [])

    def test_submit_posts_the_fresh_form_once_and_reads_it_back(self) -> None:
        reply, board, _ = writer()
        draft = reply.prepare(COURSE_ITEM, THREAD, "Thanks, <noted> & clear.")
        result = reply.submit(draft["draft_id"], True)
        self.assertEqual(len(board.submissions), 1)
        sent = board.submissions[0]
        self.assertEqual(sent["url"], THREAD)
        self.assertEqual(sent["files"], [])
        self.assertEqual(sent["data"]["__EVENTTARGET"], "ctl00$ContentPlaceHolder1$lbMesajEkle")
        self.assertEqual(sent["data"]["__VIEWSTATE"], "state-1")
        self.assertEqual(sent["data"]["ctl00$ContentPlaceHolder1$txtMesaj"],
                         "<p>Thanks, &lt;noted&gt; &amp; clear.</p>")
        self.assertIs(result["posted"], True)
        self.assertEqual(result["post"]["author"], "Ada Lovelace")
        self.assertEqual(result["post"]["body_text"], "Thanks, <noted> & clear.")
        with self.assertRaisesRegex(NinovaError, "Unknown or expired"):
            reply.submit(draft["draft_id"], True)
        self.assertEqual(len(board.submissions), 1)

    def test_confirmation_and_expiry(self) -> None:
        reply, board, clock = writer()
        draft = reply.prepare(COURSE_ITEM, THREAD, "hi")
        for confirm in (False, "true", 1):
            with self.subTest(confirm=confirm), self.assertRaisesRegex(NinovaError, "approval"):
                reply.submit(draft["draft_id"], confirm)  # type: ignore[arg-type]
        clock.now += DRAFT_TTL_SECONDS + 1
        with self.assertRaisesRegex(NinovaError, "expired"):
            reply.submit(draft["draft_id"], True)
        self.assertEqual(board.submissions, [])

    def test_missing_readback_is_uncertain(self) -> None:
        reply, _, _ = writer(store_reply=False)
        result = reply.submit(reply.prepare(COURSE_ITEM, THREAD, "hi")["draft_id"], True)
        self.assertEqual(result["posted"], "uncertain")
        self.assertIn("before trying again", result["message"])

    def test_failed_post_is_not_retried(self) -> None:
        reply, board, _ = writer(fail_post=True)
        draft = reply.prepare(COURSE_ITEM, THREAD, "hi")
        with self.assertRaisesRegex(NinovaError, "may or may not"):
            reply.submit(draft["draft_id"], True)
        self.assertEqual(len(board.submissions), 1)
        with self.assertRaisesRegex(NinovaError, "Unknown or expired"):
            reply.submit(draft["draft_id"], True)

    def test_closed_form_keeps_the_draft(self) -> None:
        reply, board, _ = writer()
        draft = reply.prepare(COURSE_ITEM, THREAD, "hi")
        board.reply_form = False
        with self.assertRaisesRegex(NinovaError, "no longer accepts"):
            reply.submit(draft["draft_id"], True)
        board.reply_form = True
        self.assertIs(reply.submit(draft["draft_id"], True)["posted"], True)
        self.assertEqual(len(board.submissions), 1)

    def test_identical_existing_reply_is_not_posted_again(self) -> None:
        reply, board, _ = writer()
        board.posts.append(("ADA LOVELACE", "17 Ekim 2026 11:00", "<p>hi there</p>"))
        with self.assertRaisesRegex(NinovaError, "already has a post"):
            reply.submit(reply.prepare(COURSE_ITEM, THREAD, "hi  there")["draft_id"], True)
        self.assertEqual(board.submissions, [])


class MessageBoardToolTests(unittest.TestCase):
    def app(self):
        from ninova_mcp.server import NinovaMcpApp

        app = NinovaMcpApp()
        board = FakeBoard()
        app._client = board  # type: ignore[assignment]
        app._course_cache.set("courses", [COURSE_ITEM])
        return app, board

    def test_tools_resolve_course_and_thread(self) -> None:
        app, board = self.app()
        draft = app.ninova_prepare_message_reply("ABC 101", "Final exam", "Thanks!")
        self.assertEqual(draft["preview"]["thread_url"], THREAD)
        self.assertIs(app.ninova_post_message_reply(draft["draft_id"], confirm=True)["posted"], True)
        self.assertEqual(len(board.submissions), 1)

    def test_operator_switch(self) -> None:
        app, board = self.app()
        with patch.dict(os.environ, {"NINOVA_MESSAGE_BOARD_WRITES": "0"}):
            with self.assertRaisesRegex(NinovaError, "disabled"):
                app.ninova_prepare_message_reply("ABC 101", "555", "hi")
            with self.assertRaisesRegex(NinovaError, "disabled"):
                app.ninova_post_message_reply("x", True)
        self.assertEqual(board.submissions, [])

    def test_registry_marks_the_post_destructive(self) -> None:
        from ninova_mcp import server

        names = {"ninova_prepare_message_reply", "ninova_post_message_reply"}
        self.assertLessEqual(names, server.STATEFUL_TOOL_NAMES)
        self.assertLessEqual(names, server.REMOTE_EXCLUDED_TOOLS)
        self.assertIn("ninova_post_message_reply", server.DESTRUCTIVE_TOOL_NAMES)
        self.assertNotIn("ninova_prepare_message_reply", server.DESTRUCTIVE_TOOL_NAMES)
        self.assertIn("ninova_post_message_reply", server.SERVER_INSTRUCTIONS)

    def test_remote_needs_writes(self) -> None:
        from ninova_mcp import remote

        def names(writes: bool, **env: str) -> set[str]:
            with patch.dict(os.environ, env, clear=False), \
                    patch.object(remote, "remote_file_capabilities", return_value=(True, writes)):
                os.environ.pop("NINOVA_REMOTE_ALLOWED_TOOLS", None)
                return set(remote._selected_remote_tool_names())

        tools = {"ninova_prepare_message_reply", "ninova_post_message_reply"}
        self.assertTrue(tools <= names(True, NINOVA_MESSAGE_BOARD_WRITES="1"))
        self.assertIn("get_course_message_thread", names(False))
        self.assertFalse(tools & names(False, NINOVA_MESSAGE_BOARD_WRITES="1"))
        self.assertFalse(tools & names(True, NINOVA_MESSAGE_BOARD_WRITES="0"))


if __name__ == "__main__":
    unittest.main()
