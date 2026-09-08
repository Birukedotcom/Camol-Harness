# Projection copying performance

The runtime keeps public state snapshots detached from its incremental replay
cache. Event application also returns a new projection without mutating its
input. These ownership guarantees remain unchanged; this optimization changes
only how those two whole-projection copies are made.

## Measured bottleneck

A local 12-box, 25-task DAG fixture exceeded the original 120-second trial
timeout both under concurrent test load and when run alone. A separately
profiled run with a 600-second observation ceiling completed in 386.774 seconds,
producing 1,092 events and 77 artifacts. Its source checkout was unchanged and
its exported ledger replayed to the same final projection.

The profile recorded 469,430,383 recursive `deepcopy` calls and 279.560 seconds
of cumulative copying time. `Orchestrator.state` was called 2,090 times and took
238.844 cumulative seconds; `state.apply_event` took 147.700 cumulative seconds.
These timings overlap and must not be added together. Profiling overhead also
means the profiled wall time is not a baseline for an unprofiled speedup claim.

## Narrow implementation

`clone_projection` fast-copies only exact built-in dictionaries and lists, with
one memo preserving repeated references and cycles. Exact built-in immutable
scalar values are shared. Type identity, not user-defined metaclass equality,
decides whether a value qualifies for the fast path. There is no serialization,
so large integers and non-string scalar keys retain their types and values.

If any other type appears, the partial fast copy is discarded and the entire
original graph is passed to standard-library `deepcopy`. This preserves custom
copy hooks, subclass attributes, tuples, and hooks that affect another entry
through the shared memo. An explicitly supplied memo also delegates unchanged.

Only `state.apply_event`'s initial copy and `Orchestrator.state`'s detached public
return use this helper. This is not a cache of validation results. Source
identity, admission freshness, capacity, lease authorization, evaluator assets,
candidate gates, and integration checks are unchanged. Previously accepted
tasks are still reverified after integration; that cost is not optimized away.

## Reproduce

The benchmark accepts an existing read-only SQLite snapshot and compares the
old and new copy functions in the same process with identical replay handlers:

```sh
python3 scripts/benchmark_projection_copy.py /path/to/snapshot.sqlite3 RUN_ID
python3 -m unittest tests.test_projection_copy -v
python3 scripts/run_runtime_soak.py --iterations 1 --boxes 12 --trial-timeout 120 --progress-seconds 10
```

The snapshot benchmark emits machine-readable timings, state size, projection
and digest equality, and caller-mutation isolation. It performs no model calls,
commands, readiness probes, or ledger writes. The runtime soak does execute
local disposable fixture workers and verification commands, then checks source
preservation and export replay. Timings are observations, never unit-test
correctness thresholds. Machine load and Python version affect them.

The implementation still copies each projection in full. Growing evidence
therefore still increases the cost of snapshots and replay; this is a bounded
constant-factor improvement, not a claim of constant-time history or arbitrary
N-box scalability.

## Checkpoint results

The saved read-only snapshot contained 941 events and a 4,250,226-byte projected
state. Five detached-copy samples per implementation, measured in one process
per Python version, produced these medians:

| Python | Standard copy | Projection copy | Standard replay | Projection replay |
| --- | ---: | ---: | ---: | ---: |
| 3.12.13 | 24.43 ms | 8.23 ms | 10.894 s | 4.175 s |
| 3.9.6 | 40.90 ms | 14.83 ms | 17.426 s | 7.127 s |

Both versions returned equal projections and canonical digests, with detached
nested caller mutations. Copy medians improved about 2.97x and 2.76x respectively;
replay improved about 2.61x and 2.45x. These are snapshot measurements, not an
end-to-end harness or provider-usage speedup claim.

The original unprofiled 12-box/25-task soak then passed with its 120-second task
deadline unchanged. Total trial time, including export and replay, was 119.021
seconds: 962 events, 77 artifacts, every task completed, unchanged source, and
exact export replay. The margin is narrow and this was one local run; other
hardware and larger histories need their own capacity and timing measurements.
Event count differs from the slower profiled run because elapsed-time lease
refreshes are themselves recorded; no refresh policy or guard was removed.

For transparency, an intermediate type-ID-set implementation still timed out:
24 tasks had succeeded and the final task was verifying. The final direct
identity-chain scalar checks removed that measured overhead without accepting
custom types into the fast path. The final nine copy-semantic regressions pass
on Python 3.9 and 3.12. A 99-test state/runtime/recovery group also passed while
the earlier fast-path implementation was under test; the complete integration
checkpoint still requires its normal full-suite run after this narrow change.
