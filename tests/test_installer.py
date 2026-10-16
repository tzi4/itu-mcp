from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ninova_mcp import env as env_module
from ninova_mcp.env import load_ninova_env
from ninova_mcp.installer import (
    SERVER_KEY,
    client_config_paths,
    format_dotenv_value,
    format_systemd_value,
    merge_codex_config,
    merge_json_config,
    prepare_server_env,
    read_env_values,
    run_setup,
    server_env_template,
    set_env_values,
    systemd_unit,
)

COMMAND = ["/home/me/.local/bin/itu-mcp"]
SAMPLE_VALUES = ["plain", "with space", "it's", 'say "hi"', "a #b", "p@ss$word\\x", "tab\tvalue"]


def _parse_systemd_value(raw: str) -> str:
    """The subset of systemd's EnvironmentFile parser that our quoting uses."""
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    if raw.startswith('"') and raw.endswith('"'):
        out, escaped = [], False
        for char in raw[1:-1]:
            if escaped:
                out.append(char if char in '"\\`$' else "\\" + char)
                escaped = False
            elif char == "\\":
                escaped = True
            else:
                out.append(char)
        return "".join(out)
    return raw


class ValueFormattingTests(unittest.TestCase):
    def test_dotenv_values_round_trip_through_the_loader(self) -> None:
        for value in SAMPLE_VALUES:
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / ".env"
                set_env_values(path, {"NINOVA_PASSWORD": value})
                with patch.dict(os.environ, {"NINOVA_ENV_FILE": str(path)}):
                    os.environ.pop("NINOVA_PASSWORD", None)
                    load_ninova_env()
                    self.assertEqual(os.environ["NINOVA_PASSWORD"], value)

    def test_dotenv_rejects_values_it_cannot_store(self) -> None:
        for value in ("line\nbreak", "both ' and \""):
            with self.assertRaises(ValueError):
                format_dotenv_value(value)

    def test_systemd_values_round_trip(self) -> None:
        for value in SAMPLE_VALUES + ["it's $HOME `x` \"q\""]:
            self.assertEqual(_parse_systemd_value(format_systemd_value(value)), value)
        with self.assertRaises(ValueError):
            format_systemd_value("line\nbreak")


class EnvFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_set_values_keeps_other_lines_and_is_private(self) -> None:
        path = self.root / "settings" / ".env"
        path.parent.mkdir()
        path.write_text("# comment\nNINOVA_USERNAME=old\nITU_MCP_TOOL_PROFILE=full\n", encoding="utf-8")
        set_env_values(path, {"NINOVA_USERNAME": "new@itu.edu.tr", "NINOVA_PASSWORD": "x y"})
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.splitlines()[0], "# comment")
        self.assertIn("ITU_MCP_TOOL_PROFILE=full", text)
        self.assertEqual(read_env_values(path)["NINOVA_USERNAME"], "new@itu.edu.tr")
        self.assertEqual(read_env_values(path)["NINOVA_PASSWORD"], "x y")
        self.assertEqual(text.count("NINOVA_USERNAME="), 1)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_user_env_file_is_the_fallback(self) -> None:
        user_file = self.root / "home" / ".itu-mcp" / ".env"
        set_env_values(user_file, {"NINOVA_USERNAME": "fallback"})
        project = self.root / "project"
        project.mkdir()
        cwd = Path.cwd()
        os.chdir(project)
        try:
            with patch.object(env_module, "user_env_file", return_value=user_file), \
                    patch.dict(os.environ, {}, clear=False):
                os.environ.pop("NINOVA_ENV_FILE", None)
                os.environ.pop("NINOVA_USERNAME", None)
                self.assertEqual(load_ninova_env(), user_file)
                self.assertEqual(os.environ["NINOVA_USERNAME"], "fallback")
                # A project .env still wins over the per-user file.
                (project / ".env").write_text("NINOVA_USERNAME=project\n", encoding="utf-8")
                self.assertEqual(load_ninova_env(override=True), (project / ".env").resolve())
        finally:
            os.chdir(cwd)


class ClientConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_platform_paths(self) -> None:
        home = Path("/home/me")
        self.assertEqual(client_config_paths("linux", home, {})["Claude Desktop"],
                         home / ".config" / "Claude" / "claude_desktop_config.json")
        self.assertEqual(client_config_paths("darwin", home, {})["Claude Desktop"],
                         home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
        windows = client_config_paths("win32", home, {"APPDATA": "C:\\Users\\me\\AppData\\Roaming"})
        self.assertEqual(windows["Claude Desktop"].name, "claude_desktop_config.json")
        self.assertEqual(windows["Cursor"], home / ".cursor" / "mcp.json")

    def test_json_merge_keeps_other_servers_and_has_no_credentials(self) -> None:
        path = self.root / "claude_desktop_config.json"
        path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}),
                        encoding="utf-8")
        self.assertEqual(merge_json_config(path, COMMAND), "added")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["other"], {"command": "x"})
        self.assertEqual(data["mcpServers"][SERVER_KEY], {"command": COMMAND[0], "args": []})
        self.assertEqual(data["theme"], "dark")
        self.assertNotIn("env", data["mcpServers"][SERVER_KEY])
        self.assertTrue(path.with_name(path.name + ".bak").is_file())
        self.assertEqual(merge_json_config(path, COMMAND), "unchanged")
        self.assertEqual(merge_json_config(path, ["/new/itu-mcp"]), "updated")

    def test_json_merge_leaves_invalid_files_alone(self) -> None:
        path = self.root / "mcp.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(merge_json_config(path, COMMAND), "invalid")
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    def test_codex_merge_appends_once(self) -> None:
        path = self.root / "config.toml"
        path.write_text('model = "x"\n', encoding="utf-8")
        windows_path = ["C:\\Users\\me\\.local\\bin\\itu-mcp.exe"]
        self.assertEqual(merge_codex_config(path, windows_path), "added")
        self.assertEqual(merge_codex_config(path, windows_path), "present")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(f"[mcp_servers.{SERVER_KEY}]"), 1)
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python 3.10
            return
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["mcp_servers"][SERVER_KEY]["command"], windows_path[0])
        self.assertEqual(parsed["model"], "x")


class SetupWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env_file = self.root / ".itu-mcp" / ".env"
        self.output: list[str] = []

    def _run(self, answers: list[str], hidden: list[str], **kwargs) -> int:
        answers_iter, hidden_iter = iter(answers), iter(hidden)
        claude = self.root / "Claude" / "claude_desktop_config.json"
        claude.parent.mkdir(exist_ok=True)
        return run_setup(
            self.env_file, interactive=kwargs.pop("interactive", True),
            ask=lambda prompt: next(answers_iter), ask_hidden=lambda prompt: next(hidden_iter),
            out=self.output.append, command=COMMAND,
            clients={"Claude Desktop": claude, "Cursor": self.root / "missing" / "mcp.json"},
            codex_config=self.root / "no-codex" / "config.toml", **kwargs)

    def test_saves_hidden_password_and_configures_found_clients(self) -> None:
        self.assertEqual(self._run(["me@itu.edu.tr", "y"], ["s3cret pass"]), 0)
        values = read_env_values(self.env_file)
        self.assertEqual(values["NINOVA_USERNAME"], "me@itu.edu.tr")
        self.assertEqual(values["NINOVA_PASSWORD"], "s3cret pass")
        output = "\n".join(self.output)
        self.assertNotIn("s3cret", output)
        config = json.loads((self.root / "Claude" / "claude_desktop_config.json").read_text(encoding="utf-8"))
        self.assertNotIn("s3cret", json.dumps(config))
        self.assertIn(SERVER_KEY, config["mcpServers"])
        self.assertFalse((self.root / "missing").exists())

    def test_rerun_keeps_saved_credentials_by_default(self) -> None:
        set_env_values(self.env_file, {"NINOVA_USERNAME": "me", "NINOVA_PASSWORD": "old"})
        self.assertEqual(self._run(["", "n"], []), 0)
        self.assertEqual(read_env_values(self.env_file)["NINOVA_PASSWORD"], "old")

    def test_without_a_terminal_nothing_changes(self) -> None:
        self.assertEqual(self._run([], [], interactive=False), 0)
        self.assertFalse(self.env_file.exists())
        self.assertIn("claude mcp add", "\n".join(self.output))


class ServerInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "etc" / "env"

    def test_template_binds_loopback_and_requires_a_key(self) -> None:
        text = server_env_template("/var/lib/itu", 8123, token=lambda n: "t" * n)
        self.assertIn("NINOVA_REMOTE_HOST=127.0.0.1\n", text)
        self.assertIn("NINOVA_REMOTE_PORT=8123\n", text)
        self.assertIn("NINOVA_REMOTE_REQUIRE_API_KEY=1\n", text)
        self.assertIn("NINOVA_REMOTE_API_KEY=" + "t" * 32 + "\n", text)
        self.assertIn("NINOVA_PASSWORD=\n", text)

    def test_server_env_is_created_once_and_kept(self) -> None:
        self.assertEqual(prepare_server_env(self.path, "/var/lib/itu", 8000, ask_credentials=False), "created")
        first = read_env_values(self.path)
        self.assertEqual(len(first["NINOVA_REMOTE_API_KEY"]), 64)
        if os.name != "nt":
            os.chmod(self.path, 0o644)
        self.assertEqual(prepare_server_env(self.path, "/var/lib/itu", 9000, ask_credentials=False), "kept")
        self.assertEqual(read_env_values(self.path), first)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_server_env_asks_only_for_missing_credentials(self) -> None:
        prepare_server_env(self.path, "/var/lib/itu", 8000, ask_credentials=True,
                           ask=lambda p: "me@itu.edu.tr", ask_hidden=lambda p: "it's $ecret",
                           out=lambda text: None)
        values = read_env_values(self.path)
        self.assertEqual(values["NINOVA_USERNAME"], "me@itu.edu.tr")
        raw = [line for line in self.path.read_text(encoding="utf-8").splitlines()
               if line.startswith("NINOVA_PASSWORD=")]
        self.assertEqual(_parse_systemd_value(raw[0].split("=", 1)[1]), "it's $ecret")
        prepare_server_env(self.path, "/var/lib/itu", 8000, ask_credentials=True,
                           ask=lambda p: self.fail("asked again"), ask_hidden=lambda p: self.fail("asked"))

    def test_systemd_unit(self) -> None:
        unit = systemd_unit(executable="/opt/itu/venv/bin/itu-mcp-remote", env_file="/etc/itu/env",
                            state_dir="/var/lib/itu", user="itu-mcp")
        self.assertIn("ExecStart=/opt/itu/venv/bin/itu-mcp-remote\n", unit)
        self.assertIn("EnvironmentFile=/etc/itu/env\n", unit)
        self.assertIn("User=itu-mcp\n", unit)
        self.assertIn("ReadWritePaths=/var/lib/itu\n", unit)
        self.assertIn("WantedBy=multi-user.target\n", unit)
        with self.assertRaises(ValueError):
            systemd_unit(executable="relative", env_file="/e", state_dir="/s", user="u")
        with self.assertRaises(ValueError):
            systemd_unit(executable="/x", env_file="/e", state_dir="/s", user="bad user")


class InstallScriptTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1] / "scripts"

    def test_scripts_install_this_fork_by_commit(self) -> None:
        for name in ("install.sh", "install.ps1", "install-server.sh"):
            text = (self.ROOT / name).read_text(encoding="utf-8")
            self.assertIn("tzi4/itu-mcp-paperbridge", text, name)
            self.assertIn("/archive/$", text, name)
            self.assertIn("--reinstall-package itu-mcp", text, name)
            self.assertNotIn("pip install itu-mcp", text, name)
            self.assertNotIn("NINOVA_PASSWORD=", text, name)

    def test_server_script_keeps_the_service_on_loopback(self) -> None:
        text = (self.ROOT / "install-server.sh").read_text(encoding="utf-8")
        self.assertNotIn("0.0.0.0", text)
        self.assertIn('chmod 600 "$ENV_FILE"', text)


if __name__ == "__main__":
    unittest.main()
