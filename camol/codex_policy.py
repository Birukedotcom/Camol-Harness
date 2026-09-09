"""The explicitly narrower Codex worker tier; configuration is not model proof."""

import ipaddress
import hashlib
import json
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from .providers import ProviderError


def validate_codex_policy(profile):
    if profile.adapter_kind not in {"codex_cli", "codex_oss"}:
        raise ProviderError("schema2 execution_policy is currently only implemented for Codex workers")
    expected = {
        "cost_enforcement": "observed_only" if profile.adapter_kind == "codex_cli" else "not_applicable",
        "model_identity": "requested_only", "inner_turn_limit": "unsupported",
        "tool_policy": "sandbox", "network_enforcement": "ambient",
    }
    if profile.execution_policy != expected:
        raise ProviderError("POLICY_DENIED: Codex cannot enforce this execution_policy; hard spend, exact model, inner-turn and restricted-egress guarantees are unsupported")
    if profile.network_destinations != ("*",):
        raise ProviderError("POLICY_DENIED: Codex runtime requires an explicit unrestricted network grant; ambient egress cannot satisfy a restricted grant")
    if profile.allowed_tools or profile.permission_mode != "acceptEdits":
        raise ProviderError("POLICY_DENIED: Codex workers support workspace-write sandbox tools, not a named-tool allowlist or planning-only authority")
    if profile.adapter_kind == "codex_cli":
        if profile.provider != "openai" or profile.local_provider is not None or profile.local_endpoint is not None:
            raise ProviderError("hosted Codex profile requires openai and no local endpoint")
    else:
        if profile.provider != "local" or profile.local_provider not in {"ollama", "lmstudio"}:
            raise ProviderError("local Codex profile requires local provider ollama or lmstudio")
        endpoint = urlparse(profile.local_endpoint or "")
        try:
            loopback = ipaddress.ip_address(endpoint.hostname or "").is_loopback
            valid_port = endpoint.port is not None and 0 < endpoint.port < 65536
        except ValueError:
            loopback = valid_port = False
        if not loopback or not valid_port or endpoint.scheme != "http" or endpoint.path not in {"", "/"} or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ProviderError("local model endpoint must be an explicit loopback HTTP origin without credentials")
        if profile.credential_refs or profile.credential_read_paths:
            raise ProviderError("local Codex workers cannot inherit provider credentials")
        if "cloud" in profile.requested_model.lower():
            raise ProviderError("local Codex policy does not permit cloud-labelled models")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("local model inventory must not redirect")


def local_model_inventory(profile):
    """Read only an existing local catalog; never download or invoke a model."""
    validate_codex_policy(profile)
    suffix = "/api/tags" if profile.local_provider == "ollama" else "/v1/models"
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(profile.local_endpoint.rstrip("/") + suffix, timeout=5) as response:
        raw = response.read((1 << 20) + 1)
    if len(raw) > 1 << 20:
        raise ProviderError("local model inventory exceeds capture limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ProviderError("local model inventory must be an object")
    items = payload.get("models") if profile.local_provider == "ollama" else payload.get("data")
    if not isinstance(items, list):
        raise ProviderError("local model inventory is malformed")
    return [item for item in items if isinstance(item, dict)]


def require_local_model(profile):
    for item in local_model_inventory(profile):
        name = item.get("name", item.get("id"))
        if name == profile.requested_model and not item.get("remote_host") and not item.get("remote_model"):
            return {"requested_model": name, "catalog_digest": hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()}
    raise ProviderError("NEEDS_DOWNLOAD: requested model is absent from the existing local catalog; Camol never auto-downloads it")
