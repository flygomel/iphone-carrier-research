"""Single-session file transport and exact Books backup/return verification."""
import asyncio
import json
import os
from pathlib import Path
import plistlib
import re

import device

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "private"
AIRLOCK = "/var/mobile/Media/Airlock/Book"
BOOKS_FILES = ("Books/Books.plist", "Books/Sync/Books.plist", "Books/Sync/Upload.plist",
               "Books/Sync/Database/OutstandingAssets_4.sqlite",
               "Books/Sync/Database/OutstandingAssets_4.sqlite-shm",
               "Books/Sync/Database/OutstandingAssets_4.sqlite-wal")

def durable_bytes(path, value):
    with path.open("xb") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def read_events(path):
    # An incomplete last line after a crash is deliberately not ignored.
    return [json.loads(line) for line in path.read_text().splitlines()]


def pre_export_recovery_allowed(state, events):
    if state.get("phase") not in ("stage_intent", "export_intent", "pre_export_recovery_intent"):
        return False
    if not any(e.get("event") == "native_intent" and e.get("command") == "stage" for e in events):
        return False
    moves = [i for i, event in enumerate(events) if event.get("event") == "move_intent"]
    if not moves:
        return True
    # These specific helper exits occur before the FileComplete loop. A timeout
    # or an absent response is not evidence that no move happened.
    for event in events[moves[-1] + 1:]:
        result = event.get("result", {})
        if (event.get("event") == "export_worker_result" and result.get("ok") is False
                and result.get("error") in ("expected assets absent from manifest", "SyncAllowed not observed", "ReadyForSync not observed")):
            return True
    return False


def store(path, data):
    temporary = path.with_suffix(".next")
    with temporary.open("w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def worker_result(output):
    for line in reversed(output.decode(errors="replace").splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "ok" in value:
            return value
    raise ValueError("Worker returned no final JSON result")


async def info(afc, path):
    try:
        return await afc.stat(path)
    except Exception as error:
        from pymobiledevice3.services.afc import AfcFileNotFoundError
        if isinstance(error, AfcFileNotFoundError):
            return None
        raise


async def tree(afc, root, *, allow_links=False):
    files, links, dirs = {}, {}, []
    total = 0
    count = 0

    async def visit(path, relative="", depth=0):
        nonlocal total, count
        count += 1
        device.require(depth < 20 and count <= 4096, "Tree limits exceeded")
        meta = await info(afc, path)
        device.require(meta is not None, "Tree object disappeared")
        kind = meta.get("st_ifmt")
        if kind == "S_IFDIR":
            dirs.append(relative)
            for name in sorted(await afc.listdir(path)):
                if name in (".", ".."):
                    continue
                device.safe_name(name)
                device.require("/" not in name, "Unexpected directory entry")
                child = name if not relative else relative + "/" + name
                await visit(path + "/" + name, child, depth + 1)
        elif kind == "S_IFREG":
            size = int(meta["st_size"])
            total += size
            device.require(total <= 256 * 1024 * 1024, "Tree exceeds backup size limit")
            data = await afc.get_file_contents(path)
            device.require(len(data) == size and (await info(afc, path)) == meta,
                            "File changed during read")
            files[relative] = data
        elif kind == "S_IFLNK":
            device.require(allow_links and relative and "/" not in relative,
                            "Unexpected symlink; not followed")
            link = meta.get("LinkTarget")
            device.require(isinstance(link, str) and "/" not in link
                            and link.endswith(".bundle"), "Unexpected bundle link")
            device.safe_name(link)
            links[relative] = link
        else:
            raise ValueError("Unexpected file type")

    initial = await info(afc, root)
    if initial is None:
        return None
    device.require(initial.get("st_ifmt") == "S_IFDIR", "Expected root directory")
    await visit(root)
    return files, links, dirs


async def remove_generated(afc, path):
    device.safe_name(path)
    device.require(re.fullmatch(r"airlift-(?:src|link|recovered)-[0-9a-f]{20}", path.split("/")[0]),
                    "Cleanup outside generated scaffold")
    meta = await info(afc, path)
    if meta is None:
        return
    if meta.get("st_ifmt") == "S_IFDIR":
        for name in await afc.listdir(path):
            if name not in (".", ".."):
                device.safe_name(name)
                device.require("/" not in name, "Unexpected cleanup entry")
                await remove_generated(afc, path + "/" + name)
    await afc.rm_single(path)
    device.require(await info(afc, path) is None, "Cleanup incomplete")


def worker_stopped(state):
    pid = state.get("worker_pid")
    device.require(isinstance(pid, int) and pid > 1, "Worker identity unavailable; manual review required")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise ValueError("Recorded worker may still be running; do not race recovery")


class Session:
    def __init__(self, module, serial, directory, state):
        self.module, self.serial, self.directory, self.state = module, serial, directory, state
        self.journal = device.Journal(directory / "journal.jsonl")
        self.worker = None


    def phase(self, value, **extra):
        self.state.update(phase=value, **extra)
        store(self.directory / "state.json", self.state)
        self.journal.append(value)


    async def native(self, command, *arguments):
        self.journal.append("native_intent", command=command)
        task = asyncio.create_task(asyncio.to_thread(
            self.module.native, command, self.serial, *map(str, arguments)))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancelling to_thread does not stop its native subprocess. Wait for
            # its bounded completion before recovery can touch the same files.
            try:
                result = await task
                self.journal.append("native_result", command=command, result=result)
            finally:
                self.journal.append("native_cancelled_after_join", command=command)
            raise
        self.journal.append("native_result", command=command, result=result)
        device.require(self.module.operation_ok(result), "Native operation failed: " + command)
        return result


    async def await_export(self, afc, present):
        for _ in range(40):
            meta = await info(afc, self.state["exported"])
            if (meta is not None) == present:
                return
            await asyncio.sleep(0.25)
        raise ValueError("Export state did not settle; do not repeat blindly")


    async def return_file(self, afc):
        # A missing exported directory is never proof of a successful return.
        device.require(await info(afc, self.state["exported"]) is not None,
                        "Export absent; return needs manual review")
        device.require(self.worker is not None and self.worker.returncode is None,
                        "Original sync session unavailable; offline recovery review required")
        self.phase("return_intent")
        await self.finish_worker("continue")
        await self.await_export(afc, False)
        self.phase("returned_transport_only")


    async def finish_worker(self, choice):
        device.require(choice == "continue", "Invalid worker choice")
        self.worker.stdin.write((choice + "\n").encode())
        await self.worker.stdin.drain()
        stdout, stderr = await asyncio.wait_for(self.worker.communicate(), 30)
        (self.directory / "return-worker-stdout.txt").write_bytes(stdout)
        (self.directory / "return-worker-stderr.txt").write_bytes(stderr)
        result = worker_result(stdout)
        self.journal.append("return_worker_result", exit_code=self.worker.returncode, result=result)
        device.require(self.worker.returncode == 0 and result.get("ok") is True,
                        "Return worker failed; preserve recovery state")
        return result


    async def export_paused(self, pairs):
        command = [str(self.module.AIRTRAFFIC_HOST), self.serial]
        for source, destination in pairs:
            command.extend([source, destination])
        self.journal.append("move_intent", asset_count=len(pairs), same_session_return=True)
        self.worker = await asyncio.create_subprocess_exec(
            *command, env={**os.environ, "AIRLIFT_PAUSE_AFTER": "2", "AIRLIFT_RETURN_ON_DISCONNECT": "1",
                          "AIRLIFT_ALLOW_CANDIDATE": "0"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self.state["worker_pid"] = self.worker.pid
        store(self.directory / "state.json", self.state)
        async with asyncio.timeout(110):
            while True:
                line = await self.worker.stdout.readline()
                device.require(bool(line), "Export worker ended without pause")
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(result, dict) and ("paused" in result or "ok" in result):
                    break
        self.journal.append("export_worker_result", result=result)
        device.require(result.get("paused") is True and result.get("fileCompleteMessages") == 2,
                        "Export worker did not pause; preserve recovery state")


    async def cleanup(self, afc):
        device.require(self.state["phase"] in ("returned_transport_only", "cleanup_intent"),
                        "Cannot clean before confirmed return")
        before = self.validate_books_backup()
        self.phase("cleanup_intent")
        link_meta = await info(afc, self.state["link"])
        device.require(link_meta is None or link_meta.get("st_ifmt") == "S_IFLNK",
                        "Scaffold link became a directory; preserve it for recovery")
        await remove_generated(afc, self.state["link"])
        await remove_generated(afc, self.state["source"])
        await self.native("restore-books", self.directory / "books-native")
        after = await tree(afc, "Books")
        device.require(after == before, "Full Books tree differs after restore")
        self.phase("complete")


    def load_books(self):
        data = json.loads((self.directory / "books-full.json").read_text())
        if data is None:
            return None
        files = {}
        for name, row in data["files"].items():
            device.safe_name(name)
            device.require(re.fullmatch(r"[0-9]+\.bin", row["file"]), "Unsafe Books backup name")
            value = (self.directory / "books-full" / row["file"]).read_bytes()
            device.require(device.digest(value) == row["sha256"], "Books backup damaged")
            files[name] = value
        return files, {}, data["directories"]


    def validate_books_backup(self):
        before = self.load_books()
        files, directories = ({}, []) if before is None else (before[0], before[2])
        native_root = self.directory / "books-native"
        manifest = plistlib.loads((native_root / "manifest.plist").read_bytes())
        device.require(manifest.get("version") == 1, "Unknown native Books backup")
        for index, path in enumerate(BOOKS_FILES):
            row = manifest["files"][path]
            relative = path.removeprefix("Books/")
            device.require(row["localName"] == f"file-{index}.bin"
                            and row["exists"] == (relative in files), "Books backup manifests disagree")
            if row["exists"]:
                device.require((native_root / row["localName"]).read_bytes() == files[relative],
                                "Native Books backup differs from hashed preimage")
        for path in ("Books", "Books/Sync", "Books/Sync/Database"):
            relative = "" if path == "Books" else path.removeprefix("Books/")
            device.require(manifest["directories"][path] == (relative in directories),
                            "Books directory manifests disagree")
        return before


    async def recover_before_export(self, afc):
        events = read_events(self.directory / "journal.jsonl")
        device.require(pre_export_recovery_allowed(self.state, events),
                        "Cannot infer pre-export safety from this journal")
        if any(e.get("event") == "move_intent" for e in events):
            if self.worker is not None:
                await asyncio.wait_for(self.worker.wait(), 5)
            else:
                worker_stopped(self.state)
        before = self.validate_books_backup()
        for key in ("link", "exported"):
            device.require(await info(afc, self.state[key]) is None,
                            "Unexpected moved object; preserve for recovery")
        self.phase("pre_export_recovery_intent")
        await self.native("restore-books", self.directory / "books-native")
        device.require(before == await tree(afc, "Books"), "Full Books tree differs after restore")
        await remove_generated(afc, self.state["source"])
        self.phase("complete", outcome="aborted_before_export", file_moved=False)


    async def backup_books(self, afc):
        s, d = self.state, self.directory
        await self.native("probe")
        for key in ("source", "link", "exported"):
            device.require(await info(afc, s[key]) is None, "Scaffold collision")
        self.phase("books_backup")
        books = await tree(afc, "Books")
        device.require(books == await tree(afc, "Books"), "Books not stable; close syncing apps")
        (d / "books-full").mkdir()
        record = None
        if books is not None:
            record = {"files": {}, "directories": books[2]}
            for index, (name, value) in enumerate(sorted(books[0].items())):
                filename = str(index) + ".bin"
                durable_bytes(d / "books-full" / filename, value)
                record["files"][name] = {"file": filename, "sha256": device.digest(value)}
        store(d / "books-full.json", record)
        (d / "books-native").mkdir()
        await self.native("snapshot-books", d / "books-native")
        self.validate_books_backup()
        for saved in (d / "books-native").iterdir():
            with saved.open("rb") as stream:
                os.fsync(stream.fileno())


