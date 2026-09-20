import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camol.orchestrator import Orchestrator
from camol.projection_copy import clone_projection
from camol.runbook import load_runbook
from camol.state import apply_event, empty_state, project
from camol.store import SQLiteEventStore


ROOT = Path(__file__).resolve().parents[1]


class ProjectionCopyTests(unittest.TestCase):
    def test_builtin_graph_uses_fast_path_preserves_aliases_and_is_detached(self):
        child = {'nested': [1, True, None, 'text', {'value': 5}]}
        original = {'left': child, 'right': child, 'list': [child]}
        with patch('camol.projection_copy.deepcopy', side_effect=AssertionError('not the fast path')):
            result = clone_projection(original)
        self.assertEqual(result, original)
        self.assertIs(result['left'], result['right'])
        self.assertIs(result['left'], result['list'][0])
        self.assertIsNot(result['left'], child)
        result['left']['nested'][-1]['value'] = 9
        self.assertEqual(child['nested'][-1]['value'], 5)

    def test_cycles_preserve_graph_identity_without_sharing_mutable_input(self):
        original = {'list': []}
        original['self'] = original
        original['list'].extend([original, original['list']])
        result = clone_projection(original)
        self.assertIs(result['self'], result)
        self.assertIs(result['list'][0], result)
        self.assertIs(result['list'][1], result['list'])
        self.assertIsNot(result, original)
        self.assertIsNot(result['list'], original['list'])

    def test_large_integers_numeric_keys_and_scalars_are_not_serialized(self):
        large = 1 << 20000
        original = {large: [large], None: [-0.0], 2.5: {'yes': True}}
        result = clone_projection(original)
        self.assertEqual(result, original)
        self.assertIs(type(next(iter(result))), int)
        self.assertIsNot(result[large], original[large])
        for scalar in (None, True, 1, large, -0.0, 'hello'):
            self.assertIs(clone_projection(scalar), scalar)

    def test_tuple_keys_and_mutable_tuple_contents_use_shared_deepcopy_memo(self):
        child = [1]
        original = {(1, 'key'): (child,), 'child': child}
        result = clone_projection(original)
        self.assertEqual(result, copy.deepcopy(original))
        self.assertIs(result[(1, 'key')][0], result['child'])
        self.assertIsNot(result['child'], child)

    def test_subclasses_and_custom_copy_hooks_preserve_standard_semantics(self):
        class SpecialDict(dict):
            pass
        class SpecialList(list):
            pass
        class SpecialString(str):
            pass
        for value in (SpecialDict(a=[]), SpecialList([[]]), SpecialString('text')):
            value.metadata = []
            result = clone_projection({'value': value})['value']
            self.assertIs(type(result), type(value))
            self.assertEqual(result, value)
            self.assertIsNot(result.metadata, value.metadata)
        scalar = 'custom memo target'
        class Hook:
            def __deepcopy__(self, memo):
                memo[id(scalar)] = ['hook override']
                return {'hook': True}
        value = {'earlier': [], 'hook': Hook(), 'scalar': scalar}
        self.assertEqual(clone_projection(value), copy.deepcopy(value))

    def test_explicit_memo_overrides_and_cross_call_aliases_are_respected(self):
        scalar = 'explicit scalar override'
        replacement = []
        self.assertIs(clone_projection(scalar, {id(scalar): replacement}), replacement)
        shared = []
        memo = {}
        left = clone_projection({'shared': shared}, memo)
        right = clone_projection([shared], memo)
        self.assertIs(left['shared'], right[0])
        self.assertIsNot(right[0], shared)

    def test_metaclass_equality_cannot_spoof_an_exact_builtin_fast_path(self):
        class Spoof(type):
            def __hash__(cls):
                return hash(str)
            def __eq__(cls, other):
                return other is str
        class Custom(metaclass=Spoof):
            pass
        original = {'custom': Custom()}
        standard_result = object()
        with patch('camol.projection_copy.deepcopy', return_value=standard_result) as fallback:
            self.assertIs(clone_projection(original), standard_result)
        fallback.assert_called_once_with(original)

    def test_event_projection_is_pure_and_replay_matches_standard_copy(self):
        runbook = load_runbook(ROOT / 'examples/local-n-box-runbook.json')
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / 'events.sqlite3')
            try:
                orchestrator = Orchestrator(store)
                state = orchestrator.initialize(runbook)
                orchestrator.approve_plan(state['run_id'], 'owner', state['plan_digest'])
                orchestrator.start(state['run_id'])
                events = store.read(state['run_id'])
                expected_events = copy.deepcopy(events)
                fast = project(events)
                with patch('camol.state.clone_projection', side_effect=copy.deepcopy):
                    reference = project(events)
                self.assertEqual(fast, reference)
                self.assertEqual(events, expected_events)
                source = empty_state()
                result = apply_event(source, events[0])
                self.assertEqual(source, empty_state())
                result['tasks'][next(iter(result['tasks']))]['steps'][0]['instruction'] = 'changed'
                self.assertEqual(events, expected_events)
            finally:
                store.close()

    def test_public_snapshots_cannot_mutate_incremental_cached_state(self):
        runbook = load_runbook(ROOT / 'examples/local-n-box-runbook.json')
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(Path(directory) / 'events.sqlite3')
            try:
                orchestrator = Orchestrator(store)
                original = orchestrator.initialize(runbook)
                run_id = original['run_id']
                caller = orchestrator.state(run_id)
                caller['runbook']['tasks'][0]['steps'][0]['instruction'] = 'caller edit'
                caller['tasks'][runbook['tasks'][0]['id']]['checkpoints'].append({'forged': []})
                caller['admissions']['forged'] = {'receipt': []}
                self.assertEqual(orchestrator.state(run_id), original)
                orchestrator.approve_plan(run_id, 'owner', original['plan_digest'])
                approved = orchestrator.state(run_id)
                self.assertEqual(original['status'], 'draft')
                self.assertEqual(approved['status'], 'ready')
                self.assertNotIn('forged', approved['admissions'])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
