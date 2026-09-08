import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from camol.cli import main
from camol.schema import canonical_digest
from camol.target_inventory import FORMATS, MAX_BYTES, MAX_RECORDS, capabilities, normalize_inventory
from camol.targets import TargetError
from tests import test_targets as target_fixture


def instance(identifier="123", name="build-vm", zone="us-central1-a"):
    base = "https://www.googleapis.com/compute/v1/projects/project-1/zones/" + zone
    return dict(kind="compute#instance", id=identifier, name=name, zone=base,
                selfLink=base + "/instances/" + name, status="RUNNING")


def normalize(value, **kwargs):
    return normalize_inventory(value, format=kwargs.pop("format", FORMATS[1]),
                               account="account-1", project="project-1", **kwargs)


class InventoryTests(unittest.TestCase):
    def test_array_and_aggregated_formats_project_same_identity(self):
        item = instance()
        before = copy.deepcopy(item)
        first = normalize([item])
        second = normalize(dict(kind="compute#instanceAggregatedList", items={"zones/us-central1-a": {"instances": [item]}}), format=FORMATS[0])
        self.assertEqual(first["records"], second["records"])
        self.assertEqual(item, before)
        provider = first["records"][0]["provider"]
        self.assertEqual(provider, dict(kind="gcp", account="account-1", project="project-1", location="us-central1-a", resource_id="123", resource_name="build-vm"))
        self.assertFalse(first["records"][0]["execution_authority"])
        self.assertFalse(first["coverage"]["authenticated"])
        self.assertFalse(first["coverage"]["complete_inventory"])
        self.assertEqual(first["coverage"]["freshness"], "unknown")
        self.assertEqual(first["digest"], canonical_digest({k: v for k, v in first.items() if k != "digest"}))

    def test_optional_drift_cannot_break_identity_or_leak_provider_bodies(self):
        item = instance()
        marker = "private-provider-body-must-not-appear"
        item.update(metadata={"items": [{"key": "startup-script", "value": marker}]},
                    futureField={marker: marker}, networkInterfaces=[{"accessConfigs": [{"natIP": marker}]}],
                    status={"renamed": marker}, serviceAccounts=[{"email": marker}])
        report = normalize([item])
        self.assertEqual(len(report["records"]), 1)
        self.assertIsNone(report["records"][0]["reported_status"])
        self.assertEqual(report["records"][0]["status_coverage"], "unrecognized")
        self.assertNotIn(marker, json.dumps(report))
        item.pop("status")
        self.assertEqual(normalize([item])["records"][0]["status_coverage"], "missing")

    def test_bad_identity_quarantines_only_affected_row(self):
        for change in ({"id": None}, {"id": True}, {"id": "01"}, {"id": str(2**64)}, {"id": 1.5},
                       {"name": "../../escape"}, {"zone": "https://["}, {"zone": "https://evil.invalid/projects/project-1/zones/us-central1-a"},
                       {"zone": "projects/other-project/zones/us-central1-a"},
                       {"selfLink": "projects/project-1/zones/us-central1-a/instances/other"},
                       {"zone": "https://user:password@www.googleapis.com/compute/v1/projects/project-1/zones/us-central1-a"},
                       {"kind": "compute#futureInstance"}):
            with self.subTest(change=change):
                bad = dict(instance(), **change)
                report = normalize([bad, instance("456", "good-vm")])
                self.assertEqual(len(report["records"]), 1)
                self.assertEqual(report["records"][0]["provider"]["resource_id"], "456")
                self.assertEqual(len(report["issues"]), 1)
                self.assertEqual(report["issues"][0]["record_index"], 0)

    def test_unknown_required_identity_is_not_inferred_from_name(self):
        item = instance()
        item["futureInstanceId"] = item.pop("id")
        report = normalize([item])
        self.assertEqual(report["records"], [])
        self.assertEqual(report["issues"][0]["reason"], "missing_or_invalid_resource_id")

    def test_all_duplicates_are_quarantined_without_first_wins(self):
        for other in (instance(), instance(name="other-name"), instance("456")):
            with self.subTest(other=other):
                report = normalize([instance(), other, instance("789", "safe")])
                self.assertEqual(len(report["records"]), 1)
                self.assertEqual(report["records"][0]["provider"]["resource_name"], "safe")
                self.assertEqual([v["reason"] for v in report["issues"]], ["ambiguous_resource_identity"] * 2)
        # A name in another zone is a different resource, not a global pane label.
        self.assertEqual(len(normalize([instance(), instance("456", zone="us-west1-a")])["records"]), 2)

    def test_report_marks_partial_pages_and_scopes_without_leaking_tokens(self):
        marker = "private-pagination-or-warning"
        value = dict(kind="compute#instanceAggregatedList", nextPageToken=marker,
            unreachables=[marker], warning={"message": marker}, items={
                "zones/us-central1-a": {"instances": [instance()], "warning": {"message": marker}},
                "zones/us-west1-a": {"instances": "schema-drift"}, "regions/unknown": {}})
        report = normalize(value, format=FORMATS[0])
        self.assertTrue(report["coverage"]["more_pages"])
        self.assertEqual(report["coverage"]["warning_count"], 2)
        self.assertEqual(report["coverage"]["unreachable_count"], 1)
        self.assertEqual(len(report["issues"]), 2)
        self.assertNotIn(marker, json.dumps(report))
        wrong = dict(kind="compute#instanceAggregatedList", items={"zones/us-west1-a": {"instances": [instance()]}})
        self.assertEqual(normalize(wrong, format=FORMATS[0])["issues"][0]["reason"], "aggregated_scope_conflict")

    def test_empty_inventory_and_known_uint64_preserve_unknown_completeness(self):
        self.assertEqual(normalize([])["records"], [])
        empty = normalize(dict(kind="compute#instanceAggregatedList"), format=FORMATS[0])
        self.assertFalse(empty["coverage"]["complete_inventory"])
        item = instance(2**64 - 1)
        item.pop("selfLink")
        item["zone"] = "us-central1-a"
        self.assertEqual(normalize([item])["records"][0]["provider"]["resource_id"], str(2**64 - 1))

    def test_explicit_decoder_contract_and_bounded_input(self):
        self.assertEqual(capabilities()["formats"], list(FORMATS))
        for data, decoder in (({}, FORMATS[1]), ([], FORMATS[0]), ({"items": {}}, FORMATS[0]), ([], "future-v2")):
            with self.subTest(decoder=decoder), self.assertRaises(TargetError):
                normalize(data, format=decoder)
        with self.assertRaises(TargetError):
            normalize([None] * (MAX_RECORDS + 1))
        with self.assertRaises(TargetError):
            normalize([dict(instance(), metadata="x" * MAX_BYTES)])
        with self.assertRaises(TargetError):
            normalize([dict(instance(), future=float("nan"))])

    def test_cli_offline_partial_status_and_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "inventory.json"
            path.write_text(json.dumps([instance(), {"name": "bad"}]))
            args = ["target", "inventory", "--input", str(path), "--format", FORMATS[1], "--account", "account-1", "--project", "project-1"]
            output = io.StringIO()
            with patch("subprocess.Popen", side_effect=AssertionError("offline decoder launched a process")), contextlib.redirect_stdout(output):
                self.assertEqual(main(args), 2)
            self.assertEqual(len(json.loads(output.getvalue())["records"]), 1)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["inventory.json"])
            path.write_text('[{"id":"123","id":"456"}]')
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(main(args), 0)

    def test_real_child_cli_no_provider_process_or_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "inventory.json"
            path.write_text(json.dumps([instance()]))
            env = dict(os.environ, PATH=str(root))
            completed = subprocess.run([sys.executable, "-m", "camol", "target", "inventory", "--input", str(path),
                "--format", FORMATS[1], "--account", "account-1", "--project", "project-1"], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(len(json.loads(completed.stdout)["records"]), 1)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["inventory.json"])

    def test_cli_malformed_scope_and_pipe_are_bounded_non_traceback_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.json"
            path.write_text("[]")
            args = ["target", "inventory", "--input", str(path), "--format", FORMATS[1], "--account", "account-1", "--project", "bad/scope"]
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(main(args), 2)
            self.assertNotIn("Traceback", errors.getvalue())
            self.assertNotIn("bad/scope", errors.getvalue())
            fifo = Path(directory) / "pipe"
            os.mkfifo(fifo)
            args[args.index("--input") + 1] = str(fifo)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 2)

    def test_scope_and_optional_shapes_cannot_leak_or_change_identity(self):
        for changes in ({"nextPageToken": []}, {"unreachables": {}}, {"items": []}):
            with self.subTest(changes=changes), self.assertRaises(TargetError):
                normalize(dict(kind="compute#instanceAggregatedList", **changes), format=FORMATS[0])
        for name in ("ends-with-", "1numeric", "x" * 64, "private\ncontrol"):
            with self.subTest(name=name):
                self.assertEqual(normalize([instance(name=name)])["records"], [])
        with patch.dict(os.environ, {"CAMOL_ACCESS_TOKEN": "private-instance-name"}):
            report = normalize([instance(name="private-instance-name")])
            self.assertEqual(report["records"], [])
            self.assertNotIn("private-instance-name", json.dumps(report))
        item = instance()
        item["zone"] += "\n"
        self.assertEqual(normalize([item])["records"], [])


class InventoryAdoptionTests(unittest.TestCase):
    def test_decoded_identity_enters_existing_exact_human_adoption_flow(self):
        fixture = target_fixture.TargetTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        report = normalize([instance()])
        fixture.target["provider"] = report["records"][0]["provider"]
        proposed = fixture.proposal()
        self.assertEqual(fixture.registry.inspect()["count"], 0)
        with self.assertRaises(TargetError):
            fixture.registry.adopt(proposed, by="strategist", approval_digest=proposed["digest"])
        adopted = fixture.adopt(proposed)
        self.assertEqual(adopted["proposal"]["descriptor"]["provider"], report["records"][0]["provider"])
        self.assertFalse(adopted["execution_authority"])
        self.assertEqual(adopted["readiness"], "unproven")
