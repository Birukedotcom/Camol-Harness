#!/usr/bin/env python3
"""Compare detached projection copying against one read-only event snapshot.

No model calls, commands, readiness probes, or event mutations. Timing is reported,
never used as a correctness assertion. The baseline changes only this benchmark
process's copy function; event validation and replay handlers remain identical.
"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camol import state as projection
from camol.projection_copy import clone_projection
from camol.schema import canonical_digest
from camol.store import ReadOnlyEventStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('run_id')
    parser.add_argument('--samples', type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100:
        parser.error('samples must be 1..100')
    store = ReadOnlyEventStore(args.database)
    try:
        events = store.read(args.run_id)
    finally:
        store.close()
    if not events:
        parser.error('the snapshot contains no events for the run')
    reference, replay, copies = None, {}, {}
    original_clone = projection.clone_projection
    try:
        for name, clone in [('deepcopy', deepcopy), ('projection', clone_projection)]:
            projection.clone_projection = clone
            began = time.perf_counter()
            state = projection.project(events)
            replay[name] = time.perf_counter() - began
            if reference is None:
                reference = state
            assert state == reference
            assert canonical_digest(state) == canonical_digest(reference)
            samples = []
            for _ in range(args.samples):
                began = time.perf_counter()
                result = clone(state)
                samples.append(time.perf_counter() - began)
                assert result == reference
                result['admissions']['caller-mutation'] = {'nested': []}
                assert 'caller-mutation' not in state['admissions']
            copies[name] = dict(samples_seconds=samples, median_seconds=statistics.median(samples))
    finally:
        projection.clone_projection = original_clone
    print(json.dumps(dict(schema='camol.projection_copy_benchmark', schema_version=1,
        python=sys.version.split()[0], run_id=args.run_id, events=len(events),
        state_bytes=len(json.dumps(reference).encode()), replay_seconds=replay, copies=copies,
        projection_equal=True, digest_equal=True, nested_mutation_isolated=True), indent=2))


if __name__ == '__main__':
    main()
