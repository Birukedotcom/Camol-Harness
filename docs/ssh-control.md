# Optional SSH control-plane attachment

This adapter connects to an **already running** Camol supervisor. It does not
provision a machine, admit remote workers, deploy a repository, forward model
accounts, or prove distributed execution readiness. Local Camol has no SSH or
cmux dependency. The reusable client is `camol.ssh_transport.SSHControlClient`.

## Trust and setup

Install the approved Camol package on the remote host. The fixed
`camol-ssh-bridge` entry point must be on the remote account's noninteractive
PATH (a pipx/venv install needs that entry point exposed explicitly). Start the
supervisor on that host using its ordinary local owner-approved workflow.
Configure its bridge policy as an owner-only regular file at
`~/.config/camol/ssh-bridge.json`:

```json
{
  "schema": "camol.ssh_bridge_policy",
  "schema_version": 1,
  "targets": {
    "project": {
      "state_dir": "/absolute/remote/camol-state",
      "run_id": "approved-run",
      "plan_digest": "sha256:<64 lowercase hex digits>",
      "owner": "project-owner",
      "allowed_commands": ["status", "boxes", "box", "plan", "events"]
    }
  }
}
```

The symbolic digests above are placeholders, not valid configuration. Obtain the
actual plan digest from that supervisor. Obtain the installed bridge identity by
running `camol remote identity` **on that trusted host**, then verify it through
your own authenticated channel. This contains Camol version, package inventory
hash, resolved Python executable path/hash, and control protocol version 3.
The handshake is an authenticated host's **self-report**, not hardware/software
attestation; a compromised host can lie.

Identity hashing measures bounded, stable public installation bytes regardless
of their write-permission bits. Managed runtime caches may make executables
group/world writable; their exact hashes still need owner approval. This is not
a claim that those permissions are safe. Control credentials, owner policy,
known-host inputs and dispatch journals keep their separate strict file checks.

The local strict target profile has these fields:

```text
schema = "camol.ssh_target"; schema_version = 1
name, host, port, login
known_hosts = absolute local pinned public-host-key file
known_hosts_sha256 = sha256 of that file's exact bytes
identity_file = absolute private-key file reference (never its contents)
target_id = key in remote policy's targets mapping
target_digest = canonical_digest(remote policy's exact target object)
run_id, plan_digest, owner = exact matching remote policy values
bridge_identity = exact object reported on the approved host
allowed_commands = explicit subset; omitted defaults to read-only commands
```

Use `camol.schema.canonical_digest` for the target object, not a hash of arbitrary
JSON whitespace. Host keys must be verified out of band before pinning; there is
no accept-new or automatic trust-on-first-use path. Camol snapshots the bounded
known-hosts file privately and verifies its digest before launch. It checks the
private-key file's ownership/type/mode but never reads or logs key bytes; OpenSSH
uses the explicitly selected file. Batch mode means passphrase/password prompts
and agent-only keys are unsupported in this slice.

The adapter ignores ambient SSH config and disables agents, agent forwarding,
X11, tunnels, proxies, jump hosts, multiplexing and credential forwarding. These
are explicit OpenSSH options, not a custom encryption layer. See the official
[ssh(1)](https://man.openbsd.org/ssh.1) and
[ssh_config(5)](https://man.openbsd.org/ssh_config.5) manuals.

All remote arguments are framed JSON on stdin. The SSH remote command is the
constant `camol-ssh-bridge`; no target names, paths, owner input or control
parameters are interpolated into a shell command. The remote account still has
its normal SSH account privileges. The bridge's command allowlist is not an OS
account sandbox; deployments needing an account-level restriction must configure
that on their SSH server.

## Use and outcome handling

```text
camol remote validate --target /absolute/target.json
camol remote request --target /absolute/target.json --state-dir /absolute/local-journal --command status
camol remote receipts --target /absolute/target.json --state-dir /absolute/local-journal
```

Validation and receipt inspection are offline; receipt inspection does not create
missing directories. Mutation requires both the remote owner policy and local
target profile to permit the command, plus CLI `--allow-mutation --by OWNER`.
`--params /absolute/params.json` supplies strict JSON, never command text. The
supervisor checks the run/plan binding at authoritative dispatch, including a
second check around asynchronous force-stop cancellation. Changed policy,
owner, run, plan or bridge identity fails closed.

One request uses one SSH process and one bounded frame (60 KiB request, 8 MiB
response). The overall timeout covers launch, handshake, writes and reads;
stderr is drained into a byte count/hash rather than retained as a raw banner.
Local cleanup is bounded. If the direct process has exited but another process
holds its pipes, Camol closes its local pipe transports instead of signalling an
unowned/reused process group. This does not claim containment or termination of
hostile remote descendants.

Mutations get a fsynced dispatch journal with the command and parameter **digest**,
not raw parameters. A disconnect/cancellation after possible dispatch is
`unknown`, never presumed rollback. There is no automatic retry. Outstanding
unknown mutations block subsequent mutations for that target; read-only queries
remain available. Reuse the same local journal across reconnects/restarts.

After inspecting the remote authoritative state, an owner can explicitly clear
the local hold with `remote acknowledge-unknown --target ... --state-dir ...
--request-id ... --by OWNER --reason ...`. This only acknowledges uncertainty; it
does not prove completion, reconcile a provider bill, erase history or replay the
request. Secret-shaped notes are rejected. Embedded callers can retrieve a
cancelled request's identity with `cancellation_receipt(cancelled_error)`;
Python 3.9 may wrap the enriched cancellation in an ordinary `CancelledError`,
so the durable journal remains the recovery source.

Tests use local process bridges and fake SSH executables, including broken frames,
policy drift, retained descendant pipes, backpressure and uncertain mutations.
No live SSH host, production key, remote provisioning or paid provider has been
tested by this checkpoint.
