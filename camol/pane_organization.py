"""Owner-private view preferences, separate from execution and plan authority."""

import json
import os
from pathlib import Path
import stat
import tempfile

from .json_contracts import decode_contract
from .schema import require_digest, require_identifier


MAX_BYTES = 65536
MAX_BOXES = 1000


class PaneOrganizationError(ValueError):
    pass


def empty(scope):
    return dict(schema="camol.pane_organization", schema_version=1, scope=scope, pins=[], groups={})


def validate(value):
    if not isinstance(value, dict) or set(value) != {"schema", "schema_version", "scope", "pins", "groups"}:
        raise PaneOrganizationError("pane preferences require exact versioned fields")
    if value["schema"] != "camol.pane_organization" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PaneOrganizationError("unsupported pane preference schema")
    require_digest(value["scope"], "pane scope")
    pins, groups = value["pins"], value["groups"]
    if not isinstance(pins, list) or len(pins) > MAX_BOXES or not isinstance(groups, dict) or len(groups) > MAX_BOXES:
        raise PaneOrganizationError("pane preferences support at most 1000 pins and groups")
    for box in [*pins, *groups]:
        require_identifier(box, "exact box id")
    if len(set(pins)) != len(pins):
        raise PaneOrganizationError("duplicate pinned box")
    for label in groups.values():
        if not isinstance(label, str) or not label.strip() or label != label.strip() or len(label) > 64 or not label.isprintable():
            raise PaneOrganizationError("group name must be 1..64 printable characters without surrounding whitespace")
    return dict(value, pins=list(pins), groups=dict(groups))


def load(path, scope):
    """Missing/another-scope preferences are empty; malformed records are errors."""
    descriptor = None
    try:
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PaneOrganizationError("pane preferences must be an owner-private single-link regular file")
        if info.st_size > MAX_BYTES:
            raise PaneOrganizationError("pane preferences exceed 64 KiB")
        data = bytearray()
        while len(data) <= MAX_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise PaneOrganizationError("pane preferences grew beyond 64 KiB")
        after = os.fstat(descriptor)
        named = os.stat(path, follow_symlinks=False)
        for attribute in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
            if getattr(info, attribute) != getattr(after, attribute) or getattr(info, attribute) != getattr(named, attribute):
                raise PaneOrganizationError("pane preferences changed during inspection")
        value = validate(decode_contract(bytes(data), max_bytes=MAX_BYTES))
        return value if value["scope"] == scope else empty(scope)
    except FileNotFoundError:
        # A disappearing opened record is not equivalent to an initially absent one.
        if descriptor is not None or Path(path).is_symlink():
            raise PaneOrganizationError("pane preference record disappeared or is linked")
        return empty(scope)
    except (OSError, ValueError, TypeError) as error:
        raise PaneOrganizationError("pane preferences are unavailable or invalid: " + type(error).__name__) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def save(path, value):
    """Caller holds the project's SessionStore.transaction across read/edit/save."""
    path = Path(path)
    value = validate(value)
    data = (json.dumps(value, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
    if len(data) > MAX_BYTES:
        raise PaneOrganizationError("pane preferences exceed 64 KiB")
    # Refuse replacing linked/unsafe records, even for this display-only state.
    load(path, value["scope"])
    descriptor, temporary = tempfile.mkstemp(prefix="pane-organization.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return value


def update(value, box_id, *, pinned=None, group=None, clear_group=False):
    value = validate(value)
    require_identifier(box_id, "exact box id")
    if pinned is not None:
        if type(pinned) is not bool:
            raise PaneOrganizationError("pin setting must be boolean")
        if pinned and box_id not in value["pins"]:
            value["pins"].append(box_id)
        elif not pinned and box_id in value["pins"]:
            value["pins"].remove(box_id)
    if clear_group:
        value["groups"].pop(box_id, None)
    elif group is not None:
        value["groups"][box_id] = group
    return validate(value)
