"""Explicit native peer policy and disposable owner-issued relay invocation."""

import json
import os
from pathlib import Path
import stat
import sys

from .peer_mcp import NAMES
from .peer_transport import PeerEndpoint
from .providers import ProviderError
from .sandbox import system_read_paths
from .schema import canonical_digest


ENV_NAMES = ("CAMOL_PEER_ENDPOINT", "CAMOL_PEER_TOKEN")


def peer_policy():
    return dict(schema="camol.peer_policy", schema_version=1, transport="local_mcp_stdio",
        operations=["list", "observe", "inbox", "send"])


def validate_peer_policy(value):
    try:
        valid = (isinstance(value, dict) and type(value.get("schema_version")) is int
            and isinstance(value.get("operations"), list) and canonical_digest(value) == canonical_digest(peer_policy()))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ProviderError("native peer policy must explicitly freeze the supported local MCP operation set")


def parent_path(state_dir, run_id, task_id, box_id):
    if os.name != "posix":
        raise ProviderError("native peer transport requires POSIX Unix sockets")
    identity = canonical_digest(dict(state_dir=str(Path(state_dir).resolve()), run_id=run_id,
        task_id=task_id, box_id=box_id)).split(":", 1)[1][:24]
    return Path("/tmp").resolve() / ("cmp-{}-{}".format(os.getuid(), identity))


def private_parent(path):
    info = path.lstat()
    if (path.resolve() != path or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ProviderError("native peer directory must be canonical and owner-private")
    return info.st_dev, info.st_ino


def runtime():
    # The owning Camol installation selects the interpreter/package, never PATH,
    # the worker repository, project config or an agent-generated entry point.
    package = Path(__file__).resolve().parent
    executable = str(Path(sys.executable).absolute())
    # Load the exact package file. Python 3.9's ordinary sys.path discovery
    # needs directory-content access to the enclosing checkout; that parent
    # may contain private state and is deliberately not a runtime read grant.
    code = ("import sys,importlib.util;"
        "s=importlib.util.spec_from_file_location('camol'," + repr(str(package / "__init__.py")) + ","
        "submodule_search_locations=[" + repr(str(package)) + "]);"
        "m=importlib.util.module_from_spec(s);sys.modules['camol']=m;s.loader.exec_module(m);"
        "from camol.peer_mcp import main;raise SystemExit(main())")
    return executable, ["-I", "-B", "-c", code], (str(package),) + system_read_paths(executable)


def admission_paths(state_dir, run_id, task_id, box_id):
    parent = parent_path(state_dir, run_id, task_id, box_id)
    if parent.exists() or parent.is_symlink():
        private_parent(parent)
    return (str(parent),) + runtime()[2]


class NativePeerInvocation:
    """One worker-turn endpoint; no model calls, authority grants or global config."""

    def __init__(self, adapter, profile, assignment, turn_number):
        validate_peer_policy(profile.peer_policy)
        self.adapter, self.assignment, self.turn = adapter, assignment, turn_number
        self.parent = parent_path(adapter.state_dir, adapter.run_id, assignment["task_id"], assignment["agent_id"])
        self.endpoint, self.parent_identity = None, None
        self.environment = {}
        self.argv = []
        self.cleanup_incomplete = False
        policy = adapter.sandbox_policy
        needed = admission_paths(adapter.state_dir, adapter.run_id, assignment["task_id"], assignment["agent_id"])
        if (policy is None or policy.network_destinations != ("*",)
                or not set(needed).issubset(policy.read_paths) or not set(ENV_NAMES).issubset(policy.environment_names)):
            raise ProviderError("native peers require the exact admitted transport/runtime/environment policy")
        protected = (self.parent,) + tuple(Path(path) for path in runtime()[2])
        if any(path.is_relative_to(Path(write)) or Path(write).is_relative_to(path)
                for path in protected for write in policy.write_paths):
            raise ProviderError("native peer transport and trusted relay must not be worker-writable")
        self.tools = getattr(adapter, "peer_tools", None)
        if (type(turn_number) is not int or self.tools is None or self.tools.turn != turn_number or self.tools.run_id != adapter.run_id
                or self.tools.assignment != assignment):
            raise ProviderError("native peers require the exact runner-owned worker turn")
        self.tools._check()

    async def __aenter__(self):
        self.parent.mkdir(mode=0o700, exist_ok=True)
        self.parent_identity = private_parent(self.parent)
        if any(self.parent.iterdir()):
            raise ProviderError("native peer directory has retained content; owner inspection required before launch")
        try:
            self.endpoint = await PeerEndpoint(self.tools, self.parent).start()
            token = self.endpoint.token
            # Keep the secret known through later artifact/diff collection. None
            # of these values are serialized by the redactor or plan/profile.
            redactors = [self.adapter.redactor, self.tools.control.redactor]
            if self.adapter.artifact_store is not None:
                redactors.append(self.adapter.artifact_store.redactor)
            for redactor in redactors:
                if token not in redactor._values:
                    redactor._values.append(token)
                    redactor._values.sort(key=len, reverse=True)
            self.environment = dict(CAMOL_PEER_ENDPOINT=str(self.endpoint.path), CAMOL_PEER_TOKEN=token)
            executable, args, _ = runtime()
            config = dict(command=executable, args=args, cwd=str(self.parent), env_vars=list(ENV_NAMES),
                required=True, enabled=True, enabled_tools=list(NAMES), startup_timeout_sec=10, tool_timeout_sec=10)
            # TOML inline-table syntax, never JSON object syntax. Secret values
            # are forwarded by name, never embedded in this argv or its log.
            table = ",".join(key + "=" + json.dumps(value, separators=(",", ":")) for key, value in config.items())
            self.argv = ["-c", "mcp_servers={camol_peers={" + table + "}}", "-c",
                'shell_environment_policy.filters={CAMOL_PEER_ENDPOINT="exclude",CAMOL_PEER_TOKEN="exclude"}']
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self):
        self.environment.clear()
        if self.endpoint is not None:
            await self.endpoint.close()
        if self.parent_identity is not None:
            if private_parent(self.parent) != self.parent_identity:
                raise ProviderError("native peer parent changed; refusing cleanup")
            self.parent.rmdir()
            self.parent_identity = None

    async def __aexit__(self, kind, error, traceback):
        try:
            await self.close()
        except (OSError, ValueError, ProviderError):
            self.cleanup_incomplete = True
            # Preserve already-attached billed/cancelled evidence if cleanup
            # also fails. The endpoint revokes its token before path cleanup.
            # A successful provider response must first have its usage retained;
            # the adapter then refuses completion if cleanup needs inspection.
            if error is not None:
                error.peer_cleanup_incomplete = True
