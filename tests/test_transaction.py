import asyncio
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import catalog
import transaction
import transport_canary


def fixture():
    original = ({'mobilkom_by.bundle/Info.plist': plistlib.dumps({
        'CFBundleIdentifier': 'com.apple.mobilkom_by', 'CFBundleVersion': '72.7.1'}),
        'Other.bundle/f': b'untouched'}, {'25701': 'mobilkom_by.bundle', 'other': 'Other.bundle'})
    lab = {'CarrierLab.bundle/' + name: b'fixture' for name in (
        'carrier.plist', 'overrides_V53_V54_V57.plist', 'overrides_V53_V54_V57.der.pri', 'signatures/carrier.plist')}
    lab['CarrierLab.bundle/Info.plist'] = plistlib.dumps({'CFBundleIdentifier': 'com.apple.CarrierLab', 'CFBundleVersion': '72.7.1'})
    return original, ({**original[0], **lab}, {**original[1], '25701': 'CarrierLab.bundle'})


class TransactionTests(unittest.TestCase):
    def test_inverse_preserves_every_unrelated_byte_and_link(self):
        original, candidate = fixture()
        transaction.validate_transition(original, candidate)
        self.assertEqual(transaction.original_mapping(candidate), original)

    def test_unrelated_modification_rejected(self):
        original, candidate = fixture()
        candidate[0]['Other.bundle/f'] = b'changed'
        with self.assertRaisesRegex(ValueError, 'modified or removed'):
            transaction.validate_transition(original, candidate)

    def test_other_sim_using_lab_blocks_removal(self):
        _, candidate = fixture(); candidate[1]['25704'] = 'CarrierLab.bundle'
        with self.assertRaisesRegex(ValueError, 'Other SIM'):
            transaction.original_mapping(candidate)

    def session(self, path):
        token = 'a' * 20
        s = transaction.Transaction(transport_canary.load_transport(require_binaries=False), 'fixture', path, {
            'phase': 'created', 'source': 'airlift-src-' + token,
            'link': 'airlift-link-' + token, 'exported': 'airlift-recovered-' + token})
        s.backup_books = AsyncMock(); s.native = AsyncMock(); s.export_paused = AsyncMock()
        s.await_export = AsyncMock(); s.return_directory = AsyncMock(); s.cleanup = AsyncMock()
        s.finish_worker = AsyncMock(return_value={'placement': 'candidate'}); s.verify = AsyncMock()
        return s

    def test_stale_catalog_returns_observed_original_without_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.session(Path(tmp)); original, candidate = fixture()
            changed = ({**original[0], 'Other.bundle/new': b'concurrent'}, original[1], [''])
            with patch.object(catalog, 'tree', AsyncMock(side_effect=[(*candidate, ['']), changed, changed])):
                with self.assertRaisesRegex(ValueError, 'changed since snapshot'):
                    asyncio.run(s.apply(None, original, candidate))
            s.finish_worker.assert_not_called()
            s.return_directory.assert_awaited_once()
            self.assertEqual(s.state['outcome'], 'stale_baseline_returned')

    def test_bad_staging_never_exports_protected_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.session(Path(tmp)); original, candidate = fixture()
            with patch.object(catalog, 'tree', AsyncMock(return_value=({}, {}, ['']))):
                with self.assertRaisesRegex(ValueError, 'Staged candidate'):
                    asyncio.run(s.apply(None, original, candidate))
            s.export_paused.assert_not_called(); s.finish_worker.assert_not_called()

    def test_commit_requires_exact_current_readback_then_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.session(Path(tmp)); original, candidate = fixture()
            with patch.object(catalog, 'tree', AsyncMock(side_effect=[(*candidate, ['']), (*original, ['']), (*original, [''])])), \
                 patch.object(catalog, 'info', AsyncMock(return_value=None)):
                asyncio.run(s.apply(None, original, candidate))
            s.finish_worker.assert_awaited_once_with('candidate')
            s.verify.assert_awaited_once()
            self.assertEqual(s.state['phase'], 'placement_transport_only')
            self.assertTrue((Path(tmp) / 'catalog.zip').exists())
