#!/usr/bin/env python3
"""Read-only USB diagnostics and offline catalog planning. No device writes."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import stat
import sys
import zipfile

PROFILE = {"ProductType": "iPhone18,2", "BuildVersion": "24B5084k"}
MAX_TOTAL = 32 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def manifest(files, links):
    return {"files": {n: {"bytes": len(v), "sha256": digest(v)}
                       for n, v in sorted(files.items())},
            "symlinks": dict(sorted(links.items()))}


def safe_name(name):
    parts = name.split("/")
    require(name and not name.startswith("/") and "\\" not in name
            and all(p not in ("", ".", "..") for p in parts)
            and not any(ord(c) < 32 for c in name), "Invalid archive path")
    return PurePosixPath(name)


def read_catalog(path):
    files, links = {}, {}
    with zipfile.ZipFile(path) as archive:
        require(len(archive.infolist()) <= 4096, "Too many ZIP entries")
        require(sum(i.file_size for i in archive.infolist()) <= MAX_TOTAL,
                "ZIP exceeds offline size limit")
        seen = set()
        for entry in archive.infolist():
            require(not entry.flag_bits & 1, "Encrypted ZIP unsupported")
            raw = entry.filename[:-1] if entry.is_dir() else entry.filename
            p = safe_name(raw)
            require(raw not in seen, "Duplicate ZIP path")
            seen.add(raw)
            mode = entry.external_attr >> 16
            if entry.is_dir():
                require(stat.S_IFMT(mode) in (0, stat.S_IFDIR), "Invalid directory")
                continue
            data = archive.read(entry)
            if stat.S_ISLNK(mode):
                target = data.decode("utf-8")
                require(len(p.parts) == 1 and len(safe_name(target).parts) == 1
                        and target.endswith(".bundle"), "Unsafe bundle link")
                links[raw] = target
            else:
                require(stat.S_IFMT(mode) in (0, stat.S_IFREG), "Special ZIP file")
                require(len(p.parts) >= 2 and p.parts[0].endswith(".bundle"),
                        "Expected catalog root, not an IPCC Payload directory")
                files[raw] = data
    for target in links.values():
        require(any(n.startswith(target + "/") for n in files), "Dangling bundle link")
    return files, links


def bundle_files(path):
    require(path.is_dir() and not path.is_symlink(), "Expected regular bundle directory")
    files = {}
    total = 0
    for p in sorted(path.rglob("*")):
        require(not p.is_symlink(), "Bundle must not contain symlinks")
        if p.is_dir():
            continue
        require(p.is_file(), "Bundle contains a special file")
        total += p.stat().st_size
        require(total <= MAX_TOTAL, "Bundle too large")
        name = p.relative_to(path).as_posix()
        safe_name(name)
        files["CarrierLab.bundle/" + name] = p.read_bytes()
    for name in ("Info.plist", "carrier.plist", "overrides_V53_V54_V57.plist",
                 "overrides_V53_V54_V57.der.pri"):
        require("CarrierLab.bundle/" + name in files, "Missing " + name)
    require(any(n.startswith("CarrierLab.bundle/signatures/") for n in files),
            "Signature files missing (presence is not signature verification)")
    info = plistlib.loads(files["CarrierLab.bundle/Info.plist"])
    require(info.get("CFBundleIdentifier") == "com.apple.CarrierLab"
            and info.get("CFBundleVersion") == "72.7.1", "Wrong CarrierLab version")
    return files


def make_plan(original, lab, state):
    require(all(state.get(k) == v for k, v in PROFILE.items()), "Unstudied device/build")
    carriers = state.get("carriers", [])
    require(any(c.get("CFBundleIdentifier") == "com.apple.mobilkom_by"
                and c.get("CFBundleVersion") == "72.7.1"
                and c.get("MCC") == "257" and c.get("MNC") == "01"
                for c in carriers), "Expected A1 72.7.1 metadata")
    require(any(c.get("CFBundleIdentifier") == "com.apple.life_by"
                and c.get("CFBundleVersion") == "72.7"
                and c.get("MCC") == "257" and c.get("MNC") == "04"
                for c in carriers), "Expected life 72.7 metadata")
    files, links = original
    require(links.get("25701") == "mobilkom_by.bundle", "A1 mapping differs")
    require(not any(n.startswith("CarrierLab.bundle/") for n in files),
            "CarrierLab already exists; do not overwrite")
    info = plistlib.loads(files.get("mobilkom_by.bundle/Info.plist", b""))
    require(info.get("CFBundleIdentifier") == "com.apple.mobilkom_by"
            and info.get("CFBundleVersion") == "72.7.1", "Baseline A1 bundle differs")
    changed = {**files, **lab}, {**links, "25701": "CarrierLab.bundle"}
    return changed


def write_catalog(path, files, links):
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            item = zipfile.ZipInfo(name)
            item.create_system = 3
            item.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(item, data)
        for name, target in sorted(links.items()):
            item = zipfile.ZipInfo(name)
            item.create_system = 3
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(item, target.encode())
    require(read_catalog(path) == (files, links), "Archive readback mismatch")


def save_json(path, value):
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


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


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="Read selected USB metadata; never pair or write")
    d.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("plan", help="Build offline candidate and original catalog ZIPs")
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--carrierlab", type=Path, required=True)
    p.add_argument("--device-report", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            require(not args.out.exists(), "Output exists; choose a fresh report filename")
            args.out.parent.mkdir(parents=True, exist_ok=True)
            result = asyncio.run(asyncio.wait_for(doctor(), timeout=25))
            save_json(args.out, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            original = read_catalog(args.baseline)
            lab = bundle_files(args.carrierlab)
            state = json.loads(args.device_report.read_text())
            candidate = make_plan(original, lab, state)
            require(not args.out.exists(), "Plan directory exists; choose a new directory")
            args.out.mkdir(parents=True)
            write_catalog(args.out / "original-catalog.zip", *original)
            write_catalog(args.out / "candidate-catalog.zip", *candidate)
            report = {
                "device_access": False, "device_write_supported": False,
                "signature_verified": False, "snapshot_freshness_verified": False,
                "profile": PROFILE,
                "baseline_sha256": digest(args.baseline.read_bytes()),
                "original": manifest(*original), "candidate": manifest(*candidate),
                "changed_links": {"25701": {"before": "mobilkom_by.bundle", "after": "CarrierLab.bundle"}},
                "added_files": sorted(lab),
                "archives": {n: digest((args.out / n).read_bytes()) for n in
                             ("original-catalog.zip", "candidate-catalog.zip")}}
            save_json(args.out / "plan.json", report)
            print("Offline plan verified. No phone access. These ZIPs are NOT installable IPCCs.")
        return 0
    except Exception as error:
        # Third-party exception text may contain personal device metadata.
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        print("Stopped: " + detail, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
