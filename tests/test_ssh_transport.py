"""Local-process bridges and fake SSH only: no remote connections or keys."""

import asyncio
import copy
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from camol.schema import canonical_digest
from camol.ssh_bridge import bridge_identity
from camol.ssh_protocol import SSHTarget, SSHTransportError, strict_json, read_frame, sha256
from camol.ssh_transport import SSHControlClient, cancellation_receipt
from camol.supervisor import Supervisor, SupervisorPaths, SupervisorError


ROOT = Path(__file__).resolve().parents[1]


class SSHTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ssh-fixture-", dir="/tmp")
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "remote-state"
        self.paths = SupervisorPaths.under(self.state)
        self.paths.control_dir.mkdir(parents=True)
        self.token = "private-control-token-fixture-" + "x" * 40
        self.paths.token.write_text(self.token)
        self.paths.token.chmod(0o600)
        self.requests, self.handlers = [], set()
        self.response_delay = 0
        self.supervisor = Supervisor.__new__(Supervisor)
        self.supervisor.token, self.supervisor.run_id = self.token, "remote-run"
        self.plan_digest = "sha256:" + "a" * 64
        self.supervisor.orchestrator = Mock()
        self.supervisor.orchestrator.state.return_value = {"plan_digest": self.plan_digest}
        self.supervisor.status = lambda: {"run": {"run_id": "remote-run", "plan_digest": self.plan_digest}, "draining": self.supervisor.draining}
        self.supervisor.draining, self.supervisor.mode = False, "running"
        self.supervisor._wake = asyncio.Event()
        self.supervisor.stop_after_drain = False

        async def handler(reader, writer):
            current = asyncio.current_task()
            self.handlers.add(current)
            try:
                request = strict_json(await reader.readline())
                self.requests.append(request)
                try:
                    response = await self.supervisor._dispatch(request)
                except SupervisorError:
                    response = {"ok": False, "error": "deliberately safe fixture rejection"}
                if request["command"] == "drain":
                    await asyncio.sleep(self.response_delay)
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                self.handlers.discard(current)
        self.server = await asyncio.start_unix_server(handler, path=str(self.paths.socket))
        self.paths.socket.chmod(0o600)
        self.known = self.root / "known-hosts"
        self.known.write_text("fixture.example ssh-ed25519 fixture-public-host-key\n")
        self.known.chmod(0o600)
        self.key = self.root / "key-reference"
        self.key.write_text("NEVER-READ-THIS-PRIVATE-KEY-FIXTURE")
        self.key.chmod(0o600)
        self.policy_path = self.root / "bridge-policy.json"
        self.binding = dict(state_dir=str(self.state), run_id="remote-run", plan_digest=self.plan_digest,
                            owner="human-owner", allowed_commands=["status", "drain", "accept"])
        self.policy = dict(schema="camol.ssh_bridge_policy", schema_version=1, targets={"fixture": self.binding})
        self.write_policy()
        self.fake_ssh = self.root / "fake-ssh"
        self.capture = self.root / "ssh-capture.json"
        self.mode = "bridge"
        self.install_fake()
        self.profile = SSHTarget(name="fixture", host="fixture.example", port=2222, login="fixture",
            known_hosts=str(self.known), known_hosts_sha256=sha256(self.known.read_bytes()), identity_file=str(self.key),
            target_id="fixture", target_digest=canonical_digest(self.binding), run_id="remote-run", plan_digest=self.plan_digest,
            owner="human-owner", bridge_identity=bridge_identity(), allowed_commands=("status", "drain", "accept"))

    def write_policy(self):
        self.policy_path.write_text(json.dumps(self.policy))
        self.policy_path.chmod(0o600)

    def install_fake(self):
        script = '''#!%s
import asyncio,json,os,struct,sys,time
from pathlib import Path
sys.path.insert(0,%r)
from camol.ssh_bridge import _stdio,bridge_identity
from camol.ssh_protocol import encode_frame
capture=Path(%r)
known=next(v.split('=',1)[1] for v in sys.argv if v.startswith('UserKnownHostsFile='))
capture.write_text(json.dumps({'argv':sys.argv[1:],'environment':dict(os.environ),'snapshot':Path(known).read_text(),'snapshot_path':known,'pid':os.getpid()}))
mode=%r
if mode=='mutate_known_hosts': Path(%r).write_text('CHANGED ORIGINAL')
if mode=='stderr': sys.stderr.buffer.write(b'secret-banner'*300000);sys.stderr.buffer.flush()
if mode in {'bad_identity','trailing','duplicate','oversized','no_stdin','orphan_pipes'}:
 identity=bridge_identity()
 if mode=='bad_identity': identity['camol_version']='not-approved'
 sys.stdout.buffer.write(encode_frame({'schema':'camol.ssh_hello','schema_version':1,'nonce':'n'*64,'identity':identity}));sys.stdout.buffer.flush()
 if mode=='no_stdin': time.sleep(30)
 if mode=='bad_identity':
  if sys.stdin.buffer.read(1): Path(%r).write_text('unexpected request')
 else:
  length=struct.unpack('!I',sys.stdin.buffer.read(4))[0];request=json.loads(sys.stdin.buffer.read(length))
  if mode=='orphan_pipes':
   if os.fork()==0:time.sleep(5);os._exit(0)
   os._exit(0)
  if mode=='oversized':sys.stdout.buffer.write(struct.pack('!I',9000000))
  elif mode=='duplicate':
   data=b'{"schema":"camol.ssh_response","schema":"other"}';sys.stdout.buffer.write(struct.pack('!I',len(data))+data)
  else:
   response={k:request[k] for k in ('request_id','target_id','target_digest','run_id','plan_digest')};response.update(schema='camol.ssh_response',schema_version=1,outcome='completed',response={'ok':True,'result':{}})
   sys.stdout.buffer.write(encode_frame(response));sys.stdout.buffer.write(b'TRAILING')
  sys.stdout.buffer.flush()
else: asyncio.run(_stdio(Path(%r)))
''' % (sys.executable, str(ROOT), str(self.capture), self.mode, str(self.known), str(self.root / "unexpected-request"), str(self.policy_path))
        self.fake_ssh.write_text(script)
        self.fake_ssh.chmod(0o700)

    def client(self, **kwargs):
        return SSHControlClient(self.profile, state_dir=self.root / "client-state", ssh_binary=self.fake_ssh, **kwargs)

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        self.temp.cleanup()

    async def test_local_bridge_status_and_bound_owner_control_without_forwarding_credentials(self):
        with patch.dict(os.environ, {"SSH_AUTH_SOCK": "/must-not-forward", "SSH_ASKPASS": "/must-not-run", "SECRET_TOKEN": "never-log-me"}):
            client = self.client()
            status = await client.request("status")
            self.assertFalse(status["result"]["draining"])
            result = await client.request("drain")
        self.assertTrue(result["ok"])
        self.assertTrue(self.supervisor.draining)
        self.assertEqual(self.requests[-1]["schema_version"], 3)
        capture = json.loads(self.capture.read_text())
        for option in ("StrictHostKeyChecking=yes", "IdentityAgent=none", "ForwardAgent=no", "ClearAllForwardings=yes", "ProxyCommand=none", "ProxyJump=none", "ControlMaster=no"):
            self.assertIn(option, capture["argv"])
        self.assertEqual(capture["argv"][-2:], ["fixture.example", "camol-ssh-bridge"])
        self.assertNotIn("SSH_AUTH_SOCK", capture["environment"])
        self.assertNotIn("SSH_ASKPASS", capture["environment"])
        self.assertNotIn("SECRET_TOKEN", capture["environment"])
        self.assertNotIn(str(self.state), " ".join(capture["argv"]))
        self.assertNotEqual(capture["snapshot_path"], str(self.known))
        self.assertFalse(Path(capture["snapshot_path"]).exists())
        self.assertEqual(client.receipts()[0]["status"], "completed")
        self.assertNotIn(self.token, json.dumps(result))
        self.assertNotIn("NEVER-READ-THIS", json.dumps(client.receipts()))

    async def test_known_hosts_snapshot_survives_original_change_but_changed_pin_is_denied_next_call(self):
        self.mode = "mutate_known_hosts"
        self.install_fake()
        self.assertTrue((await self.client().request("status"))["ok"])
        captured = json.loads(self.capture.read_text())
        self.assertEqual(sha256(captured["snapshot"].encode()), self.profile.known_hosts_sha256)
        with self.assertRaisesRegex(SSHTransportError, "known_hosts"):
            await self.client().request("status")

    async def test_reaped_ssh_leader_with_descendant_pipes_cannot_hang_or_signal_unowned_group(self):
        self.mode = "orphan_pipes"
        self.install_fake()
        client = self.client(timeout=2)
        started = time.monotonic()
        with patch("camol.ssh_transport.os.killpg") as signal_group:
            with self.assertRaises(SSHTransportError) as caught:
                await client.request("drain")
        self.assertEqual(caught.exception.outcome, "unknown")
        self.assertLess(time.monotonic() - started, 4)
        signal_group.assert_not_called()
        cleanup = client.receipts()[0]["cleanup"]
        self.assertTrue(cleanup["pipes_detached"])
        self.assertTrue(cleanup["leader_reaped"])
        self.assertEqual(cleanup["group_signal"], "not_proven")

    async def test_identity_mismatch_sends_no_request_and_does_not_touch_supervisor(self):
        self.mode = "bad_identity"
        self.install_fake()
        with self.assertRaises(SSHTransportError) as caught:
            await self.client().request("drain")
        self.assertEqual(caught.exception.code, "IDENTITY_DENIED")
        self.assertEqual(caught.exception.outcome, "not_dispatched")
        self.assertFalse(self.requests)
        self.assertFalse((self.root / "unexpected-request").exists())

    async def test_readonly_client_and_changed_remote_policy_cannot_authorize_mutations(self):
        payload = self.profile.to_dict()
        payload.pop("allowed_commands")
        readonly = SSHControlClient(SSHTarget.from_dict(payload), state_dir=self.root / "readonly-state", ssh_binary=self.fake_ssh)
        with self.assertRaises(SSHTransportError):
            await readonly.request("drain")
        self.assertFalse(self.capture.exists())
        self.binding["allowed_commands"].append("stop")
        self.write_policy()
        with self.assertRaises(SSHTransportError) as caught:
            await self.client().request("drain")
        self.assertEqual(caught.exception.outcome, "not_dispatched")
        self.assertFalse(self.requests)

    async def test_authoritative_run_plan_binding_and_approval_actor_deny_before_side_effects(self):
        self.supervisor.orchestrator.state.return_value["plan_digest"] = "sha256:" + "b" * 64
        denied = await self.client().request("drain")
        self.assertFalse(denied["ok"])
        self.assertFalse(self.supervisor.draining)
        self.assertEqual(self.client().receipts()[0]["status"], "rejected")
        self.supervisor.orchestrator.state.return_value["plan_digest"] = self.plan_digest
        with self.assertRaises(SSHTransportError):
            await self.client().request("accept", params={"approved_by": "different-owner", "outcome_digest": self.plan_digest})
        self.supervisor.orchestrator.accept_run.assert_not_called()

    async def test_timeout_after_dispatch_is_durable_unknown_and_never_retried(self):
        self.response_delay = 8
        client = self.client(timeout=3)
        with self.assertRaises(SSHTransportError) as caught:
            await client.request("drain")
        self.assertEqual(caught.exception.outcome, "unknown")
        self.assertTrue(self.supervisor.draining)
        fresh = self.client()
        with self.assertRaises(SSHTransportError) as held:
            await fresh.request("drain")
        self.assertEqual(held.exception.code, "OUTCOME_UNKNOWN")
        self.assertEqual(len(self.requests), 1)
        self.assertTrue((await fresh.request("status"))["result"]["draining"])
        with self.assertRaises(SSHTransportError):
            fresh.acknowledge_unknown(caught.exception.request_id, requested_by="other", note="no")
        receipt = fresh.acknowledge_unknown(caught.exception.request_id, requested_by="human-owner", note="Inspected status and accepted the uncertainty; do not replay the previous request.")
        self.assertEqual(receipt["status"], "acknowledged_unknown")
        self.response_delay = 0
        self.assertTrue((await fresh.request("drain"))["ok"])
        self.assertEqual([item["command"] for item in self.requests], ["drain", "status", "drain"])

    async def test_cancellation_keeps_unknown_without_sending_remote_stop(self):
        self.response_delay = 5
        client = self.client()
        task = asyncio.create_task(client.request("drain"))
        for _ in range(100):
            if self.requests:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.requests)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertEqual(cancellation_receipt(caught.exception)["outcome"], "unknown")
        self.assertEqual(client.receipts()[0]["status"], "unknown")
        self.assertEqual([item["command"] for item in self.requests], ["drain"])
        pid = json.loads(self.capture.read_text())["pid"]
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_stderr_backpressure_and_nonreading_stdin_are_bounded(self):
        self.mode = "stderr"
        self.install_fake()
        result = await self.client().request("status")
        self.assertGreater(result["transport"]["stderr"]["bytes"], 1 << 20)
        self.assertNotIn("secret-banner", json.dumps(result))
        self.mode = "no_stdin"
        self.install_fake()
        with self.assertRaises(SSHTransportError):
            await self.client(timeout=0.3).request("drain", params={"large": "x" * 55000})
        self.assertEqual(self.client().receipts()[0]["status"], "unknown")

    async def test_duplicate_oversized_and_trailing_frames_fail_closed(self):
        for mode in ("duplicate", "oversized", "trailing"):
            self.mode = mode
            self.install_fake()
            with self.assertRaises(SSHTransportError):
                await self.client().request("status")

    async def test_protocol_validation_rejects_ambiguous_and_nonfinite_values_before_launch(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'{"x":' + b'9' * 129 + b'}'):
            with self.assertRaises(SSHTransportError):
                strict_json(raw)
        for bad in ("host;touch /tmp/no", "-oProxyCommand=bad", "host\nother"):
            profile = self.profile.to_dict()
            profile["host"] = bad
            with self.assertRaises(SSHTransportError):
                SSHTarget.from_dict(profile)
        with self.assertRaises(SSHTransportError):
            await self.client().request("status", params={"number": float("nan")})
        self.assertFalse(self.capture.exists())

    async def test_profile_nested_identity_and_inflight_params_are_frozen(self):
        detached = self.profile.to_dict()
        detached["bridge_identity"]["camol_version"] = "changed"
        self.assertNotEqual(self.profile.bridge_identity["camol_version"], "changed")
        with self.assertRaises(TypeError):
            self.profile.bridge_identity["camol_version"] = "changed"
        original = self.supervisor._dispatch
        async def observe(request):
            if request["command"] == "status":
                return {"ok": True, "result": request["params"]}
            return await original(request)
        self.supervisor._dispatch = observe
        params = {"nested": {"value": "approved"}}
        pending = asyncio.create_task(self.client().request("status", params=params))
        await asyncio.sleep(0)
        params["nested"]["value"] = "substituted"
        response = await pending
        self.assertEqual(response["result"]["nested"]["value"], "approved")
        self.assertEqual(response["transport"]["request_digest"], canonical_digest({"command": "status", "params": {"nested": {"value": "approved"}}}))

    async def test_receipt_inspection_is_noncreating_and_notes_cannot_log_secrets(self):
        state = self.root / "never-created"
        inspector = SSHControlClient(self.profile, state_dir=state, ssh_binary=self.fake_ssh, read_only=True)
        self.assertEqual(inspector.receipts(), [])
        self.assertFalse(state.exists())
        with self.assertRaises(SSHTransportError):
            inspector.acknowledge_unknown("missing", requested_by="human-owner", note="none")
        with self.assertRaises(SSHTransportError):
            self.client().acknowledge_unknown("missing", requested_by="human-owner", note="token=ghp_" + "A" * 24)


if __name__ == "__main__":
    unittest.main()
