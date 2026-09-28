import asyncio
import json
import types
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, Mock

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

    def test_archive_format_unchanged_after_adapter_cleanup(self):
        module=device.load_transport(require_binaries=False)
        self.assertEqual(device.digest(module.build_archive('/var/mobile/Library/CountryBundles/Overlay',b'unused')),
                         '05fdbe115faa4abf925fad1f565a7c3c4020855af4b2664fc6b4f33061c5fb76')
        self.assertEqual(device.digest(module.build_books(['one','two','three'])),
                         '8e9759bf617bbb181e6025f87ec4a147bb605868d4b24d36a90ea698c1e202bd')

    def test_inspect_and_restore_are_exclusive(self):
        result=subprocess.run([sys.executable,str(ROOT/'launch.py'),'--inspect','--restore'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('not allowed with argument',result.stderr)

    def test_changed_metadata_blocks_file_session_on_bound_device(self):
        report={**device.EXPECTED,'SIMStatus':'ready','carriers':[]}
        connection=Mock()
        context=Mock()
        context.__aenter__=AsyncMock(return_value=connection)
        context.__aexit__=AsyncMock(return_value=False)
        connect=AsyncMock(return_value=context)
        modules={'pymobiledevice3.lockdown':types.SimpleNamespace(create_using_usbmux=connect),
                 'pymobiledevice3.services.afc':types.SimpleNamespace(AfcService=Mock())}
        with patch.dict(sys.modules,modules), patch.object(device,'load_transport',return_value=Mock()), \
             patch.object(device,'read_report',AsyncMock(return_value={**report,'SIMStatus':'changed'})), \
             patch.object(country,'new_session') as create:
            with self.assertRaisesRegex(ValueError,'изменились'):
                asyncio.run(country.execute('apply','selected-device',report))
            create.assert_not_called()
        connect.assert_awaited_once_with(serial='selected-device',autopair=False,connection_type='USB')
