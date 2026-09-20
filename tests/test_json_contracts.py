import os
from pathlib import Path
import tempfile
import unittest

from camol.json_contracts import decode_contract, load_contract
from camol.schema import SchemaError


class JsonContractTests(unittest.TestCase):
    def test_ambiguous_and_nonfinite_values_are_rejected_at_every_level(self):
        for raw in ('{"owner":"a","owner":"b"}', '{"scope":{"allow":false,"allow":true}}',
                    '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', b"\xff", '{',
                    '{"x":' + "9" * 129 + '}'):
            with self.subTest(raw=raw), self.assertRaises(SchemaError):
                decode_contract(raw)
        self.assertEqual(decode_contract('{"owner":"a","values":[0,2.5,true,null]}'),
                         {"owner": "a", "values": [0, 2.5, True, None]})

    def test_size_limit_and_nonregular_files_do_not_block_or_change_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contract.json"
            path.write_text('{"safe":true}')
            self.assertEqual(load_contract(path), {"safe": True})
            with self.assertRaises(SchemaError):
                load_contract(path, max_bytes=4)
            pipe = Path(temporary) / "pipe"
            os.mkfifo(pipe)
            with self.assertRaises(SchemaError):
                load_contract(pipe)
            self.assertTrue(pipe.exists())
            with self.assertRaises(SchemaError):
                decode_contract(" " * 50, max_bytes=10)
