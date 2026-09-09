"""Shared, fail-closed capacity accounting across independent Camol runs.

This broker is an admission ledger, not an OS resource limiter or provider proxy.
Provider-request quotas require a request-observing adapter; CLI session estimates
are explicitly labeled and never claim physical provider RPM/TPM enforcement.
"""

import json
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .schema import canonical_digest, parse_timestamp, require_digest, require_identifier, require_timestamp
from .store import SQLiteEventStore


class CapacityError(ValueError):
    pass


TARGET_RESOURCES = frozenset({"slots", "cpu_millis", "memory_bytes", "gpu_millis", "vram_bytes", "disk_bytes"})
TASK_RESOURCES = TARGET_RESOURCES - {"slots"}
PLACEMENT_KEYS = frozenset({"os", "architecture", "region", "trust_tier", "locality"})
RATE_SCOPES = frozenset({"harness_turn_estimate", "provider_request"})


def _fields(value, names, label):
    if not isinstance(value, dict) or set(value) != set(names):
        raise CapacityError(label + " has missing or unknown fields")


def _number(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise CapacityError(label + " must be an integer >= " + str(minimum))
    return value


def _names(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value) or len(value) != len(set(value)):
        raise CapacityError(label + " must be a unique array")
    for item in value:
        require_identifier(item, label)
    return value


def _attributes(value):
    if not isinstance(value, dict) or set(value) - PLACEMENT_KEYS:
        raise CapacityError("capacity attributes have unknown placement keys")
    for name, item in value.items():
        require_identifier(item, "capacity " + name)
    return deepcopy(value)


def _resources(value, kind):
    expected = TARGET_RESOURCES if kind == "target" else {"concurrency"}
    _fields(value, expected, "capacity resources")
    for name, amount in value.items():
        _number(amount, name)
    return deepcopy(value)


def validate_capacity_policy(value):
    _fields(value, {"namespace", "allocation", "reservation_ttl_seconds", "allow_owner_declared_supply", "rate_scope"}, "capacity policy")
    require_identifier(value["namespace"], "capacity namespace")
    if value["allocation"] != "saturate_connected" or value["rate_scope"] not in RATE_SCOPES:
        raise CapacityError("unsupported allocation or provider rate scope")
    ttl = _number(value["reservation_ttl_seconds"], "reservation_ttl_seconds", 1)
    if ttl > 3600 or type(value["allow_owner_declared_supply"]) is not bool:
        raise CapacityError("capacity TTL must be <=3600 and supply trust must be explicit")
    return deepcopy(value)


def validate_pool_bindings(value):
    _fields(value, {"target", "runtime", "provider"}, "capacity pool bindings")
    for kind, identity in value.items():
        if kind == "provider" and identity is None:
            continue
        require_identifier(identity, kind + " pool")
    identifiers = [item for item in value.values() if item is not None]
    if len(identifiers) != len(set(identifiers)):
        raise CapacityError("target, runtime, and provider pools have separate identities")
    return deepcopy(value)


def validate_resource_requirements(value):
    _fields(value, set(TASK_RESOURCES) | {"placement"}, "task resource requirements")
    for name in TASK_RESOURCES:
        _number(value[name], name)
    _attributes(value["placement"])
    return deepcopy(value)


def validate_supply(value):
    _fields(value, {"schema", "schema_version", "namespace", "pool_id", "kind", "limits", "outside_usage", "capabilities",
                    "attributes", "rate_limit", "status", "provenance", "source", "observed_at", "expires_at"}, "capacity supply")
    if value["schema"] != "camol.capacity_supply" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise CapacityError("unsupported capacity supply schema")
    for name in ("namespace", "pool_id", "source"):
        require_identifier(value[name], name)
    kind = value["kind"]
    if kind not in {"target", "runtime", "provider"}:
        raise CapacityError("unknown pool kind")
    _resources(value["limits"], kind)
    _resources(value["outside_usage"], kind)
    if any(value["outside_usage"][name] > amount for name, amount in value["limits"].items()):
        raise CapacityError("outside use cannot exceed the supply limit")
    _names(value["capabilities"], "capacity capability")
    _attributes(value["attributes"])
    if value["status"] not in {"ready", "unknown", "unavailable"} or value["provenance"] not in {"observed", "owner_declared"}:
        raise CapacityError("capacity readiness/provenance is invalid")
    for name in ("observed_at", "expires_at"):
        require_timestamp(value[name], name)
    if parse_timestamp(value["observed_at"], "observation") >= parse_timestamp(value["expires_at"], "expiry"):
        raise CapacityError("capacity supply must expire after observation")
    rate = value["rate_limit"]
    if rate is not None:
        _fields(rate, {"window_seconds", "max_requests", "max_tokens", "scope"}, "capacity rate limit")
        if kind != "provider" or rate["scope"] not in RATE_SCOPES:
            raise CapacityError("rate windows belong to an explicit provider scope")
        if _number(rate["window_seconds"], "window seconds", 1) > 86400:
            raise CapacityError("capacity rate window exceeds one day")
        _number(rate["max_requests"], "rate requests", 1)
        _number(rate["max_tokens"], "rate tokens", 1)
    return deepcopy(value)


def validate_request(value):
    _fields(value, {"schema", "schema_version", "namespace", "controller_id", "run_id", "plan_digest", "task_id", "agent_id", "attempt",
                    "policy", "needs", "capabilities", "placement"}, "capacity request")
    if value["schema"] != "camol.capacity_request" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise CapacityError("unsupported capacity request schema")
    for name in ("namespace", "controller_id", "run_id", "task_id", "agent_id"):
        require_identifier(value[name], name)
    require_digest(value["plan_digest"], "capacity plan")
    _number(value["attempt"], "capacity attempt", 1)
    validate_capacity_policy(value["policy"])
    if value["namespace"] != value["policy"]["namespace"]:
        raise CapacityError("capacity request namespace differs from policy")
    _names(value["capabilities"], "request capabilities")
    _attributes(value["placement"])
    if not isinstance(value["needs"], list) or not 2 <= len(value["needs"]) <= 3:
        raise CapacityError("capacity request requires target/runtime and optional provider")
    kinds, pools = set(), set()
    for item in value["needs"]:
        _fields(item, {"pool_id", "kind", "resources"}, "resource need")
        require_identifier(item["pool_id"], "pool ID")
        if item["kind"] not in {"target", "runtime", "provider"} or item["kind"] in kinds or item["pool_id"] in pools:
            raise CapacityError("capacity need has duplicate/invalid pool identity")
        kinds.add(item["kind"])
        pools.add(item["pool_id"])
        _resources(item["resources"], item["kind"])
    if not {"target", "runtime"} <= kinds:
        raise CapacityError("capacity request lacks target or runtime")
    return deepcopy(value)


class CapacityBroker:
    """Owner-only shared SQLite broker; callers provide actual observed supply."""

    def __init__(self, path, *, clock=lambda: datetime.now(timezone.utc), read_only=False):
        self.path = Path(path)
        self.clock = clock
        self.read_only = read_only
        if read_only:
            if self.path.is_symlink() or self.path.parent.is_symlink() or not self.path.is_file():
                raise CapacityError("read-only capacity inspection requires an existing real database")
            self.connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
            self.connection.row_factory = sqlite3.Row
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink():
            raise CapacityError("capacity directory cannot be a symlink")
        SQLiteEventStore._secure_file(self.path, create=True)
        self.connection = sqlite3.connect(str(self.path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS capacity_pools(namespace TEXT,pool_id TEXT,body TEXT,PRIMARY KEY(namespace,pool_id));
            CREATE TABLE IF NOT EXISTS capacity_queue(seq INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT UNIQUE,namespace TEXT,run_key TEXT,body TEXT,status TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS capacity_reservations(reservation_id TEXT PRIMARY KEY,request_id TEXT,namespace TEXT,body TEXT,status TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS capacity_grants(seq INTEGER PRIMARY KEY AUTOINCREMENT,run_key TEXT);
            CREATE TABLE IF NOT EXISTS capacity_calls(call_id TEXT PRIMARY KEY,reservation_id TEXT,pool_id TEXT,namespace TEXT,body TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS capacity_audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,occurred_at TEXT,body TEXT);
        """)
        self._harden()
        self.connection.commit()

    def close(self):
        self.connection.close()

    def _now(self):
        value = self.clock()
        if value.tzinfo is None:
            raise CapacityError("broker clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _harden(self):
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            SQLiteEventStore._secure_file(path, create=False)

    @contextmanager
    def _transaction(self):
        if self.read_only:
            raise CapacityError("capacity inspection is read-only")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self._harden()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    @staticmethod
    def _encode(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _audit(self, kind, value):
        self.connection.execute("INSERT INTO capacity_audit(kind,occurred_at,body) VALUES(?,?,?)", (kind, self._now().isoformat(timespec="microseconds"), self._encode(value)))

    def publish(self, supply):
        value = validate_supply(supply)
        if parse_timestamp(value["observed_at"], "observed") > self._now():
            raise CapacityError("future-dated capacity is not observed supply")
        with self._transaction():
            row = self.connection.execute("SELECT body FROM capacity_pools WHERE namespace=? AND pool_id=?", (value["namespace"], value["pool_id"])).fetchone()
            if row:
                previous = json.loads(row[0])
                old_time = parse_timestamp(previous["observed_at"], "old observation")
                new_time = parse_timestamp(value["observed_at"], "observation")
                if new_time < old_time or (new_time == old_time and previous != value) or previous["kind"] != value["kind"]:
                    raise CapacityError("capacity observation regressed or changed at the same identity/time")
                if previous["rate_limit"] is not None and value["rate_limit"] is not None and any(previous["rate_limit"][name] != value["rate_limit"][name] for name in ("scope", "window_seconds")):
                    raise CapacityError("changing rate-window semantics requires a new pool identity")
            self.connection.execute("INSERT OR REPLACE INTO capacity_pools VALUES(?,?,?)", (value["namespace"], value["pool_id"], self._encode(value)))
            self._audit("SUPPLY_OBSERVED", value)
        return canonical_digest(value)

    def inventory(self, namespace):
        require_identifier(namespace, "namespace")
        now = self._now()
        values = []
        for row in self.connection.execute("SELECT body FROM capacity_pools WHERE namespace=? ORDER BY pool_id", (namespace,)):
            value = json.loads(row[0])
            values.append({"supply": value, "supply_digest": canonical_digest(value), "fresh": parse_timestamp(value["observed_at"], "observed") <= now < parse_timestamp(value["expires_at"], "expires")})
        return values

    def change_cursor(self):
        """Read the shared monotonic mutation cursor without changing state."""
        return self.connection.execute("SELECT COALESCE(MAX(seq),0) FROM capacity_audit").fetchone()[0]

    def _expire(self, now):
        suspects = self.connection.execute("UPDATE capacity_reservations SET status='suspect' WHERE status='active' AND expires<=?", (now.timestamp(),)).rowcount
        # A dead queued controller cannot indefinitely starve live runs. This
        # expiry never releases granted/suspect execution capacity.
        queues = self.connection.execute("UPDATE capacity_queue SET status='expired' WHERE status='waiting' AND updated<=?", (now.timestamp() - 3600,)).rowcount
        if suspects or queues:
            self._audit("CAPACITY_EXPIRED", dict(at=now.isoformat(), suspect_reservations=suspects, expired_queue_entries=queues))

    def _assess(self, request, *, exclude=None):
        now = self._now()
        snapshots, reasons, remaining = {}, [], {}
        held = self.connection.execute("SELECT reservation_id,body FROM capacity_reservations WHERE namespace=? AND status IN ('active','suspect')", (request["namespace"],)).fetchall()
        for need in request["needs"]:
            pool = need["pool_id"]
            row = self.connection.execute("SELECT body FROM capacity_pools WHERE namespace=? AND pool_id=?", (request["namespace"], pool)).fetchone()
            if not row:
                reasons.append({"pool_id": pool, "reason": "SUPPLY_UNKNOWN"})
                continue
            supply = json.loads(row[0])
            snapshots[pool] = supply
            if supply["kind"] != need["kind"]:
                reasons.append({"pool_id": pool, "reason": "POOL_KIND_MISMATCH"})
                continue
            if supply["status"] != "ready" or not (parse_timestamp(supply["observed_at"], "observed") <= now < parse_timestamp(supply["expires_at"], "expires")):
                reasons.append({"pool_id": pool, "reason": "SUPPLY_STALE_OR_UNREADY"})
            if supply["provenance"] == "owner_declared" and not request["policy"]["allow_owner_declared_supply"]:
                reasons.append({"pool_id": pool, "reason": "SUPPLY_UNPROVEN"})
            if need["kind"] in {"target", "runtime"} and set(request["capabilities"]) - set(supply["capabilities"]):
                reasons.append({"pool_id": pool, "reason": "CAPABILITY_MISMATCH"})
            if need["kind"] == "target" and any(supply["attributes"].get(key) != value for key, value in request["placement"].items()):
                reasons.append({"pool_id": pool, "reason": "PLACEMENT_MISMATCH"})
            if need["kind"] == "provider" and (supply["rate_limit"] is None or supply["rate_limit"]["scope"] != request["policy"]["rate_scope"]):
                reasons.append({"pool_id": pool, "reason": "RATE_SCOPE_UNSUPPORTED"})
            used = {name: supply["outside_usage"][name] for name in need["resources"]}
            for row in held:
                if row["reservation_id"] == exclude:
                    continue
                for existing in json.loads(row["body"])["request"]["needs"]:
                    if existing["pool_id"] == pool:
                        for name, amount in existing["resources"].items():
                            used[name] += amount
            remaining[pool] = {name: max(0, supply["limits"][name] - used[name]) for name in used}
            for name, amount in need["resources"].items():
                if amount > supply["limits"][name]:
                    reasons.append({"pool_id": pool, "reason": "REQUIREMENT_EXCEEDS_SUPPLY", "resource": name})
                elif amount > supply["limits"][name] - used[name]:
                    reasons.append({"pool_id": pool, "reason": "CAPACITY_EXHAUSTED", "resource": name})
        return snapshots, reasons, remaining

    def reserve(self, request, *, local_reservation_id):
        value = validate_request(request)
        require_identifier(local_reservation_id, "local reservation")
        request_id = canonical_digest(value)
        run_key = value["controller_id"] + ":" + value["run_id"]
        now = self._now()
        with self._transaction():
            self._expire(now)
            previous = self.connection.execute("SELECT body,status FROM capacity_reservations WHERE request_id=? AND status IN ('active','suspect') ORDER BY rowid DESC LIMIT 1", (request_id,)).fetchone()
            if previous and previous["status"] in {"active", "suspect"}:
                receipt = json.loads(previous["body"])
                if previous["status"] == "active" and receipt["local_reservation_id"] == local_reservation_id:
                    return {"status": "granted", "reservation": receipt, "reasons": [], "remaining": {}}
                return {"status": "waiting", "reservation": None, "reasons": [{"reason": "RESERVATION_RECONCILIATION_REQUIRED"}], "remaining": {}}
            subject = tuple(value[name] for name in ("controller_id", "run_id", "task_id", "attempt"))
            for held in self.connection.execute("SELECT body FROM capacity_reservations WHERE namespace=? AND status IN ('active','suspect')", (value["namespace"],)):
                other = json.loads(held["body"])["request"]
                if tuple(other[name] for name in ("controller_id", "run_id", "task_id", "attempt")) == subject:
                    return {"status": "waiting", "reservation": None, "reasons": [{"reason": "TASK_ALREADY_RESERVED"}], "remaining": {}}
            self.connection.execute("INSERT INTO capacity_queue(request_id,namespace,run_key,body,status,updated) VALUES(?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET status='waiting',updated=excluded.updated",
                                    (request_id, value["namespace"], run_key, self._encode(value), "waiting", now.timestamp()))
            snapshots, reasons, remaining = self._assess(value)
            desired_pools = {item["pool_id"] for item in value["needs"]}
            # Round-robin among runs with queued work, FIFO within a run. A
            # resource-starved oldest request blocks younger conflicting work,
            # but does not block work on independent pools.
            queue = self.connection.execute("SELECT q.*,COALESCE((SELECT MAX(g.seq) FROM capacity_grants g WHERE g.run_key=q.run_key),0) AS last_grant FROM capacity_queue q WHERE q.namespace=? AND q.status='waiting' ORDER BY last_grant,q.seq", (value["namespace"],)).fetchall()
            wake_at = None
            for row in queue:
                if row["request_id"] == request_id:
                    break
                other = json.loads(row["body"])
                if tuple(other[name] for name in ("controller_id", "run_id", "task_id", "attempt")) == subject:
                    continue
                if desired_pools & {item["pool_id"] for item in other["needs"]}:
                    _, failures, _ = self._assess(other)
                    if not failures or all(item["reason"] == "CAPACITY_EXHAUSTED" for item in failures):
                        reasons.append({"reason": "FAIR_QUEUE_WAIT", "ahead_request_id": row["request_id"]})
                        wake_at = datetime.fromtimestamp(row["updated"] + 3600, timezone.utc).isoformat()
                        break
            if reasons:
                return {"status": "waiting", "reservation": None, "reasons": reasons, "remaining": remaining,
                        "wake_at": wake_at,
                        "snapshot_digests": {key: canonical_digest(item) for key, item in snapshots.items()}}
            expiry = min([now + timedelta(seconds=value["policy"]["reservation_ttl_seconds"])] + [parse_timestamp(item["expires_at"], "supply expiry") for item in snapshots.values()])
            receipt = {"schema": "camol.global_reservation", "schema_version": 1, "reservation_id": "capacity-" + uuid4().hex,
                       "request_id": request_id, "request": value, "local_reservation_id": local_reservation_id,
                       "snapshots": snapshots, "reserved_at": now.isoformat(timespec="microseconds"), "expires_at": expiry.isoformat(timespec="microseconds")}
            self.connection.execute("INSERT INTO capacity_reservations VALUES(?,?,?,?,?,?)", (receipt["reservation_id"], request_id, value["namespace"], self._encode(receipt), "active", expiry.timestamp()))
            self.connection.execute("UPDATE capacity_queue SET status='granted' WHERE request_id=?", (request_id,))
            for row in queue:
                other = json.loads(row["body"])
                if row["request_id"] != request_id and tuple(other[name] for name in ("controller_id", "run_id", "task_id", "attempt")) == subject:
                    self.connection.execute("UPDATE capacity_queue SET status='cancelled' WHERE request_id=?", (row["request_id"],))
            self.connection.execute("INSERT INTO capacity_grants(run_key) VALUES(?)", (run_key,))
            self._audit("CAPACITY_RESERVED", receipt)
            return {"status": "granted", "reservation": receipt, "reasons": [], "remaining": remaining}

    def renew(self, reservation_id, *, controller_id, run_id):
        now = self._now()
        with self._transaction():
            self._expire(now)
            row = self.connection.execute("SELECT body,status FROM capacity_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if not row or row["status"] != "active":
                raise CapacityError("expired/missing capacity requires reconciliation, not renewal")
            value = json.loads(row["body"])
            request = value["request"]
            if (request["controller_id"], request["run_id"]) != (controller_id, run_id):
                raise CapacityError("capacity renewal belongs to another controller/run")
            snapshots, reasons, _ = self._assess(request, exclude=reservation_id)
            if reasons:
                raise CapacityError("capacity renewal denied: " + ",".join(sorted({item["reason"] for item in reasons})))
            expiry = min([now + timedelta(seconds=request["policy"]["reservation_ttl_seconds"])] + [parse_timestamp(item["expires_at"], "supply expiry") for item in snapshots.values()])
            value.update(snapshots=snapshots, reserved_at=now.isoformat(timespec="microseconds"), expires_at=expiry.isoformat(timespec="microseconds"))
            self.connection.execute("UPDATE capacity_reservations SET body=?,expires=? WHERE reservation_id=?", (self._encode(value), expiry.timestamp(), reservation_id))
            self._audit("CAPACITY_RENEWED", value)
            return value

    def release(self, reservation_id, *, controller_id, run_id, reconciliation_digest):
        require_digest(reconciliation_digest, "capacity reconciliation")
        with self._transaction():
            row = self.connection.execute("SELECT body,status FROM capacity_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if not row:
                raise CapacityError("unknown global reservation")
            value = json.loads(row["body"])
            if (value["request"]["controller_id"], value["request"]["run_id"]) != (controller_id, run_id):
                raise CapacityError("capacity release belongs to another controller/run")
            if row["status"] == "released":
                if value.get("reconciliation_digest") != reconciliation_digest:
                    raise CapacityError("released capacity cannot replace its reconciliation proof")
                return value
            value["reconciliation_digest"] = reconciliation_digest
            self.connection.execute("UPDATE capacity_reservations SET status='released',body=? WHERE reservation_id=?", (self._encode(value), reservation_id))
            self._audit("CAPACITY_RELEASED", value)
            return value

    def reserve_call(self, reservation_id, *, call_id, max_requests, max_tokens, scope):
        require_identifier(call_id, "capacity call ID")
        _number(max_requests, "maximum call requests", 1)
        _number(max_tokens, "maximum call tokens", 1)
        if scope not in RATE_SCOPES:
            raise CapacityError("unknown capacity call scope")
        now = self._now()
        with self._transaction():
            self._expire(now)
            row = self.connection.execute("SELECT body,status FROM capacity_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if not row or row["status"] != "active":
                raise CapacityError("model call requires live global capacity")
            reservation = json.loads(row["body"])
            request = reservation["request"]
            needs = [item for item in request["needs"] if item["kind"] == "provider"]
            if not needs:
                raise CapacityError("model call has no approved provider pool")
            pool = needs[0]["pool_id"]
            existing = self.connection.execute("SELECT body FROM capacity_calls WHERE call_id=?", (call_id,)).fetchone()
            if existing:
                value = json.loads(existing[0])
                if any(value[name] != expected for name, expected in {"reservation_id": reservation_id, "max_requests": max_requests, "max_tokens": max_tokens, "scope": scope}.items()):
                    raise CapacityError("call identity cannot be reused with a different charge")
                if parse_timestamp(value["expires_at"], "call debit expiry") <= now:
                    return {"status": "denied", "call": None, "wake_at": None, "reasons": [{"reason": "EXPIRED_CALL_ID_REQUIRES_NEW_INVOCATION"}]}
                return {"status": "granted", "call": value, "wake_at": None}
            snapshots, reasons, _ = self._assess(request, exclude=reservation_id)
            if reasons:
                return {"status": "waiting", "call": None, "wake_at": None, "reasons": reasons}
            rate = snapshots[pool]["rate_limit"]
            if rate["scope"] != scope:
                raise CapacityError("provider rate scope does not match the execution adapter")
            # Rolling windows avoid an artificial reset-boundary burst. Every
            # debit survives cancellation and unknown provider outcomes.
            active = self.connection.execute("SELECT body,expires FROM capacity_calls WHERE namespace=? AND pool_id=? AND expires>?", (request["namespace"], pool, now.timestamp())).fetchall()
            values = [json.loads(item["body"]) for item in active]
            if max_requests > rate["max_requests"] or max_tokens > rate["max_tokens"]:
                return {"status": "denied", "call": None, "wake_at": None, "reasons": [{"reason": "CALL_EXCEEDS_WINDOW_LIMIT"}]}
            if sum(item["max_requests"] for item in values) + max_requests > rate["max_requests"] or sum(item["max_tokens"] for item in values) + max_tokens > rate["max_tokens"]:
                wake = datetime.fromtimestamp(min(item["expires"] for item in active), timezone.utc).isoformat(timespec="microseconds")
                return {"status": "waiting", "call": None, "wake_at": wake, "reasons": [{"reason": "RATE_WINDOW_EXHAUSTED"}]}
            expiry = now + timedelta(seconds=rate["window_seconds"])
            value = {"schema": "camol.capacity_call", "schema_version": 1, "call_id": call_id, "reservation_id": reservation_id,
                     "pool_id": pool, "namespace": request["namespace"], "max_requests": max_requests, "max_tokens": max_tokens,
                     "scope": scope, "reserved_at": now.isoformat(timespec="microseconds"), "expires_at": expiry.isoformat(timespec="microseconds"),
                     "supply_digest": canonical_digest(snapshots[pool]), "supply": snapshots[pool]}
            self.connection.execute("INSERT INTO capacity_calls VALUES(?,?,?,?,?,?)", (call_id, reservation_id, pool, request["namespace"], self._encode(value), expiry.timestamp()))
            self._audit("CALL_RESERVED", value)
            return {"status": "granted", "call": value, "wake_at": None}

    def reservations(self, *, namespace=None):
        if self.read_only:
            rows = self.connection.execute("SELECT body,status,expires FROM capacity_reservations" + (" WHERE namespace=?" if namespace else ""), (namespace,) if namespace else ()).fetchall()
            now = self._now().timestamp()
            return [{"reservation": json.loads(row["body"]), "status": "suspect" if row["status"] == "active" and row["expires"] <= now else row["status"]} for row in rows]
        with self._transaction():
            self._expire(self._now())
            rows = self.connection.execute("SELECT body,status FROM capacity_reservations" + (" WHERE namespace=?" if namespace else ""), (namespace,) if namespace else ()).fetchall()
            return [{"reservation": json.loads(row["body"]), "status": row["status"]} for row in rows]

    def restore_for_verification(self, reservation_id, *, controller_id, run_id, reconciliation_digest):
        """Keep an existing held reservation after proven process-stop recovery.

        A suspect reservation never became free, so this cannot overbook a pool.
        This owner-control method is not a worker heartbeat or a new model lease.
        """
        require_digest(reconciliation_digest, "verification reconciliation")
        with self._transaction():
            row = self.connection.execute("SELECT body,status FROM capacity_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if not row or row["status"] not in {"active", "suspect"}:
                raise CapacityError("verification recovery requires existing held capacity")
            value = json.loads(row["body"])
            if (value["request"]["controller_id"], value["request"]["run_id"]) != (controller_id, run_id):
                raise CapacityError("verification recovery belongs to another controller/run")
            snapshots, reasons, _ = self._assess(value["request"], exclude=reservation_id)
            if reasons:
                raise CapacityError("verification capacity remains unavailable")
            now = self._now()
            expiry = min([now + timedelta(seconds=value["request"]["policy"]["reservation_ttl_seconds"])] + [parse_timestamp(item["expires_at"], "supply expiry") for item in snapshots.values()])
            value.update(snapshots=snapshots, reserved_at=now.isoformat(timespec="microseconds"), expires_at=expiry.isoformat(timespec="microseconds"))
            self.connection.execute("UPDATE capacity_reservations SET status='active',body=?,expires=? WHERE reservation_id=?", (self._encode(value), expiry.timestamp(), reservation_id))
            self._audit("VERIFICATION_CAPACITY_RECONCILED", {"reservation": value, "reconciliation_digest": reconciliation_digest})
            return value
