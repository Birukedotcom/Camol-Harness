import unittest
from datetime import datetime, timezone

from camol.evidence import EvidenceError, EvidenceRecord
from camol.probes import Redactor


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc).isoformat()
DIGEST = "sha256:" + "1" * 64


def record(**overrides):
    values = dict(
        evidence_id="evidence-1",
        run_id="run-1",
        task_id="task-1",
        debug_case_id=None,
        agent_id="agent-1",
        lease_id="lease-1",
        fence_digest=DIGEST,
        kind="command",
        epistemic_status="EXECUTED",
        producer="adapter",
        observed_at=NOW,
        data={"argv": ["python3", "-V"], "exit_code": 0},
        artifact_refs=(),
    )
    values.update(overrides)
    return EvidenceRecord(**values)


class EvidenceRecordTests(unittest.TestCase):
    def test_round_trip_and_epistemic_gate(self):
        executed = record()
        self.assertEqual(EvidenceRecord.from_dict(executed.to_dict()), executed)
        self.assertTrue(executed.satisfies_gate())
        self.assertFalse(record(epistemic_status="UNVERIFIED").satisfies_gate())
        self.assertTrue(record(kind="claim", epistemic_status="UNVERIFIED").satisfies_gate())

    def test_task_subject_and_unknown_fields_are_strict(self):
        with self.assertRaisesRegex(EvidenceError, "bind agent, lease, and fence"):
            record(agent_id=None)
        payload = record().to_dict()
        payload["trusted"] = True
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            EvidenceRecord.from_dict(payload)

    def test_untrusted_data_is_redacted_and_size_bounded(self):
        secret = "secret-value-that-must-not-land"
        clean = EvidenceRecord.task(
            evidence_id="evidence-2",
            run_id="run-1",
            task_id="task-1",
            agent_id="agent-1",
            lease_id="lease-1",
            fence_digest=DIGEST,
            kind="claim",
            epistemic_status="UNVERIFIED",
            producer="worker",
            observed_at=NOW,
            data={"summary": "observed {}".format(secret)},
            redactor=Redactor({"TEST_SECRET": secret}),
        )
        self.assertNotIn(secret, str(clean.to_dict()))
        with self.assertRaisesRegex(EvidenceError, "256 KiB"):
            record(data={"text": "x" * 300000})


if __name__ == "__main__":
    unittest.main()
