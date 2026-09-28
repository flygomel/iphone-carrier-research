"""USB identity, private journals and shared validation helpers."""
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
EXPECTED = {"ProductType": "iPhone18,2", "ProductVersion": "27.2", "BuildVersion": "24B5084k"}
PROFILE = EXPECTED

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

async def doctor():
    from pymobiledevice3.usbmux import list_devices
    from pymobiledevice3.lockdown import create_using_usbmux
    devices = [d for d in await list_devices() if d.connection_type == "USB"]
    require(len(devices) == 1, "Connect exactly one iPhone by USB and unlock it")
    async with await create_using_usbmux(serial=devices[0].serial, autopair=False,
                                         connection_type="USB") as device:
        result = {k: await device.get_value(key=k)
                  for k in ("ProductType", "ProductVersion", "BuildVersion", "SIMStatus")}
        result["carriers"] = [{k: c.get(k) for k in
                               ("CFBundleIdentifier", "CFBundleVersion", "MCC", "MNC", "Slot")}
                              for c in (await device.get_value(key="CarrierBundleInfoArray") or [])]
    result["matches_research_model_build"] = all(result.get(k) == v for k, v in PROFILE.items())
    result["activation_supported"] = False
    result["read_only"] = True
    return result

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
        require(actual == EXPECTED,
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
    for prior in directory.glob('country-*/state.json'):
        require(json.loads(prior.read_text()).get('phase') == 'complete',
                'Unfinished country operation; preserve its backup and journal: ' + str(prior.parent))
    for prior in directory.glob("canary-*/journal.jsonl"):
        events = [json.loads(line) for line in prior.read_text().splitlines()]
        require(events and events[-1]["event"] == "completed",
                "Prior canary is unresolved; review its private journal before retrying")
