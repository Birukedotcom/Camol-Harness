# Encrypted worker evidence delivery

`camol.worker_tls` supplies an opt-in, standard-library TLS transport for the
[owner-enrolled worker evidence stream](worker-enrollment.md). It carries signed
batches and authenticated durable-spool acknowledgments. It does **not** launch
remote workers, adopt machines, transfer source code, renew leases, promote worker
claims into kernel results, or prove a model connection.

## Controller embedding

On the existing controller's asyncio loop and owning kernel thread:

```python
from camol.worker_tls import WorkerTLSServer

async with WorkerTLSServer(
    harness.worker_streams,
    certificate=certificate_path,
    private_key=private_key_path,
    address="127.0.0.1",
    port=9443,
) as transport:
    await run_controller_until_stopped()
```

The names above are embedding inputs, not a standalone runnable supervisor.
Construction reads TLS material but does not listen; entering the context or
calling `start()` binds the socket. `close()` cancels acceptance and active
connections. Non-loopback binding requires explicit `allow_non_loopback=True`.
There is no automatic listener, certificate generation, firewall configuration,
account login, public tunnel or certificate/key distribution. The controller
must already have approved the exact evidence stream on a current admitted lease.

Use owner-controlled regular certificate/key files with no symlink traversal or
hard links. Private keys must belong to the current owner with no group/other
permissions. Public certificate files must be owner/root-controlled and not
group/world writable. Certificate bytes and key bytes are loaded from checked
descriptors into short-lived private snapshots before SSL loads them. Encrypted
private keys are not supported; loading never prompts interactively for a password.
Keep TLS keys and enrollment keys outside all model/workspace write grants.

## Worker embedding and CLI

An exact V1 endpoint record freezes:

- `schema`: `camol.worker_tls_endpoint`; `schema_version`: `1`.
- `address`: literal IPv4/IPv6, with no DNS lookup or zone identifier.
- `port`: 1–65535; `server_name`: the expected certificate hostname/IP.
- `ca_file`: absolute resolved trust-bundle path.
- `ca_digest`: SHA-256 of the exact CA file bytes, prefixed `sha256:`.
- `certificate_digest`: SHA-256 of the server leaf certificate's DER bytes,
  prefixed `sha256:`; `certificate_digest(pem)` computes it for setup.
- `scope`: the exact approved stream digest.
- `timeout_seconds`: finite 0.1–60 seconds.

The connection requires certificate-chain validation, expected-name validation,
the frozen CA-file digest and an exact leaf pin **before sending a batch**.
There are no default trust-store additions, ambient proxies, redirect handling,
automatic DNS discovery or ambient `SSLKEYLOGFILE` output.

```python
from camol.worker_tls import WorkerTLSClient

client = WorkerTLSClient(endpoint_record)
receipt = await client.deliver(producer_spool, allow_network=True)
```

`producer_spool` is an existing `WorkerDelivery` producer. For an existing private
spool, `camol worker-delivery flush --help` describes `--root`, `--binding`,
`--key-file`, `--role producer`, `--target` and mandatory `--allow-network`.
The target file contains the record above. These are owner-supplied paths and
reviewed identities, not placeholders Camol can discover automatically. The CLI
does one batch exchange; it does not auto-retry or drain the whole backlog.
`worker-delivery inspect` remains offline. Transport and spool keys are separate.

## Failure, accounting and bounds

TLS 1.3 is required. Python version alone does not establish support: Apple's
Python 3.9 with LibreSSL 2.8.3 lacks it and gets `TLS13_RUNTIME_REQUIRED`, without
silently downgrading. Python 3.9 built against a capable TLS library is supported.
Other local Camol operations do not require this optional transport capability.

One connection carries one length-prefixed batch and one bounded reply. Batches
are capped at 256 KiB and acknowledgments at 4 KiB. The listener limits concurrent
connections **before the TLS handshake**, defaults to 16 and permits 1–64. A slow
handshake cannot create unbounded admitted connection tasks. Requests and replies
have explicit timeouts; reader buffers are bounded. The owning thread still runs
bounded synchronous filesystem/SQLite receipt operations: the async timeout is
not hard preemption of those operations or arbitrary embedding callbacks.

The enrollment service authenticates before its kernel write lock, rechecks
current lease/revocation while holding it, and commits the evidence spool. A
server-side service failure returns only a fixed error, not raw exceptions or
credentials. Receipt storage is not an atomic kernel-result commit.

If a reply disappears after receipt commit, the producer remains unacknowledged;
retrying the same batch recovers its receipt without duplicating evidence. A
revoked/expired enrollment still refuses receipt retrieval. Cancellation is
propagated. Invalid acknowledgments or local cursor-write failures do not report
success. Network success alone cannot advance a producer cursor: its MAC, scope
and queued-record digest must validate first.

`client.last_attempt` reports `not_sent`, `unknown`,
`receipt_received_unverified`, `local_ack_pending_or_invalid` or `acknowledged`.
It includes elapsed local milliseconds, offered plaintext frame sizes, TLS version
and observed certificate digest when known. A later denied attempt does not retain
an earlier successful status. These are **not** wire-traffic counts or provider
bills; provider tokens/cost remain unknown. Metrics and listener counters are
in-memory only. Durable transport-attempt logging remains a separate integration
requirement; evidence spool durability does not make these metrics durable.

## Verification and unfinished integration

Tests generate temporary certificates and use real loopback TLS sockets, a real
approved kernel lease, durable spools and a separate CLI process. They cover lost
and forged receipts, cancellation, timeout, oversized/truncated/extra frames,
certificate/name rejection, revocation and pre-handshake connection limits.
This is transport verification, not a cross-host model build or distributed
executor acceptance. An optional [supervisor gateway](worker-gateway.md) now owns
listener lifecycle and approved report pumping. Enrollment material transfer,
remote target admission/launch/cancellation, trusted result promotion, durable
transport telemetry and operational key/spool recovery remain open.

Implementation references: Python's [TLS contexts and certificate checks](https://docs.python.org/3/library/ssl.html)
and [accepted-socket TLS API](https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.connect_accepted_socket).
