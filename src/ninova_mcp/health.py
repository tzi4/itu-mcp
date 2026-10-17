"""One read-only health report for every ITU service the server talks to.

Each check runs with a short deadline, and the checks run concurrently. The
Ninova, OBS and Portal checks share one login, so they run one after another
in a single worker; mail, papers, archive and the public catalog run beside
them. A check that misses the deadline is reported as ``timeout``; its
request finishes in the background and changes nothing.

The report never contains credentials, usernames, cookie names, file paths
or upstream error text: only states, error categories and latencies.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable

DEFAULT_TIMEOUT_SECONDS = 10.0
MIN_TIMEOUT_SECONDS = 2.0
MAX_TIMEOUT_SECONDS = 30.0
SERVICES = ("ninova", "obs", "portal", "mail", "papers", "archive", "public")
# A small public endpoint that shows whether paper metadata lookups can work.
CROSSREF_PROBE_URL = "https://api.crossref.org/types"
_OFF_VALUES = {"0", "false", "no", "off"}


def _credentials_configured() -> bool:
    return bool(os.getenv("NINOVA_USERNAME") and os.getenv("NINOVA_PASSWORD"))


def _error_state(error: BaseException) -> dict[str, Any]:
    """Classify a failure without repeating its message, which may name the user."""
    import requests

    from .client import NinovaAuthError
    from .mail_client import ItuMailAuthError

    if isinstance(error, _Skipped):
        return {"status": "skipped", "reason": "Ninova login failed; this service uses the same login."}
    if isinstance(error, (NinovaAuthError, ItuMailAuthError)):
        return {"status": "authentication_failed", "authenticated": False}
    if isinstance(error, (TimeoutError, requests.Timeout)):
        return {"status": "timeout"}
    return {"status": "unreachable", "error_type": type(error).__name__}


class _Recorder:
    """Collects results from worker threads; late results after the deadline are dropped."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self.results: dict[str, dict[str, Any]] = {}

    def run(self, service: str, check: Callable[[], dict[str, Any]]) -> bool:
        started = time.monotonic()
        try:
            state = {"status": "ok", **check()}
        except Exception as error:  # noqa: BLE001 - reported as a category, never raised
            state = _error_state(error)
        state["latency_ms"] = round((time.monotonic() - started) * 1000)
        with self._lock:
            if not self._closed:
                self.results[service] = state
        return state["status"] == "ok"

    def close(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            self._closed = True
            return dict(self.results)


class _Skipped(Exception):
    """OBS and the Portal share the Ninova login, so they are not tried without it."""


def _skipped() -> dict[str, Any]:
    raise _Skipped


def _login_chain(app: Any, recorder: _Recorder) -> None:
    if not recorder.run("ninova", lambda: _ninova(app)):
        recorder.run("obs", _skipped)
        recorder.run("portal", _skipped)
        return
    recorder.run("obs", lambda: _obs(app))
    recorder.run("portal", lambda: _portal(app))


def _ninova(app: Any) -> dict[str, Any]:
    app.client.ensure_logged_in(verify=True)
    return {"authenticated": True}


def _obs(app: Any) -> dict[str, Any]:
    ready = app.obs.ensure_ready()
    if not ready.get("jwt_present"):
        raise RuntimeError("OBS returned no session token")
    return {"authenticated": True}


def _portal(app: Any) -> dict[str, Any]:
    app.client.get_portal_html("/apps/default/")
    return {"authenticated": True}


def _mail(app: Any) -> dict[str, Any]:
    app.mail.status()
    return {"authenticated": True}


def _papers(app: Any) -> dict[str, Any]:
    import requests

    status = app.papers.call("status")
    response = requests.get(CROSSREF_PROBE_URL, timeout=MAX_TIMEOUT_SECONDS,
                            headers={"User-Agent": "itu-mcp health check"})
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")
    return {"metadata_reachable": True,
            "full_text_providers": list(status.get("configured_providers") or [])}


def _archive(app: Any) -> dict[str, Any]:
    app.archive.get_index()
    return {}


def _public(app: Any) -> dict[str, Any]:
    app.obs_public.get_academic_calendar()
    return {}


def write_actions(published: set[str] | None, remote: bool, remote_writes: bool | None) -> dict[str, Any]:
    """Which data-changing actions this server would perform, by operator switch and tool list."""
    from .env import env_switch
    from .mail_send import mail_drafts_enabled, mail_send_enabled

    switches = {
        "assignment_uploads": (
            (os.getenv("NINOVA_ALLOW_UPLOADS", "1").strip().lower() not in _OFF_VALUES),
            ("submit_assignment",),
        ),
        "obs_registration_draft_saves": (
            env_switch("NINOVA_OBS_REGISTRATION_WRITES"), ("obs_save_registration_draft",),
        ),
        "mail_send": (mail_send_enabled(), ("mail_send_message",)),
        "mail_save_drafts": (mail_drafts_enabled(), ("mail_save_draft",)),
        "help_ticket_writes": (env_switch("NINOVA_HELP_TICKET_WRITES"), ("help_submit_ticket",)),
    }
    actions: dict[str, Any] = {}
    for name, (enabled, tools) in switches.items():
        # A switched-on action is only available when its tool is published.
        actions[name] = bool(enabled) and (published is None or all(tool in published for tool in tools))
    actions["remote_writes"] = bool(remote_writes) if remote else None
    actions["note"] = ("Every enabled action still previews first and needs the user's explicit "
                       "confirmation. remote_writes is null on a local server.")
    return actions


def health_report(app: Any, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                  version: str, profile: str) -> dict[str, Any]:
    from . import update_check

    timeout = max(MIN_TIMEOUT_SECONDS, min(float(timeout_seconds), MAX_TIMEOUT_SECONDS))
    credentials = _credentials_configured()
    mail_configured = bool(os.getenv("NINOVA_MAIL_USERNAME") or os.getenv("NINOVA_USERNAME")) and bool(
        os.getenv("NINOVA_MAIL_PASSWORD") or os.getenv("NINOVA_PASSWORD"))
    configured = {"ninova": credentials, "obs": credentials, "portal": credentials,
                  "mail": mail_configured, "papers": True, "archive": True, "public": True}
    published = getattr(app, "published_tool_names", None)
    mail_published = published is None or "mail_status" in published

    recorder = _Recorder()
    executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="itu-health")
    futures = []
    try:
        if credentials:
            futures.append(executor.submit(_login_chain, app, recorder))
        if mail_configured and mail_published:
            futures.append(executor.submit(recorder.run, "mail", lambda: _mail(app)))
        for service, check in (("papers", _papers), ("archive", _archive), ("public", _public)):
            futures.append(executor.submit(recorder.run, service, lambda check=check: check(app)))
        wait(futures, timeout=timeout)
    finally:
        # Do not wait for a late check; it is read-only and its result is dropped.
        executor.shutdown(wait=False, cancel_futures=True)
    results = recorder.close()

    services: dict[str, dict[str, Any]] = {}
    for service in SERVICES:
        state: dict[str, Any] = {"configured": configured[service]}
        if not configured[service]:
            state["status"] = "not_configured"
        elif service == "mail" and not mail_published:
            state["status"] = "disabled"
        else:
            state.update(results.get(service) or {"status": "timeout"})
        services[service] = state

    ok = sum(state["status"] == "ok" for state in services.values())
    remote = bool(getattr(app, "remote_transport", False))
    commit = update_check.installed_commit()
    report: dict[str, Any] = {
        "read_only": True,
        "summary": f"{ok} of {len(services)} services ok",
        "services": services,
        "write_actions": write_actions(published, remote, getattr(app, "remote_writes_enabled", None)),
        "version": update_check.build_label(version, commit),
        "commit": commit[:7] if commit else None,
        "tool_profile": profile,
        "transport": "remote" if remote else "local",
        # API-key and rate-limit protection apply to the remote HTTP transport only.
        "remote_security": getattr(app, "remote_security", None) if remote else None,
        "timeout_seconds": timeout,
    }
    notice = update_check.cached_notice(app.state_dir)
    if notice:
        report["update_notice"] = notice
    return report
