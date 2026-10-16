"""Setup helpers used by the install scripts in ``scripts/``.

``itu-mcp --setup`` runs on a personal computer after installation: it asks
for the ITU credentials with hidden input, stores them in the private per-user
file that the server loads (``~/.itu-mcp/.env``), and adds the server to the
MCP clients it finds. Credentials never appear on a command line or in a
client configuration file.

``python -m ninova_mcp.installer server-env`` and ``systemd-unit`` are used by
``scripts/install-server.sh`` to create the server's private environment file
and its systemd unit, so the logic is tested here instead of in shell.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import sys
from pathlib import Path
from typing import Callable

from .env import _split_key_value, _strip_inline_comment, _unquote, user_env_file

SERVER_KEY = "itu-mcp-paperbridge"
CREDENTIAL_KEYS = ("NINOVA_USERNAME", "NINOVA_PASSWORD")
_BARE_VALUE = re.compile(r"[A-Za-z0-9_@.,:/+=%-]*")


def format_dotenv_value(value: str) -> str:
    """Quote ``value`` so that ``env.load_ninova_env`` reads it back unchanged."""
    if "\n" in value or "\r" in value:
        raise ValueError("Values cannot contain line breaks.")
    if _BARE_VALUE.fullmatch(value):
        formatted = value
    elif "'" not in value:
        formatted = f"'{value}'"
    elif '"' not in value:
        formatted = f'"{value}"'
    else:
        raise ValueError("Values with both quote characters must be edited by hand.")
    if _unquote(_strip_inline_comment(formatted)) != value:
        raise ValueError("This value must be edited by hand.")
    return formatted


def format_systemd_value(value: str) -> str:
    """Quote ``value`` for a systemd ``EnvironmentFile=``."""
    if "\n" in value or "\r" in value:
        raise ValueError("Values cannot contain line breaks.")
    if _BARE_VALUE.fullmatch(value):
        return value
    if "'" not in value:
        # Single quotes are literal in systemd environment files.
        return f"'{value}'"
    escaped = re.sub(r'([\\"`$])', r"\\\1", value)
    return f'"{escaped}"'


def read_env_values(path: Path) -> dict[str, str]:
    """Return the active ``KEY=value`` assignments of an env file."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _split_key_value(line)
        if parsed:
            values[parsed[0]] = _unquote(_strip_inline_comment(parsed[1]))
    return values


def write_private_text(path: Path, text: str) -> None:
    """Replace ``path`` atomically with a file only its owner can read."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def set_env_values(path: Path, values: dict[str, str],
                   formatter: Callable[[str], str] = format_dotenv_value) -> None:
    """Set ``values`` in an env file, keeping every other line as it was."""
    remaining = dict(values)
    lines: list[str] = []
    try:
        existing = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        existing = []
    for raw_line in existing:
        stripped = raw_line.strip()
        parsed = None if stripped.startswith("#") else _split_key_value(stripped)
        if parsed and parsed[0] in remaining:
            key = parsed[0]
            lines.append(f"{key}={formatter(remaining.pop(key))}")
        else:
            lines.append(raw_line)
    lines.extend(f"{key}={formatter(value)}" for key, value in remaining.items())
    write_private_text(path, "\n".join(lines) + "\n")


def server_command() -> list[str]:
    """Return the absolute command that starts the local stdio server."""
    name = "itu-mcp.exe" if os.name == "nt" else "itu-mcp"
    beside = Path(sys.executable).parent / name
    if beside.is_file():
        return [str(beside)]
    found = shutil.which("itu-mcp")
    if found:
        return [str(Path(found).resolve())]
    return [sys.executable, "-m", "ninova_mcp"]


def client_config_paths(platform: str = sys.platform, home: Path | None = None,
                        environ: dict[str, str] | None = None) -> dict[str, Path]:
    """Config files of the MCP clients that use the ``mcpServers`` JSON format."""
    home = home or Path.home()
    environ = os.environ if environ is None else environ
    if platform == "darwin":
        claude = home / "Library" / "Application Support" / "Claude"
    elif platform == "win32":
        claude = Path(environ.get("APPDATA") or home / "AppData" / "Roaming") / "Claude"
    else:
        claude = Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "Claude"
    return {
        "Claude Desktop": claude / "claude_desktop_config.json",
        "Cursor": home / ".cursor" / "mcp.json",
    }


def codex_config_path(home: Path | None = None, environ: dict[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    root = environ.get("CODEX_HOME") or (home or Path.home()) / ".codex"
    return Path(root) / "config.toml"


def merge_json_config(path: Path, command: list[str]) -> str:
    """Add this server to an ``mcpServers`` JSON file; keep all other entries."""
    entry = {"command": command[0], "args": command[1:]}
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError:
            return "invalid"
        if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
            return "invalid"
    servers = data.setdefault("mcpServers", {})
    if servers.get(SERVER_KEY) == entry:
        return "unchanged"
    result = "updated" if SERVER_KEY in servers else "added"
    servers[SERVER_KEY] = entry
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def merge_codex_config(path: Path, command: list[str]) -> str:
    """Append this server's table to Codex's ``config.toml`` if it is missing."""
    header = f"[mcp_servers.{SERVER_KEY}]"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if any(line.strip() == header for line in existing.splitlines()):
        return "present"
    # A JSON string is a valid TOML basic string for paths.
    block = f"{header}\ncommand = {json.dumps(command[0])}\n"
    if command[1:]:
        block += f"args = {json.dumps(command[1:])}\n"
    separator = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing + separator + block, encoding="utf-8")
    return "added"


def quote_command(command: list[str], platform: str = sys.platform) -> str:
    if platform == "win32":
        return " ".join(f'"{part}"' if " " in part or not part else part for part in command)
    return " ".join(shlex.quote(part) for part in command)


def manual_instructions(command: list[str], platform: str = sys.platform) -> str:
    """Config snippets for clients the setup did not change itself."""
    snippet = json.dumps({"mcpServers": {SERVER_KEY: {"command": command[0], "args": command[1:]}}},
                         indent=2)
    quoted = quote_command(command, platform)
    return (
        f"Claude Code:  claude mcp add --scope user {SERVER_KEY} {quoted}\n"
        f"Codex:        codex mcp add {SERVER_KEY} -- {quoted}\n"
        "Other clients that use an mcpServers JSON file:\n"
        f"{snippet}\n"
    )


def _ask_yes(question: str, ask: Callable[[str], str], default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = ask(question + suffix).strip().lower()
    return default if not answer else answer in {"y", "yes"}


def run_setup(env_file: Path | None = None, *, interactive: bool | None = None,
              ask: Callable[[str], str] = input,
              ask_hidden: Callable[[str], str] = getpass.getpass,
              out: Callable[[str], None] = print,
              command: list[str] | None = None,
              clients: dict[str, Path] | None = None,
              codex_config: Path | None = None) -> int:
    """Store credentials privately and register the server with local clients."""
    env_file = env_file or user_env_file()
    interactive = sys.stdin.isatty() if interactive is None else interactive
    command = command or server_command()
    clients = client_config_paths() if clients is None else clients
    codex_config = codex_config or codex_config_path()

    out("ITU MCP + PaperBridge setup")
    out(f"Settings file: {env_file}")
    if not interactive:
        out("No terminal is attached, so nothing was changed. Run `itu-mcp --setup` "
            "in a terminal, or add NINOVA_USERNAME and NINOVA_PASSWORD to the file above.")
        out(manual_instructions(command))
        return 0

    current = read_env_values(env_file)
    have_credentials = all(current.get(key) for key in CREDENTIAL_KEYS)
    if not have_credentials or _ask_yes("ITU credentials are already saved. Replace them?", ask, False):
        out("Your ITU login (usually name@itu.edu.tr) is stored only in the file above.\n"
            "Leave it empty to use only the public tools for now.")
        username = ask("ITU username: ").strip()
        if username:
            secret = ask_hidden("ITU password (hidden): ")
            try:
                set_env_values(env_file, {CREDENTIAL_KEYS[0]: username, CREDENTIAL_KEYS[1]: secret})
            except ValueError as exc:
                out(f"Could not save the credentials: {exc} Edit {env_file} by hand.")
                return 1
            out("Saved. Only your user account can read the file.")

    changed = False
    for name, path in clients.items():
        if not path.parent.is_dir():
            continue
        if not _ask_yes(f"Add ITU MCP to {name} ({path})?", ask):
            continue
        result = merge_json_config(path, command)
        if result == "invalid":
            out(f"{name}: {path} is not valid JSON, so I left it alone. Add the snippet below by hand.")
        else:
            changed = changed or result != "unchanged"
            out(f"{name}: {result}.")
    if codex_config.parent.is_dir() and _ask_yes(f"Add ITU MCP to Codex ({codex_config})?", ask):
        result = merge_codex_config(codex_config, command)
        changed = changed or result == "added"
        out(f"Codex: {'added' if result == 'added' else 'already configured'}.")

    out("\nFor other clients:\n" + manual_instructions(command))
    if changed:
        out("Restart the changed clients, then ask your assistant to run auth_status.")
    out(f"Check the login from a terminal with: {quote_command(command)} --check-auth")
    return 0


def server_env_template(state_dir: str, port: int,
                        token: Callable[[int], str] = secrets.token_hex) -> str:
    """A new server environment file with fresh secrets and placeholders."""
    return (
        "# ITU MCP + PaperBridge server settings, read by systemd (EnvironmentFile=).\n"
        "# Keep this file private. Restart the service after editing it.\n"
        "NINOVA_USERNAME=\n"
        "NINOVA_PASSWORD=\n"
        "NINOVA_REMOTE_HOST=127.0.0.1\n"
        f"NINOVA_REMOTE_PORT={port}\n"
        f"NINOVA_REMOTE_MCP_PATH=/mcp-{token(16)}\n"
        f"NINOVA_REMOTE_API_KEY={token(32)}\n"
        "NINOVA_REMOTE_REQUIRE_API_KEY=1\n"
        f"NINOVA_STATE_DIR={state_dir}\n"
        "# Your own HTTPS address (tunnel or reverse proxy) for ChatGPT and Claude.ai:\n"
        "# NINOVA_PUBLIC_BASE_URL=https://itu-mcp.example.com\n"
        "# NINOVA_ALLOWED_HOSTS=itu-mcp.example.com\n"
        "# Set only behind a real reverse proxy that sets X-Forwarded-For:\n"
        "# NINOVA_REMOTE_TRUST_PROXY_HEADERS=1\n"
        "# Remote file tools and write actions need a managed folder:\n"
        f"# NINOVA_REMOTE_FILE_ROOT={state_dir}/remote-files\n"
        "# Turn off the daily update check:\n"
        "# ITU_MCP_UPDATE_CHECK=0\n"
    )


def prepare_server_env(path: Path, state_dir: str, port: int, *, ask_credentials: bool,
                       ask: Callable[[str], str] = input,
                       ask_hidden: Callable[[str], str] = getpass.getpass,
                       out: Callable[[str], None] = print) -> str:
    """Create the server env file once; later runs keep it and only fix its mode."""
    created = not path.exists()
    if created:
        write_private_text(path, server_env_template(state_dir, port))
    else:
        os.chmod(path, 0o600)
    current = read_env_values(path)
    if ask_credentials and not all(current.get(key) for key in CREDENTIAL_KEYS):
        out("Enter your ITU login for the server (leave empty to add it to the file later).")
        username = ask("ITU username: ").strip()
        if username:
            secret = ask_hidden("ITU password (hidden): ")
            set_env_values(path, {CREDENTIAL_KEYS[0]: username, CREDENTIAL_KEYS[1]: secret},
                           formatter=format_systemd_value)
    return "created" if created else "kept"


_UNIT_NAME = re.compile(r"^[A-Za-z0-9_.@-]+$")


def systemd_unit(*, executable: str, env_file: str, state_dir: str, user: str) -> str:
    """The systemd unit for ``itu-mcp-remote``; it listens on loopback only."""
    for value in (executable, env_file, state_dir):
        if not value.startswith("/") or any(char.isspace() for char in value):
            raise ValueError(f"Expected an absolute path without spaces: {value!r}")
    if not _UNIT_NAME.match(user):
        raise ValueError(f"Invalid user name: {user!r}")
    return (
        "[Unit]\n"
        "Description=ITU MCP + PaperBridge remote MCP server\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={user}\n"
        f"EnvironmentFile={env_file}\n"
        f"Environment=HOME={state_dir}\n"
        f"WorkingDirectory={state_dir}\n"
        f"ExecStart={executable}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "UMask=0077\n"
        "NoNewPrivileges=yes\n"
        "PrivateTmp=yes\n"
        "ProtectSystem=strict\n"
        "ProtectHome=yes\n"
        f"ReadWritePaths={state_dir}\n"
        "ProtectKernelTunables=yes\n"
        "ProtectKernelModules=yes\n"
        "ProtectControlGroups=yes\n"
        "RestrictSUIDSGID=yes\n"
        "LockPersonality=yes\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _terminal_prompts() -> tuple[Callable[[str], str], Callable[[str], str]] | None:
    """Prompts that read the terminal even when stdin is a pipe (``curl | sh``)."""
    if sys.stdin.isatty():
        return input, getpass.getpass
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ninova_mcp.installer")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Save credentials and configure local MCP clients.")
    setup.add_argument("--env-file", type=Path)
    env = commands.add_parser("server-env", help="Create the server's private env file.")
    env.add_argument("--file", type=Path, required=True)
    env.add_argument("--state-dir", required=True)
    env.add_argument("--port", type=int, default=8000)
    env.add_argument("--ask-credentials", action="store_true")
    unit = commands.add_parser("systemd-unit", help="Print the systemd unit.")
    unit.add_argument("--executable", required=True)
    unit.add_argument("--env-file", required=True)
    unit.add_argument("--state-dir", required=True)
    unit.add_argument("--user", required=True)
    args = parser.parse_args(argv)

    if args.command == "setup":
        return run_setup(args.env_file)
    if args.command == "server-env":
        prompts = _terminal_prompts() if args.ask_credentials else None
        ask, ask_hidden = prompts or (input, getpass.getpass)
        result = prepare_server_env(args.file, args.state_dir, args.port,
                                    ask_credentials=prompts is not None, ask=ask, ask_hidden=ask_hidden)
        print(f"Environment file {result}: {args.file}")
        return 0
    print(systemd_unit(executable=args.executable, env_file=args.env_file,
                       state_dir=args.state_dir, user=args.user), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
