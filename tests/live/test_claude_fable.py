"""Opt-in, billable provider smoke; excluded from ordinary test discovery."""

import os
import tempfile
import unittest
from pathlib import Path

from camol.probes import local_target_id
from camol.providers import create_claude_capability, load_model_profile


@unittest.skipUnless(os.environ.get("CAMOL_LIVE_CLAUDE") == "1", "set CAMOL_LIVE_CLAUDE=1 to authorize live smoke")
class LiveClaudeFableTests(unittest.TestCase):
    def test_spend_capped_model_resolution(self):
        root = Path(__file__).resolve().parents[2]
        profile = load_model_profile(root, "profiles/models/claude-fable-5-1.yaml")
        with tempfile.TemporaryDirectory(prefix="camol-live-state-") as state:
            receipt = create_claude_capability(
                profile,
                target_id=local_target_id(),
                state_dir=Path(state),
                cwd=root,
                accept_spend=True,
                spend_ceiling_cents=10,
            )
        self.assertEqual(receipt.resolved_model, "claude-fable-5")
        self.assertLessEqual(receipt.cost_usd_micros, 100_000)


if __name__ == "__main__":
    unittest.main()
