import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile
import assets


class AssetsTests(unittest.TestCase):
    def fixture(self, root):
        locks = {'bundles': {}, 'profile': {'fixture': True}}
        for name in ('CarrierLab.bundle', 'mobilkom_by.bundle'):
            d = root / name; d.mkdir()
            (d / 'Info.plist').write_bytes(b'fixture')
            locks['bundles'][name] = {'Info.plist': hashlib.sha256(b'fixture').hexdigest()}
        return locks

    def test_ipcc_preserves_original_bytes_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); src = root/'src'; src.mkdir(); locks = self.fixture(src)
            assets.prepare(src, root/'out', locks)
            with zipfile.ZipFile(root/'out/A1-72.7.1.ipcc') as z:
                self.assertEqual(z.namelist(), ['Payload/mobilkom_by.bundle/Info.plist'])
                self.assertEqual(z.read(z.namelist()[0]), b'fixture')

    def test_wrong_bytes_fail_before_output_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); src = root/'src'; src.mkdir(); locks = self.fixture(src)
            (src/'CarrierLab.bundle/Info.plist').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'Wrong source'):
                assets.prepare(src, root/'out', locks)
            self.assertFalse((root/'out').exists())

    def test_unexpected_file_not_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); locks = self.fixture(root)
            (root/'CarrierLab.bundle/unexpected').write_bytes(b'no')
            with self.assertRaisesRegex(ValueError, 'Unexpected source'):
                assets.locked_bundle(root, 'CarrierLab.bundle', locks['bundles']['CarrierLab.bundle'])

    def test_source_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); locks = self.fixture(root)
            (root/'CarrierLab.bundle/extra').symlink_to(root/'mobilkom_by.bundle/Info.plist')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                assets.locked_bundle(root, 'CarrierLab.bundle', locks['bundles']['CarrierLab.bundle'])

    def test_corrupted_cached_ipcc_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/'src';src.mkdir();locks=self.fixture(src)
            assets.prepare(src,root/'out',locks)
            (root/'out/A1-72.7.1.ipcc').write_bytes(b'corrupted')
            with self.assertRaisesRegex(ValueError,'Cached A1'):
                assets.fetch_all(root/'out',locks)
