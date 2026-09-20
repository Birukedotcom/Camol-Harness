import hashlib
import fcntl
import http.server
import os
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from camol.models import DownloadFile, DownloadPlan, HTTPSDownloadTransport, ModelError, ModelStore


PAYLOAD = (b"passive model fixture -- never import or execute\n" * 4096)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        data = self.server.payload
        offset = 0
        raw_range = self.headers.get("Range")
        self.server.requests.append((self.path, raw_range))
        if raw_range and self.path != "/ignore-range":
            offset = int(raw_range.removeprefix("bytes=").removesuffix("-"))
        self.send_response(206 if raw_range and self.path != "/ignore-range" else 200)
        self.send_header("Content-Length", str(len(data) - offset))
        if raw_range and self.path != "/ignore-range":
            self.send_header("Content-Range", "bytes {}-{}/{}".format(offset, len(data) - 1, len(data)))
        self.end_headers()
        if self.path == "/corrupt":
            data = b"X" + data[1:]
        try:
            self.wfile.write(data[offset:])
        except (BrokenPipeError, ConnectionResetError):
            pass


class Stream:
    def __init__(self, response, cancel=None):
        self.response, self.cancel = response, cancel
        self.status = response.status
        self.headers = {name.lower(): value for name, value in response.headers.items()}
        # This explicitly injected test transport maps one approved fixture
        # identity to a tiny loopback HTTP server; production stays HTTPS-only.
        self.source_origin = "https://fixtures.invalid"

    def read(self, count):
        result = self.response.read(count)
        if self.cancel:
            self.cancel.set()
        return result

    def close(self):
        self.response.close()


class FixtureTransport:
    def __init__(self, address, cancel=None):
        self.address, self.cancel = address, cancel

    def open(self, file, *, offset, plan, headers):
        request_headers = {"Range": "bytes={}-".format(offset)} if offset else {}
        request = urllib.request.Request(self.address + urllib.parse.urlsplit(file.url).path, headers=request_headers)
        return Stream(urllib.request.urlopen(request, timeout=2), self.cancel)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "models"
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.payload, self.server.requests = PAYLOAD, []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.transport = FixtureTransport("http://127.0.0.1:" + str(self.server.server_port))
        self.store = ModelStore(self.root)
        self.plan = self.make_plan()

    def tearDown(self):
        self.store.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def make_plan(self, path="/model", *, transfer=None, credential_ref=None):
        file = DownloadFile("model.gguf", "https://fixtures.invalid" + path,
                            "sha256:" + hashlib.sha256(PAYLOAD).hexdigest(), len(PAYLOAD), credential_ref)
        return DownloadPlan("fixture-download", "local/fixture", "revision-one", "owner", (file,),
                            ("https://fixtures.invalid",), len(PAYLOAD), transfer or len(PAYLOAD) * 3, 2, 1)

    def prepare(self, plan=None):
        plan = plan or self.plan
        self.store.prepare(plan)
        self.store.approve(plan.digest(), "owner")
        return plan

    def test_approval_digest_and_passive_verified_download(self):
        state = self.store.prepare(self.plan)
        self.assertEqual(state["status"], "planned")
        self.assertEqual(self.server.requests, [])
        with self.assertRaisesRegex(ModelError, "approve the exact"):
            self.store.download(self.plan.digest(), "owner", transport=self.transport)
        with self.assertRaisesRegex(ModelError, "only the plan owner"):
            self.store.approve(self.plan.digest(), "worker")
        self.store.approve(self.plan.digest(), "owner")
        state = self.store.download(self.plan.digest(), "owner", transport=self.transport)
        self.assertEqual(state["status"], "downloaded_verified")
        self.assertEqual(state["accounted_bytes"], len(PAYLOAD))
        self.assertEqual(state["observed_bytes"], len(PAYLOAD))
        self.assertEqual(state["loaded"], "unverified")
        self.assertEqual(state["inference_ready"], "unverified")
        artifact = self.store.verified_artifacts(self.plan.digest())[0]
        self.assertEqual(Path(artifact["path"]).read_bytes(), PAYLOAD)
        before = list(self.server.requests)
        self.store.download(self.plan.digest(), "owner", transport=self.transport)
        self.assertEqual(self.server.requests, before, "verified cache must not fetch again")

    def test_cancel_returns_only_after_checkpoint_and_exact_range_resumes(self):
        self.prepare()
        cancel = threading.Event()
        transport = FixtureTransport(self.transport.address, cancel)
        state = self.store.download(self.plan.digest(), "owner", transport=transport, cancel_event=cancel)
        self.assertEqual(state["status"], "paused")
        self.assertEqual(state["files"][0]["partial_bytes"], 65536)
        before = self.store.events(self.plan.digest())
        self.store.close()
        self.store = ModelStore(self.root)
        state = self.store.download(self.plan.digest(), "owner", transport=self.transport)
        self.assertEqual(state["status"], "downloaded_verified")
        self.assertEqual(self.server.requests[-1][1], "bytes=65536-")
        self.assertEqual(state["accounted_bytes"], len(PAYLOAD))
        self.assertTrue(before)

    def test_ignored_range_preserves_partial_and_corrupt_hash_never_promotes(self):
        plan = self.prepare(self.make_plan("/ignore-range"))
        cancel = threading.Event()
        self.store.download(plan.digest(), "owner", transport=FixtureTransport(self.transport.address, cancel), cancel_event=cancel)
        partial = self.store.status(plan.digest())["files"][0]["partial_bytes"]
        with self.assertRaises(ModelError):
            self.store.download(plan.digest(), "owner", transport=self.transport)
        self.assertEqual(self.store.status(plan.digest())["files"][0]["partial_bytes"], partial)
        # A separate content store has no shared partial prefix for this failure.
        with ModelStore(Path(self.temp.name) / "corrupt-models") as store:
            plan = self.make_plan("/corrupt")
            store.prepare(plan)
            store.approve(plan.digest(), "owner")
            with self.assertRaisesRegex(ModelError, "pinned SHA-256"):
                store.download(plan.digest(), "owner", transport=self.transport)
            self.assertEqual(store.status(plan.digest())["status"], "failed")
            self.assertFalse(list((store.root / "blobs").iterdir()))

    def test_disk_limits_readonly_integrity_and_manifest_validation(self):
        self.prepare()
        with patch("camol.models.shutil.disk_usage", return_value=type("Disk", (), {"free": 0})()):
            with self.assertRaisesRegex(ModelError, "disk capacity"):
                self.store.download(self.plan.digest(), "owner", transport=self.transport)
        self.assertEqual(self.server.requests, [])
        self.store.download(self.plan.digest(), "owner", transport=self.transport)
        with ModelStore(self.root, read_only=True) as observer:
            self.assertEqual(observer.status(self.plan.digest(), verify=True)["status"], "downloaded_verified")
            with self.assertRaisesRegex(ModelError, "read-only"):
                observer.approve(self.plan.digest(), "owner")
        for update in ({"unknown": 1}, {"schema_version": True}, {"max_disk_bytes": 1}):
            payload = dict(self.plan.to_dict(), **update)
            with self.assertRaises(ValueError):
                DownloadPlan.from_dict(payload)
        for url in ("http://fixtures.invalid/model", "https://owner:secret@fixtures.invalid/model", "https://fixtures.invalid/model?token=private"):
            payload = self.plan.to_dict()
            payload["files"][0]["url"] = url
            with self.assertRaises(ModelError):
                DownloadPlan.from_dict(payload)
        blob = Path(self.store.verified_artifacts(self.plan.digest())[0]["path"])
        blob.write_bytes(b"X" + PAYLOAD[1:])
        with self.assertRaisesRegex(ModelError, "no longer matches"):
            self.store.verified_artifacts(self.plan.digest())

    def test_partial_symlink_swap_never_writes_outside_the_store(self):
        self.prepare()
        outside = Path(self.temp.name) / "outside"
        outside.write_bytes(b"preserve")
        original_open = self.transport.open
        def swap(*args, **kwargs):
            response = original_open(*args, **kwargs)
            (self.root / "partial" / (self.plan.digest().split(":")[1] + "-" + self.plan.files[0].digest.split(":")[1])).symlink_to(outside)
            return response
        with patch.object(self.transport, "open", side_effect=swap):
            with self.assertRaises(ModelError):
                self.store.download(self.plan.digest(), "owner", transport=self.transport)
        self.assertEqual(outside.read_bytes(), b"preserve")

    def test_crash_between_publication_and_unlink_recovers_without_redownload(self):
        self.prepare()
        original_unlink = os.unlink
        interrupted = False
        def interrupt(path, *args, **kwargs):
            nonlocal interrupted
            if not interrupted and kwargs.get("dir_fd") == self.store._dirs["partial"]:
                interrupted = True
                raise OSError("simulated crash after publication")
            return original_unlink(path, *args, **kwargs)
        with patch("camol.models.os.unlink", side_effect=interrupt):
            with self.assertRaises(ModelError):
                self.store.download(self.plan.digest(), "owner", transport=self.transport)
        requests = list(self.server.requests)
        state = self.store.download(self.plan.digest(), "owner", transport=self.transport)
        self.assertEqual(state["status"], "downloaded_verified")
        self.assertEqual(self.server.requests, requests)
        self.assertEqual(len(self.store.verified_artifacts(self.plan.digest())), 1)

    def test_resolver_secrets_and_transport_errors_do_not_enter_receipts(self):
        plan = self.prepare(self.make_plan(credential_ref="fixture-key"))
        class Broken:
            def open(self, file, **kwargs):
                raise OSError("https://private.invalid?token=do-not-retain Bearer private-value")
        with self.assertRaises(ModelError):
            self.store.download(plan.digest(), "owner", transport=Broken(),
                                credential_resolver=lambda reference, origin: {"Authorization": "Bearer private-value"})
        text = str(self.store.events(plan.digest())) + str(self.store.status(plan.digest()))
        self.assertNotIn("private-value", text)
        self.assertNotIn("do-not-retain", text)

    def test_production_https_redirects_strip_credentials_and_reject_new_origins(self):
        payload = self.plan.to_dict()
        payload["allowed_origins"].append("https://cdn.invalid")
        plan = DownloadPlan.from_dict(payload)
        class Response:
            status, headers = 200, {}
            def geturl(self):
                return "https://fixtures.invalid/model"
        with patch("camol.models.urllib.request.build_opener") as build:
            build.return_value.open.return_value = Response()
            HTTPSDownloadTransport().open(plan.files[0], offset=65536, plan=plan,
                                          headers={"Authorization": "Bearer synthetic-secret", "X-Api-Key": "synthetic-key"})
            request = build.return_value.open.call_args[0][0]
            redirect = next(item for item in build.call_args[0] if isinstance(item, urllib.request.HTTPRedirectHandler))
            redirected = redirect.redirect_request(request, None, 302, "Found", {}, "https://cdn.invalid/model?token=transient")
            self.assertFalse(any(key.lower() in {"authorization", "x-api-key"} for key in redirected.headers))
            self.assertEqual(redirected.get_header("Range"), "bytes=65536-")
            with self.assertRaises(ModelError):
                redirect.redirect_request(request, None, 302, "Found", {}, "https://unapproved.invalid/model")

    def test_transfer_unknown_is_reserved_and_partial_discard_does_not_refund(self):
        plan = self.prepare(self.make_plan(transfer=len(PAYLOAD)))
        original_open = self.transport.open
        def fail_read(*args, **kwargs):
            response = original_open(*args, **kwargs)
            response.read = lambda count: (_ for _ in ()).throw(OSError("unknown transfer outcome"))
            return response
        with patch.object(self.transport, "open", side_effect=fail_read):
            with self.assertRaises(ModelError):
                self.store.download(plan.digest(), "owner", transport=self.transport)
        state = self.store.status(plan.digest())
        self.assertEqual(state["accounted_bytes"], 65536)
        self.assertEqual(state["observed_bytes"], 0)
        with self.assertRaisesRegex(ModelError, "transfer ceiling"):
            self.store.download(plan.digest(), "owner", transport=self.transport)
        state = self.store.status(plan.digest())
        self.assertLessEqual(state["accounted_bytes"], plan.max_transfer_bytes)
        with self.assertRaisesRegex(ModelError, "approving owner"):
            self.store.discard_partial(plan.digest(), "worker")
        discarded = self.store.discard_partial(plan.digest(), "owner")
        self.assertGreater(discarded["discarded_bytes"], 0)
        self.assertEqual(discarded["accounted_bytes"], state["accounted_bytes"])
        self.assertEqual(discarded["files"][0]["partial_bytes"], 0)

    def test_store_lock_and_private_root_gate_mutation(self):
        self.prepare()
        with self.store._lock():
            with ModelStore(self.root) as other:
                with self.assertRaisesRegex(ModelError, "another model preparation"):
                    other.download(self.plan.digest(), "owner", transport=self.transport)
        self.assertEqual(self.server.requests, [])
        with self.store.connection:
            self.store.connection.execute("UPDATE plans SET status='downloading' WHERE digest=?", (self.plan.digest(),))
        state = self.store.status(self.plan.digest())
        self.assertEqual(state["status"], "paused")
        self.assertEqual(state["activity"], "interrupted_requires_resume")
        unsafe = Path(self.temp.name) / "unsafe"
        unsafe.mkdir(mode=0o755)
        with self.assertRaisesRegex(ModelError, "owner-only"):
            ModelStore(unsafe)


if __name__ == "__main__":
    unittest.main()
