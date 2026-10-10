"""Bounded page rendering in a separate process, with no credential inheritance."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys


class PageRenderUnavailable(RuntimeError):
    pass


def render_page(data: bytes, page: int, max_edge: int = 2000) -> dict:
    if type(page) is not int or not 1 <= page <= 200:
        raise ValueError("Page must be an integer from 1 to 200.")
    if type(max_edge) is not int or not 800 <= max_edge <= 2400:
        raise ValueError("Image edge must be an integer from 800 to 2400 pixels.")
    if len(data) > 20_000_000 or not data.startswith(b"%PDF-"):
        raise ValueError("A PDF of at most 20 MB is required.")
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).with_name("render_worker.py")),
             str(page), str(max_edge)], input=data, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=25, check=True,
            env={k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "LANG") if k in os.environ})
        if len(result.stdout) > 11_000_000:
            raise ValueError()
        body = json.loads(result.stdout)
        png = base64.b64decode(body["png"], validate=True)
        if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > 8_000_000:
            raise ValueError()
        return {"image_bytes": png, "width": body["width"], "height": body["height"]}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise PageRenderUnavailable("PDF page rendering failed or exceeded its limits.") from None
