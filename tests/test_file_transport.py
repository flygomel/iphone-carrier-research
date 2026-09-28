import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import file_transport


class FakeAfc:
    def __init__(self):
        self.nodes = {
            "root": {"st_ifmt": "S_IFDIR"},
            "root/A.bundle": {"st_ifmt": "S_IFDIR"},
            "root/A.bundle/f": {"st_ifmt": "S_IFREG", "st_size": 3},
            "root/25701": {"st_ifmt": "S_IFLNK", "LinkTarget": "A.bundle"}}
        self.read = []

    async def stat(self, path):
        return self.nodes[path]

    async def listdir(self, path):
        return [p[len(path)+1:] for p in self.nodes if p.startswith(path + "/")
                and "/" not in p[len(path)+1:]]

    async def get_file_contents(self, path):
        self.read.append(path)
        return b"abc"


class CatalogTests(unittest.TestCase):
    def test_links_not_followed(self):
        afc = FakeAfc()
        data = asyncio.run(file_transport.tree(afc, "root", allow_links=True))
        self.assertEqual(data[1], {"25701": "A.bundle"})
        self.assertEqual(afc.read, ["root/A.bundle/f"])

    def test_books_links_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unexpected symlink"):
            asyncio.run(file_transport.tree(FakeAfc(), "root"))


    def test_cleanup_refuses_user_paths(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            asyncio.run(file_transport.remove_generated(FakeAfc(), "Books"))

    def test_durable_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            file_transport.store(path, {"phase": "return_intent"})
            self.assertEqual(json.loads(path.read_text()), {"phase": "return_intent"})

    def test_no_cleanup_without_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = file_transport.Session(None, "fixture", Path(tmp), {"phase": "exported"})
            with self.assertRaisesRegex(ValueError, "before confirmed return"):
                asyncio.run(s.cleanup(FakeAfc()))

    def test_cleanup_rejects_prefix_collision_and_traversal(self):
        for path in ("airlift-src-personal", "airlift-link-" + "0" * 20 + "/../Books"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                asyncio.run(file_transport.remove_generated(FakeAfc(), path))

    def test_return_failure_keeps_recoverable_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {"phase": "exported", "exported": "export", "link": "link"}
            session = file_transport.Session(Mock(), "fixture", Path(tmp), state)
            session.module.build_books.return_value = b"fixture"
            session.worker = Mock(returncode=None)
            session.worker.stdin.drain = AsyncMock(side_effect=TimeoutError())
            afc = Mock(set_file_contents=AsyncMock())
            with patch.object(file_transport, "info", AsyncMock(return_value={"st_ifmt": "S_IFDIR"})):
                with self.assertRaises(TimeoutError):
                    asyncio.run(session.return_file(afc))
            self.assertEqual(state["phase"], "return_intent")
            self.assertEqual(json.loads((Path(tmp) / "state.json").read_text())["phase"], "return_intent")
            with self.assertRaisesRegex(ValueError, "before confirmed return"):
                asyncio.run(session.cleanup(afc))

    def test_books_mismatch_never_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {"phase": "returned_transport_only", "source": "source", "link": "link"}
            session = file_transport.Session(None, "fixture", Path(tmp), state)
            session.native = AsyncMock()
            session.validate_books_backup = Mock(return_value=({"original": b"data"}, {}, [""]))
            with patch.object(file_transport, "info", AsyncMock(return_value=None)), \
                 patch.object(file_transport, "remove_generated", AsyncMock()), \
                 patch.object(file_transport, "tree", AsyncMock(return_value=({}, {}, [""]))):
                with self.assertRaisesRegex(ValueError, "Books tree differs"):
                    asyncio.run(session.cleanup(None))
            self.assertEqual(state["phase"], "cleanup_intent")

    def test_worker_output_allows_framework_noise(self):
        self.assertEqual(file_transport.worker_result(b'noise\n{"ok": true}\nframework stopped\n'), {"ok": True})
        with self.assertRaisesRegex(ValueError, "no final JSON"):
            file_transport.worker_result(b'framework only\n')

    def test_return_refuses_new_sync_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = file_transport.Session(None, "fixture", Path(tmp), {"phase": "exported", "exported": "fixture"})
            with patch.object(file_transport, "info", AsyncMock(return_value={"st_ifmt": "S_IFDIR"})):
                with self.assertRaisesRegex(ValueError, "Original sync session unavailable"):
                    asyncio.run(session.return_file(None))
            self.assertEqual(session.state["phase"], "exported")

    def test_cleanup_preserves_directory_at_link_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = file_transport.Session(None, "fixture", Path(tmp), {"phase": "returned_transport_only", "link": "fixture"})
            session.validate_books_backup = Mock(return_value=None)
            remove = AsyncMock()
            with patch.object(file_transport, "info", AsyncMock(return_value={"st_ifmt": "S_IFDIR"})), \
                 patch.object(file_transport, "remove_generated", remove):
                with self.assertRaisesRegex(ValueError, "preserve it"):
                    asyncio.run(session.cleanup(None))
            remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
