"""Optional local publisher egress, configured by the host operator only."""
from __future__ import annotations

import os
from urllib.parse import urlsplit


def publisher_proxy() -> str | None:
    """Use the same explicit proxy for Chromium and authenticated PDF requests.

    Do not inherit ambient HTTP_PROXY credentials or accept proxy URLs from MCP.
    This prototype supports a loopback HTTP CONNECT proxy supplied by the host.
    It does not configure or disable a VPN, create a proxy, or modify routing.
    """
    raw = os.environ.get("PAPERBRIDGE_PUBLISHER_PROXY", "")
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or not parsed.port or not 1024 <= parsed.port <= 65535
                or any(ord(c) < 33 for c in raw)):
            raise ValueError()
        host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
        return f"http://{host}:{parsed.port}"
    except ValueError:
        raise ValueError("PAPERBRIDGE_PUBLISHER_PROXY must be a loopback HTTP proxy with port 1024..65535.") from None
