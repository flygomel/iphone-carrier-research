import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import country
import device
import file_transport

ROOT = Path(__file__).resolve().parents[1]


class EntrypointTests(unittest.TestCase):
    def test_help_works_without_device_and_only_lists_country_options(self):
        result = subprocess.run([sys.executable, str(ROOT/'launch.py'), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--restore', result.stdout)
        self.assertNotIn('--experimental', result.stdout)

    def test_removed_workflows_cannot_fall_through_to_write(self):
        for flag in ['--experimental', '--legacy-carrierlab', '--rollback', '--assets']:
            with self.subTest(flag=flag):
                result = subprocess.run([sys.executable, str(ROOT/'launch.py'), flag], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn('unrecognized arguments', result.stderr)

    def test_old_and_new_incomplete_operations_still_block(self):
        for prefix in ['country', 'catalog', 'install']:
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); run=root/(prefix+'-fixture');run.mkdir()
                (run/'state.json').write_text(json.dumps({'phase':'return_intent'}))
                with patch.object(file_transport, 'PRIVATE', root):
                    with self.assertRaises(ValueError): country.pending()
                (run/'state.json').write_text(json.dumps({'phase':'complete'}))
                with patch.object(file_transport, 'PRIVATE', root):country.pending()

    def test_old_incomplete_canary_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=root/'canary-fixture';run.mkdir()
            device.Journal(run/'journal.jsonl').append('native_intent')
            with patch.object(file_transport,'PRIVATE',root):
                with self.assertRaises(ValueError):country.pending()

    def test_transport_loads_from_shipped_sources(self):
        module=device.load_transport(require_binaries=False)
        self.assertTrue(callable(module.build_archive))
        self.assertTrue(callable(module.build_books))
