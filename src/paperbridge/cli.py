from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .discovery import DiscoveryError, MetadataClient
from .institutions import ItuProfile
from .service import PaperBridge
from .storage import DocumentStore, Rights


def default_data_dir() -> Path:
    return Path(os.environ.get("PAPERBRIDGE_DATA_DIR", "~/.local/share/paperbridge")).expanduser()


def main(argv=None):
    parser = argparse.ArgumentParser(description="PaperBridge research prototype")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("browser-install", help="Install the browser used by the host's PaperBridge dependency")
    login = commands.add_parser("browser-session", help="Start a private in-memory session for operator login")
    login.add_argument("--port", type=int, default=9222)
    login.add_argument("--headed", action="store_true")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--local-only", action="store_true")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("doi")
    fetch = commands.add_parser("fetch")
    fetch.add_argument("id")
    get_text = commands.add_parser("get-text")
    get_text.add_argument("reference")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--title", required=True)
    ingest.add_argument("--url", required=True)
    ingest.add_argument("--rights-file", required=True, type=Path,
                        help="Operator-recorded permissions for this document; never an MCP argument")
    args = parser.parse_args(argv)
    try:
        if args.command == "browser-install":
            engine = os.environ.get("PAPERBRIDGE_BROWSER_ENGINE", "chromium")
            if engine not in {"chromium", "firefox"}:
                raise ValueError("PAPERBRIDGE_BROWSER_ENGINE must be chromium or firefox.")
            subprocess.run([sys.executable, "-m", "playwright", "install", engine], check=True)
            return
        if args.command == "browser-session":
            from .browser import RetrievalUnavailable, run_session
            try:
                run_session("https://ieeexplore.ieee.org/Xplore/home.jsp",
                            port=args.port, headed=args.headed)
            except RetrievalUnavailable as exc:
                parser.exit(1, f"{exc.reason}\n")
            return
        if args.command == "status":
            result = PaperBridge.status()
        else:
            store = DocumentStore(args.data_dir)
            bridge = PaperBridge(store, MetadataClient(os.environ.get("PAPERBRIDGE_CONTACT_EMAIL")),
                                 institution=ItuProfile())
            if args.command == "search":
                result = bridge.search(args.query, args.limit, local_only=args.local_only)
            elif args.command == "resolve":
                result = bridge.resolve(args.doi)
            elif args.command == "fetch":
                result = bridge.fetch(args.id)
            elif args.command == "get-text":
                result = bridge.get_text(args.reference)
            else:
                rights = Rights(**json.loads(args.rights_file.read_text(encoding="utf-8")))
                result = store.ingest(args.path, title=args.title, url=args.url, rights=rights)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, TypeError, LookupError, OSError, DiscoveryError) as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
