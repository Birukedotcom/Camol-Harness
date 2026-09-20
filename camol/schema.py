"""Canonical JSON hashing and the shared validation toolkit for versioned contracts.

Every durable Camol contract (plans, probe results, readiness receipts, workspace
receipts, reservations, grants, lease fences) is hashed through
:func:`canonical_digest`. The canonical form is deterministic: keys are sorted,
separators carry no whitespace, non-ASCII text is escaped, and the bytes are UTF-8.
Values that JSON cannot represent unambiguously (NaN, infinities, non-string keys,
sets, bytes, datetimes, arbitrary objects, cyclic containers) are rejected instead
of being coerced, so two producers can never disagree about what a digest covers.
Tuples are accepted and encoded as JSON arrays because contracts store their
collections as tuples for deep immutability.

The byte layout intentionally matches the pre-M0 ``runbook_digest`` formula so
existing frozen plan digests do not change.
"""

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence

__all__ = [
    "SchemaError",
    "canonical_json_bytes",
    "canonical_digest",
    "DIGEST_PATTERN",
    "ID_PATTERN",
    "require_object",
    "require_identifier",
    "require_string",
    "require_optional_string",
    "require_digest",
    "require_optional_digest",
    "require_non_negative_int",
    "require_positive_int",
    "require_bool",
    "require_string_list",
    "require_choice",
    "require_timestamp",
    "require_optional_timestamp",
    "reject_unknown_fields",
    "require_schema_header",
    "normalize_timestamp",
    "parse_timestamp",
]


class SchemaError(ValueError):
    """A contract payload is malformed, unsupported, or ambiguous."""


DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_FRACTION = re.compile(r"\.(\d+)(?=[+-]\d{2}:\d{2}$)")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$")


# --------------------------------------------------------------------------- hashing


def _normalize(value: Any, path: str, active: Optional[FrozenSet[int]] = None) -> Any:
    """Return a JSON-safe copy of ``value`` or raise ``SchemaError``.

    ``bool`` is checked before ``int`` because ``bool`` subclasses ``int``.
    Floats are accepted only when finite and are re-emitted as floats, never
    silently converted to integers; ``1`` and ``1.0`` therefore hash differently,
    which is the ambiguity-free choice. ``active`` holds the ids of containers on
    the current descent path so a cyclic structure is rejected deterministically
    instead of overflowing the stack.
    """
    if value is None or isinstance(value, bool) or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise SchemaError("{}: non-finite float cannot be canonicalized".format(path))
        return value
    if isinstance(value, (dict, list, tuple)):
        if active is None:
            active = frozenset()
        if id(value) in active:
            raise SchemaError("{}: cyclic structure cannot be canonicalized".format(path))
        inner = active | {id(value)}
        if isinstance(value, dict):
            normalized: Dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise SchemaError("{}: object keys must be strings, not {}".format(path, type(key).__name__))
                normalized[key] = _normalize(item, "{}.{}".format(path, key), inner)
            return normalized
        if isinstance(value, tuple):
            # Tuples are the immutable internal form of contract collections and
            # canonicalize as JSON arrays. Bare tuples from callers are accepted
            # for the same reason; sets remain rejected because they are unordered.
            return [_normalize(item, "{}[{}]".format(path, index), inner) for index, item in enumerate(value)]
        return [_normalize(item, "{}[{}]".format(path, index), inner) for index, item in enumerate(value)]
    raise SchemaError(
        "{}: unsupported value type {} (only null, bool, int, finite float, str, list, tuple, dict)".format(
            path, type(value).__name__
        )
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize ``value`` to canonical UTF-8 JSON bytes."""
    normalized = _normalize(value, "$")
    text = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return text.encode("utf-8")


def canonical_digest(value: Any) -> str:
    """Return ``sha256:<hex>`` of the canonical JSON encoding of ``value``."""
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


# --------------------------------------------------------------------------- timestamps


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError("{} must be an ISO-8601 timestamp string".format(label))
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python 3.9's fromisoformat accepts only 3- or 6-digit fractions. Pad
    # shorter fractions to microseconds; reject longer ones outright so two
    # distinct sub-microsecond instants can never normalize to one value.
    fraction = _FRACTION.search(text)
    if fraction is not None and len(fraction.group(1)) > 6:
        raise SchemaError(
            "{} carries sub-microsecond precision ({} digits); at most 6 fractional digits are supported".format(
                label, len(fraction.group(1))
            )
        )
    text = _FRACTION.sub(lambda match: "." + match.group(1).ljust(6, "0"), text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise SchemaError("{} is not a valid ISO-8601 timestamp: {!r}".format(label, value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchemaError("{} must carry an explicit UTC offset".format(label))
    return parsed.astimezone(timezone.utc)


def normalize_timestamp(value: Any, label: str) -> str:
    """Return the timestamp as ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00`` in UTC."""
    parsed = parse_timestamp(value, label)
    return parsed.replace(microsecond=parsed.microsecond).isoformat(timespec="microseconds")


# --------------------------------------------------------------------------- field helpers


def require_object(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError("{} must be an object".format(label))
    return value


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError("{} must be a non-empty string".format(label))
    return value


def require_optional_string(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    return require_string(value, label)


def require_identifier(value: Any, label: str) -> str:
    text = require_string(value, label)
    if not ID_PATTERN.fullmatch(text):
        raise SchemaError(
            "{} must contain only letters, numbers, dot, underscore, colon, slash, at, or dash".format(label)
        )
    return text


def require_digest(value: Any, label: str) -> str:
    text = require_string(value, label)
    if not DIGEST_PATTERN.fullmatch(text):
        raise SchemaError("{} must look like sha256:<64 hex chars>".format(label))
    return text


def require_optional_digest(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    return require_digest(value, label)


def require_non_negative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SchemaError("{} must be a non-negative integer".format(label))
    return value


def require_positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SchemaError("{} must be a positive integer".format(label))
    return value


def require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaError("{} must be a boolean".format(label))
    return value


def require_string_list(value: Any, label: str, *, allow_empty: bool = True, sort: bool = False) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise SchemaError("{} must be an array of non-empty strings".format(label))
    if not allow_empty and not value:
        raise SchemaError("{} must not be empty".format(label))
    if len(set(value)) != len(value):
        raise SchemaError("{} must not contain duplicates".format(label))
    return sorted(value) if sort else list(value)


def require_choice(value: Any, label: str, choices: Iterable[str]) -> str:
    options = sorted(choices)
    if not isinstance(value, str) or value not in options:
        raise SchemaError("{} must be one of: {}".format(label, ", ".join(options)))
    return value


def require_timestamp(value: Any, label: str) -> str:
    return normalize_timestamp(value, label)


def require_optional_timestamp(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    return normalize_timestamp(value, label)


def reject_unknown_fields(payload: Mapping[str, Any], allowed: Sequence[str], label: str) -> None:
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise SchemaError("{} has unknown fields: {}".format(label, ", ".join(unknown)))


def require_schema_header(payload: Mapping[str, Any], schema: str, version: int, label: str) -> None:
    """Reject payloads whose ``schema``/``schema_version`` do not match exactly."""
    found_schema = payload.get("schema")
    if found_schema != schema:
        raise SchemaError("{} schema must be {!r}, found {!r}".format(label, schema, found_schema))
    found_version = payload.get("schema_version")
    if not isinstance(found_version, int) or isinstance(found_version, bool):
        raise SchemaError("{} schema_version must be an integer".format(label))
    if found_version != version:
        raise SchemaError(
            "{} schema_version {} is not supported (expected {})".format(label, found_version, version)
        )
