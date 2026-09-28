#!/usr/bin/env python3
"""Scoped carrier directory export/return with durable recovery state.

Snapshot is NOT read-only: the directory is temporarily moved through Media.
Only the fixed iPhone carrier directory can be targeted.
"""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import plistlib
import posixpath
import re
import secrets
import sys

import carrier
import transport_canary as canary

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "private"
PARENT = "/var/mobile/Library/Carrier Bundles"
TARGET = PARENT + "/iPhone"
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
        carrier.require(depth < 20 and count <= 4096, "Tree limits exceeded")
        meta = await info(afc, path)
        carrier.require(meta is not None, "Tree object disappeared")
        kind = meta.get("st_ifmt")
        if kind == "S_IFDIR":
            dirs.append(relative)
            for name in sorted(await afc.listdir(path)):
                if name in (".", ".."):
                    continue
                carrier.safe_name(name)
                carrier.require("/" not in name, "Unexpected directory entry")
                child = name if not relative else relative + "/" + name
                await visit(path + "/" + name, child, depth + 1)
        elif kind == "S_IFREG":
            size = int(meta["st_size"])
            total += size
            carrier.require(total <= 256 * 1024 * 1024, "Tree exceeds backup size limit")
            data = await afc.get_file_contents(path)
            carrier.require(len(data) == size and (await info(afc, path)) == meta,
                            "File changed during read")
            files[relative] = data
        elif kind == "S_IFLNK":
            carrier.require(allow_links and relative and "/" not in relative,
                            "Unexpected symlink; not followed")
            link = meta.get("LinkTarget")
            carrier.require(isinstance(link, str) and "/" not in link
                            and link.endswith(".bundle"), "Unexpected bundle link")
            carrier.safe_name(link)
            links[relative] = link
        else:
            raise ValueError("Unexpected file type")

    initial = await info(afc, root)
    if initial is None:
        return None
    carrier.require(initial.get("st_ifmt") == "S_IFDIR", "Expected root directory")
    await visit(root)
    return files, links, dirs


async def remove_generated(afc, path):
    carrier.safe_name(path)
    carrier.require(re.fullmatch(r"airlift-(?:src|link|recovered)-[0-9a-f]{20}", path.split("/")[0]),
                    "Cleanup outside generated scaffold")
    meta = await info(afc, path)
    if meta is None:
        return
    if meta.get("st_ifmt") == "S_IFDIR":
        for name in await afc.listdir(path):
            if name not in (".", ".."):
                carrier.safe_name(name)
                carrier.require("/" not in name, "Unexpected cleanup entry")
                await remove_generated(afc, path + "/" + name)
    await afc.rm_single(path)
    carrier.require(await info(afc, path) is None, "Cleanup incomplete")


def check_state(state, serial):
    carrier.require(state["device"] == serial and state["target"] == TARGET,
                    "Recovery device or target mismatch")
    token = state["token"]
    carrier.require(len(token) == 20 and all(c in "0123456789abcdef" for c in token), "Invalid token")
    for key, prefix in [("source", "airlift-src-"), ("link", "airlift-link-"),
                        ("exported", "airlift-recovered-")]:
        carrier.require(state[key] == prefix + token, "Invalid generated path")


def saved_catalog(directory, state):
    path = directory / "catalog.zip"
    carrier.require(carrier.digest(path.read_bytes()) == state.get("catalog_sha256"),
                    "Catalog backup checksum mismatch")
    return carrier.read_catalog(path)


def worker_stopped(state):
    pid = state.get("worker_pid")
    carrier.require(isinstance(pid, int) and pid > 1, "Worker identity unavailable; manual review required")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise ValueError("Recorded worker may still be running; do not race recovery")


def new_session(module, serial, profile, **extra):
    token = secrets.token_hex(10)
    directory = PRIVATE / ("catalog-" + token)
    directory.mkdir(mode=0o700)
    state = {"device": serial, "profile": profile, "target": TARGET, "token": token,
             "source": "airlift-src-" + token, "link": "airlift-link-" + token,
             "exported": "airlift-recovered-" + token, "phase": "created", **extra}
    if canary.experimental_context.target() is not None:
        state["experimental"] = True
    store(directory / "state.json", state)
    return Session(module, serial, directory, state)


class Session:
    def __init__(self, module, serial, directory, state):
        self.module, self.serial, self.directory, self.state = module, serial, directory, state
        self.journal = canary.Journal(directory / "journal.jsonl")
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
        carrier.require(self.module.operation_ok(result), "Native operation failed: " + command)
        return result

    async def move(self, pairs):
        command = [str(self.module.AIRTRAFFIC_HOST), self.serial]
        for source, destination in pairs:
            command.extend([source, destination])
        self.journal.append("move_intent", asset_count=len(pairs))
        result = await asyncio.to_thread(self.module.run_json, command, 115)
        self.journal.append("move_result", result=result)
        carrier.require(result.get("ok") is True and result.get("exitCode") == 0,
                        "Move result inconclusive; preserve recovery state")

    async def await_export(self, afc, present):
        for _ in range(40):
            meta = await info(afc, self.state["exported"])
            if (meta is not None) == present:
                return
            await asyncio.sleep(0.25)
        raise ValueError("Export state did not settle; do not repeat blindly")

    async def return_directory(self, afc):
        # A missing exported directory is never proof of a successful return.
        carrier.require(await info(afc, self.state["exported"]) is not None,
                        "Export absent; return needs manual review")
        carrier.require(self.worker is not None and self.worker.returncode is None,
                        "Original sync session unavailable; offline recovery review required")
        self.phase("return_intent")
        await self.finish_worker("continue")
        await self.await_export(afc, False)
        self.phase("returned_transport_only")

    async def finish_worker(self, choice):
        carrier.require(choice in ("continue", "candidate"), "Invalid worker choice")
        self.worker.stdin.write((choice + "\n").encode())
        await self.worker.stdin.drain()
        stdout, stderr = await asyncio.wait_for(self.worker.communicate(), 30)
        (self.directory / "return-worker-stdout.txt").write_bytes(stdout)
        (self.directory / "return-worker-stderr.txt").write_bytes(stderr)
        result = worker_result(stdout)
        self.journal.append("return_worker_result", exit_code=self.worker.returncode, result=result)
        carrier.require(self.worker.returncode == 0 and result.get("ok") is True,
                        "Return worker failed; preserve recovery state")
        return result

    async def export_paused(self, pairs, *, allow_candidate=False):
        command = [str(self.module.AIRTRAFFIC_HOST), self.serial]
        for source, destination in pairs:
            command.extend([source, destination])
        self.journal.append("move_intent", asset_count=len(pairs), same_session_return=True)
        self.worker = await asyncio.create_subprocess_exec(
            *command, env={**os.environ, "AIRLIFT_PAUSE_AFTER": "2", "AIRLIFT_RETURN_ON_DISCONNECT": "1",
                          "AIRLIFT_ALLOW_CANDIDATE": "1" if allow_candidate else "0"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self.state["worker_pid"] = self.worker.pid
        store(self.directory / "state.json", self.state)
        async with asyncio.timeout(110):
            while True:
                line = await self.worker.stdout.readline()
                carrier.require(bool(line), "Export worker ended without pause")
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(result, dict) and ("paused" in result or "ok" in result):
                    break
        self.journal.append("export_worker_result", result=result)
        carrier.require(result.get("paused") is True and result.get("fileCompleteMessages") == 2,
                        "Export worker did not pause; preserve recovery state")

    async def cleanup(self, afc):
        carrier.require(self.state["phase"] in ("returned_transport_only", "cleanup_intent"),
                        "Cannot clean before confirmed return")
        before = self.validate_books_backup()
        self.phase("cleanup_intent")
        link_meta = await info(afc, self.state["link"])
        carrier.require(link_meta is None or link_meta.get("st_ifmt") == "S_IFLNK",
                        "Scaffold link became a directory; preserve it for recovery")
        await remove_generated(afc, self.state["link"])
        await remove_generated(afc, self.state["source"])
        await self.native("restore-books", self.directory / "books-native")
        after = await tree(afc, "Books")
        carrier.require(after == before, "Full Books tree differs after restore")
        self.phase("complete")

    def load_books(self):
        data = json.loads((self.directory / "books-full.json").read_text())
        if data is None:
            return None
        files = {}
        for name, row in data["files"].items():
            carrier.safe_name(name)
            carrier.require(re.fullmatch(r"[0-9]+\.bin", row["file"]), "Unsafe Books backup name")
            value = (self.directory / "books-full" / row["file"]).read_bytes()
            carrier.require(carrier.digest(value) == row["sha256"], "Books backup damaged")
            files[name] = value
        return files, {}, data["directories"]

    def validate_books_backup(self):
        before = self.load_books()
        files, directories = ({}, []) if before is None else (before[0], before[2])
        native_root = self.directory / "books-native"
        manifest = plistlib.loads((native_root / "manifest.plist").read_bytes())
        carrier.require(manifest.get("version") == 1, "Unknown native Books backup")
        for index, path in enumerate(BOOKS_FILES):
            row = manifest["files"][path]
            relative = path.removeprefix("Books/")
            carrier.require(row["localName"] == f"file-{index}.bin"
                            and row["exists"] == (relative in files), "Books backup manifests disagree")
            if row["exists"]:
                carrier.require((native_root / row["localName"]).read_bytes() == files[relative],
                                "Native Books backup differs from hashed preimage")
        for path in ("Books", "Books/Sync", "Books/Sync/Database"):
            relative = "" if path == "Books" else path.removeprefix("Books/")
            carrier.require(manifest["directories"][path] == (relative in directories),
                            "Books directory manifests disagree")
        return before

    async def recover_before_export(self, afc):
        events = read_events(self.directory / "journal.jsonl")
        carrier.require(pre_export_recovery_allowed(self.state, events),
                        "Cannot infer pre-export safety from this journal")
        if any(e.get("event") == "move_intent" for e in events):
            if self.worker is not None:
                await asyncio.wait_for(self.worker.wait(), 5)
            else:
                worker_stopped(self.state)
        before = self.validate_books_backup()
        for key in ("link", "exported"):
            carrier.require(await info(afc, self.state[key]) is None,
                            "Unexpected moved object; preserve for recovery")
        self.phase("pre_export_recovery_intent")
        await self.native("restore-books", self.directory / "books-native")
        carrier.require(before == await tree(afc, "Books"), "Full Books tree differs after restore")
        await remove_generated(afc, self.state["source"])
        self.phase("complete", outcome="aborted_before_export", catalog_moved=False)

    async def recover_return_by_readback(self, afc):
        expected = saved_catalog(self.directory, self.state)
        worker_stopped(self.state)
        retained = await info(afc, self.state["exported"]) is not None
        child_name = self.state.get("verification_run")
        if child_name is not None:
            carrier.require(re.fullmatch(r"catalog-[0-9a-f]{20}", child_name), "Invalid verification run")
            previous_path = PRIVATE / child_name
            previous = json.loads((previous_path / "state.json").read_text())
            check_state(previous, self.serial)
            carrier.require(previous.get("recovery_parent") == self.directory.name, "Verification parent mismatch")
            if previous.get("phase") == "complete" and previous.get("outcome") == "aborted_before_export":
                self.state.setdefault("failed_verification_runs", []).append(child_name)
                del self.state["verification_run"]
                child_name = None
                self.phase("recovery_verify_prepare")
        if child_name is None:
            before = self.validate_books_backup()
            link_meta = await info(afc, self.state["link"])
            carrier.require(link_meta is None or link_meta.get("st_ifmt") == "S_IFLNK",
                            "Unexpected scaffold directory; preserve it")
            self.phase("recovery_verify_prepare")
            # Reset the interrupted sync ledger before declaring new assets.
            # Otherwise already-completed identifiers may not be downloadable.
            # The retained export is a separate Media root and stays untouched.
            await self.native("restore-books", self.directory / "books-native")
            carrier.require(before == await tree(afc, "Books"), "Books restore mismatch")
            for key in ("link", "source"):
                await remove_generated(afc, self.state[key])
            child = new_session(self.module, self.serial, self.state["profile"],
                                recovery_parent=self.directory.name)
            self.phase("recovery_verification_started", verification_run=child.directory.name)
            try:
                await child.snapshot(afc, adopt=(self.state["exported"], expected) if retained else None)
            except BaseException as error:
                child.journal.append("stopped", error_type=type(error).__name__)
                if pre_export_recovery_allowed(child.state, read_events(child.journal.path)):
                    await child.recover_before_export(afc)
                elif child.state["phase"] in ("exported", "backup_verified"):
                    await child.return_directory(afc)
                    await child.cleanup(afc)
                raise
        else:
            carrier.require(re.fullmatch(r"catalog-[0-9a-f]{20}", child_name), "Invalid verification run")
            directory = PRIVATE / child_name
            state = json.loads((directory / "state.json").read_text())
            check_state(state, self.serial)
            carrier.require(state.get("recovery_parent") == self.directory.name, "Verification parent mismatch")
            child = Session(self.module, self.serial, directory, state)
        carrier.require(child.state["phase"] == "complete", "Recover verification child first")
        if child.state.get("retained_original_returned"):
            # The prior child read the retained Media copy, not the protected
            # destination. A separate fresh snapshot must prove placement.
            placed = child.directory.name
            child = new_session(self.module, self.serial, self.state["profile"],
                                recovery_parent=self.directory.name)
            self.phase("recovery_verification_started", verification_run=child.directory.name,
                       retained_return_run=placed)
            try:
                await child.snapshot(afc)
            except BaseException as error:
                child.journal.append("stopped", error_type=type(error).__name__)
                if pre_export_recovery_allowed(child.state, read_events(child.journal.path)):
                    await child.recover_before_export(afc)
                elif child.state["phase"] in ("exported", "backup_verified"):
                    await child.return_directory(afc)
                    await child.cleanup(afc)
                raise
        carrier.require(saved_catalog(child.directory, child.state) == expected,
                        "Protected catalog differs; observed catalog was returned unchanged")
        before = self.validate_books_backup()
        await self.native("restore-books", self.directory / "books-native")
        carrier.require(before == await tree(afc, "Books"), "Books restore mismatch")
        # A retained duplicate is deleted only after exact protected readback.
        remaining = await tree(afc, self.state["exported"], allow_links=True)
        if remaining is not None:
            carrier.require(remaining[:2] == expected, "Retained original changed; preserve it")
            await remove_generated(afc, self.state["exported"])
        for key in ("link", "source"):
            await remove_generated(afc, self.state[key])
        self.phase("complete", outcome="recovered_by_protected_readback", verified_by=child.directory.name)

    async def backup_books(self, afc):
        s, d = self.state, self.directory
        await self.native("probe")
        for key in ("source", "link", "exported"):
            carrier.require(await info(afc, s[key]) is None, "Scaffold collision")
        self.phase("books_backup")
        books = await tree(afc, "Books")
        carrier.require(books == await tree(afc, "Books"), "Books not stable; close syncing apps")
        (d / "books-full").mkdir()
        record = None
        if books is not None:
            record = {"files": {}, "directories": books[2]}
            for index, (name, value) in enumerate(sorted(books[0].items())):
                filename = str(index) + ".bin"
                durable_bytes(d / "books-full" / filename, value)
                record["files"][name] = {"file": filename, "sha256": carrier.digest(value)}
        store(d / "books-full.json", record)
        (d / "books-native").mkdir()
        await self.native("snapshot-books", d / "books-native")
        self.validate_books_backup()
        for saved in (d / "books-native").iterdir():
            with saved.open("rb") as stream:
                os.fsync(stream.fileno())

    async def retained_original(self, afc, source, expected):
        carrier.require(re.fullmatch(r"airlift-recovered-[0-9a-f]{20}", source), "Invalid retained original")
        carrier.require(source != self.state["exported"], "Cannot adopt own export")
        carrier.require(await info(afc, self.state["exported"]) is None, "New export already exists")
        original = await tree(afc, source, allow_links=True)
        carrier.require(original is not None and original[:2] == expected,
                        "Retained original differs from backup")
        carrier.require(original == await tree(afc, source, allow_links=True), "Retained original changed")
        self.journal.append("retained_original_validated", source=source)
        return original

    async def snapshot(self, afc, *, adopt=None):
        s, d = self.state, self.directory
        await self.backup_books(afc)
        identifier = posixpath.relpath(TARGET, AIRLOCK)
        pairs = [("../../" + s["source"] + "/p0/p1/p2/link", s["link"]),
                 (identifier, s["exported"]),
                 ("../../" + s["exported"], s["link"] + "/iPhone")]
        if adopt is not None:
            carrier.require(re.fullmatch(r"airlift-recovered-[0-9a-f]{20}", adopt[0]), "Invalid retained source")
            pairs.append(("../../" + adopt[0], s["link"] + "/iPhone"))
        (d / "payload.zip").write_bytes(self.module.build_archive(PARENT, b"unused scaffold payload"))
        identifiers = [p[0] for p in pairs]
        if adopt is not None:
            # Keep previous sync assets declared while recovering a retained
            # original; removing IDs can delete the objects we are recovering.
            before = self.load_books()
            prior = before[0].get("Sync/Books.plist") if before else None
            if prior:
                rows = plistlib.loads(prior).get("Books", [])
                for row in rows:
                    value = row.get("Persistent ID")
                    carrier.require(isinstance(value, str), "Unknown existing Books identifier")
                    if value not in identifiers:
                        identifiers.append(value)
        (d / "Books.plist").write_bytes(self.module.build_books(identifiers))
        self.phase("stage_intent")
        await self.native("stage", s["source"], s["link"], s["exported"],
                          d / "payload.zip", d / "Books.plist", d / "books-native")
        self.phase("export_intent")
        await self.export_paused(pairs, allow_candidate=adopt is not None)
        retained_data = None
        try:
            await self.await_export(afc, True)
        except ValueError:
            if adopt is None:
                raise
            retained_data = await self.retained_original(afc, *adopt)
        self.phase("exported")
        data = retained_data if retained_data is not None else await tree(afc, s["exported"], allow_links=True)
        if retained_data is None:
            carrier.require(data is not None and data == await tree(afc, s["exported"], allow_links=True),
                            "Export not stable")
        # This validator rejects unexpected objects; nothing is extracted on the host.
        carrier.write_catalog(d / "catalog.zip", data[0], data[1])
        with (d / "catalog.zip").open("rb") as saved:
            os.fsync(saved.fileno())
        carrier.require(carrier.read_catalog(d / "catalog.zip") == (data[0], data[1]),
                        "Local backup mismatch")
        self.phase("backup_verified", catalog_sha256=carrier.digest((d / "catalog.zip").read_bytes()))
        if retained_data is not None:
            self.phase("return_intent", retained_source=adopt[0])
            result = await self.finish_worker("candidate")
            carrier.require(result.get("placement") == "candidate", "Retained return was not selected")
            carrier.require(await info(afc, adopt[0]) is None, "Retained original not consumed")
            self.phase("returned_transport_only", retained_original_returned=True)
        else:
            await self.return_directory(afc)
        await self.cleanup(afc)


async def execute(args):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
    module = canary.load_transport()
    serial, profile = await canary.identify()
    if args.command == "snapshot":
        canary.require_completed_canaries(PRIVATE)
        for p in PRIVATE.glob("catalog-*/state.json"):
            carrier.require(json.loads(p.read_text())["phase"] == "complete",
                            "Previous catalog run unresolved; use recover or inspect it")
        created = new_session(module, serial, profile)
        directory, state = created.directory, created.state
    else:
        directory = args.run.resolve()
        carrier.require(directory.parent == PRIVATE.resolve() and directory.name.startswith("catalog-"),
                        "Recovery requires exact local run folder")
        state = json.loads((directory / "state.json").read_text())
    check_state(state, serial)
    carrier.require(state.get("operation", "snapshot") == "snapshot",
                    "Use transaction.py recover for this operation")
    session = Session(module, serial, directory, state)
    async with await create_using_usbmux(serial=serial, autopair=False, connection_type="USB") as dev:
        async with AfcService(dev) as afc:
            try:
                if args.command == "snapshot":
                    await session.snapshot(afc)
                elif state["phase"] == "complete":
                    print("Run already complete; no change.")
                    return
                elif state["phase"] in ("returned_transport_only", "cleanup_intent"):
                    await session.cleanup(afc)
                elif pre_export_recovery_allowed(state, read_events(directory / "journal.jsonl")):
                    await session.recover_before_export(afc)
                elif state["phase"] in ("backup_verified", "return_intent", "recovery_verify_prepare", "recovery_verification_started"):
                    await session.recover_return_by_readback(afc)
                else:
                    carrier.require(state["phase"] in ("exported", "backup_verified", "return_intent", "export_intent"),
                                    "Stage failure requires inspection; no automatic recovery for this phase")
                    await session.return_directory(afc)
                    await session.cleanup(afc)
            except BaseException as error:
                session.journal.append("stopped", error_type=type(error).__name__)
                if pre_export_recovery_allowed(state, read_events(directory / "journal.jsonl")):
                    try:
                        await session.recover_before_export(afc)
                    except BaseException as recovery_error:
                        session.journal.append("recovery_stopped", error_type=type(recovery_error).__name__)
                elif state["phase"] in ("exported", "backup_verified"):
                    # No mutations have occurred. Return a known exported original
                    # even if host-side backup/validation failed.
                    try:
                        await session.return_directory(afc)
                        await session.cleanup(afc)
                        session.journal.append("original_returned_after_error")
                    except BaseException as recovery_error:
                        session.journal.append("recovery_stopped", error_type=type(recovery_error).__name__)
                # Do not delete an exported catalog or erase its last durable phase.
                raise
    print(json.dumps({"run": str(directory), "phase": state["phase"],
                      "catalog_sha256": state.get("catalog_sha256"),
                      "contents_changed": False,
                      "protected_readback_verified": state.get("outcome") == "recovered_by_protected_readback"}))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "recover"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--confirmed-device-write", action="store_true")
        if name == "recover":
            cmd.add_argument("--run", type=Path, required=True)
    args = p.parse_args()
    if not args.confirmed_device_write:
        p.error("Export/return temporarily MOVES files on iPhone; explicit opt-in required")
    os.umask(0o077)
    PRIVATE.mkdir(exist_ok=True)
    try:
        with (PRIVATE / "device-operation.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            asyncio.run(execute(args))
        return 0
    except Exception as error:
        print("Stopped: " + (str(error) if isinstance(error, ValueError) else type(error).__name__), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
