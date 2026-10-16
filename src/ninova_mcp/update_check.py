"""Tell the user, at most once a day, when this fork's main branch has moved on.

The fork does not publish releases and its package version rarely changes, so
the installed build is identified by the Git commit it was installed from. The
installers install a commit archive (``.../archive/<sha>.tar.gz`` or ``.zip``)
and pip and uv record that URL in the package's ``direct_url.json`` (PEP 610);
a ``git+https`` install records ``vcs_info.commit_id`` instead. Installs whose
commit is unknown, such as ``pip install .`` from a clone or a bundle, are not
checked.

One small GitHub API request (``compare/main...<installed commit>``) answers
how many commits the installed build is behind. It runs on a background
thread, never on a tool call, uses a short timeout, sends only the installed
commit, and its result is cached in the state directory for a day. Set
``ITU_MCP_UPDATE_CHECK=0`` to turn it off.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

from .env import env_switch

LOGGER = logging.getLogger(__name__)

REPOSITORY = "tzi4/itu-mcp-paperbridge"
BRANCH = "main"
DISTRIBUTION = "itu-mcp"
INSTALL_URL = f"https://github.com/{REPOSITORY}#quick-start"
CACHE_FILE_NAME = "update-check.json"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 1024 * 1024

_SHA = re.compile(r"^[0-9a-f]{40}$")
_ARCHIVE_URL = re.compile(
    r"^https://(?:codeload\.)?github\.com/" + re.escape(REPOSITORY)
    + r"/(?:archive|zip|tar\.gz)/([0-9a-f]{40})(?:\.zip|\.tar\.gz)?$",
    re.IGNORECASE,
)
_REPOSITORY_URL = re.compile(
    r"^(?:git\+)?(?:https|ssh)://(?:git@)?github\.com/" + re.escape(REPOSITORY) + r"(?:\.git)?/?$",
    re.IGNORECASE,
)


def update_check_enabled() -> bool:
    """The check is on unless ITU_MCP_UPDATE_CHECK is set to 0/false/no/off."""
    return env_switch("ITU_MCP_UPDATE_CHECK")


def commit_from_direct_url(direct_url: dict[str, Any]) -> str | None:
    """Return the fork commit recorded in a PEP 610 ``direct_url.json``."""
    url = str(direct_url.get("url") or "").strip()
    vcs_info = direct_url.get("vcs_info")
    if isinstance(vcs_info, dict):
        commit = str(vcs_info.get("commit_id") or "").lower()
        if _SHA.match(commit) and _REPOSITORY_URL.match(url):
            return commit
        return None
    match = _ARCHIVE_URL.match(url)
    return match.group(1).lower() if match else None


def installed_commit(distribution: str = DISTRIBUTION) -> str | None:
    """Return the commit this package was installed from, or None if unknown."""
    try:
        text = metadata.distribution(distribution).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return commit_from_direct_url(data) if isinstance(data, dict) else None


def build_label(version: str, commit: str | None = None) -> str:
    """Return ``version`` plus the short installed commit when it is known."""
    commit = installed_commit() if commit is None else commit
    return f"{version} (commit {commit[:7]})" if commit else version


def default_state_dir() -> Path:
    return Path(os.getenv("NINOVA_STATE_DIR") or Path.home() / ".ninova_state")


def cache_path(state_dir: str | Path | None = None) -> Path:
    return Path(state_dir or default_state_dir()) / CACHE_FILE_NAME


def read_cache(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_cache(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def is_fresh(record: dict[str, Any] | None, commit: str, now: float,
             interval: float = CHECK_INTERVAL_SECONDS) -> bool:
    """Whether ``record`` already answers for ``commit`` within ``interval``."""
    if not record or record.get("installed_commit") != commit:
        return False
    try:
        checked_at = float(record.get("checked_at") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= now - checked_at < interval


def fetch_status(commit: str, *, get: Callable[..., Any] | None = None,
                 timeout: float = REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Ask GitHub how far ``commit`` is behind the fork's main branch.

    ``compare/main...<commit>`` lists only commits that main lacks, so the
    usual answer for an older install is a few kilobytes with ``behind_by``.
    """
    if get is None:
        import requests

        get = requests.get
    url = f"https://api.github.com/repos/{REPOSITORY}/compare/{BRANCH}...{commit}"
    response = get(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "itu-mcp-update-check",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=timeout,
        stream=True,
    )
    try:
        if response.status_code == 404:
            return {"status": "unknown", "error": "commit not found on GitHub"}
        if response.status_code != 200:
            return {"status": "error", "error": f"HTTP {response.status_code}"}
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                return {"status": "error", "error": "response too large"}
        data = json.loads(bytes(body).decode("utf-8"))
    finally:
        response.close()
    behind_by = data.get("behind_by")
    if not isinstance(behind_by, int) or behind_by < 0:
        return {"status": "error", "error": "unexpected response"}
    latest = str(((data.get("base_commit") or {}).get("sha")) or "")
    return {
        "status": str(data.get("status") or ""),
        "behind_by": behind_by,
        "latest_commit": latest if _SHA.match(latest) else None,
        "compare_url": f"https://github.com/{REPOSITORY}/compare/{commit[:12]}...{BRANCH}",
    }


def check_now(state_dir: str | Path | None = None, *, force: bool = False,
              commit: str | None = None, now: float | None = None,
              fetch: Callable[[str], dict[str, Any]] = fetch_status) -> dict[str, Any] | None:
    """Refresh the cached result when it is older than a day; return it.

    Returns None when the check is turned off or the installed commit is
    unknown. Network failures are recorded too, so an offline machine tries
    again the next day instead of on every start.
    """
    if not update_check_enabled():
        return None
    commit = commit or installed_commit()
    if not commit:
        return None
    now = time.time() if now is None else now
    path = cache_path(state_dir)
    record = read_cache(path)
    if not force and is_fresh(record, commit, now):
        return record
    try:
        result = fetch(commit)
    except Exception as exc:  # offline, DNS, timeout, TLS: never fatal
        result = {"status": "error", "error": type(exc).__name__}
    record = {"checked_at": now, "installed_commit": commit, **result}
    try:
        write_cache(path, record)
    except OSError:
        LOGGER.debug("Could not write the update check cache.", exc_info=True)
    return record


def notice_from_record(record: dict[str, Any] | None, commit: str | None) -> str | None:
    """Return a one-line update notice, or None when nothing newer is known."""
    if not record or not commit or record.get("installed_commit") != commit:
        return None
    behind_by = record.get("behind_by")
    if not isinstance(behind_by, int) or behind_by <= 0:
        return None
    plural = "commit" if behind_by == 1 else "commits"
    return (
        f"An update for ITU MCP + PaperBridge is available: main has {behind_by} new "
        f"{plural} since the installed build ({commit[:7]}). To update, run the install "
        f"command again ({INSTALL_URL}) and restart the MCP client or service."
    )


def cached_notice(state_dir: str | Path | None = None) -> str | None:
    """Read the notice from the cache only; this never touches the network."""
    if not update_check_enabled():
        return None
    commit = installed_commit()
    if not commit:
        return None
    return notice_from_record(read_cache(cache_path(state_dir)), commit)


def update_instructions(state_dir: str | Path | None = None) -> str:
    """Server instruction text for a known update, read once at startup."""
    notice = cached_notice(state_dir)
    if not notice:
        return ""
    return (
        f"\n\nUpdate notice: {notice} Mention this to the user once, briefly, at a "
        "natural point in the conversation; do not repeat it in later answers."
    )


def start_background_check(state_dir: str | Path | None = None, *,
                           interval: float = CHECK_INTERVAL_SECONDS) -> threading.Thread | None:
    """Check now and then once per ``interval`` on a daemon thread.

    The thread never blocks startup or tool calls; long-running servers keep
    the cached answer current. A newly found update is logged once per check.
    """
    if not update_check_enabled():
        return None
    commit = installed_commit()
    if not commit:
        return None

    def run() -> None:
        while True:
            before = read_cache(cache_path(state_dir))
            try:
                record = check_now(state_dir, commit=commit)
            except Exception:  # pragma: no cover - the check must never crash the server
                LOGGER.debug("Update check failed.", exc_info=True)
                record = None
            notice = notice_from_record(record, commit)
            if notice and (before or {}).get("checked_at") != (record or {}).get("checked_at"):
                LOGGER.warning("%s", notice)
            time.sleep(interval)

    thread = threading.Thread(target=run, name="itu-mcp-update-check", daemon=True)
    thread.start()
    return thread
