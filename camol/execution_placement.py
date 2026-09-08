"""Execution-backed placement for the local runner, separate from capacity labels."""

import platform

from .capacity import _attributes
from .probes import Probe


class ExecutionPlacementError(ValueError):
    pass


def required_placement(task):
    return _attributes(task.get("resource_requirements", {}).get("placement", {}))


def local_attributes(trust_tier):
    # No environment-variable region guesses or self-declared capacity labels.
    # These are host/runtime observations, not hardware attestation.
    return dict(os=platform.system().lower() or None,
                architecture=platform.machine().lower() or None,
                locality="local", trust_tier=trust_tier, region=None)


def mismatches(required, observed):
    return sorted(key for key, value in required.items() if observed.get(key) != value)


def describe(observed):
    return ", ".join("{}={}".format(key, observed.get(key) or "unproven")
                     for key in ("os", "architecture", "locality", "trust_tier", "region"))


class LocalExecutionPlacementProbe(Probe):
    probe_id = "execution.placement"
    kind = "runtime"
    VERSION = 1

    def __init__(self, placement, trust_tier):
        self.placement = _attributes(placement)
        self.trust_tier = trust_tier

    def config(self, context):
        return dict(required=dict(self.placement), executor="local", sandbox_trust_tier=self.trust_tier)

    def observe(self, context):
        actual = local_attributes(self.trust_tier)
        missing = mismatches(self.placement, actual)
        facts = dict(required=dict(self.placement), observed=actual, executor="local",
                     trust_tier_basis=("frozen_policy_and_separate_required_sandbox_probe"
                                       if self.trust_tier is not None else "unproven"))
        if missing:
            return self.red(context, "execution placement mismatch or unproven fields: {}; observed {}".format(
                ", ".join(missing), describe(actual)), reason="POLICY_DENIED",
                wake="use execution-backed target placement or review an amended plan; capacity labels are not placement proof",
                missing=["execution-backed " + key for key in missing], facts=facts)
        return self.green(context, "execution placement matches: " + describe(actual), facts=facts)


def require_local_placement(task, bundle):
    """A legacy/misbound receipt must not authorize a new local launch."""
    required = required_placement(task)
    if not required:
        return
    probe = LocalExecutionPlacementProbe(required, bundle.sandbox_policy.trust_tier)
    expected = probe.definition_digest(None)
    requirements = [item for item in bundle.probe_policy.required_probes if item.probe_id == probe.probe_id]
    results = [item for item in bundle.receipt.probes if item.probe_id == probe.probe_id]
    if (len(requirements) != 1 or len(results) != 1
            or requirements[0].definition_digest != expected or requirements[0].kind != probe.kind
            or requirements[0].target_bound is not True
            or results[0].definition_digest != expected or results[0].kind != probe.kind
            or results[0].target_id != bundle.binding.target_id or results[0].status != "green"):
        raise ExecutionPlacementError("execution-backed placement proof is absent or differs from the frozen task; fresh admission is required")
    actual = local_attributes(bundle.sandbox_policy.trust_tier)
    missing = mismatches(required, actual)
    if missing:
        raise ExecutionPlacementError("execution placement changed or is unproven before launch: " + ", ".join(missing))
