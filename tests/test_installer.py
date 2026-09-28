import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import assets
import carrier
import installer


class InstallerTests(unittest.TestCase):
    def test_a1_rejects_unpinned_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.ipcc'
            with zipfile.ZipFile(p,'w') as z: z.writestr('Payload/mobilkom_by.bundle/Info.plist',b'changed')
            with self.assertRaisesRegex(ValueError,'pinned'):
                installer.package_files(p,'a1',{'bundles':{'mobilkom_by.bundle':{'Info.plist':carrier.digest(b'original')}}})

    def test_traversal_rejected_even_with_matching_container_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.ipcc'
            with zipfile.ZipFile(p,'w') as z: z.writestr('Payload/Docomo_jp.bundle/../../escape',b'x')
            with self.assertRaisesRegex(ValueError,'archive path'):
                installer.package_files(p,'docomo',{'docomo':{'sha256':carrier.digest(p.read_bytes())}})

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.ipcc'
            with zipfile.ZipFile(p,'w') as z:
                e=zipfile.ZipInfo('Payload/Docomo_jp.bundle/link');e.external_attr=0o120777<<16;z.writestr(e,b'outside')
            with self.assertRaisesRegex(ValueError,'Special'):
                installer.package_files(p,'docomo',{'docomo':{'sha256':carrier.digest(p.read_bytes())}})

    def test_duplicate_entries_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.ipcc'
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                with zipfile.ZipFile(p,'w') as z:
                    for _ in range(2): z.writestr('Payload/Docomo_jp.bundle/a',b'x')
            with self.assertRaisesRegex(ValueError,'Duplicate'):
                installer.package_files(p,'docomo',{'docomo':{'sha256':carrier.digest(p.read_bytes())}})
