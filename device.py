"""USB identity, private journals and shared validation helpers."""
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
EXPECTED = {"ProductType": "iPhone18,2", "ProductVersion": "27.2", "BuildVersion": "24B5084k"}

def require(condition, message):
    if not condition:
        raise ValueError(message)

def digest(data):
    return hashlib.sha256(data).hexdigest()

def safe_name(name):
    parts = name.split("/")
    require(name and not name.startswith("/") and "\\" not in name
            and all(p not in ("", ".", "..") for p in parts)
            and not any(ord(c) < 32 for c in name), "Invalid archive path")
    return PurePosixPath(name)

async def read_report(connection):
    result = {key: await connection.get_value(key=key)
              for key in (*EXPECTED, "SIMStatus")}
    result["carriers"] = [{key: row.get(key) for key in
                           ("CFBundleIdentifier", "CFBundleVersion", "MCC", "MNC", "Slot")}
                          for row in (await connection.get_value(key="CarrierBundleInfoArray") or [])]
    return result


async def inspect_device():
    from pymobiledevice3.usbmux import list_devices
    from pymobiledevice3.lockdown import create_using_usbmux
    devices = [d for d in await list_devices() if d.connection_type == "USB"]
    require(len(devices) == 1, "Connect exactly one unlocked USB iPhone")
    serial = devices[0].serial
    async with await create_using_usbmux(serial=serial, autopair=False, connection_type="USB") as connection:
        return serial, await read_report(connection)


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

def load_transport(*, require_binaries=True):
    path = ROOT / "vendor/airlift/airlift.py"
    spec = importlib.util.spec_from_file_location("carrier_airlift", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    require(not require_binaries or (module.DEVICE_HELPER.is_file() and module.AIRTRAFFIC_HOST.is_file()),
            "Build helpers first: make -C vendor/airlift")
    return module

def check_legacy_canaries(directory):
    for prior in directory.glob("canary-*/journal.jsonl"):
        events = [json.loads(line) for line in prior.read_text().splitlines()]
        require(events and events[-1]["event"] == "completed",
                "Prior canary is unresolved; review its private journal before retrying")
