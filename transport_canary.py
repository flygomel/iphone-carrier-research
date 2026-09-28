#!/usr/bin/env python3
"""Explicitly authorized fresh-file test. Never installs a carrier bundle."""
import argparse
import asyncio
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import experimental_context

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "private"
TARGET = "/var/mobile/Library/Caches"
EXPECTED = {"ProductType": "iPhone18,2", "ProductVersion": "27.2", "BuildVersion": "24B5084k"}


def require(value, message):
    if not value:
        raise ValueError(message)


def complete(result):
    return all(result.get(k) is True for k in (
        "stageSucceeded", "airTrafficSucceeded", "exactBytesRecovered",
        "cleanupComplete", "targetAbsent", "booksPreimageRestored"))


class Journal:
    def __init__(self, path):
        self.path = path

    def append(self, event, **data):
        row = {"time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "event": event, **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


async def identify():
    from pymobiledevice3.usbmux import list_devices
    from pymobiledevice3.lockdown import create_using_usbmux
    devices = [d for d in await list_devices() if d.connection_type == "USB"]
    require(len(devices) == 1, "Connect exactly one unlocked USB iPhone")
    serial = devices[0].serial
    async with await create_using_usbmux(serial=serial, autopair=False, connection_type="USB") as d:
        actual = {k: await d.get_value(key=k) for k in EXPECTED}
        binding = experimental_context.target()
        require(experimental_context.allows(serial, actual) if binding is not None else actual == EXPECTED,
                "Device/build outside selected scope")
    return serial, actual


def load_transport(*, require_binaries=True):
    path = ROOT / "vendor/airlift/airlift.py"
    spec = importlib.util.spec_from_file_location("carrier_airlift", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    require(not require_binaries or (module.DEVICE_HELPER.is_file() and module.AIRTRAFFIC_HOST.is_file()),
            "Build helpers first: make -C vendor/airlift")
    return module


def require_completed_canaries(directory):
    for prior in directory.glob("canary-*/journal.jsonl"):
        events = [json.loads(line) for line in prior.read_text().splitlines()]
        require(events and events[-1]["event"] == "completed",
                "Prior canary is unresolved; review its private journal before retrying")


def run(module, serial, run_dir, journal):
    native = module.native
    run_json = module.run_json

    def tracked_native(command, device, *arguments):
        require(device == serial, "Device binding changed")
        journal.append("native_intent", command=command, arguments=list(arguments))
        result = native(command, device, *arguments)
        journal.append("native_result", command=command, result=result)
        return result

    def tracked_json(command, timeout):
        # Keep UDID only in the private binding file, never in command logs.
        journal.append("process_intent", executable=Path(command[0]).name)
        result = run_json(command, timeout)
        journal.append("process_result", executable=Path(command[0]).name, result=result)
        return result

    module.native = tracked_native
    module.run_json = tracked_json
    os.environ["AIRLIFT_AUDIT_ROOT"] = str(run_dir)
    leaf = "airlift-canary-" + secrets.token_hex(16) + ".bin"
    payload = ("carrier research canary\n" + secrets.token_hex(24) + "\n").encode()
    journal.append("canary_intent", target=TARGET, leaf=leaf, sha256=hashlib.sha256(payload).hexdigest())
    try:
        result = module.attempt(serial, module.normalize_target(TARGET), leaf, payload, verbose=True)
        journal.append("completed" if complete(result) else "recovery_review_required", result=result)
        return result
    finally:
        module.native = native
        module.run_json = run_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirmed-device-write", action="store_true",
                        help="Allow one fresh-file test and temporary Books sync changes")
    args = parser.parse_args()
    if not args.confirmed_device_write:
        parser.error("This test writes to iPhone. Explicit --confirmed-device-write is required.")
    os.umask(0o077)
    PRIVATE.mkdir(exist_ok=True, mode=0o700)
    journal = None
    try:
        with (PRIVATE / "device-operation.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Block a second canary after any unfinished/failed prior run.
            require_completed_canaries(PRIVATE)
            for prior in PRIVATE.glob("catalog-*/state.json"):
                require(json.loads(prior.read_text())["phase"] == "complete",
                        "Prior catalog run is unresolved; recover it before another operation")
            module = load_transport()
            serial, profile = asyncio.run(asyncio.wait_for(identify(), 25))
            run_dir = PRIVATE / ("canary-" + secrets.token_hex(8))
            run_dir.mkdir(mode=0o700)
            (run_dir / "device-binding.json").write_text(json.dumps({"udid": serial, **profile}))
            journal = Journal(run_dir / "journal.jsonl")
            journal.append("started", profile=profile)
            result = run(module, serial, run_dir, journal)
            print(json.dumps({"ok": complete(result), "private_run": str(run_dir),
                              "carrier_bundles_changed": False}))
            return 0 if complete(result) else 2
    except Exception as error:
        if journal:
            journal.append("interrupted_review_required", error_type=type(error).__name__)
        print("Stopped: " + (str(error) if isinstance(error, ValueError) else type(error).__name__),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
