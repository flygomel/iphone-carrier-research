import asyncio
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import catalog
import carrier


class RecoveryTests(unittest.TestCase):
    def setup_session(self, root):
        s = catalog.Session(None, "fixture", root, {
            "phase": "stage_intent", "source": "airlift-src-" + "a" * 20,
            "link": "airlift-link-" + "a" * 20, "exported": "airlift-recovered-" + "a" * 20})
        s.journal.append("native_intent", command="stage")
        (root / "books-full").mkdir()
        (root / "books-native").mkdir()
        content = b"original books fixture"
        catalog.durable_bytes(root / "books-full/0.bin", content)
        catalog.store(root / "books-full.json", {"files": {
            "Books.plist": {"file": "0.bin", "sha256": carrier.digest(content)}}, "directories": [""]})
        manifest = {"version": 1, "files": {}, "directories": {
            "Books": True, "Books/Sync": False, "Books/Sync/Database": False}}
        for i, name in enumerate(catalog.BOOKS_FILES):
            manifest["files"][name] = {"exists": i == 0, "localName": f"file-{i}.bin"}
        catalog.durable_bytes(root / "books-native/file-0.bin", content)
        catalog.durable_bytes(root / "books-native/manifest.plist", plistlib.dumps(manifest))
        return s

    def test_stage_crash_restores_before_cleaning(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.setup_session(Path(tmp))
            calls = []
            async def restore(*args): calls.append("restore")
            async def remove(*args): calls.append("cleanup")
            s.native = AsyncMock(side_effect=restore)
            with patch.object(catalog, "info", AsyncMock(return_value=None)), \
                 patch.object(catalog, "tree", AsyncMock(return_value=s.load_books())), \
                 patch.object(catalog, "remove_generated", AsyncMock(side_effect=remove)):
                asyncio.run(s.recover_before_export(None))
            self.assertEqual(calls, ["restore", "cleanup"])
            self.assertEqual(s.state["outcome"], "aborted_before_export")

    def test_move_intent_blocks_pre_export_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.setup_session(Path(tmp))
            s.journal.append("move_intent")
            s.native = AsyncMock()
            with self.assertRaisesRegex(ValueError, "journal"):
                asyncio.run(s.recover_before_export(None))
            s.native.assert_not_called()

    def test_unexpected_export_blocks_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.setup_session(Path(tmp)); s.native = AsyncMock()
            with patch.object(catalog, "info", AsyncMock(return_value={"st_ifmt": "S_IFDIR"})):
                with self.assertRaisesRegex(ValueError, "Unexpected moved"):
                    asyncio.run(s.recover_before_export(None))
            s.native.assert_not_called()

    def test_corrupted_native_backup_blocks_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.setup_session(Path(tmp)); s.native = AsyncMock()
            (Path(tmp) / "books-native/file-0.bin").write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "hashed preimage"):
                asyncio.run(s.recover_before_export(None))
            s.native.assert_not_called()

    def test_restore_failure_preserves_scaffold_and_retry_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.setup_session(Path(tmp)); s.native = AsyncMock(side_effect=TimeoutError())
            remove = AsyncMock()
            with patch.object(catalog, "info", AsyncMock(return_value=None)), \
                 patch.object(catalog, "remove_generated", remove):
                with self.assertRaises(TimeoutError): asyncio.run(s.recover_before_export(None))
            remove.assert_not_called()
            self.assertEqual(s.state["phase"], "pre_export_recovery_intent")
            self.assertTrue(catalog.pre_export_recovery_allowed(s.state, catalog.read_events(s.journal.path)))

    def test_partial_journal_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "journal.jsonl"; p.write_text('{"event":"move_')
            with self.assertRaises(json.JSONDecodeError): catalog.read_events(p)

    def test_backup_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.setup_session(Path(tmp))
            p = Path(tmp) / "books-full.json"; d = json.loads(p.read_text())
            d["files"]["Books.plist"]["file"] = "../../outside"
            p.write_text(json.dumps(d))
            with self.assertRaisesRegex(ValueError, "Unsafe Books"):
                s.load_books()

    def test_cancellation_joins_native_before_recovery(self):
        async def run(root):
            started, finish = asyncio.Event(), asyncio.Event()
            async def native_thread(*args):
                started.set()
                await finish.wait()
                return {"ok": True}
            s = catalog.Session(Mock(), "fixture", root, {})
            with patch.object(asyncio, "to_thread", native_thread):
                task = asyncio.create_task(s.native("stage"))
                await started.wait()
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done(), "Cancellation must not race native writes")
                finish.set()
                with self.assertRaises(asyncio.CancelledError): await task
            events = catalog.read_events(s.journal.path)
            self.assertEqual([e["event"] for e in events],
                             ["native_intent", "native_result", "native_cancelled_after_join"])
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))
