"""Owner-approved, content-pinned model downloads; never an implicit model load."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Tuple

from .probes import Redactor
from .schema import canonical_digest, require_digest, require_identifier


class ModelError(ValueError):
    pass


class DownloadPaused(ModelError):
    pass


def _exact(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ModelError(label + " has missing or unknown fields")
    return value


def _positive(value, label, minimum=1):
    if type(value) is not int or value < minimum or value > 2 ** 63 - 1:
        raise ModelError(label + " must be an integer >= " + str(minimum))
    return value


def _text(value, label, maximum=4096):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ModelError(label + " must be bounded non-empty text")
    return value


def _origin(url: str, *, manifest=False) -> str:
    _text(url, "download URL", 16384)
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ModelError("download URL is malformed") from error
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or "\\" in parsed.netloc
            or (manifest and parsed.query)):
        raise ModelError("manifest URLs require HTTPS without userinfo, fragments or credential/query strings")
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    return "https://" + host + (":" + str(port) if port and port != 443 else "")


@dataclass(frozen=True)
class DownloadFile:
    path: str
    url: str
    digest: str
    size_bytes: int
    credential_ref: Optional[str] = None

    def __post_init__(self):
        path = Path(_text(self.path, "model file path"))
        if path.is_absolute() or ".." in path.parts or str(path) in {".", ""} or "\\" in self.path:
            raise ModelError("model file names must be safe relative logical paths")
        object.__setattr__(self, "path", str(path))
        _origin(self.url, manifest=True)
        require_digest(self.digest, "model file digest")
        _positive(self.size_bytes, "model file size")
        if self.credential_ref is not None:
            require_identifier(self.credential_ref, "model credential reference")

    def to_dict(self):
        return dict(path=self.path, url=self.url, digest=self.digest, size_bytes=self.size_bytes,
                    credential_ref=self.credential_ref)

    @classmethod
    def from_dict(cls, value):
        return cls(**_exact(value, {"path", "url", "digest", "size_bytes", "credential_ref"}, "download file"))


@dataclass(frozen=True)
class DownloadPlan:
    plan_id: str
    model_id: str
    revision: str
    owner: str
    files: Tuple[DownloadFile, ...]
    allowed_origins: Tuple[str, ...]
    max_disk_bytes: int
    max_transfer_bytes: int
    timeout_seconds: int = 15
    max_redirects: int = 3

    def __post_init__(self):
        for name in ("plan_id", "model_id", "owner"):
            require_identifier(getattr(self, name), name)
        _text(self.revision, "model revision")
        if not isinstance(self.files, (list, tuple)) or not 1 <= len(self.files) <= 1024 or any(not isinstance(item, DownloadFile) for item in self.files):
            raise ModelError("download plan needs 1-1024 pinned files")
        files = tuple(sorted(self.files, key=lambda item: item.path))
        if len({item.path for item in files}) != len(files):
            raise ModelError("logical model file paths must be unique")
        blobs = {}
        for item in files:
            if item.digest in blobs and blobs[item.digest] != item.size_bytes:
                raise ModelError("one content digest cannot have conflicting sizes")
            blobs[item.digest] = item.size_bytes
        if not isinstance(self.allowed_origins, (list, tuple)) or not 1 <= len(self.allowed_origins) <= 32:
            raise ModelError("download origins must be an explicit bounded list")
        origins = tuple(sorted(set(_origin(item, manifest=True) for item in self.allowed_origins)))
        if any(item != _origin(item, manifest=True) for item in self.allowed_origins):
            raise ModelError("allowed_origins must contain canonical HTTPS origins, without paths")
        if any(_origin(item.url) not in origins for item in files):
            raise ModelError("every source origin needs explicit download authority")
        for name in ("max_disk_bytes", "max_transfer_bytes", "timeout_seconds"):
            _positive(getattr(self, name), name)
        _positive(self.max_redirects, "max_redirects", 0)
        if self.timeout_seconds > 120 or self.max_redirects > 8:
            raise ModelError("download timeout/redirect ceilings exceed supported bounds")
        required = sum(blobs.values())
        if self.max_disk_bytes < required or self.max_transfer_bytes < required:
            raise ModelError("disk and transfer ceilings must cover the pinned unique files")
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "allowed_origins", origins)
        if Redactor().value(self.to_dict()) != self.to_dict():
            raise ModelError("download plans cannot contain secret-shaped data; use credential references")

    def to_dict(self):
        return dict(schema="camol.model_download_plan", schema_version=1, plan_id=self.plan_id,
                    model_id=self.model_id, revision=self.revision, owner=self.owner,
                    files=[item.to_dict() for item in self.files], allowed_origins=list(self.allowed_origins),
                    max_disk_bytes=self.max_disk_bytes, max_transfer_bytes=self.max_transfer_bytes,
                    timeout_seconds=self.timeout_seconds, max_redirects=self.max_redirects)

    def digest(self):
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        fields = {"schema", "schema_version", "plan_id", "model_id", "revision", "owner", "files",
                  "allowed_origins", "max_disk_bytes", "max_transfer_bytes", "timeout_seconds", "max_redirects"}
        _exact(value, fields, "download plan")
        if value["schema"] != "camol.model_download_plan" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ModelError("unsupported model download plan version")
        if not isinstance(value["files"], list):
            raise ModelError("download files must be an array")
        return cls(**dict({name: value[name] for name in fields - {"schema", "schema_version", "files"}},
                          files=tuple(DownloadFile.from_dict(item) for item in value["files"])))


class DownloadStream(Protocol):
    status: int
    headers: Dict[str, str]
    source_origin: str
    def read(self, count: int) -> bytes: ...
    def close(self) -> None: ...


class DownloadTransport(Protocol):
    """Trusted transport plugins implement bytes only, never model execution."""
    def open(self, file: DownloadFile, *, offset: int, plan: DownloadPlan, headers: dict) -> DownloadStream: ...


class ModelHost(Protocol):
    """Hosting stays outside download lifecycle and requires separate authority."""
    name: str
    def prepare(self, plan): ...
    def approve(self, plan_digest: str, by: str): ...
    def load(self, plan_digest: str, by: str, *, operation_id: str, cancel_event=None): ...
    def status(self, plan_digest: str, *, live: bool = False): ...
    def inventory(self) -> list: ...
    def unload(self, request, by: str, *, approve_digest: str): ...


class _HTTPStream:
    def __init__(self, response):
        self.response = response
        self.status = response.status
        self.headers = {key.lower(): value for key, value in response.headers.items()}
        self.source_origin = _origin(response.geturl())
    def read(self, count):
        return self.response.read(count)
    def close(self):
        self.response.close()


class HTTPSDownloadTransport:
    def open(self, file, *, offset, plan, headers):
        allowed, ceiling = set(plan.allowed_origins), plan.max_redirects

        class Redirects(urllib.request.HTTPRedirectHandler):
            count = 0
            def redirect_request(self, request, response, code, message, response_headers, new_url):
                self.count += 1
                if self.count > ceiling or _origin(new_url) not in allowed:
                    raise ModelError("redirect exceeds its approved origin/count envelope")
                redirected = super().redirect_request(request, response, code, message, response_headers, new_url)
                if redirected is None:
                    raise ModelError("unsupported download redirect")
                if _origin(request.full_url) != _origin(new_url):
                    for name in ("Authorization", "X-api-key", "Cookie", "Proxy-authorization"):
                        redirected.remove_header(name)
                return redirected

        request_headers = {"Accept-Encoding": "identity", "User-Agent": "Camol-model-download/1"}
        for key, value in headers.items():
            if key.lower() not in {"authorization", "x-api-key"} or not isinstance(value, str) or "\r" in value or "\n" in value:
                raise ModelError("credential resolver returned unsupported authorization headers")
            _text(value, "authorization header", 16384)
            request_headers[key] = value
        if offset:
            request_headers["Range"] = "bytes={}-".format(offset)
        # Ambient proxy credentials/egress are not hidden download authority.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirects())
        try:
            response = opener.open(urllib.request.Request(file.url, headers=request_headers), timeout=plan.timeout_seconds)
        except urllib.error.HTTPError as error:
            error.close()
            raise ModelError("download HTTP status " + str(error.code)) from None
        except (OSError, urllib.error.URLError) as error:
            raise ModelError("download transport unavailable: " + type(error).__name__) from None
        return _HTTPStream(response)


class ModelStore:
    """Private local catalog. Explicit byte downloads never run a model or import it.

    Status is durable historical download evidence. ``verified_artifacts`` rehashes
    the files before a separately authorized hosting adapter can use them.
    """
    def __init__(self, root: Path, *, read_only: bool = False):
        original = Path(root).absolute()
        if original.is_symlink():
            raise ModelError("model store root cannot be a symlink")
        self.root, self.read_only = original.resolve(), read_only
        if not read_only:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = self.root.stat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ModelError("model store must be an owner-only directory (0700)")
        self._root_identity = (metadata.st_dev, metadata.st_ino)
        self._root_fd = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        self._dirs = {}
        self.connection = None
        try:
            for name in ("blobs", "partial"):
                if not read_only:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=self._root_fd)
                    except FileExistsError:
                        pass
                descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=self._root_fd)
                info = os.fstat(descriptor)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    os.close(descriptor)
                    raise ModelError("model content directories must be owner-only")
                self._dirs[name] = descriptor
            database = self.root / "models.sqlite3"
            if not read_only:
                descriptor = os.open("models.sqlite3", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=self._root_fd)
                os.close(descriptor)
            info = database.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ModelError("model catalog must be an owner-only regular file")
            if any((self.root / ("models.sqlite3" + suffix)).is_symlink() for suffix in ("-journal", "-wal", "-shm")):
                raise ModelError("model catalog sidecars cannot be symlinks")
            self.connection = sqlite3.connect(database.as_uri() + ("?mode=ro" if read_only else "?mode=rw"), uri=True)
            self.connection.row_factory = sqlite3.Row
            self._assert_contained()
            if not read_only:
                self.connection.execute("CREATE TABLE IF NOT EXISTS plans (digest TEXT PRIMARY KEY, document TEXT NOT NULL, status TEXT NOT NULL, approved_by TEXT, accounted_bytes INTEGER NOT NULL, observed_bytes INTEGER NOT NULL, error TEXT)")
                self.connection.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, digest TEXT NOT NULL, kind TEXT NOT NULL, observed_at TEXT NOT NULL, data TEXT NOT NULL)")
                self.connection.commit()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        for descriptor in self._dirs.values():
            os.close(descriptor)
        self._dirs = {}
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None

    def __enter__(self):
        return self
    def __exit__(self, *exc):
        self.close()

    def _assert_contained(self):
        if self._root_fd is None:
            raise ModelError("model store is closed")
        info = self.root.lstat()
        if stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != self._root_identity:
            raise ModelError("model store root changed during use")
        for name, descriptor in self._dirs.items():
            current = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
            actual = os.fstat(descriptor)
            if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (actual.st_dev, actual.st_ino):
                raise ModelError("model content directory changed during use")

    @contextmanager
    def _lock(self):
        self._assert_contained()
        if self.read_only:
            raise ModelError("model store is read-only")
        descriptor = os.open("download.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=self._root_fd)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ModelError("another model preparation owns this store") from error
            yield
        finally:
            os.close(descriptor)

    def _event(self, digest, kind, data):
        clean = Redactor().value(data)
        self.connection.execute("INSERT INTO events(digest,kind,observed_at,data) VALUES(?,?,?,?)",
                                (digest, kind, datetime.now(timezone.utc).isoformat(), json.dumps(clean, sort_keys=True)))

    def _row(self, digest):
        self._assert_contained()
        require_digest(digest, "download plan digest")
        row = self.connection.execute("SELECT * FROM plans WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise ModelError("unknown model download plan")
        plan = DownloadPlan.from_dict(json.loads(row["document"]))
        if plan.digest() != digest:
            raise ModelError("stored download plan failed its immutable digest")
        if row["status"] not in {"planned", "approved", "downloading", "paused", "failed", "downloaded_verified"}:
            raise ModelError("model download has an unknown lifecycle state")
        if row["approved_by"] not in {None, plan.owner}:
            raise ModelError("stored model approval has a foreign owner")
        if not 0 <= row["observed_bytes"] <= row["accounted_bytes"] <= plan.max_transfer_bytes:
            raise ModelError("stored model transfer accounting violates its ceiling")
        return row, plan

    def prepare(self, plan: DownloadPlan):
        plan = DownloadPlan.from_dict(plan.to_dict())
        with self._lock(), self.connection:
            existing = self.connection.execute("SELECT digest FROM plans WHERE digest=?", (plan.digest(),)).fetchone()
            if not existing:
                self.connection.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?)",
                                        (plan.digest(), json.dumps(plan.to_dict(), sort_keys=True), "planned", None, 0, 0, None))
                self._event(plan.digest(), "MODEL_DOWNLOAD_PLANNED", {"plan": plan.to_dict()})
        return self.status(plan.digest())

    def approve(self, plan_digest: str, by: str):
        with self._lock(), self.connection:
            row, plan = self._row(plan_digest)
            if by != plan.owner:
                raise ModelError("only the plan owner can approve its exact digest")
            if row["approved_by"] is None:
                self.connection.execute("UPDATE plans SET approved_by=?,status='approved' WHERE digest=?", (by, plan_digest))
                self._event(plan_digest, "MODEL_DOWNLOAD_APPROVED", {"approved_by": by, "plan_digest": plan_digest})
        return self.status(plan_digest)

    def list(self):
        self._assert_contained()
        return [self.status(row[0]) for row in self.connection.execute("SELECT digest FROM plans ORDER BY digest")]

    def status(self, plan_digest: str, verify: bool = False):
        row, plan = self._row(plan_digest)
        result = {name: row[name] for name in ("status", "approved_by", "accounted_bytes", "observed_bytes", "error")}
        result.update(plan_digest=plan_digest, plan=plan.to_dict(), loaded="unverified", inference_ready="unverified",
                      transfer_accounting_scope="application_payload_reads_not_wire_egress",
                      files=[{"path": item.path, "digest": item.digest, "expected_bytes": item.size_bytes,
                              "available_bytes": self._size("blobs", item.digest), "partial_bytes": self._size("partial", item.digest, plan_digest)}
                             for item in plan.files])
        if row["status"] == "downloading":
            try:
                lock = os.open("download.lock", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=self._root_fd)
            except FileNotFoundError:
                active = False
            else:
                try:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        active = False
                    except BlockingIOError:
                        active = True
                finally:
                    os.close(lock)
            result["activity"] = "store_download_active" if active else "interrupted_requires_resume"
            if not active:
                result.update(status="paused", last_recorded_status="downloading")
        if verify:
            result["artifacts"] = self.verified_artifacts(plan_digest)
        return result

    def events(self, plan_digest: str):
        self._row(plan_digest)
        return [dict(seq=row["seq"], type=row["kind"], observed_at=row["observed_at"], data=json.loads(row["data"]))
                for row in self.connection.execute("SELECT * FROM events WHERE digest=? ORDER BY seq", (plan_digest,))]

    def _name(self, digest):
        return require_digest(digest, "model content digest").split(":", 1)[1]

    def _content_name(self, folder, digest, plan_digest=None):
        return (self._name(plan_digest) + "-" if folder == "partial" else "") + self._name(digest)

    def _size(self, folder, digest, plan_digest=None):
        try:
            info = os.stat(self._content_name(folder, digest, plan_digest), dir_fd=self._dirs[folder], follow_symlinks=False)
        except FileNotFoundError:
            return 0
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ModelError("model content must be an owner-owned regular file without links")
        return info.st_size

    def _present(self, folder, digest, plan_digest=None):
        try:
            os.stat(self._content_name(folder, digest, plan_digest), dir_fd=self._dirs[folder], follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def _hash(self, folder, file, *, plan_digest=None, publication=False):
        self._assert_contained()
        descriptor = os.open(self._content_name(folder, file.digest, plan_digest), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=self._dirs[folder])
        digest, total = hashlib.sha256(), 0
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != (2 if publication else 1) or before.st_uid != os.getuid():
                raise ModelError("model content identity is unsafe")
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > file.size_bytes:
                    raise ModelError("model content exceeds its frozen size")
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ModelError("model content changed while hashing")
        return digest, total

    def verified_artifacts(self, plan_digest):
        row, plan = self._row(plan_digest)
        if row["status"] != "downloaded_verified":
            raise ModelError("model has no completed verified download")
        result = []
        for file in plan.files:
            digest, size = self._hash("blobs", file)
            if size != file.size_bytes or "sha256:" + digest.hexdigest() != file.digest:
                raise ModelError("model artifact no longer matches its download receipt")
            result.append(dict(logical_path=file.path, path=str(self.root / "blobs" / self._name(file.digest)),
                               digest=file.digest, size_bytes=size, loaded="unverified"))
        return result

    def _transition(self, plan_digest, status, error=None):
        with self.connection:
            self.connection.execute("UPDATE plans SET status=?,error=? WHERE digest=?", (status, error, plan_digest))
            self._event(plan_digest, "MODEL_DOWNLOAD_" + status.upper(), {"status": status, "error": error})

    def download(self, plan_digest: str, by: str, *, transport: Optional[DownloadTransport] = None,
                 cancel_event: Optional[threading.Event] = None, credential_resolver=None):
        """Stream in the calling thread; Ctrl+C/cancel preserves partial bytes.

        No automatic retry occurs. A new call may resume the same immutable plan
        within remaining payload-read authority, not a wire-egress limit.
        Accounted transfer includes application reads whose
        outcome was lost to interruption; such reservations are never refunded by
        guessing that bytes were not transferred.
        """
        with self._lock():
            row, plan = self._row(plan_digest)
            if row["approved_by"] != plan.owner or by != plan.owner:
                raise ModelError("approve the exact download plan before network or disk work")
            if row["status"] == "downloaded_verified":
                self.verified_artifacts(plan_digest)
                return self.status(plan_digest)
            self._transition(plan_digest, "downloading")
            try:
                for file in {item.digest: item for item in plan.files}.values():
                    self._recover_publication(plan, file)
                    self._download_file(plan, file, transport or HTTPSDownloadTransport(), cancel_event, credential_resolver)
                self._transition(plan_digest, "downloaded_verified")
            except (DownloadPaused, KeyboardInterrupt):
                self._transition(plan_digest, "paused", "cancelled_or_partial")
            except Exception as error:
                # Never put provider exceptions, redirect URLs or auth headers in
                # durable metadata. Code/class identifies the actionable boundary.
                self._transition(plan_digest, "failed", type(error).__name__)
                if isinstance(error, ModelError):
                    raise
                raise ModelError("download failed: " + type(error).__name__) from None
            return self.status(plan_digest)

    def _download_file(self, plan, file, transport, cancel, credential_resolver):
        self._assert_contained()
        if cancel and cancel.is_set():
            raise DownloadPaused("cancelled")
        if self._present("blobs", file.digest):
            digest, size = self._hash("blobs", file)
            if size != file.size_bytes or "sha256:" + digest.hexdigest() != file.digest:
                raise ModelError("existing model blob failed its pinned hash")
            if self._size("partial", file.digest, plan.digest()):
                os.unlink(self._content_name("partial", file.digest, plan.digest()), dir_fd=self._dirs["partial"])
                os.fsync(self._dirs["partial"])
            return
        offset = self._size("partial", file.digest, plan.digest())
        digest = hashlib.sha256()
        if offset:
            digest, offset = self._hash("partial", file, plan_digest=plan.digest())
        if offset == file.size_bytes:
            if "sha256:" + digest.hexdigest() != file.digest:
                raise ModelError("partial model reached its size with the wrong content hash")
            self._promote(plan, file)
            return
        remaining = sum(max(0, item.size_bytes - self._size("blobs", item.digest) - self._size("partial", item.digest, plan.digest()))
                        for item in {item.digest: item for item in plan.files}.values())
        if shutil.disk_usage(self.root).free < remaining:
            raise ModelError("insufficient observed disk capacity for the approved download")
        if sum(item.size_bytes for item in {item.digest: item for item in plan.files}.values()) > plan.max_disk_bytes:
            raise ModelError("download exceeds its disk ceiling")
        headers = {}
        if file.credential_ref:
            if credential_resolver is None:
                raise ModelError("download requires its declared credential resolver")
            headers = credential_resolver(file.credential_ref, _origin(file.url))
            if not isinstance(headers, dict):
                raise ModelError("credential resolver must return scoped authorization headers")
        response = transport.open(file, offset=offset, plan=plan, headers=headers)
        descriptor = None
        try:
            self._assert_contained()
            if response.source_origin not in plan.allowed_origins:
                raise ModelError("download response came from an unapproved origin")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ModelError("compressed transport cannot satisfy exact model byte offsets")
            length = response.headers.get("content-length")
            if length is None or not re.fullmatch(r"[0-9]+", length) or int(length) != file.size_bytes - offset:
                raise ModelError("model response must declare its exact remaining byte length")
            if response.status == 206:
                match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", response.headers.get("content-range", ""))
                if not match or tuple(map(int, match.groups())) != (offset, file.size_bytes - 1, file.size_bytes):
                    raise ModelError("partial response does not match the pinned offset and total")
            elif response.status != 200 or offset:
                raise ModelError("server did not honor the immutable resumable download range")
            descriptor = os.open(self._content_name("partial", file.digest, plan.digest()), os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600,
                                 dir_fd=self._dirs["partial"])
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size != offset:
                raise ModelError("partial model changed before its resumable write")
            os.lseek(descriptor, offset, os.SEEK_SET)
            while offset < file.size_bytes:
                self._assert_contained()
                if cancel and cancel.is_set():
                    raise DownloadPaused("cancelled")
                count = min(65536, file.size_bytes - offset)
                row, _ = self._row(plan.digest())
                if row["accounted_bytes"] + count > plan.max_transfer_bytes:
                    raise ModelError("download exhausted its approved transfer ceiling")
                with self.connection:
                    self.connection.execute("UPDATE plans SET accounted_bytes=accounted_bytes+? WHERE digest=?", (count, plan.digest()))
                chunk = response.read(count)
                if not isinstance(chunk, bytes) or len(chunk) > count:
                    raise ModelError("download transport violated its bounded byte contract")
                with self.connection:
                    self.connection.execute("UPDATE plans SET accounted_bytes=accounted_bytes-?,observed_bytes=observed_bytes+? WHERE digest=?",
                                            (count - len(chunk), len(chunk), plan.digest()))
                if not chunk:
                    raise DownloadPaused("source ended before its pinned length")
                # A cancellation arriving during read consumes those network
                # bytes, but does not race a background write after returning.
                written = 0
                while written < len(chunk):
                    written += os.write(descriptor, chunk[written:])
                os.fsync(descriptor)
                digest.update(chunk)
                offset += len(chunk)
            if "sha256:" + digest.hexdigest() != file.digest:
                raise ModelError("downloaded model bytes failed their pinned SHA-256")
        finally:
            response.close()
            if descriptor is not None:
                os.close(descriptor)
        self._promote(plan, file)
        with self.connection:
            self._event(plan.digest(), "MODEL_FILE_VERIFIED", {"path": file.path, "digest": file.digest,
                                                               "size_bytes": file.size_bytes, "source_origin": _origin(file.url)})

    def _promote(self, plan, file):
        self._assert_contained()
        # Never overwrite a user/other writer's destination. Under the store
        # lock this hard-link publication is atomic, then removes our partial.
        os.link(self._content_name("partial", file.digest, plan.digest()), self._name(file.digest), src_dir_fd=self._dirs["partial"],
                dst_dir_fd=self._dirs["blobs"], follow_symlinks=False)
        try:
            digest, size = self._hash("blobs", file, publication=True)
            if size != file.size_bytes or "sha256:" + digest.hexdigest() != file.digest:
                raise ModelError("published model changed before immutable verification")
        except BaseException:
            os.unlink(self._name(file.digest), dir_fd=self._dirs["blobs"])
            raise
        os.unlink(self._content_name("partial", file.digest, plan.digest()), dir_fd=self._dirs["partial"])
        os.fsync(self._dirs["blobs"])
        os.fsync(self._dirs["partial"])

    def _recover_publication(self, plan, file):
        """A crash between link and unlink must not strand a verified model."""
        try:
            source = os.stat(self._content_name("partial", file.digest, plan.digest()), dir_fd=self._dirs["partial"], follow_symlinks=False)
            target = os.stat(self._name(file.digest), dir_fd=self._dirs["blobs"], follow_symlinks=False)
        except FileNotFoundError:
            return
        if (stat.S_ISREG(source.st_mode) and stat.S_ISREG(target.st_mode)
                and source.st_nlink == target.st_nlink == 1
                and (source.st_dev, source.st_ino) != (target.st_dev, target.st_ino)):
            # Another approved plan already populated the shared verified cache.
            # The normal path rehashes that blob before removing our redundant partial.
            return
        if (not stat.S_ISREG(source.st_mode) or not stat.S_ISREG(target.st_mode)
                or (source.st_dev, source.st_ino) != (target.st_dev, target.st_ino)
                or source.st_nlink != 2 or target.st_nlink != 2):
            raise ModelError("conflicting model publication requires operator review")
        digest, size = self._hash("blobs", file, publication=True)
        if size != file.size_bytes or "sha256:" + digest.hexdigest() != file.digest:
            raise ModelError("interrupted model publication failed its pinned hash")
        os.unlink(self._content_name("partial", file.digest, plan.digest()), dir_fd=self._dirs["partial"])
        os.fsync(self._dirs["blobs"])
        os.fsync(self._dirs["partial"])

    def discard_partial(self, plan_digest: str, by: str):
        """Explicitly discard this plan's unverified bytes, never verified blobs.

        Transfer authority is not refunded. Downloading a replacement is another
        explicit call within the remaining original ceiling, or a new reviewed plan.
        """
        with self._lock(), self.connection:
            row, plan = self._row(plan_digest)
            if row["approved_by"] != plan.owner or by != plan.owner:
                raise ModelError("only the approving owner may discard partial downloads")
            discarded = 0
            for file in {item.digest: item for item in plan.files}.values():
                self._recover_publication(plan, file)
                size = self._size("partial", file.digest, plan_digest)
                if size:
                    os.unlink(self._content_name("partial", file.digest, plan_digest), dir_fd=self._dirs["partial"])
                    discarded += size
            os.fsync(self._dirs["partial"])
            self._event(plan_digest, "MODEL_PARTIAL_DISCARDED", {"approved_by": by, "discarded_bytes": discarded,
                                                                "recoverable": False, "verified_blobs_untouched": True})
            if row["status"] != "downloaded_verified":
                self.connection.execute("UPDATE plans SET status='paused',error=NULL WHERE digest=?", (plan_digest,))
        return dict(self.status(plan_digest), discarded_bytes=discarded)
