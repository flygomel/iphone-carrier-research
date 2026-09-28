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

    def test_live_worker_blocks_return_recovery_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.setup_session(Path(tmp)); s.native = AsyncMock()
            with patch.object(catalog, "saved_catalog", return_value=({}, {})), \
                 patch.object(catalog, "worker_stopped", side_effect=ValueError("still running")):
                with self.assertRaisesRegex(ValueError, "still running"):
                    asyncio.run(s.recover_return_by_readback(None))
            s.native.assert_not_called()

    def test_unexpected_scaffold_blocks_blind_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.setup_session(Path(tmp)); s.native = AsyncMock()
            with patch.object(catalog, "saved_catalog", return_value=({}, {})), \
                 patch.object(catalog, "worker_stopped"), \
                 patch.object(catalog, "info", AsyncMock(return_value={"st_ifmt": "S_IFDIR"})):
                with self.assertRaisesRegex(ValueError, "Unexpected scaffold"):
                    asyncio.run(s.recover_return_by_readback(None))
            s.native.assert_not_called()

    def test_readback_mismatch_never_marks_parent_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.setup_session(Path(tmp)); s.native = AsyncMock()
            s.state.update(profile=catalog.canary.EXPECTED)
            child = Mock(directory=Path(tmp)/"child", state={"phase": "complete"}, snapshot=AsyncMock())
            with patch.object(catalog, "saved_catalog", side_effect=[({"old": b"data"}, {}), ({"new": b"data"}, {})]), \
                 patch.object(catalog, "worker_stopped"), \
                 patch.object(catalog, "info", AsyncMock(return_value=None)), \
                 patch.object(catalog, "tree", AsyncMock(return_value=s.load_books())), \
                 patch.object(catalog, "remove_generated", AsyncMock()), \
                 patch.object(catalog, "new_session", return_value=child):
                with self.assertRaisesRegex(ValueError, "differs"):
                    asyncio.run(s.recover_return_by_readback(None))
            self.assertEqual(s.state["phase"], "recovery_verification_started")

    def test_adoption_refuses_changed_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.setup_session(Path(tmp)); afc = Mock(rename=AsyncMock())
            with patch.object(catalog, "info", AsyncMock(return_value=None)), \
                 patch.object(catalog, "tree", AsyncMock(return_value=({"changed": b"x"}, {}, [""]))):
                with self.assertRaisesRegex(ValueError, "differs"):
                    asyncio.run(s.retained_original(afc, "airlift-recovered-" + "b" * 20, ({"original": b"y"}, {})))
            afc.rename.assert_not_called()

    def test_adoption_never_overwrites_observed_current_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.setup_session(Path(tmp)); afc = Mock(rename=AsyncMock())
            with patch.object(catalog, "info", AsyncMock(return_value={"st_ifmt": "S_IFDIR"})):
                with self.assertRaisesRegex(ValueError, "already exists"):
                    asyncio.run(s.retained_original(afc, "airlift-recovered-" + "b" * 20, ({}, {})))
            afc.rename.assert_not_called()

    def test_retained_validation_never_uses_afc_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.setup_session(Path(tmp)); afc = Mock(rename=AsyncMock())
            original = ({"A.bundle/f": b"original"}, {"25701": "A.bundle"}, ["", "A.bundle"])
            source = "airlift-recovered-" + "b" * 20
            with patch.object(catalog, "info", AsyncMock(return_value=None)), \
                 patch.object(catalog, "tree", AsyncMock(return_value=original)):
                asyncio.run(s.retained_original(afc, source, original[:2]))
            afc.rename.assert_not_called()
            self.assertEqual(catalog.read_events(s.journal.path)[-1]["event"], "retained_original_validated")

    def test_only_pre_filecomplete_rejections_allow_cleanup(self):
        events = [{"event": "native_intent", "command": "stage"}, {"event": "move_intent"}]
        state = {"phase": "export_intent"}
        for error, allowed in (("expected assets absent from manifest", True), ("timeout", False)):
            with self.subTest(error=error):
                report = {"event": "export_worker_result", "result": {"ok": False, "error": error}}
                self.assertEqual(catalog.pre_export_recovery_allowed(state, events + [report]), allowed)
        report = {"event": "export_worker_result", "result": {"ok": False, "error": "expected assets absent from manifest"}}
        self.assertFalse(catalog.pre_export_recovery_allowed(state, events + [report, {"event": "move_intent"}]))

    def test_retained_copy_cannot_substitute_for_protected_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.setup_session(Path(tmp)); s.native = AsyncMock()
            s.state.update(profile=catalog.canary.EXPECTED)
            retained = Mock(directory=Path(tmp)/"retained", state={
                "phase": "complete", "retained_original_returned": True}, snapshot=AsyncMock())
            protected = Mock(directory=Path(tmp)/"protected", state={
                "phase": "complete"}, snapshot=AsyncMock())
            expected = ({"original": b"data"}, {})
            with patch.object(catalog, "saved_catalog", side_effect=[expected, ({"different": b"data"}, {})]) as read, \
                 patch.object(catalog, "worker_stopped"), \
                 patch.object(catalog, "info", AsyncMock(return_value=None)), \
                 patch.object(catalog, "tree", AsyncMock(return_value=s.load_books())), \
                 patch.object(catalog, "remove_generated", AsyncMock()), \
                 patch.object(catalog, "new_session", side_effect=[retained, protected]):
                with self.assertRaisesRegex(ValueError, "Protected catalog differs"):
                    asyncio.run(s.recover_return_by_readback(None))
            protected.snapshot.assert_awaited_once_with(None)
            self.assertEqual(read.call_args.args[0], protected.directory)
            self.assertNotEqual(s.state["phase"], "complete")
