"""Opt-in remote adapters; never expose arbitrary host filesystem paths."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import Any, Iterable
import uuid

from .client import NinovaError
from .parsing import make_snapshot_payload, parse_html_page, sanitize_filename
from .server import NinovaMcpApp
from .text_extract import DEFAULT_MAX_CHARS, guess_extension
from .tracking import utc_now_iso

MIB = 1024 * 1024
REMOTE_FILE_TOOLS = {
    "download_resource", "read_resource_text", "read_page", "snapshot_page",
    "diff_snapshot", "obs_download_transcript", "get_assignment_upload_slots",
}
REMOTE_WRITE_TOOLS = {"submit_assignment", "library_renew_loan", "library_reserve_item"}
REMOTE_FILE_INSTRUCTIONS = (
    "\nRemote files are stored on the server's managed data disk, not on the user's PC. "
    "Use returned paths with read_resource_text. File paths outside the managed "
    "downloads/uploads folders are forbidden. Users can place assignment files in "
    "the managed uploads folder via SSH/SFTP. Never execute downloaded files. "
    "For submit_assignment/library_renew_loan/library_reserve_item first preview "
    "with confirm=false, then obtain explicit user approval of that exact operation "
    "before confirm=true. Enabling a tool is not approval to perform a submission, "
    "replacement, renewal or reservation. On HTTP 429 respect Retry-After."
)


class ManagedFiles:
    def __init__(self, root: Path, mount: Path | None = None) -> None:
        self.root = root.absolute()
        self.mount = mount.absolute() if mount else None
        self.lock = threading.RLock()
        self.max_bytes = max(1, min(int(os.getenv("NINOVA_REMOTE_MAX_DOWNLOAD_MIB", "256")), 1024)) * MIB
        self.min_free = max(1, int(os.getenv("NINOVA_REMOTE_MIN_FREE_MIB", "2048"))) * MIB
        self.check()
        for name in ("downloads", "uploads", "snapshots"):
            self.directory(name)

    def check(self) -> None:
        if self.root.resolve() != self.root or not self.root.is_dir():
            raise NinovaError("Managed file root must exist and must not contain symlinks.")
        if self.mount:
            if not self.root.is_relative_to(self.mount) or self.root == self.mount:
                raise NinovaError("Managed file root must be a child of the required mount.")
            # autofs and the SD-card underlay are not acceptable substitutes.
            mounted = False
            for line in Path("/proc/self/mountinfo").read_text().splitlines():
                fields = line.split()
                separator = fields.index("-")
                target = fields[4].replace("\\040", " ").replace("\\134", "\\")
                if target == str(self.mount) and fields[separator + 1] not in {"autofs", "tmpfs"}:
                    mounted = True
            if not mounted or self.root.stat().st_dev == Path("/").stat().st_dev:
                raise NinovaError("Required data disk is not mounted; refusing SD-card fallback.")

    def checked_path(self, value: str | Path, areas: tuple[str, ...]) -> Path:
        self.check()
        supplied = Path(value)
        candidate = supplied if supplied.is_absolute() else self.root / supplied
        # Check every component before resolve(), including links that stay in-root.
        if ".." in candidate.parts or any(p.is_symlink() for p in (candidate, *candidate.parents)):
            raise NinovaError("Traversal and symbolic links are not allowed in managed paths.")
        candidate = candidate.resolve()
        if not any(candidate.is_relative_to(self.root / area) for area in areas):
            raise NinovaError("Path is outside the permitted managed folders.")
        return candidate

    def directory(self, area: str, supplied: str | None = None) -> Path:
        candidate = Path(supplied) if supplied else self.root / area
        if not candidate.is_absolute():
            # Bare folder names are relative to the tool's area. Preserve older
            # root-relative downloads/... paths, without reinterpreting uploads
            # or snapshots as download subfolders. Validation below still rejects
            # cross-area paths, traversal and symlinks before creating anything.
            if candidate.parts and candidate.parts[0] in {"downloads", "uploads", "snapshots"}:
                candidate = self.root / candidate
            else:
                candidate = self.root / area / candidate
        path = self.checked_path(candidate, (area,))
        current = self.root
        for part in path.relative_to(self.root).parts:
            current = current / part
            if current.exists():
                if not current.is_dir():
                    raise NinovaError("Managed output_dir must be a directory, not a file.")
            else:
                current.mkdir(mode=0o770)
                # RestrictSUIDSGID=yes forbids requesting setgid in mkdir/chmod.
                # Assign the managed root's group explicitly, then counter the
                # service UMask=0077 without any special permission bits.
                if os.name == "posix":
                    os.chown(current, -1, self.root.stat().st_gid)
                    current.chmod(0o770)
        return path

    def read(self, value: str, areas: tuple[str, ...], limit: int) -> bytes:
        path = self.checked_path(value, areas)
        if not path.is_file() or path.stat().st_size > limit:
            raise NinovaError("Managed file is missing or exceeds the reading size limit.")
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise NinovaError("Managed file exceeds the reading size limit.")
        return data

    def save(self, chunks: Iterable[bytes], name: str, *, area: str = "downloads",
             directory: str | None = None) -> dict[str, Any]:
        with self.lock:
            target_dir = self.directory(area, directory)
            # UUID makes parallel downloads collision-safe; no existing file is overwritten.
            safe_name = sanitize_filename(name).strip(". ")[:180] or "download"
            target = target_dir / f"{uuid.uuid4().hex[:16]}-{safe_name}"
            partial = target.with_name(target.name + ".partial")
            total = 0
            digest = hashlib.sha256()
            try:
                with partial.open("xb") as handle:
                    for chunk in chunks:
                        if not chunk:
                            continue
                        self.check()
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise NinovaError("Download exceeds the configured file size limit.")
                        if shutil.disk_usage(self.root).free - len(chunk) < self.min_free:
                            raise NinovaError("Data disk free-space reserve reached.")
                        handle.write(chunk)
                        digest.update(chunk)
                    if not total:
                        raise NinovaError("Server returned an empty file.")
                    handle.flush()
                    os.fsync(handle.fileno())
                self.check()
                if os.name == "posix":
                    # Newly created directories need no setgid bit. Their files
                    # still belong to the managed root's retrieval group.
                    os.chown(partial, -1, self.root.stat().st_gid)
                    partial.chmod(0o640)
                partial.replace(target)
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
            return {"path": str(target), "filename": safe_name, "size_bytes": total,
                    "sha256": digest.hexdigest(), "storage": "server_managed_disk"}


class RemoteNinovaMcpApp(NinovaMcpApp):
    remote_tool_notes = {
        "download_resource": " Remote: saves to the managed HDD downloads folder. Omit output_dir for its root; 'course/notes' and 'downloads/course/notes' both select its course/notes subfolder. Absolute paths are accepted only inside managed downloads. Returned path is on the server, not the user's PC. Never execute files.",
        "read_resource_text": " Remote: paths must be inside managed downloads/uploads. Parsing is bounded to 25 MiB, 200 PDF pages, 30 seconds and (Linux) 512 MiB memory. save_download saves to HDD.",
        "obs_download_transcript": " Remote: omit output_dir for the managed HDD downloads folder; 'transcripts' and 'downloads/transcripts' select its transcripts subfolder. Absolute paths must stay inside managed downloads.",
        "submit_assignment": " Remote: file_path must be a file deliberately staged in the managed HDD uploads folder via SSH/SFTP, for example 'uploads/homework.pdf' or its absolute server path. Files in downloads are intentionally rejected, including with confirm=false; do not automatically copy downloads into uploads. First preview with confirm=false, obtain explicit approval of this exact submission, then confirm=true; replacement needs separate approval.",
        "library_renew_loan": " First preview with confirm=false and obtain explicit user approval of this exact renewal before confirm=true.",
        "library_reserve_item": " First preview with confirm=false and obtain explicit user approval of this exact reservation before confirm=true.",
        "snapshot_page": " Remote: snapshots are stored only in the managed HDD snapshots folder.",
        "diff_snapshot": " Remote: snapshot_path must refer to a snapshot in the managed HDD snapshots folder.",
        "help_prepare_ticket": " Remote: attachments and file fields must be lists of files on the server's managed disk, in uploads or downloads.",
        "mail_prepare_message": " Remote: attachments must be files on the server's managed disk, in uploads (staged via SSH/SFTP) or downloads (for example a file saved by download_resource).",
    }

    def __init__(self) -> None:
        root = os.getenv("NINOVA_REMOTE_FILE_ROOT")
        if not root or not Path(root).is_absolute():
            raise NinovaError("Opt-in remote file/write tools require an absolute NINOVA_REMOTE_FILE_ROOT.")
        mount = os.getenv("NINOVA_REMOTE_REQUIRED_MOUNT")
        self.files = ManagedFiles(Path(root), Path(mount) if mount else None)
        self._file_operations = threading.RLock()
        super().__init__()
        self.snapshot_dir = self.files.directory("snapshots")

    def _bounded_response(self, response: Any, limit: int) -> bytes:
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            total += len(chunk)
            if total > limit:
                raise NinovaError("Resource exceeds the remote reading size limit; use download_resource.")
            chunks.append(chunk)
        return b"".join(chunks)

    def _page(self, url: str) -> dict[str, Any]:
        with self.client.get(url, stream=True) as response:
            response._content = self._bounded_response(response, 5 * MIB)
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                raise NinovaError("Resource is not HTML; use download_resource or read_resource_text.")
            html = self.client._decode_response(response)
            return parse_html_page(response.url, html, base_url=self.client.base_url)

    def download_resource(self, url: str, output_dir: str | None = None,
                          filename: str | None = None) -> dict[str, Any]:
        self.files.directory("downloads", output_dir)  # Reject unsafe input before any network call.
        with self._file_operations, self.client.get(url, stream=True) as response:
            result = self.files.save(response.iter_content(chunk_size=64 * 1024),
                                     filename or self._filename_from_response(response), directory=output_dir)
            result.update(url=response.url, content_type=response.headers.get("Content-Type"))
            return result

    def read_resource_text(self, url: str | None = None, path: str | None = None,
                           max_chars: int = DEFAULT_MAX_CHARS, filename: str | None = None,
                           save_download: bool = False) -> dict[str, Any]:
        if bool(url) == bool(path):
            raise NinovaError("Provide exactly one of url or path.")
        with self._file_operations:
            self.files.check()
            if path:
                data = self.files.read(path, ("downloads", "uploads"), 25 * MIB)
                name = filename or Path(path).name
                info = {"source": "path", "path": str(self.files.checked_path(path, ("downloads", "uploads")))}
                extension = guess_extension(filename=name)
            else:
                with self.client.get(url, stream=True) as response:
                    data = self._bounded_response(response, 25 * MIB)
                    name = filename or self._filename_from_response(response)
                    extension = guess_extension(filename=name, content_type=response.headers.get("Content-Type"))
                    info = {"source": "url", "url": response.url, "filename": name}
                if save_download:
                    info.update(self.files.save([data], name))
            # Parsing untrusted PDF/DOCX happens without credentials, under resource/time limits.
            env = {"PYTHONPATH": str(Path(__file__).resolve().parents[1])}
            if "SYSTEMROOT" in os.environ:
                env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "ninova_mcp.file_text_worker", extension,
                     str(max(1000, min(int(max_chars), 200_000)))],
                    input=data, capture_output=True, timeout=30, env=env, check=False,
                )
                if result.returncode or len(result.stdout) > 2 * MIB:
                    raise NinovaError("File parsing failed or exceeded its resource limits.")
                extracted = json.loads(result.stdout)
            except (subprocess.TimeoutExpired, ValueError) as exc:
                raise NinovaError("File parsing failed or exceeded its time limit.") from exc
            return {**extracted, **info, "size_bytes": len(data)}

    def read_page(self, url: str, include_text: bool = True, link_limit: int = 200,
                  compact: bool = False) -> dict[str, Any]:
        page = self._page(url)
        page.pop("text", None)
        if not include_text:
            page.pop("text_excerpt", None)
        page["links"] = page.get("links", [])[:max(1, min(int(link_limit), 500))]
        return self._out(page, compact=compact)

    def snapshot_page(self, url: str, label: str | None = None) -> dict[str, Any]:
        with self._file_operations:
            self.files.check()
            page = self._page(url)
            payload = make_snapshot_payload(page, label=label)
            data = json.dumps({"captured_at": utc_now_iso(), "snapshot": payload}, ensure_ascii=False).encode()
            saved = self.files.save([data], "snapshot.json", area="snapshots")
            return {"snapshot_path": saved["path"], "url": page["url"],
                    "title": page["title"], "text_hash": page["text_hash"]}

    def diff_snapshot(self, url: str, snapshot_path: str | None = None,
                      label: str | None = None) -> dict[str, Any]:
        from .parsing import compare_snapshot_payloads
        from .parsing import normalize_url
        with self._file_operations:
            self.files.check()
            if snapshot_path:
                candidates = [self.files.checked_path(snapshot_path, ("snapshots",))]
            else:
                candidates = [self.files.checked_path(str(p), ("snapshots",))
                              for p in self.snapshot_dir.glob("*.json")]
                candidates.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
            previous = None
            previous_path = None
            for candidate in candidates:
                try:
                    item = json.loads(self.files.read(str(candidate), ("snapshots",), 10 * MIB))["snapshot"]
                except (ValueError, KeyError):
                    continue
                if item.get("url") != normalize_url(url, self.client.base_url):
                    continue
                if label is not None and item.get("label") != label:
                    continue
                previous, previous_path = item, str(candidate)
                break
            if previous is None:
                raise NinovaError("No matching readable snapshot was found.")
            page = self._page(url)
            return {**compare_snapshot_payloads(previous, make_snapshot_payload(page, label=label)),
                    "snapshot_path": previous_path}

    def obs_download_transcript(self, english: bool = False,
                                output_dir: str | None = None) -> dict[str, Any]:
        with self._file_operations:
            self.files.directory("downloads", output_dir)
            payload = self.obs.get_transcript_preview(english=english)
            encoded = payload.get("belgeAsByteArray") if isinstance(payload, dict) else None
            if not isinstance(encoded, str) or len(encoded) > 36 * MIB:
                raise NinovaError("Transcript response missing or too large.")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise NinovaError("Invalid transcript encoding.") from exc
            if not raw.startswith(b"%PDF-"):
                raise NinovaError("Transcript response is not a PDF.")
            result = self.files.save([raw], "transcript-en.pdf" if english else "transcript-tr.pdf", directory=output_dir)
            return {**result, "english": english, "statusCode": payload.get("statusCode")}

    def mail_prepare_message(self, subject: str = "", body: str = "", to: list[str] | None = None,
                             cc: list[str] | None = None, reply_to_uid: str | None = None,
                             attachments: list[str] | None = None, forward_uid: str | None = None,
                             forward_mailbox: str = "INBOX",
                             forward_attachments: bool = True) -> dict[str, Any]:
        # Attach only managed files, never service state or credentials. Forwarded
        # attachments come from the mailbox, not from the server's disk.
        paths = [str(self.files.checked_path(item, ("uploads", "downloads"))) for item in attachments or []]
        return super().mail_prepare_message(subject, body, to=to, cc=cc, reply_to_uid=reply_to_uid,
                                            attachments=paths or None, forward_uid=forward_uid,
                                            forward_mailbox=forward_mailbox,
                                            forward_attachments=forward_attachments)

    def help_prepare_ticket(self, unit_id: int, category_id: int, title: str, description: str,
                            subcategory_id: int | None = None, extra_fields: dict[str, Any] | None = None,
                            attachments: list[str] | None = None) -> dict[str, Any]:
        def managed(item: Any) -> str:
            return str(self.files.checked_path(str(item), ("uploads", "downloads")))

        # File-typed extra fields hold paths too; text values are left as they are.
        fields = dict(extra_fields or {})
        for key, value in fields.items():
            if isinstance(value, list):
                fields[key] = [managed(item) for item in value]
        return super().help_prepare_ticket(unit_id, category_id, title, description,
                                           subcategory_id=subcategory_id, extra_fields=fields or None,
                                           attachments=[managed(item) for item in attachments or []] or None)

    def submit_assignment(self, file_path: str, course: str | None = None,
                          assignment: str | None = None, upload_url: str | None = None,
                          slot_index: int | None = None, slot_description: str | None = None,
                          confirm: bool = False, allow_replace: bool = False) -> dict[str, Any]:
        with self._file_operations:
            # Only deliberately staged uploads may be submitted, never state/credentials.
            try:
                path = self.files.checked_path(file_path, ("uploads",))
            except NinovaError as exc:
                raise NinovaError(
                    "submit_assignment requires a file deliberately staged in the managed uploads folder "
                    "via SSH/SFTP (for example uploads/homework.pdf). Downloads are intentionally not "
                    "accepted, even with confirm=false. Do not automatically promote downloaded files. "
                    f"Path validation: {exc}"
                ) from exc
            return super().submit_assignment(str(path), course=course, assignment=assignment,
                upload_url=upload_url, slot_index=slot_index, slot_description=slot_description,
                confirm=confirm, allow_replace=allow_replace)
