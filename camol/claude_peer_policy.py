"""Explicit opt-in Claude tier; never silently replace legacy safe mode."""

from .providers import ProviderError


BUILTINS = frozenset({"Read", "Write", "Edit", "Glob", "Grep", "Bash"})


def execution_policy(*, strict_startup=False):
    return dict(customizations="restricted_explicit_settings", managed_policy="host_applies",
        peer_capability_exposure="worker_environment", peer_startup="initialized_before_prompt" if strict_startup else "observed_before_completion",
        network_enforcement="ambient", cost_enforcement="provider_estimated",
        inner_turn_limit="provider_max_turns")


def validate(profile):
    if (profile.adapter_kind != "claude_cli" or profile.provider != "anthropic"
            or profile.execution_policy not in (execution_policy(), execution_policy(strict_startup=True)) or profile.peer_policy is None
            or profile.local_provider is not None or profile.local_endpoint is not None):
        raise ProviderError("Claude peer profile requires the exact explicit restricted-execution policy")
    if profile.network_destinations != ("*",):
        raise ProviderError("Claude peers require an explicit ambient network grant; denied egress is not widened")
    if (profile.permission_mode not in {"dontAsk", "acceptEdits"}
            or not set(profile.allowed_tools).issubset(BUILTINS)
            or len(profile.allowed_tools) != len(set(profile.allowed_tools))):
        raise ProviderError("Claude peer profiles require unique explicit built-in names and unattended permissions")


def settings():
    return dict(disableAllHooks=True, autoMemoryEnabled=False, claudeMdExcludes=["**"])
