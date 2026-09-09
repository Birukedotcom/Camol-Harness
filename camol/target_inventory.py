"""Offline provider inventory decoding; never discovery authentication or admission."""

import re
from collections import Counter
from copy import deepcopy
from urllib.parse import urlsplit

from .json_contracts import decode_contract
from .probes import Redactor
from .schema import canonical_digest, canonical_json_bytes, SchemaError
from .targets import TargetError, _text


MAX_BYTES = 8 << 20
MAX_RECORDS = 4096
FORMATS = ("gcp-compute-v1-aggregated", "gcloud-compute-json-v1")
STATUSES = frozenset({"PROVISIONING", "STAGING", "RUNNING", "STOPPING", "SUSPENDING",
                      "SUSPENDED", "TERMINATED", "REPAIRING", "PENDING_STOP"})
SEGMENT = re.compile(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?\Z")
PROJECT = re.compile(r"(?:[a-z][a-z0-9-]{4,61}[a-z0-9]|[0-9]{1,20})\Z")


def capabilities():
    return dict(schema="camol.inventory_decoders", schema_version=1,
                formats=list(FORMATS), max_bytes=MAX_BYTES, max_records=MAX_RECORDS,
                capabilities=["provider_identity", "reported_instance_status"],
                authenticated_discovery=False, task_readiness=False, execution=False)


def _segment(value):
    if not isinstance(value, str) or SEGMENT.fullmatch(value) is None:
        raise TargetError("invalid_resource_identity")
    return value


def _path(value):
    """Accept documented v1 URLs/relative references, not arbitrary URL tails."""
    if not isinstance(value, str) or len(value) > 1024 or not value.isprintable():
        raise TargetError("invalid_resource_reference")
    if value.startswith("https://"):
        try:
            parsed = urlsplit(value)
        except ValueError as error:
            raise TargetError("invalid_resource_reference") from error
        if parsed.netloc not in {"www.googleapis.com", "compute.googleapis.com"} or parsed.query or parsed.fragment:
            raise TargetError("invalid_resource_reference")
        if not parsed.path.startswith("/compute/v1/"):
            raise TargetError("unsupported_resource_reference")
        value = parsed.path[len("/compute/v1/"):]
    if not value.startswith("projects/"):
        raise TargetError("invalid_resource_reference")
    parts = value.split("/")
    if len(parts) not in {4, 6} or parts[2] != "zones":
        raise TargetError("invalid_resource_reference")
    if PROJECT.fullmatch(parts[1]) is None:
        raise TargetError("invalid_resource_reference")
    _segment(parts[3])
    if len(parts) == 6 and (parts[4] != "instances" or not _segment(parts[5])):
        raise TargetError("invalid_resource_reference")
    return parts


def _instance(value, *, account, project, scope):
    if not isinstance(value, dict):
        raise TargetError("instance_not_object")
    if "kind" in value and value["kind"] != "compute#instance":
        raise TargetError("unsupported_instance_kind")
    identifier = value.get("id")
    if type(identifier) is int:
        identifier = str(identifier)
    if (not isinstance(identifier, str) or re.fullmatch(r"[1-9][0-9]{0,19}", identifier) is None
            or int(identifier) > 2**64 - 1):
        raise TargetError("missing_or_invalid_resource_id")
    name = _segment(value.get("name"))
    zone = value.get("zone")
    if isinstance(zone, str) and SEGMENT.fullmatch(zone):
        zone = _segment(zone)  # gcloud's flattened presentation.
    else:
        parts = _path(zone)
        if len(parts) != 4 or parts[1] != project:
            raise TargetError("resource_project_or_zone_conflict")
        zone = parts[3]
    if scope is not None and scope != "zones/" + zone:
        raise TargetError("aggregated_scope_conflict")
    if "selfLink" in value:
        parts = _path(value["selfLink"])
        if parts != ["projects", project, "zones", zone, "instances", name]:
            raise TargetError("resource_self_link_conflict")
    status = value.get("status")
    known_status = isinstance(status, str) and status in STATUSES
    provider = dict(kind="gcp", account=account, project=project, location=zone,
                    resource_id=identifier, resource_name=name)
    # Provider dumps can carry metadata, startup scripts, encryption keys, IPs,
    # account scopes or unexpected future fields. None is projected into output.
    if Redactor().value(provider) != provider:
        raise TargetError("protected_identity")
    return dict(provider=provider, reported_status=status if known_status else None,
                status_coverage="known" if known_status else ("missing" if status is None else "unrecognized"),
                authenticated=False, readiness="unproven", execution_authority=False,
                deletion_authority=False)


def normalize_inventory(value, *, format, account, project):
    """Decode one bounded imported page/dump; unknown optional fields are ignored.

    The caller names its account/project scope. Imported JSON cannot prove that
    scope, freshness, complete enumeration, or that a machine belongs to Camol.
    Invalid identities quarantine that row; ambiguous identities quarantine all
    colliding rows rather than making their order select a machine.
    """
    if format not in FORMATS:
        raise TargetError("unsupported_inventory_decoder")
    _text(account, "inventory account scope")
    if not isinstance(project, str) or PROJECT.fullmatch(project) is None:
        raise TargetError("invalid_inventory_project_scope")
    if Redactor().value(dict(account=account, project=project)) != dict(account=account, project=project):
        raise TargetError("protected_inventory_scope")
    try:
        encoded = canonical_json_bytes(value)
        value = decode_contract(encoded, max_bytes=MAX_BYTES)
    except (SchemaError, ValueError, TypeError, RecursionError) as error:
        raise TargetError("invalid_or_oversized_inventory_json") from error
    rows, issues = [], []
    more_pages, warnings, unreachable = False, 0, 0
    if format == "gcloud-compute-json-v1":
        if not isinstance(value, list):
            raise TargetError("gcloud_inventory_requires_array")
        rows = [(None, item) for item in value]
    else:
        if not isinstance(value, dict) or value.get("kind") != "compute#instanceAggregatedList":
            raise TargetError("aggregated_inventory_requires_v1_kind")
        token = value.get("nextPageToken")
        if token is not None and not isinstance(token, str):
            raise TargetError("invalid_inventory_pagination")
        more_pages = bool(token)
        missing = value.get("unreachables", [])
        if not isinstance(missing, list):
            raise TargetError("invalid_inventory_unreachables")
        unreachable = len(missing)
        warnings += int("warning" in value)
        scopes = value.get("items", {})
        if not isinstance(scopes, dict) or len(scopes) > MAX_RECORDS:
            raise TargetError("invalid_inventory_scopes")
        for index, (scope, group) in enumerate(sorted(scopes.items())):
            if (not isinstance(group, dict) or not scope.startswith("zones/")
                    or SEGMENT.fullmatch(scope[len("zones/"):]) is None):
                issues.append(dict(scope_index=index, reason="invalid_inventory_scope"))
                continue
            warnings += int("warning" in group)
            items = group.get("instances", [])
            if not isinstance(items, list):
                issues.append(dict(scope_index=index, reason="invalid_scope_instances"))
                continue
            rows.extend((scope, item) for item in items)
            if len(rows) > MAX_RECORDS:
                raise TargetError("inventory_record_ceiling_exceeded")
    if len(rows) > MAX_RECORDS:
        raise TargetError("inventory_record_ceiling_exceeded")
    candidates = []
    for index, (scope, item) in enumerate(rows):
        try:
            candidates.append((index, _instance(item, account=account, project=project, scope=scope)))
        except TargetError as error:
            issues.append(dict(record_index=index, reason=str(error)))
    identities = Counter((r["provider"]["location"], r["provider"]["resource_id"]) for _, r in candidates)
    names = Counter((r["provider"]["location"], r["provider"]["resource_name"]) for _, r in candidates)
    records = []
    for index, record in candidates:
        provider = record["provider"]
        if (identities[(provider["location"], provider["resource_id"])] > 1
                or names[(provider["location"], provider["resource_name"])] > 1):
            issues.append(dict(record_index=index, reason="ambiguous_resource_identity"))
            continue
        record["digest"] = canonical_digest(record)
        records.append(record)
    records.sort(key=lambda r: (r["provider"]["location"], r["provider"]["resource_id"]))
    result = dict(schema="camol.target_inventory", schema_version=1, decoder=format,
        source_digest=canonical_digest(value), declared_scope=dict(account=account, project=project),
        input_records=len(rows), records=records, issues=issues,
        coverage=dict(more_pages=more_pages, warning_count=warnings, unreachable_count=unreachable,
                      complete_inventory=False, freshness="unknown", authenticated=False),
        meaning="imported_provider_claims_require_identity_review_and_task_admission")
    result["digest"] = canonical_digest(result)
    return deepcopy(result)
