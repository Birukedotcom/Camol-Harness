"""Atomic hosted-worker admission over the existing durable invocation journal.

No second billing ledger: outstanding intents reserve their full ceiling and
terminal receipts replace that ceiling with observed cost. Unknown terminal
charges require reconciliation. The lock is held only while admitting a turn,
never while a provider executes, so independent boxes can run concurrently.
"""

import fcntl
import os
import stat
from pathlib import Path

from .adapter import AdapterError
from .invocations import _publish_once
from .json_contracts import decode_contract
from .schema import canonical_digest, require_identifier, require_non_negative_int
from .usage import UsageRecord, _legacy_receipts, _trusted_receipts, provider_cost_used


class ProviderBudgetError(AdapterError):
    pass


def budget_baseline(state):
    records = _trusted_receipts(state.get("evidence", {}).values())
    legacy, _ = _legacy_receipts(state.get("evidence", {}).values(), records)
    inherited = (state.get("revision") or {}).get("inherited_usage", {})
    return dict(run_id=state["run_id"], records=[record.to_dict() for record in records.values()],
                other_cost=provider_cost_used(state) - sum(r.cost_charge for r in records.values())
                - inherited.get("provider_cost_usd_micros", 0),
                other_task_cost={task_id: provider_cost_used(state, task_id=task_id)
                                 - sum(r.cost_charge for r in records.values() if r.task_id == task_id)
                                 for task_id in state.get("tasks", {})},
                inherited_unknown=inherited.get("unknown_usage", False)
                or any(item["cost_usd_micros"] is None for item in legacy.values()))


def _safe(path, directory=False):
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.getuid() or info.st_mode & 0o022
            or (not directory and info.st_nlink != 1)):
        raise ProviderBudgetError("provider budget storage must be owner-controlled, not linked or writable by others")


def _read(path, allowance):
    _safe(path)
    fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > allowance[0]:
            raise ProviderBudgetError("provider journal exceeds the bounded inspection allowance")
        raw = stream.read(allowance[0] + 1)
    allowance[0] -= len(raw)
    if allowance[0] < 0:
        raise ProviderBudgetError("provider journal exceeds the bounded inspection allowance")
    return decode_contract(raw, max_bytes=32 << 20)


def _receipt(payload, run_id):
    binding = payload["binding"]
    if (set(payload) != {"schema", "schema_version", "binding", "binding_digest", "observed_evidence"}
            or payload["schema"] != "camol.provider_invocation" or type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1 or payload["binding_digest"] != canonical_digest(binding)
            or set(binding) != {"run_id", "task_id", "agent_id", "lease_id", "fence_digest", "packet_sha256", "profile_digest", "turn_number", "workspace"}
            or binding["run_id"] != run_id):
        raise ProviderBudgetError("invalid provider journal identity")
    records = [UsageRecord.from_dict(item["data"]) for item in payload["observed_evidence"]
               if item.get("kind") == "model_usage" and item.get("producer") == "adapter"
               and item.get("epistemic_status") == "OBSERVED"]
    if len(records) != 1:
        raise ProviderBudgetError("provider journal must contain exactly one observed usage receipt")
    record = records[0]
    if any(getattr(record, name) != binding[name] for name in ("run_id", "task_id", "agent_id", "lease_id", "turn_number")):
        raise ProviderBudgetError("provider receipt belongs to another invocation subject")
    return record


def _journals(root, run_id, allowance):
    directory = root / run_id
    if not directory.exists():
        if directory.is_symlink():
            raise ProviderBudgetError("linked provider journal directory")
        return {}
    _safe(directory, True)
    result, count = {}, 0
    for task in directory.iterdir():
        if task.is_symlink():
            raise ProviderBudgetError("linked provider journal directory or policy")
        if not task.is_dir():
            continue
        _safe(task, True)
        for path in task.iterdir():
            count += 1
            if count > 20000:
                raise ProviderBudgetError("too many provider journal entries; inspect before continuing")
            if path.name.endswith(".charge-pending.observed.json") and not path.with_name(path.name.replace(".observed.json", ".json")).exists():
                raise ProviderBudgetError("provider outcome has lost its reservation")
            if not path.name.endswith(".charge-pending.json"):
                continue
            intent = _read(path, allowance)
            reserved = _receipt(intent, run_id)
            if intent["binding"]["task_id"] != task.name:
                raise ProviderBudgetError("provider journal task directory differs from its subject")
            outcome_path = path.with_suffix(".observed.json")
            terminal = outcome_path.exists() or outcome_path.is_symlink()
            observed = _read(outcome_path, allowance) if terminal else intent
            if observed["binding"] != intent["binding"]:
                raise ProviderBudgetError("provider terminal observation changes its subject")
            record = _receipt(observed, run_id)
            if (record.invocation_id != reserved.invocation_id
                    or record.reserved_cost_usd_micros != reserved.reserved_cost_usd_micros):
                raise ProviderBudgetError("provider terminal observation changes its reservation")
            if record.invocation_id in result:
                raise ProviderBudgetError("duplicate provider invocation journals")
            result[record.invocation_id] = (record, terminal)
    return result


def reserve_hosted(journal, *, state_dir, profile, plan_digest, requested_cents, evidence_factory, baselines=()):
    """Return the atomically allocated cents; persist intent before releasing lock.

    Baselines are trusted event projections, never model-supplied context. Every
    revision ancestor must be included by the runner to preserve prior charges.
    Missing/corrupt records deny admission, not an automatic budget reset.
    """
    state_dir = Path(state_dir)
    run_id, task_id = journal.binding["run_id"], journal.binding["task_id"]
    require_identifier(run_id, "budget run")
    require_identifier(task_id, "budget task")
    fd = None
    try:
        _safe(state_dir, True)
        # macOS exposes /var and /tmp as OS aliases. Reject a linked selected
        # state directory, then validate the canonical ancestry used by adapters.
        state_dir = state_dir.resolve()
        for ancestor in state_dir.parents:
            info = ancestor.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}
                    or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
                raise ProviderBudgetError("provider budget state ancestry is not owner/root controlled")
        root = state_dir / "packets"
        _safe(root, True)
        lock = state_dir / "provider-budget.lock"
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
        _safe(lock)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.fstat(fd).st_ino != lock.lstat().st_ino:
            raise ProviderBudgetError("provider budget lock identity changed")
        if journal.path.exists() or journal.path.is_symlink():
            original = journal._read(journal.path)
            previous = journal._read(journal.outcome_path) if journal.outcome_path.exists() else original
            raise AdapterError("provider invocation was already launched; reconcile before retrying",
                               observed_evidence=previous["observed_evidence"])
        _safe(root / run_id, True)
        policy_path = root / run_id / "hosted-budget-policy.json"
        policy = dict(schema="camol.hosted_budget_policy", schema_version=1, run_id=run_id,
                      plan_digest=plan_digest, max_run_usd_cents=profile.max_run_usd_cents)
        try:
            _publish_once(policy_path, policy)
        except FileExistsError:
            if _read(policy_path, [65536]) != policy:
                raise ProviderBudgetError("hosted workers disagree on the frozen run budget or plan identity")
        # The per-task ceiling is also shared across workers assigned that task.
        task_policy = root / run_id / task_id / "hosted-task-budget-policy.json"
        _safe(task_policy.parent, True)
        task_limit = dict(task_id=task_id, max_task_usd_cents=profile.max_task_usd_cents)
        try:
            _publish_once(task_policy, task_limit)
        except FileExistsError:
            if _read(task_policy, [65536]) != task_limit:
                raise ProviderBudgetError("hosted workers disagree on the frozen task budget")
        baseline_by_run = {item["run_id"]: item for item in baselines}
        if len(baseline_by_run) != len(baselines):
            raise ProviderBudgetError("duplicate provider budget baseline runs")
        baseline_by_run.setdefault(run_id, dict(records=[], other_cost=0, inherited_unknown=False))
        total = task_total = 0
        allowance = [32 << 20]
        for subject, baseline in baseline_by_run.items():
            require_identifier(subject, "budget ancestor")
            require_non_negative_int(baseline["other_cost"], "baseline provider cost")
            for amount in baseline.get("other_task_cost", {}).values():
                require_non_negative_int(amount, "baseline task provider cost")
            if baseline["inherited_unknown"]:
                raise ProviderBudgetError("inherited unknown provider usage requires reconciliation")
            records = _journals(root, subject, allowance)
            for value in baseline["records"]:
                observed = UsageRecord.from_dict(value)
                if observed.run_id != subject:
                    raise ProviderBudgetError("baseline usage belongs to another run")
                previous = records.get(observed.invocation_id)
                if previous and previous[0] != observed:
                    raise ProviderBudgetError("event usage differs from its durable provider journal")
                records[observed.invocation_id] = (observed, True)
            total += baseline["other_cost"]
            if subject == run_id:
                task_total += baseline.get("other_task_cost", {}).get(task_id, 0)
            for record, terminal in records.values():
                if record.cost_usd_micros is None and record.reserved_cost_usd_micros and (terminal or subject != run_id):
                    raise ProviderBudgetError("unknown provider charge requires reconciliation before another hosted launch")
                total += record.cost_charge
                if subject == run_id and record.task_id == task_id:
                    task_total += record.cost_charge
        ceiling = min(requested_cents, profile.max_turn_usd_cents,
                      (profile.max_run_usd_cents * 10000 - total) // 10000,
                      (profile.max_task_usd_cents * 10000 - task_total) // 10000)
        if ceiling <= 0:
            raise ProviderBudgetError("shared provider budget is exhausted or held by outstanding invocations")
        journal.reserve(evidence_factory(ceiling))
        return ceiling
    except ProviderBudgetError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProviderBudgetError("provider budget cannot be safely inspected or locked; no hosted request launched") from error
    finally:
        if fd is not None:
            os.close(fd)
