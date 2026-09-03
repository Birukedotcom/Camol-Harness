import hashlib
import json
import math
import unittest
from datetime import datetime, timezone

from camol.schema import (
    SchemaError,
    canonical_digest,
    canonical_json_bytes,
    normalize_timestamp,
    parse_timestamp,
    reject_unknown_fields,
    require_digest,
    require_schema_header,
)


class CanonicalJsonTests(unittest.TestCase):
    def test_key_order_does_not_change_the_digest(self):
        a = {"z": 1, "a": {"y": [1, 2], "b": "x"}}
        b = {"a": {"b": "x", "y": [1, 2]}, "z": 1}
        self.assertEqual(canonical_digest(a), canonical_digest(b))
        self.assertEqual(canonical_json_bytes(a), b'{"a":{"b":"x","y":[1,2]},"z":1}')

    def test_list_order_is_significant(self):
        self.assertNotEqual(canonical_digest([1, 2]), canonical_digest([2, 1]))

    def test_nested_values_and_unicode_are_encoded_deterministically(self):
        value = {"text": "camél ☕", "nested": [{"k": None, "flag": True}, 3, 4.5]}
        encoded = canonical_json_bytes(value)
        self.assertEqual(
            encoded,
            b'{"nested":[{"flag":true,"k":null},3,4.5],"text":"cam\\u00e9l \\u2615"}',
        )
        self.assertEqual(json.loads(encoded.decode("utf-8")), value)

    def test_digest_is_sha256_of_the_canonical_bytes(self):
        value = {"a": [1, {"b": 2}]}
        expected = "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()
        self.assertEqual(canonical_digest(value), expected)
        self.assertTrue(require_digest(expected, "digest"))

    def test_any_mutation_changes_the_digest(self):
        base = {"plan": {"tasks": [{"id": "a", "goal": "x"}], "max": 3}, "flag": False}
        baseline = canonical_digest(base)
        mutations = [
            {"plan": {"tasks": [{"id": "a", "goal": "y"}], "max": 3}, "flag": False},
            {"plan": {"tasks": [{"id": "a", "goal": "x"}], "max": 4}, "flag": False},
            {"plan": {"tasks": [{"id": "a", "goal": "x"}], "max": 3}, "flag": True},
            {"plan": {"tasks": [{"id": "a", "goal": "x"}], "max": 3}},
            {"plan": {"tasks": [], "max": 3}, "flag": False},
            {"plan": {"tasks": [{"id": "a", "goal": "x"}], "max": 3}, "flag": False, "extra": None},
        ]
        digests = {canonical_digest(mutation) for mutation in mutations}
        self.assertNotIn(baseline, digests)
        self.assertEqual(len(digests), len(mutations))

    def test_int_and_float_with_equal_value_are_not_conflated(self):
        self.assertNotEqual(canonical_digest({"n": 1}), canonical_digest({"n": 1.0}))
        self.assertNotEqual(canonical_digest({"n": True}), canonical_digest({"n": 1}))

    def test_unsupported_values_are_rejected(self):
        bad = [
            {"nan": math.nan},
            {"inf": math.inf},
            {"neg_inf": -math.inf},
            {1: "non-string key"},
            {"set": {1, 2}},
            {"frozenset": frozenset({1})},
            {"bytes": b"x"},
            {"dt": datetime.now(timezone.utc)},
            {"obj": object()},
            {"nested": [{"deep": {"x": float("nan")}}]},
        ]
        for value in bad:
            with self.assertRaises(SchemaError, msg=repr(value)):
                canonical_json_bytes(value)

    def test_error_names_the_offending_path(self):
        with self.assertRaisesRegex(SchemaError, r"\$\.outer\[1\]\.inner"):
            canonical_json_bytes({"outer": [0, {"inner": {1, 2}}]})

    def test_tuples_canonicalize_as_json_arrays(self):
        # Tuples are the immutable internal form of contract collections.
        self.assertEqual(canonical_json_bytes({"t": (1, "a", (2,))}), b'{"t":[1,"a",[2]]}')
        self.assertEqual(canonical_digest({"t": (1, 2)}), canonical_digest({"t": [1, 2]}))

    def test_cyclic_structures_are_rejected_deterministically(self):
        cyclic_list = [1]
        cyclic_list.append(cyclic_list)
        with self.assertRaisesRegex(SchemaError, r"\$\[1\]: cyclic structure"):
            canonical_json_bytes(cyclic_list)
        cyclic_dict = {"a": {}}
        cyclic_dict["a"]["back"] = cyclic_dict
        with self.assertRaisesRegex(SchemaError, r"\$\.a\.back: cyclic structure"):
            canonical_json_bytes(cyclic_dict)
        # The same object appearing twice on different branches is not a cycle.
        shared = [1, 2]
        self.assertEqual(canonical_json_bytes({"x": shared, "y": shared}), b'{"x":[1,2],"y":[1,2]}')

    def test_input_is_not_mutated(self):
        value = {"b": [3, 1], "a": {"y": 1, "x": 2}}
        snapshot = json.loads(json.dumps(value))
        canonical_digest(value)
        self.assertEqual(value, snapshot)


class TimestampTests(unittest.TestCase):
    def test_timestamps_normalize_to_utc_microseconds(self):
        self.assertEqual(normalize_timestamp("2026-09-03T10:00:00Z", "t"), "2026-09-03T10:00:00.000000+00:00")
        self.assertEqual(
            normalize_timestamp("2026-09-03T12:00:00+02:00", "t"), "2026-09-03T10:00:00.000000+00:00"
        )
        self.assertEqual(
            normalize_timestamp("2026-09-03T10:00:00.5+00:00", "t"), "2026-09-03T10:00:00.500000+00:00"
        )

    def test_sub_microsecond_precision_is_rejected_rather_than_collided(self):
        with self.assertRaisesRegex(SchemaError, "sub-microsecond precision \\(7 digits\\)"):
            normalize_timestamp("2026-09-03T10:00:00.1234567+00:00", "t")
        with self.assertRaisesRegex(SchemaError, "sub-microsecond precision \\(9 digits\\)"):
            normalize_timestamp("2026-09-03T10:00:00.123456789Z", "t")
        # Two instants that differ only past the sixth digit must not normalize equal;
        # since neither is representable, both are rejected.
        for value in ("2026-09-03T10:00:00.1234561+00:00", "2026-09-03T10:00:00.1234562+00:00"):
            with self.assertRaises(SchemaError):
                normalize_timestamp(value, "t")
        self.assertEqual(normalize_timestamp("2026-09-03T10:00:00.123456+00:00", "t"), "2026-09-03T10:00:00.123456+00:00")

    def test_naive_or_malformed_timestamps_are_rejected(self):
        for value in ["2026-09-03T10:00:00", "not-a-time", "", None, 12345]:
            with self.assertRaises(SchemaError, msg=repr(value)):
                parse_timestamp(value, "t")


class HeaderTests(unittest.TestCase):
    def test_unknown_schema_version_is_rejected_clearly(self):
        with self.assertRaisesRegex(SchemaError, "schema_version 7 is not supported"):
            require_schema_header({"schema": "x", "schema_version": 7}, "x", 1, "x")
        with self.assertRaisesRegex(SchemaError, "schema_version must be an integer"):
            require_schema_header({"schema": "x", "schema_version": "1"}, "x", 1, "x")
        with self.assertRaisesRegex(SchemaError, "schema must be 'x'"):
            require_schema_header({"schema": "y", "schema_version": 1}, "x", 1, "x")

    def test_unknown_fields_are_rejected_clearly(self):
        with self.assertRaisesRegex(SchemaError, "unknown fields: extra, more"):
            reject_unknown_fields({"a": 1, "extra": 2, "more": 3}, ("a",), "thing")


if __name__ == "__main__":
    unittest.main()
