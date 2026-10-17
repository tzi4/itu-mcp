"""itu_health: concurrent, bounded, read-only and free of secrets."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ninova_mcp.client import NinovaAuthError
from ninova_mcp.health import SERVICES, health_report, write_actions

USERNAME = "student-secret-name"
PASSWORD = "pw-not-for-output"


class _App:
    """The attributes health_report reads from NinovaMcpApp, as mocks."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = Path(state_dir)
        self.client = mock.Mock()
        self.client.ensure_logged_in.return_value = {"username": USERNAME, "cookie_names": ["ASP.NET_SessionId"]}
        self.client.get_portal_html.return_value = ("<html></html>", "https://portal.itu.edu.tr/apps/default/")
        self.obs = mock.Mock()
        self.obs.ensure_ready.return_value = {"jwt_present": True}
        self.mail = mock.Mock()
        self.mail.status.return_value = {"connected": True, "unread_messages": 3}
        self.papers = mock.Mock()
        self.papers.call.return_value = {"configured_providers": ["ieee"]}
        self.archive = mock.Mock()
        self.obs_public = mock.Mock()


def _ok_response(*args, **kwargs):
    return mock.Mock(status_code=200)


class HealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = _App(self.directory.name)
        env = {"NINOVA_USERNAME": USERNAME, "NINOVA_PASSWORD": PASSWORD,
               "NINOVA_STATE_DIR": self.directory.name}
        patches = [
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch("requests.get", side_effect=_ok_response),
            mock.patch("ninova_mcp.update_check.installed_commit", return_value="a" * 40),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("NINOVA_MAIL_USERNAME", "NINOVA_MAIL_PASSWORD"):
            os.environ.pop(name, None)

    def report(self, **kwargs):
        return health_report(self.app, version="1.0", profile="router", **kwargs)

    def test_every_service_is_checked_and_timed(self) -> None:
        report = self.report()
        self.assertEqual(set(report["services"]), set(SERVICES))
        for name, state in report["services"].items():
            with self.subTest(service=name):
                self.assertEqual(state["status"], "ok")
                self.assertTrue(state["configured"])
                self.assertIsInstance(state["latency_ms"], int)
        self.assertEqual(report["summary"], "7 of 7 services ok")
        self.assertEqual(report["version"], "1.0 (commit aaaaaaa)")
        self.assertEqual(report["commit"], "aaaaaaa")
        self.assertEqual(report["tool_profile"], "router")
        self.assertEqual(report["transport"], "local")
        self.assertIsNone(report["remote_security"])
        self.app.client.ensure_logged_in.assert_called_once_with(verify=True)

    def test_report_has_no_secrets_paths_or_cookie_names(self) -> None:
        self.app.archive.get_index.side_effect = RuntimeError(f"failed for {USERNAME} at /home/x")
        text = repr(self.report())
        for secret in (USERNAME, PASSWORD, "ASP.NET_SessionId", self.directory.name, "/home/x"):
            self.assertNotIn(secret, text)
        self.assertEqual(self.report()["services"]["archive"],
                         {"configured": True, "status": "unreachable", "error_type": "RuntimeError",
                          "latency_ms": mock.ANY})

    def test_failed_login_skips_the_services_that_share_it(self) -> None:
        self.app.client.ensure_logged_in.side_effect = NinovaAuthError(f"login failed for {USERNAME}")
        services = self.report()["services"]
        self.assertEqual(services["ninova"]["status"], "authentication_failed")
        self.assertEqual(services["obs"]["status"], "skipped")
        self.assertEqual(services["portal"]["status"], "skipped")
        self.app.obs.ensure_ready.assert_not_called()
        self.assertEqual(services["mail"]["status"], "ok")
        self.assertNotIn(USERNAME, repr(services))

    def test_slow_check_times_out_without_holding_the_report(self) -> None:
        self.app.archive.get_index.side_effect = lambda: time.sleep(3)
        started = time.monotonic()
        report = self.report(timeout_seconds=2)
        self.assertLess(time.monotonic() - started, 2.8)
        self.assertEqual(report["services"]["archive"]["status"], "timeout")
        self.assertEqual(report["services"]["public"]["status"], "ok")
        self.assertEqual(report["timeout_seconds"], 2)

    def test_missing_credentials_are_not_configured_and_not_called(self) -> None:
        with mock.patch.dict(os.environ, {"NINOVA_USERNAME": "", "NINOVA_PASSWORD": ""}):
            services = self.report()["services"]
        for name in ("ninova", "obs", "portal", "mail"):
            self.assertEqual(services[name], {"configured": False, "status": "not_configured"})
        self.app.client.ensure_logged_in.assert_not_called()
        self.app.mail.status.assert_not_called()
        self.assertEqual(services["public"]["status"], "ok")

    def test_checks_are_read_only(self) -> None:
        self.report()
        self.assertEqual(self.app.papers.call.call_args.args, ("status",))
        self.assertFalse(self.app.client.post_multipart.called)
        self.assertFalse(self.app.client.post.called)


class WriteActionTests(unittest.TestCase):
    def test_switches_and_published_tools_decide(self) -> None:
        env = {"NINOVA_ALLOW_UPLOADS": "0", "NINOVA_MAIL_SEND": "", "NINOVA_MAIL_SAVE_DRAFTS": "off",
               "NINOVA_HELP_TICKET_WRITES": "", "NINOVA_OBS_REGISTRATION_WRITES": "1"}
        with mock.patch.dict(os.environ, env):
            local = write_actions({"submit_assignment", "mail_send_message", "mail_save_draft",
                                   "obs_save_registration_draft"}, remote=False, remote_writes=None)
            remote = write_actions(None, remote=True, remote_writes=False)
        self.assertFalse(local["assignment_uploads"])
        self.assertTrue(local["mail_send"])
        self.assertFalse(local["mail_save_drafts"])
        self.assertFalse(local["help_ticket_writes"])  # help_submit_ticket is not published
        self.assertTrue(local["obs_registration_draft_saves"])
        self.assertIsNone(local["remote_writes"])
        self.assertIs(remote["remote_writes"], False)
        self.assertTrue(remote["help_ticket_writes"])


class ServerToolTests(unittest.TestCase):
    def test_tool_reports_the_registered_profile(self) -> None:
        from mcp.server.fastmcp import FastMCP

        from ninova_mcp import server

        app = server.NinovaMcpApp()
        server.register_tools(FastMCP("test"), app, ["itu_health", "mail_status"], profile="full")
        with mock.patch("ninova_mcp.health.health_report", return_value={"ok": True}) as report:
            app.itu_health(timeout_seconds=5)
        self.assertEqual(report.call_args.kwargs["profile"], "full")
        self.assertEqual(app.published_tool_names, {"itu_health", "mail_status"})


if __name__ == "__main__":
    unittest.main()
