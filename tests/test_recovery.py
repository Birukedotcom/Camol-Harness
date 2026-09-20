import base64
import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    AESGCM = None

from camol.cli import main
from camol.recovery import (MAGIC, RecoveryError, export_workspace_recovery,
                            generate_recovery_key, load_recovery_key,
                            restore_workspace_recovery, verify_workspace_recovery)
from camol.workspace import WorkspaceManager
from tests.test_workspace import git


@unittest.skipUnless(AESGCM, "optional recovery encryption dependency")
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.state = self.root / "source", self.root / "state"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.name", "Recovery Test")
        git(self.source, "config", "user.email", "recovery@example.invalid")
        (self.source / "tracked").write_text("base\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "base")
        self.manager = WorkspaceManager(self.source, self.state)
        self.box = self.manager.prepare_task("run", "task", "box")
        (self.box.path / "tracked").write_text("committed\n")
        git(self.box.path, "add", ".")
        git(self.box.path, "commit", "-qm", "worker commit")
        (self.box.path / "tracked").write_text("dirty\n")
        self.secret = b"synthetic-private-recovery-fixture-91"
        (self.box.path / "new.bin").write_bytes(self.secret + b"\0\xff")
        self.salvage = self.manager.salvage(self.box)
        self.key = os.urandom(32)
        self.archive, self.output = self.root / "archive", self.root / "restored"

    def policy(self, salvage=None):
        return dict(schema="camol.recovery_policy", schema_version=1, owner="human",
                    purpose="workspace-recovery", salvage_digest=(salvage or self.salvage).digest(),
                    allow_encrypted_raw=True)

    def export(self, **changes):
        values = dict(source=self.source, state_dir=self.state, salvage=self.salvage,
                      policy=self.policy(), key=self.key, output=self.archive)
        values.update(changes)
        return export_workspace_recovery(**values)

    def restore(self, **changes):
        values = dict(archive=self.archive, key=self.key, output=self.output)
        values.update(changes)
        return restore_workspace_recovery(**values)

    def mutate_capsule(self, mutation):
        path = self.archive / "recovery.camol"
        raw = path.read_bytes()
        cipher = AESGCM(self.key)
        payload = json.loads(cipher.decrypt(raw[len(MAGIC):len(MAGIC) + 12], raw[len(MAGIC) + 12:], MAGIC))
        mutation(payload)
        nonce = os.urandom(12)
        path.write_bytes(MAGIC + nonce + cipher.encrypt(nonce, json.dumps(payload).encode(), MAGIC))

    def test_source_independent_restore_preserves_history_dirty_and_untracked_bytes(self):
        before = git(self.source, "show-ref")
        original_head = git(self.source, "rev-parse", "HEAD")
        result = self.export()
        self.assertEqual(before, git(self.source, "show-ref"))
        self.assertEqual(original_head, git(self.source, "rev-parse", "HEAD"))
        self.assertEqual(git(self.source, "status", "--porcelain"), "")
        # Prove source/CAS independence, not a clone that still borrows objects.
        self.source.rename(self.root / "unavailable-source")
        self.state.rename(self.root / "unavailable-state")
        self.assertEqual(self.restore(), result)
        self.assertEqual((self.output / "tracked").read_text(), "dirty\n")
        self.assertEqual((self.output / "new.bin").read_bytes(), self.secret + b"\0\xff")
        self.assertEqual(git(self.output, "show", self.salvage.head_revision + ":tracked"), "committed")
        self.assertEqual(git(self.output, "show", self.salvage.base_revision + ":tracked"), "base")
        self.assertFalse(result["resume_authorized"])
        self.assertFalse(result["cleanup_authorized"])
        self.assertEqual(git(self.output, "remote"), "")

    def test_capsule_is_encrypted_private_and_new_nonce_changes_ciphertext(self):
        first = self.export()
        second_dir = self.root / "archive-two"
        second = self.export(output=second_dir)
        raw = (self.archive / "recovery.camol").read_bytes()
        self.assertNotIn(self.secret, raw)
        self.assertNotEqual(first["capsule_digest"], second["capsule_digest"])
        self.assertEqual(first["candidate_tree"], second["candidate_tree"])
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.archive / "recovery.camol").stat().st_mode & 0o777, 0o600)

    def test_wrong_key_and_tampering_do_not_create_restore_destination(self):
        self.export()
        with self.assertRaisesRegex(RecoveryError, "authentication"):
            self.restore(key=os.urandom(32))
        self.assertFalse(self.output.exists())
        path = self.archive / "recovery.camol"
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 1
        path.write_bytes(raw)
        with self.assertRaisesRegex(RecoveryError, "authentication"):
            self.restore()
        self.assertFalse(self.output.exists())

    def test_policy_is_exact_and_refusal_precedes_source_access(self):
        for changed in ({"allow_encrypted_raw": False}, {"salvage_digest": "sha256:" + "0" * 64},
                        {"purpose": "run-resume"}, {"extra": True}, {"schema_version": True}):
            policy = dict(self.policy(), **changed)
            with self.subTest(changed=changed), patch("camol.recovery._Git.call", side_effect=AssertionError("no Git")):
                with self.assertRaises(RecoveryError):
                    self.export(policy=policy)
            self.assertFalse(self.archive.exists())

    def test_missing_or_symlinked_raw_salvage_is_refused(self):
        digest = self.salvage.patch_digest
        path = self.state / "salvage" / "blobs" / digest[7:9] / digest[7:]
        outside = self.root / "outside"
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaises(RecoveryError):
            self.export()
        self.assertFalse(self.archive.exists())

    def test_receipt_paths_cannot_install_git_controls(self):
        for relative in (".git/config", ".GiT/hooks/post-checkout", "directory/.git/config",
                         ".g\u200cit/hooks/post-checkout", ".ｇｉｔ/hooks/other", "GIT~1/hooks/other"):
            items = [dict(item, path=relative) for item in self.salvage.untracked]
            salvage = replace(self.salvage, untracked=tuple(items))
            with self.subTest(path=relative), self.assertRaises(RecoveryError):
                self.export(salvage=salvage, policy=self.policy(salvage))
        self.assertFalse(self.archive.exists())

    def test_recomputed_authenticated_payload_cannot_expand_blob_inventory(self):
        self.export()
        self.mutate_capsule(lambda value: value["blobs"].update({"sha256:" + "0" * 64: ""}))
        with self.assertRaisesRegex(RecoveryError, "inventory"):
            self.restore()
        self.assertFalse(self.output.exists())

    def test_authenticated_bundle_ref_tampering_is_denied(self):
        self.export()
        def tamper(value):
            raw = base64.b64decode(value["bundle"])
            value["bundle"] = base64.b64encode(raw.replace(b"refs/recovery/head", b"refs/heads/attack")).decode()
        self.mutate_capsule(tamper)
        with self.assertRaisesRegex(RecoveryError, "unexpected references"):
            self.restore()
        self.assertFalse(self.output.exists())

    def test_authenticated_pack_object_count_is_bounded_before_git(self):
        self.export()
        def tamper(value):
            raw = bytearray(base64.b64decode(value["bundle"]))
            offset = raw.index(b"PACK")
            raw[offset + 8:offset + 12] = (100001).to_bytes(4, "big")
            value["bundle"] = base64.b64encode(raw).decode()
        self.mutate_capsule(tamper)
        with patch("camol.recovery._Git.call", side_effect=AssertionError("no Git")):
            with self.assertRaisesRegex(RecoveryError, "object inventory"):
                self.restore()
        self.assertFalse(self.output.exists())

    def test_unrelated_refs_and_their_unique_objects_are_not_copied(self):
        original = git(self.source, "rev-parse", "--abbrev-ref", "HEAD")
        git(self.source, "checkout", "-qb", "unrelated")
        (self.source / "unrelated-file").write_text("not part of the selected history\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "unrelated")
        excluded = git(self.source, "rev-parse", "HEAD")
        git(self.source, "checkout", "-q", original)
        self.export()
        self.restore()
        result = subprocess.run(["git", "-C", str(self.output), "cat-file", "-e", excluded],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.output / "unrelated-file").exists())

    def test_shallow_source_refuses_instead_of_claiming_complete_history(self):
        (self.source / ".git" / "shallow").write_text(self.salvage.base_revision + "\n")
        with self.assertRaisesRegex(RecoveryError, "shallow"):
            self.export()
        self.assertFalse(self.archive.exists())

    def test_sha256_git_history_roundtrips(self):
        source, state = self.root / "sha256", self.root / "sha256-state"
        source.mkdir()
        git(source, "init", "--object-format=sha256", "-q")
        git(source, "config", "user.name", "Recovery Test")
        git(source, "config", "user.email", "recovery@example.invalid")
        (source / "tracked").write_text("sha256 base\n")
        git(source, "add", ".")
        git(source, "commit", "-qm", "base")
        manager = WorkspaceManager(source, state)
        box = manager.prepare_task("run", "task", "box")
        (box.path / "tracked").write_text("sha256 dirty\n")
        salvage = manager.salvage(box)
        self.export(source=source, state_dir=state, salvage=salvage, policy=self.policy(salvage))
        self.restore()
        self.assertEqual((self.output / "tracked").read_text(), "sha256 dirty\n")
        self.assertEqual(git(self.output, "rev-parse", "--show-object-format"), "sha256")

    def test_many_files_use_batched_object_reads_with_exact_binary_content(self):
        from camol.recovery import _Git

        for index in range(200):
            (self.box.path / ("file-" + str(index))).write_bytes(bytes([index % 17]) * 33 + b"\0\xff")
        git(self.box.path, "add", ".")
        self.salvage = self.manager.salvage(self.box)
        calls, original = [], _Git.call
        def counted(instance, repo, *args, **kwargs):
            if args[0] == "cat-file":
                calls.append(args[1])
            return original(instance, repo, *args, **kwargs)
        with patch.object(_Git, "call", counted):
            self.export()
            self.restore()
        self.assertEqual(calls, ["--batch-check", "--batch"] * 2)
        for index in range(200):
            self.assertEqual((self.output / ("file-" + str(index))).read_bytes(), bytes([index % 17]) * 33 + b"\0\xff")

    def test_output_is_never_nested_in_input_and_never_overwrites(self):
        for output in (self.source / "backup", self.state / "backup"):
            with self.assertRaisesRegex(RecoveryError, "separate"):
                self.export(output=output)
            self.assertFalse(output.exists())
        self.export()
        with self.assertRaisesRegex(RecoveryError, "separate"):
            self.restore(output=self.archive / "recovered")
        self.restore()
        before = (self.output / "tracked").read_bytes()
        with self.assertRaises(RecoveryError):
            self.restore()
        self.assertEqual((self.output / "tracked").read_bytes(), before)

    def test_verify_performs_real_reconstruction(self):
        expected = self.export()
        self.assertEqual(verify_workspace_recovery(archive=self.archive, key=self.key), expected)
        self.assertFalse(self.output.exists())

    def test_git_hooks_filters_and_hostile_path_are_not_executed(self):
        (self.box.path / ".gitattributes").write_text("* filter=evil\n")
        git(self.box.path, "add", ".gitattributes")
        self.salvage = self.manager.salvage(self.box)
        trap = self.root / "trap"
        trap.write_text("#!/bin/sh\ntouch '" + str(self.root / "SENTINEL") + "'\n")
        trap.chmod(0o700)
        git(self.source, "config", "core.fsmonitor", str(trap))
        git(self.source, "config", "filter.evil.smudge", str(trap))
        git(self.source, "config", "core.hooksPath", str(self.root))
        (self.root / "post-checkout").symlink_to(trap)
        self.assertFalse((self.root / "SENTINEL").exists())
        with patch.dict(os.environ, {"PATH": str(self.root), "GIT_CONFIG_GLOBAL": str(trap)}):
            self.export()
            self.restore()
        self.assertFalse((self.root / "SENTINEL").exists())
        self.assertTrue((self.output / ".gitattributes").is_file())

    def test_tracked_executable_and_symlink_are_preserved_without_execution(self):
        program = self.box.path / "program"
        program.write_text("#!/bin/sh\nexit 91\n")
        program.chmod(0o700)
        (self.box.path / "link").symlink_to("program")
        git(self.box.path, "add", "program", "link")
        self.salvage = self.manager.salvage(self.box)
        self.export()
        self.restore()
        self.assertEqual(os.readlink(self.output / "link"), "program")
        self.assertEqual((self.output / "program").stat().st_mode & 0o777, 0o700)

    def test_untracked_content_cannot_write_through_tracked_symlink(self):
        (self.box.path / "link").symlink_to(str(self.root))
        git(self.box.path, "add", "link")
        salvage = self.manager.salvage(self.box)
        salvage = replace(salvage, untracked=(dict(salvage.untracked[0], path="link/SENTINEL"),))
        with self.assertRaises(RecoveryError):
            self.export(salvage=salvage, policy=self.policy(salvage))
        self.assertFalse((self.root / "SENTINEL").exists())
        self.assertFalse(self.archive.exists())

    def test_limits_and_external_submodules_refuse_before_publication(self):
        for bound, value in (("MAX_BUNDLE", 16), ("MAX_FILE", 1), ("MAX_FILES", 1), ("MAX_CONTENT", 1)):
            with self.subTest(bound=bound), patch("camol.recovery." + bound, value):
                with self.assertRaises(RecoveryError):
                    self.export()
            self.assertFalse(self.archive.exists())
        (self.box.path / "submodule").mkdir()
        git(self.box.path, "update-index", "--add", "--cacheinfo", "160000," + self.salvage.base_revision + ",submodule")
        self.salvage = self.manager.salvage(self.box)
        with self.assertRaises(RecoveryError):
            self.export()
        self.assertFalse(self.archive.exists())

    def test_key_and_cli_roundtrip_never_display_key_or_raw_content(self):
        key_dir = self.root / "keys"
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(["recovery", "keygen", "--output", str(key_dir)]), 0)
            policy_file, salvage_file = self.root / "policy.json", self.root / "salvage.json"
            policy_file.write_text(json.dumps(self.policy()))
            salvage_file.write_text(json.dumps(self.salvage.to_dict()))
            key_args = ["--key-file", str(key_dir / "key.bin")]
            self.assertEqual(main(["recovery", "export", *key_args, "--source", str(self.source), "--state-dir", str(self.state), "--salvage", str(salvage_file), "--policy", str(policy_file), "--output", str(self.archive)]), 0)
            self.assertEqual(main(["recovery", "verify", *key_args, "--archive", str(self.archive)]), 0)
            self.assertEqual(main(["recovery", "restore", *key_args, "--archive", str(self.archive), "--output", str(self.output)]), 0)
        key = load_recovery_key(path=key_dir / "key.bin")
        self.assertNotIn(key.hex(), stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(self.secret.decode(), stdout.getvalue() + stderr.getvalue())
        self.assertEqual((self.output / "tracked").read_text(), "dirty\n")
        (key_dir / "key.bin").chmod(0o644)
        with self.assertRaises(RecoveryError):
            load_recovery_key(path=key_dir / "key.bin")


if __name__ == "__main__":
    unittest.main()
