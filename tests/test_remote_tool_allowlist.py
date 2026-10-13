import os
import unittest
from unittest.mock import patch

from ninova_mcp.remote import _selected_remote_tool_names, remote_file_capabilities
from ninova_mcp.server import MAIL_TOOL_NAMES, OBS_REGISTRATION_WRITE_TOOL_NAMES, REMOTE_TOOL_NAMES


class RemoteToolAllowlistTests(unittest.TestCase):
    def test_mail_and_registration_drafts_are_enabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NINOVA_REMOTE_ALLOWED_TOOLS", None)
            os.environ.pop("NINOVA_REMOTE_ENABLE_MAIL", None)
            os.environ.pop("NINOVA_OBS_REGISTRATION_WRITES", None)
            self.assertEqual(_selected_remote_tool_names(), REMOTE_TOOL_NAMES)

    def test_mail_and_registration_drafts_can_be_switched_off(self) -> None:
        with patch.dict(os.environ, {"NINOVA_REMOTE_ENABLE_MAIL": "0",
                                     "NINOVA_OBS_REGISTRATION_WRITES": "0"}):
            os.environ.pop("NINOVA_REMOTE_ALLOWED_TOOLS", None)
            self.assertEqual(
                _selected_remote_tool_names(),
                [name for name in REMOTE_TOOL_NAMES if name not in MAIL_TOOL_NAMES | OBS_REGISTRATION_WRITE_TOOL_NAMES],
            )

    def test_allowlist_preserves_canonical_order(self) -> None:
        with patch.dict(
            os.environ,
            {"NINOVA_REMOTE_ALLOWED_TOOLS": "list_courses,auth_status"},
            clear=False,
        ):
            selected = _selected_remote_tool_names()
        self.assertEqual(set(selected), {"auth_status", "list_courses"})
        self.assertEqual(selected, [name for name in REMOTE_TOOL_NAMES if name in set(selected)])

    def test_unknown_or_excluded_tool_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {"NINOVA_REMOTE_ALLOWED_TOOLS": "auth_status,submit_assignment"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                _selected_remote_tool_names()

    def test_mail_switched_off_fails_closed_in_allowlist(self) -> None:
        with patch.dict(
            os.environ,
            {"NINOVA_REMOTE_ALLOWED_TOOLS": "auth_status,mail_status,mail_list_sent"},
            clear=False,
        ):
            os.environ.pop("NINOVA_REMOTE_ENABLE_MAIL", None)
            self.assertEqual(_selected_remote_tool_names(), ["auth_status", "mail_status", "mail_list_sent"])
            os.environ["NINOVA_REMOTE_ENABLE_MAIL"] = "0"
            with self.assertRaises(RuntimeError):
                _selected_remote_tool_names()

    def test_registration_write_switch_off_values_fail_closed(self) -> None:
        with patch.dict(os.environ, {"NINOVA_REMOTE_ALLOWED_TOOLS": "obs_save_registration_draft"}):
            for value in ("0", "false", "no", "off", " OFF "):
                with self.subTest(value=value):
                    os.environ["NINOVA_OBS_REGISTRATION_WRITES"] = value
                    with self.assertRaisesRegex(RuntimeError, "disabled"):
                        _selected_remote_tool_names()
            for value in (None, "", "1"):
                with self.subTest(value=value):
                    if value is None:
                        os.environ.pop("NINOVA_OBS_REGISTRATION_WRITES", None)
                    else:
                        os.environ["NINOVA_OBS_REGISTRATION_WRITES"] = value
                    self.assertEqual(_selected_remote_tool_names(), ["obs_save_registration_draft"])

    def test_registration_opt_in_respects_existing_remote_allowlist(self) -> None:
        with patch.dict(os.environ, {
            "NINOVA_OBS_REGISTRATION_WRITES": "1",
            "NINOVA_REMOTE_ALLOWED_TOOLS": "auth_status",
        }):
            self.assertEqual(_selected_remote_tool_names(), ["auth_status"])
            os.environ.pop("NINOVA_REMOTE_ALLOWED_TOOLS")
            self.assertIn("obs_save_registration_draft", _selected_remote_tool_names())


    def test_file_and_write_tools_follow_their_prerequisites(self) -> None:
        names = ("NINOVA_REMOTE_API_KEY", "NINOVA_REMOTE_FILE_ROOT",
                 "NINOVA_REMOTE_ENABLE_FILES", "NINOVA_REMOTE_ENABLE_WRITES")
        with patch.dict(os.environ, {}, clear=False):
            for name in names:
                os.environ.pop(name, None)
            self.assertEqual(remote_file_capabilities(), (False, False))
            os.environ["NINOVA_REMOTE_API_KEY"] = "synthetic-key"
            os.environ["NINOVA_REMOTE_FILE_ROOT"] = "/srv/itu-mcp"
            self.assertEqual(remote_file_capabilities(), (True, True))
            os.environ["NINOVA_REMOTE_ENABLE_WRITES"] = "0"
            self.assertEqual(remote_file_capabilities(), (True, False))
            os.environ.pop("NINOVA_REMOTE_API_KEY")
            os.environ["NINOVA_REMOTE_ENABLE_FILES"] = "1"
            self.assertEqual(remote_file_capabilities(), (True, False))


if __name__ == "__main__":
    unittest.main()
