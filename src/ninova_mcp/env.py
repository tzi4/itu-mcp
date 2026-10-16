from __future__ import annotations

import os
from pathlib import Path

_OFF_VALUES = {"0", "false", "no", "off"}
_ON_VALUES = {"1", "true", "yes", "on"}


def env_switch(name: str, default: bool = True) -> bool:
    """Read an on/off variable; unset or empty means ``default``."""
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    return value not in _OFF_VALUES


def env_choice(name: str) -> bool | None:
    """Read an on/off variable, returning None when it is unset or empty."""
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return None
    return value in _ON_VALUES


def user_env_file() -> Path:
    """The per-user private settings file written by ``itu-mcp --setup``."""
    return Path.home() / ".itu-mcp" / ".env"


def _find_env_file(env_file: str | Path | None = None) -> Path | None:
    explicit = env_file or os.getenv("NINOVA_ENV_FILE")
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        if candidate.is_file():
            return candidate
        return None

    current = Path.cwd().resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    # MCP clients start the server from an arbitrary directory, so fall back
    # to the per-user file when no project .env is found.
    candidate = user_env_file()
    return candidate if candidate.is_file() else None


def _split_key_value(line: str) -> tuple[str, str] | None:
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip().removeprefix("export ").strip()
    if not key:
        return None
    return key, value.strip()


def _strip_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(value):
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double:
            if index == 0 or value[index - 1].isspace():
                return value[:index].rstrip()
    return value.rstrip()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


# Only this project's settings are read from a .env file.
ENV_FILE_PREFIXES = ("NINOVA_", "ITU_MCP_")


def load_ninova_env(
    env_file: str | Path | None = None,
    *,
    override: bool = False,
) -> Path | None:
    path = _find_env_file(env_file=env_file)
    if path is None:
        return None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _split_key_value(line)
        if parsed is None:
            continue
        key, value = parsed
        if not key.startswith(ENV_FILE_PREFIXES):
            continue
        if not override and key in os.environ:
            continue
        os.environ[key] = _unquote(_strip_inline_comment(value))
    return path
