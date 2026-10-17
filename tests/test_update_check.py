from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ninova_mcp import update_check
from ninova_mcp.update_check import (
    CHECK_INTERVAL_SECONDS,
    check_now,
    commit_from_direct_url,
    fetch_status,
    notice_from_record,
)

COMMIT = "e80f48a22160e0e170b4b6c6dac7ab10dd0ea796"
LATEST = "32bd618064f179649c252096eb298dba1f064277"


class FakeResponse:
    def __init__(self, status_code: int, payload: object = None, body: bytes | None = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else json.dumps(payload).encode()
        self.closed = False

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def close(self) -> None:
        self.closed = True


class DirectUrlTests(unittest.TestCase):
    def test_archive_urls_from_the_installers(self) -> None:
        for suffix in ("tar.gz", "zip"):
            url = f"https://github.com/tzi4/itu-mcp-paperbridge/archive/{COMMIT}.{suffix}"
            self.assertEqual(commit_from_direct_url({"url": url, "archive_info": {}}), COMMIT)

    def test_git_installs_use_the_recorded_commit(self) -> None:
        data = {"url": "https://github.com/tzi4/itu-mcp-paperbridge.git",
                "vcs_info": {"vcs": "git", "commit_id": COMMIT, "requested_revision": "main"}}
        self.assertEqual(commit_from_direct_url(data), COMMIT)

    def test_unknown_sources_are_not_checked(self) -> None:
        for data in (
            {"url": "file:///home/me/itu-mcp-paperbridge", "dir_info": {"editable": True}},
            {"url": "https://github.com/tzi4/itu-mcp-paperbridge/archive/refs/heads/main.tar.gz",
             "archive_info": {}},
            {"url": f"https://github.com/someone/else/archive/{COMMIT}.zip", "archive_info": {}},
            {"url": "https://github.com/someone/else.git",
             "vcs_info": {"vcs": "git", "commit_id": COMMIT}},
        ):
            self.assertIsNone(commit_from_direct_url(data), data)

    def test_build_label_shows_the_short_commit(self) -> None:
        self.assertEqual(update_check.build_label("1.0", COMMIT), "1.0 (commit e80f48a)")
        with patch.object(update_check, "installed_commit", return_value=None):
            self.assertEqual(update_check.build_label("1.0"), "1.0")


class FetchStatusTests(unittest.TestCase):
    def test_reads_behind_by_from_the_compare_api(self) -> None:
        calls = []

        def get(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(200, {"status": "behind", "ahead_by": 0, "behind_by": 3,
                                      "base_commit": {"sha": LATEST}, "commits": [], "files": []})

        result = fetch_status(COMMIT, get=get)
        self.assertEqual(result["behind_by"], 3)
        self.assertEqual(result["latest_commit"], LATEST)
        url, kwargs = calls[0]
        self.assertEqual(url, f"https://api.github.com/repos/tzi4/itu-mcp-paperbridge/compare/main...{COMMIT}")
        self.assertLessEqual(kwargs["timeout"], 10)
        self.assertNotIn("Authorization", kwargs["headers"])

    def test_unknown_commit_and_errors(self) -> None:
        self.assertEqual(fetch_status(COMMIT, get=lambda *a, **k: FakeResponse(404, {}))["status"], "unknown")
        self.assertEqual(fetch_status(COMMIT, get=lambda *a, **k: FakeResponse(403, {}))["status"], "error")
        self.assertEqual(fetch_status(COMMIT, get=lambda *a, **k: FakeResponse(200, {"x": 1}))["status"], "error")

    def test_oversized_response_is_rejected(self) -> None:
        response = FakeResponse(200, body=b" " * (update_check.MAX_RESPONSE_BYTES + 10))
        self.assertEqual(fetch_status(COMMIT, get=lambda *a, **k: response)["error"], "response too large")
        self.assertTrue(response.closed)


class CheckNowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        env = patch.dict(os.environ, {"ITU_MCP_UPDATE_CHECK": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_caches_for_a_day_and_rechecks_after(self) -> None:
        calls = []

        def fetch(commit):
            calls.append(commit)
            return {"status": "behind", "behind_by": 2}

        first = check_now(self.state, commit=COMMIT, now=1000.0, fetch=fetch)
        again = check_now(self.state, commit=COMMIT, now=1000.0 + CHECK_INTERVAL_SECONDS - 1, fetch=fetch)
        self.assertEqual(calls, [COMMIT])
        self.assertEqual(first["behind_by"], again["behind_by"])
        check_now(self.state, commit=COMMIT, now=1000.0 + CHECK_INTERVAL_SECONDS, fetch=fetch)
        self.assertEqual(len(calls), 2)

    def test_new_install_rechecks_immediately(self) -> None:
        calls = []
        fetch = lambda commit: calls.append(commit) or {"status": "identical", "behind_by": 0}
        check_now(self.state, commit=COMMIT, now=1000.0, fetch=fetch)
        check_now(self.state, commit=LATEST, now=1001.0, fetch=fetch)
        self.assertEqual(calls, [COMMIT, LATEST])

    def test_offline_failures_are_cached_and_never_raise(self) -> None:
        calls = []

        def fetch(commit):
            calls.append(commit)
            raise OSError("offline")

        record = check_now(self.state, commit=COMMIT, now=1000.0, fetch=fetch)
        self.assertEqual(record["status"], "error")
        check_now(self.state, commit=COMMIT, now=2000.0, fetch=fetch)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(notice_from_record(record, COMMIT))

    def test_opt_out_makes_no_request(self) -> None:
        fetch = lambda commit: self.fail("fetched although the check is off")
        with patch.dict(os.environ, {"ITU_MCP_UPDATE_CHECK": "0"}):
            self.assertIsNone(check_now(self.state, commit=COMMIT, fetch=fetch))
            self.assertIsNone(update_check.start_background_check(self.state))
        self.assertFalse((self.state / update_check.CACHE_FILE_NAME).exists())

    def test_unknown_install_makes_no_request(self) -> None:
        with patch.object(update_check, "installed_commit", return_value=None):
            self.assertIsNone(check_now(self.state, fetch=lambda c: self.fail("fetched")))
            self.assertIsNone(update_check.start_background_check(self.state))

    def test_cached_notice_and_instructions(self) -> None:
        check_now(self.state, commit=COMMIT, now=1000.0, fetch=lambda c: {"status": "behind", "behind_by": 5})
        with patch.object(update_check, "installed_commit", return_value=COMMIT):
            notice = update_check.cached_notice(self.state)
            self.assertIn("5 new commits", notice)
            self.assertIn("e80f48a", notice)
            # The instructions never carry the notice itself, so they stay the same
            # in every conversation.
            self.assertIn("update_notice", update_check.update_instructions(self.state))
            self.assertNotIn("5 new commits", update_check.update_instructions(self.state))
        # After an upgrade the old record no longer applies.
        with patch.object(update_check, "installed_commit", return_value=LATEST):
            self.assertIsNone(update_check.cached_notice(self.state))
        with patch.object(update_check, "installed_commit", return_value=None):
            self.assertEqual(update_check.update_instructions(self.state), "")

    def test_notice_is_shown_first_then_every_few_days(self) -> None:
        check_now(self.state, commit=COMMIT, now=1000.0, fetch=lambda c: {"status": "behind", "behind_by": 5})
        day = 24 * 60 * 60
        with patch.object(update_check, "installed_commit", return_value=COMMIT):
            self.assertIn("5 new commits", update_check.due_notice(self.state, now=2000.0))
            self.assertIsNone(update_check.due_notice(self.state, now=2001.0))
            self.assertIsNone(update_check.due_notice(self.state, now=2000.0 + 2 * day))
            self.assertIsNotNone(update_check.due_notice(self.state, now=2000.0 + 3 * day))
            self.assertIsNone(update_check.due_notice(self.state, now=2000.0 + 3 * day + 60))
            with patch.dict(os.environ, {"ITU_MCP_UPDATE_CHECK": "0"}):
                self.assertIsNone(update_check.due_notice(self.state, now=2000.0 + 30 * day))
        # A new install is reminded again right away, and nothing is due once up to date.
        with patch.object(update_check, "installed_commit", return_value=LATEST):
            self.assertIsNone(update_check.due_notice(self.state, now=2000.0 + 3 * day + 120))

    def test_tool_results_carry_the_due_notice_once(self) -> None:
        from ninova_mcp.server import NinovaMcpApp, TOOLS, _tool_registration

        check_now(self.state, commit=COMMIT, now=1000.0, fetch=lambda c: {"status": "behind", "behind_by": 2})
        with patch.dict(os.environ, {"NINOVA_STATE_DIR": str(self.state)}), \
                patch.object(update_check, "installed_commit", return_value=COMMIT):
            app = NinovaMcpApp()
            with patch.object(NinovaMcpApp, "get_left_courses", side_effect=lambda: {"courses": []}):
                fn, _ = _tool_registration(app, "get_left_courses", {tool["name"]: tool for tool in TOOLS})
                first, second = fn(), fn()
        self.assertIn("2 new commits", first["update_notice"])
        self.assertNotIn("update_notice", second)

    def test_up_to_date_has_no_notice(self) -> None:
        record = {"installed_commit": COMMIT, "status": "identical", "behind_by": 0}
        self.assertIsNone(notice_from_record(record, COMMIT))

    def test_auth_status_reads_only_the_cache(self) -> None:
        from ninova_mcp.server import NinovaMcpApp

        check_now(self.state, commit=COMMIT, now=1000.0, fetch=lambda c: {"status": "behind", "behind_by": 1})
        with patch.dict(os.environ, {"NINOVA_STATE_DIR": str(self.state), "NINOVA_USERNAME": "",
                                     "NINOVA_PASSWORD": ""}), \
                patch.object(update_check, "installed_commit", return_value=COMMIT), \
                patch.object(update_check, "fetch_status", side_effect=AssertionError("network")):
            status = NinovaMcpApp().auth_status()
        self.assertIn("1 new commit ", status["update_notice"])


if __name__ == "__main__":
    unittest.main()


class UpdateCommandTests(unittest.TestCase):
    SHA_OLD = "a" * 40
    SHA_NEW = "b" * 40

    def test_one_command_per_install_kind(self) -> None:
        from ninova_mcp import update_check as uc
        self.assertEqual(uc.update_command(windows=False, server=False), "itu-mcp --update")
        self.assertIn("install.ps1 | iex", uc.update_command(windows=True, server=False))
        self.assertIn("install-server.sh | sudo sh", uc.update_command(windows=True, server=True))

    def test_notice_names_the_command(self) -> None:
        from unittest import mock
        from ninova_mcp import update_check as uc
        with mock.patch.object(uc, "update_command", return_value="itu-mcp --update"):
            notice = uc.notice_from_record({"installed_commit": self.SHA_OLD, "behind_by": 3}, self.SHA_OLD)
        self.assertIn("`itu-mcp --update`", notice)
        self.assertIn("3 new commits", notice)

    def _run(self, *, command="itu-mcp --update", commit=SHA_OLD, latest=SHA_NEW, uv="/home/u/.local/bin/uv",
             returncode=0):
        from unittest import mock
        from types import SimpleNamespace
        from ninova_mcp import update_check as uc
        lines, calls = [], []
        def run(argv, check=False):
            calls.append(argv)
            return SimpleNamespace(returncode=returncode)
        with mock.patch.object(uc, "installed_commit", return_value=commit), \
                mock.patch.object(uc, "update_command", return_value=command), \
                mock.patch.object(uc, "find_uv", return_value=uv), \
                mock.patch.object(uc, "latest_commit", return_value=latest):
            code = uc.run_update(out=lines.append, run=run)
        return code, lines, calls

    def test_posix_update_installs_the_latest_commit(self) -> None:
        code, lines, calls = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][:3], ["/home/u/.local/bin/uv", "tool", "install"])
        self.assertTrue(calls[0][-1].endswith(f"/archive/{self.SHA_NEW}.tar.gz"))
        self.assertIn("Restart your MCP client", lines[-1])

    def test_up_to_date_and_failures(self) -> None:
        self.assertEqual(self._run(latest=self.SHA_OLD)[2], [])
        code, lines, _ = self._run(returncode=1)
        self.assertEqual(code, 1)
        self.assertIn("unchanged", lines[-1])
        self.assertEqual(self._run(latest=None)[0], 1)

    def test_windows_and_server_print_their_command(self) -> None:
        for command in ("powershell ... install.ps1 | iex", "curl ... install-server.sh | sudo sh"):
            code, lines, calls = self._run(command=command)
            self.assertEqual((code, calls), (0, []))
            self.assertIn(command, lines[-1])

    def test_unrecorded_install_cannot_update(self) -> None:
        code, lines, calls = self._run(commit=None)
        self.assertEqual((code, calls), (1, []))

    def test_latest_commit_parses_github_sha(self) -> None:
        from types import SimpleNamespace
        from ninova_mcp import update_check as uc
        self.assertEqual(uc.latest_commit(get=lambda *a, **k: SimpleNamespace(status_code=200, text=self.SHA_NEW + "\n")),
                         self.SHA_NEW)
        self.assertIsNone(uc.latest_commit(get=lambda *a, **k: SimpleNamespace(status_code=403, text="rate limited")))
        def offline(*a, **k):
            raise OSError("offline")
        self.assertIsNone(uc.latest_commit(get=offline))
