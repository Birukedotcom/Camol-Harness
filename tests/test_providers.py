import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from camol.providers import (
    ModelProfile,
    ProviderCapabilityReceipt,
    ProviderError,
    capability_path,
    create_claude_capability,
    load_model_profile,
    read_capability,
)
from camol.probes import CommandOutcome, ProbeContext, ProviderConnectionProbe, Redactor
from camol.runbook import migrate_runbook_v1_to_v2, migrate_runbook_v2_to_v3


def profile_payload(**updates):
    payload = {
        "schema": "camol.model_profile",
        "schema_version": 1,
        "profile_id": "test-fable",
        "provider": "anthropic",
        "adapter_kind": "claude_cli",
        "runtime_binary": "fake-claude",
        "requested_model": "fable",
        "allowed_resolved_models": ["claude-fable-5"],
        "effort": "high",
        "permission_mode": "dontAsk",
        "allowed_tools": ["Read", "Edit"],
        "max_agent_turns": 4,
        "max_turn_tokens": 1000,
        "max_turn_usd_cents": 10,
        "max_task_usd_cents": 20,
        "max_run_usd_cents": 30,
        "capability_ttl_seconds": 60,
        "network_destinations": ["*"],
        "credential_refs": ["existing-login"],
        "credential_read_paths": ["{home}/.claude"],
        "maturity": "SPECULATIVE",
    }
    payload.update(updates)
    return payload


class Completed:
    returncode = 0
    stderr = b""
    stdout = json.dumps({
        "type": "result",
        "result": "CAMOL_READY",
        "total_cost_usd": 0.002,
        "usage": {"input_tokens": 4, "output_tokens": 1},
        "modelUsage": {"claude-fable-5": {}},
    }).encode()


class ProviderContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "repo"
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.workspace.mkdir()
        self.bin.mkdir()
        executable = self.bin / "fake-claude"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        self.profile_file = self.workspace / "profile.yaml"
        self.profile_file.write_text(json.dumps(profile_payload()), encoding="utf-8")
        self.profile = load_model_profile(self.workspace, "profile.yaml")
        self.now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    def test_profile_is_strict_digestible_and_never_claims_availability(self):
        self.assertEqual(self.profile.requested_model, "fable")
        self.assertEqual(self.profile.allowed_resolved_models, ("claude-fable-5",))
        self.assertTrue(self.profile.digest().startswith("sha256:"))
        unknown = dict(profile_payload(), entitlement=True)
        with self.assertRaisesRegex(Exception, "unknown fields"):
            ModelProfile.from_dict(unknown)
        bad = profile_payload(max_turn_usd_cents=31)
        with self.assertRaisesRegex(ProviderError, "turn <= task <= run"):
            ModelProfile.from_dict(bad)

    def test_packaged_profile_is_available_from_any_workspace(self):
        profile = load_model_profile(self.workspace, "@camol/claude-fable-5-1")
        self.assertEqual(profile.profile_id, "claude-fable-5-1-request")
        self.assertEqual(profile.requested_model, "fable")
        with self.assertRaisesRegex(ProviderError, "unavailable"):
            load_model_profile(self.workspace, "@camol/missing")

    def test_profile_path_may_not_escape_or_traverse_symlink(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(profile_payload()), encoding="utf-8")
        (self.workspace / "linked.yaml").symlink_to(outside)
        for name in ("../outside.json", "linked.yaml"):
            with self.assertRaises(ProviderError):
                load_model_profile(self.workspace, name)

    def test_preflight_requires_opt_in_and_writes_fresh_bound_receipt(self):
        with self.assertRaisesRegex(ProviderError, "explicit --accept-spend"):
            create_claude_capability(
                self.profile, target_id="local:test", state_dir=self.state,
                cwd=self.workspace, accept_spend=False, now=self.now,
            )
        with patch.dict(os.environ, {"PATH": str(self.bin)}):
            receipt = create_claude_capability(
                self.profile, target_id="local:test", state_dir=self.state,
                cwd=self.workspace, accept_spend=True, now=self.now,
                runner=lambda *args, **kwargs: Completed(),
            )
        self.assertEqual(receipt.resolved_model, "claude-fable-5")
        self.assertEqual(receipt.cost_usd_micros, 2000)
        self.assertTrue(capability_path(self.state, self.profile.profile_id).is_file())
        recovered = read_capability(self.state, self.profile)
        self.assertEqual(recovered.digest(), receipt.digest())
        self.assertTrue(receipt.valid_for(self.profile, "local:test", self.now)[0])
        self.assertFalse(receipt.valid_for(self.profile, "local:other", self.now)[0])
        self.assertFalse(receipt.valid_for(self.profile, "local:test", self.now + timedelta(seconds=60))[0])

    def test_resolution_is_not_invented_or_relabeled(self):
        class Wrong(Completed):
            stdout = json.dumps({
                "type": "result", "result": "CAMOL_READY", "total_cost_usd": 0,
                "usage": {}, "modelUsage": {"claude-opus-5": {}},
            }).encode()
        with patch.dict(os.environ, {"PATH": str(self.bin)}):
            with self.assertRaisesRegex(ProviderError, "outside the profile allowlist"):
                create_claude_capability(
                    self.profile, target_id="local:test", state_dir=self.state,
                    cwd=self.workspace, accept_spend=True, now=self.now,
                    runner=lambda *args, **kwargs: Wrong(),
                )
        class UnknownCost(Completed):
            stdout = json.dumps({
                "type": "result", "result": "CAMOL_READY", "total_cost_usd": "unknown",
                "usage": {}, "modelUsage": {"claude-fable-5": {}},
            }).encode()
        with patch.dict(os.environ, {"PATH": str(self.bin)}):
            with self.assertRaisesRegex(ProviderError, "total_cost_usd"):
                create_claude_capability(
                    self.profile, target_id="local:test", state_dir=self.state,
                    cwd=self.workspace, accept_spend=True, now=self.now,
                    runner=lambda *args, **kwargs: UnknownCost(),
                )

    def test_provider_probe_needs_both_existing_login_and_fresh_capability(self):
        raw = json.loads((Path(__file__).resolve().parents[1] / "examples/three-agent-runbook.json").read_text())
        v2 = migrate_runbook_v1_to_v2(
            raw,
            readiness_policy={"receipt_ttl_seconds": 300},
            trust_tiers={agent["id"]: "developer_trusted" for agent in raw["agents"]},
        )
        runbook = migrate_runbook_v2_to_v3(v2)
        runbook["agents"][0]["adapter"] = {
            "kind": "claude_cli", "profile": "profile.yaml", "timeout_seconds": 30,
        }

        def observed(argv, cwd, timeout):
            if tuple(argv[-3:]) == ("auth", "status", "--json"):
                return CommandOutcome(tuple(argv), 0, '{"loggedIn": true}', "")
            raise AssertionError(argv)

        context = ProbeContext.guarded(
            runbook=runbook, workspace=self.workspace, state_dir=self.state,
            now=self.now, ttl_seconds=30, target_id="local:test", redactor=Redactor(env={}),
            runner=observed, which=lambda name: str(self.bin / "fake-claude") if name == "fake-claude" else "/usr/bin/" + name,
        )
        probe = ProviderConnectionProbe()
        self.assertEqual(probe.observe(context).result.status, "red")
        with patch.dict(os.environ, {"PATH": str(self.bin)}):
            create_claude_capability(
                self.profile, target_id="local:test", state_dir=self.state,
                cwd=self.workspace, accept_spend=True, now=self.now,
                runner=lambda *args, **kwargs: Completed(),
            )
        outcome = probe.observe(context)
        self.assertEqual(outcome.result.status, "green", (outcome.result.to_dict(), outcome.facts))
        self.assertEqual(outcome.facts["agents"][0]["resolved_model"], "claude-fable-5")


if __name__ == "__main__":
    unittest.main()
