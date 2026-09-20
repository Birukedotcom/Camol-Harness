"""Bounded, unambiguous JSON at external approval and transport boundaries."""

import json
import math
import os
from pathlib import Path
import stat

from .schema import SchemaError


def decode_contract(raw, *, max_bytes=1 << 20):
    if type(max_bytes) is not int or not 1 <= max_bytes <= 64 << 20:
        raise SchemaError("contract byte ceiling must be between 1 and 64 MiB")
    if not isinstance(raw, (str, bytes, bytearray)):
        raise SchemaError("contract input must be JSON text")
    try:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        if len(encoded) > max_bytes:
            raise SchemaError("contract JSON exceeds its byte ceiling")
        text = encoded.decode("utf-8", "strict")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise SchemaError("contract JSON contains duplicate object keys")
                result[key] = value
            return result

        def constant(value):
            raise SchemaError("contract JSON cannot contain non-finite values")

        def real(value):
            result = float(value)
            if not math.isfinite(result):
                raise SchemaError("contract JSON cannot contain non-finite numbers")
            return result

        def integer(value):
            if len(value) > 128:
                raise SchemaError("contract integer exceeds its supported bound")
            return int(value)

        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant,
                          parse_float=real, parse_int=integer)
    except SchemaError:
        raise
    except (ValueError, UnicodeError, RecursionError) as error:
        # Never echo raw contract bytes, which may contain protected values.
        raise SchemaError("contract JSON is malformed") from error


def load_contract(path, *, max_bytes=1 << 20):
    """Read a regular descriptor, rejecting pipes/devices without blocking on them."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= 64 << 20:
        raise SchemaError("contract byte ceiling must be between 1 and 64 MiB")
    descriptor = os.open(str(Path(path)), os.O_RDONLY | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            raise SchemaError("contract file must be regular and within its byte ceiling")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return decode_contract(handle.read(max_bytes + 1), max_bytes=max_bytes)
    finally:
        os.close(descriptor)
