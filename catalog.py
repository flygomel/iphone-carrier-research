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
    carrier.require(re.fullmatch(r"airlift-(?:src|link)-[0-9a-f]{20}", path.split("/")[0]),
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


class Session:
    def __init__(self, module, serial, directory, state):
        self.module, self.serial, self.directory, self.state = module, serial, directory, state
        self.journal = canary.Journal(directory / "journal.jsonl")

    def phase(self, value, **extra):
        self.state.update(phase=value, **extra)
        store(self.directory / "state.json", self.state)
        self.journal.append(value)

    async def native(self, command, *arguments):
        self.journal.append("native_intent", command=command)
        result = await asyncio.to_thread(self.module.native, command, self.serial, *map(str, arguments))
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
        self.phase("return_intent")
        identifier = "../../" + self.state["exported"]
        await afc.set_file_contents("Books/Sync/Books.plist", self.module.build_books([identifier]))
        await self.move([(identifier, self.state["link"] + "/iPhone")])
        await self.await_export(afc, False)
        self.phase("returned_transport_only")

    async def cleanup(self, afc):
        carrier.require(self.state["phase"] in ("returned_transport_only", "cleanup_intent"),
                        "Cannot clean before confirmed return")
        self.phase("cleanup_intent")
        await remove_generated(afc, self.state["link"])
        await remove_generated(afc, self.state["source"])
        await self.native("restore-books", self.directory / "books-native")
        before = self.load_books()
        after = await tree(afc, "Books")
        carrier.require(after == before, "Full Books tree differs after restore")
        self.phase("complete")

    def load_books(self):
        data = json.loads((self.directory / "books-full.json").read_text())
        if data is None:
            return None
        files = {}
        for name, row in data["files"].items():
            value = (self.directory / "books-full" / row["file"]).read_bytes()
            carrier.require(carrier.digest(value) == row["sha256"], "Books backup damaged")
            files[name] = value
        return files, {}, data["directories"]

    async def snapshot(self, afc):
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
                (d / "books-full" / filename).write_bytes(value)
                record["files"][name] = {"file": filename, "sha256": carrier.digest(value)}
        store(d / "books-full.json", record)
        (d / "books-native").mkdir()
        await self.native("snapshot-books", d / "books-native")
        identifier = posixpath.relpath(TARGET, AIRLOCK)
        pairs = [("../../" + s["source"] + "/p0/p1/p2/link", s["link"]),
                 (identifier, s["exported"])]
        (d / "payload.zip").write_bytes(self.module.build_archive(PARENT, b"unused scaffold payload"))
        (d / "Books.plist").write_bytes(self.module.build_books([p[0] for p in pairs]))
        self.phase("stage_intent")
        await self.native("stage", s["source"], s["link"], s["exported"],
                          d / "payload.zip", d / "Books.plist", d / "books-native")
        self.phase("export_intent")
        await self.move(pairs)
        await self.await_export(afc, True)
        self.phase("exported")
        data = await tree(afc, s["exported"], allow_links=True)
        carrier.require(data is not None and data == await tree(afc, s["exported"], allow_links=True),
                        "Export not stable")
        # This validator rejects unexpected objects; nothing is extracted on the host.
        carrier.write_catalog(d / "catalog.zip", data[0], data[1])
        carrier.require(carrier.read_catalog(d / "catalog.zip") == (data[0], data[1]),
                        "Local backup mismatch")
        self.phase("backup_verified", catalog_sha256=carrier.digest((d / "catalog.zip").read_bytes()))
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
        token = secrets.token_hex(10)
        directory = PRIVATE / ("catalog-" + token)
        directory.mkdir(mode=0o700)
        state = {"device": serial, "profile": profile, "target": TARGET, "token": token,
                 "source": "airlift-src-" + token, "link": "airlift-link-" + token,
                 "exported": "airlift-recovered-" + token, "phase": "created"}
        store(directory / "state.json", state)
    else:
        directory = args.run.resolve()
        carrier.require(directory.parent == PRIVATE.resolve() and directory.name.startswith("catalog-"),
                        "Recovery requires exact local run folder")
        state = json.loads((directory / "state.json").read_text())
    check_state(state, serial)
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
                else:
                    carrier.require(state["phase"] in ("exported", "backup_verified", "return_intent", "export_intent"),
                                    "Stage failure requires inspection; no automatic recovery for this phase")
                    await session.return_directory(afc)
                    await session.cleanup(afc)
            except BaseException as error:
                session.journal.append("stopped", error_type=type(error).__name__)
                if state["phase"] in ("exported", "backup_verified"):
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
                      "contents_changed": False, "protected_readback_verified": False}))


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
