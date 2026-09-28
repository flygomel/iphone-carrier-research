import io
import json
from pathlib import Path
import stat
import struct
import tempfile
import unittest
import zipfile

import carrier
import catalog
import recovery_plan
import transport_canary


class RecoveryPlanTests(unittest.TestCase):
    def fixture(self, root):
        run = root / "run"; run.mkdir()
        carrier.write_catalog(run / "catalog.zip", {"A.bundle/file": b"original"}, {"25701": "A.bundle"})
        token = "a" * 20
        catalog.store(run / "state.json", {
            "device": "fixture", "token": token, "target": catalog.TARGET,
            "source": "airlift-src-" + token, "link": "airlift-link-" + token,
            "exported": "airlift-recovered-" + token, "profile": transport_canary.EXPECTED,
            "catalog_sha256": carrier.digest((run / "catalog.zip").read_bytes())})
        return run

    def test_apple_mode_field_preserves_links_and_file_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); run = self.fixture(root)
            archive = recovery_plan.prepare(run, root / "out")
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                self.assertEqual(z.read("saved-catalog/A.bundle/file"), b"original")
                self.assertEqual(z.read("saved-catalog/25701"), b"A.bundle")
                self.assertEqual(struct.unpack("<HHH", z.getinfo("saved-catalog/25701").extra),
                                 (0x5A53, 2, stat.S_IFLNK | 0o777))
            plan = json.loads((root / "out/plan.json").read_text())
            self.assertFalse(plan["device_access"])

    def test_corrupted_backup_not_packaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); run = self.fixture(root)
            with (run / "catalog.zip").open("ab") as f: f.write(b"corrupted")
            with self.assertRaisesRegex(ValueError, "checksum"):
                recovery_plan.prepare(run, root / "out")
            self.assertFalse((root / "out").exists())

    def test_unsupported_profile_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); run = self.fixture(root)
            p = run / "state.json"; s = json.loads(p.read_text())
            s["profile"]["BuildVersion"] = "another-build"; p.write_text(json.dumps(s))
            with self.assertRaisesRegex(ValueError, "profile"):
                recovery_plan.prepare(run, root / "out")
