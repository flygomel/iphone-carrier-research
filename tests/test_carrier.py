import json
from pathlib import Path
import plistlib
import stat
import tempfile
import unittest
import warnings
import zipfile

import carrier


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = ({
            "mobilkom_by.bundle/Info.plist": plistlib.dumps({
                "CFBundleIdentifier": "com.apple.mobilkom_by", "CFBundleVersion": "72.7.1"}),
            "mobilkom_by.bundle/carrier.plist": b"preserve-original",
            "Other.bundle/carrier.plist": b"preserve-other"},
            {"25701": "mobilkom_by.bundle", "other": "Other.bundle"})
        self.lab = {"CarrierLab.bundle/Info.plist": b"test"}
        self.state = {**carrier.PROFILE, "carriers": [
            {"CFBundleIdentifier": "com.apple.mobilkom_by", "CFBundleVersion": "72.7.1",
             "MCC": "257", "MNC": "01"},
            {"CFBundleIdentifier": "com.apple.life_by", "CFBundleVersion": "72.7",
             "MCC": "257", "MNC": "04"}]}

    def test_roundtrip_preserves_unrelated_files_and_links(self):
        candidate = carrier.make_plan(self.original, self.lab, self.state)
        path = self.root / "candidate.zip"
        carrier.write_catalog(path, *candidate)
        files, links = carrier.read_catalog(path)
        for name, data in self.original[0].items():
            self.assertEqual(files[name], data)
        self.assertEqual(links["other"], "Other.bundle")
        self.assertEqual(links["25701"], "CarrierLab.bundle")
        del files["CarrierLab.bundle/Info.plist"]
        links["25701"] = "mobilkom_by.bundle"
        self.assertEqual((files, links), self.original)

    def test_wrong_build_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unstudied"):
            carrier.make_plan(self.original, self.lab, {**self.state, "BuildVersion": "other"})

    def test_existing_carrierlab_rejected(self):
        existing = ({**self.original[0], **self.lab}, self.original[1])
        with self.assertRaisesRegex(ValueError, "already exists"):
            carrier.make_plan(existing, self.lab, self.state)

    def test_wrong_a1_source_rejected(self):
        bad = (self.original[0], {"25701": "Other.bundle"})
        with self.assertRaisesRegex(ValueError, "mapping differs"):
            carrier.make_plan(bad, self.lab, self.state)

    def test_zip_path_traversal_rejected(self):
        path = self.root / "bad.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mobilkom_by.bundle/../../escape", b"x")
        with self.assertRaisesRegex(ValueError, "Invalid archive path"):
            carrier.read_catalog(path)

    def test_zip_symlink_escape_rejected(self):
        path = self.root / "bad.zip"
        with zipfile.ZipFile(path, "w") as z:
            item = zipfile.ZipInfo("25701")
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(item, "../outside.bundle")
        with self.assertRaises(ValueError):
            carrier.read_catalog(path)

    def test_zip_duplicate_rejected(self):
        path = self.root / "bad.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("A.bundle/file", b"1")
                z.writestr("A.bundle/file", b"2")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            carrier.read_catalog(path)

    def test_report_does_not_overwrite(self):
        path = self.root / "report.json"
        carrier.save_json(path, {"a": 1})
        with self.assertRaises(FileExistsError):
            carrier.save_json(path, {"a": 2})
        self.assertEqual(json.loads(path.read_text()), {"a": 1})


if __name__ == "__main__":
    unittest.main()
