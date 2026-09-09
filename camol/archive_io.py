"""Descriptor-anchored I/O for portable archives, not restore authority."""

import os
from pathlib import Path
import stat


class ArchiveIOError(ValueError):
    pass


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class ArchiveRoot:
    """Never follow a link below the explicitly selected root.

    Resolve the caller's parent path once (including macOS /tmp and /var
    aliases), but never resolve the final root or an archive member. Reads
    reject special files and hard links before consuming any bytes. Named
    identities are checked again before successful completion.
    """

    def __init__(self, path, *, create=False):
        selected = Path(path).absolute()
        if create:
            selected.parent.mkdir(parents=True, exist_ok=True)
        self.path = selected.parent.resolve(strict=True) / selected.name
        if create:
            try:
                self.path.mkdir(mode=0o700)
            except FileExistsError:
                pass
        self.fd = os.open(str(self.path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.identity = os.fstat(self.fd)
        self.directories = {}
        self.files = {}
        try:
            if create:
                if self.identity.st_uid != os.getuid() or self.identity.st_mode & 0o077:
                    raise ArchiveIOError("archive destination must be a private owner directory (0700)")
                with os.scandir(self.fd) as entries:
                    if next(entries, None) is not None:
                        raise ArchiveIOError("archive destination must be empty")
        except BaseException:
            os.close(self.fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                self.verify()
        finally:
            os.close(self.fd)

    def _directory(self, parts, *, create=False):
        descriptor = os.dup(self.fd)
        try:
            for index, part in enumerate(parts):
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                info = os.fstat(child)
                identity = (info.st_dev, info.st_ino)
                key = tuple(parts[:index + 1])
                if key in self.directories and self.directories[key] != identity:
                    raise ArchiveIOError("archive directory changed during access")
                self.directories[key] = identity
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _parts(relative):
        # Do not let Path normalize aliases such as a//b or a/./b first.
        if not isinstance(relative, str) or not relative or "\\" in relative or "\0" in relative:
            raise ArchiveIOError("archive member path is invalid")
        parts = relative.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ArchiveIOError("archive member escaped its explicit root")
        return parts

    def _link_count_allowed(self, count):
        # Archive/recovery members are always single-link. The read-only source
        # inventory has a separate explicit policy, never an archive CLI flag.
        return count == 1

    def read(self, relative, maximum):
        parts = self._parts(relative)
        parent = self._directory(parts[:-1])
        descriptor = None
        try:
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or not self._link_count_allowed(before.st_nlink):
                raise ArchiveIOError("archive member must be a single-link regular file")
            if before.st_size > maximum:
                raise ArchiveIOError("archive member exceeds its byte ceiling")
            chunks, size = [], 0
            while True:
                chunk = os.read(descriptor, min(65536, maximum - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    raise ArchiveIOError("archive member grew beyond its byte ceiling")
                chunks.append(chunk)
            identity = _identity(before)
            if identity != _identity(os.fstat(descriptor)) or identity != _identity(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) or size != before.st_size:
                raise ArchiveIOError("archive member changed during access")
            if relative in self.files and self.files[relative] != identity:
                raise ArchiveIOError("archive member changed between reads")
            self.files[relative] = identity
            return b"".join(chunks)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)

    def write(self, relative, content, *, executable=False):
        parts = self._parts(relative)
        parent = self._directory(parts[:-1], create=True)
        try:
            descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                if executable:
                    os.fchmod(handle.fileno(), 0o700)
                os.fsync(handle.fileno())
                self.files[relative] = _identity(os.fstat(handle.fileno()))
            os.fsync(parent)
        finally:
            os.close(parent)

    def symlink(self, relative, target):
        """Preserve a link as data, never traverse it to write another member."""
        parts = self._parts(relative)
        parent = self._directory(parts[:-1], create=True)
        try:
            os.symlink(target, parts[-1], dir_fd=parent)
            self.files[relative] = _identity(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False))
            os.fsync(parent)
        finally:
            os.close(parent)

    def verify(self):
        named = os.stat(str(self.path), follow_symlinks=False)
        if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (self.identity.st_dev, self.identity.st_ino):
            raise ArchiveIOError("archive root changed during access")
        for relative, identity in self.files.items():
            parts = self._parts(relative)
            parent = self._directory(parts[:-1])
            try:
                if _identity(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) != identity:
                    raise ArchiveIOError("archive member changed before completion")
            finally:
                os.close(parent)
