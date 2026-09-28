#!/usr/bin/env python3
"""Prepare a device-bound restoration archive OFFLINE; never transfer it."""
import argparse
import io
import json
import os
from pathlib import Path
import zipfile

import carrier
import catalog
import transport_canary


def prepare(run, output):
    state = json.loads((run / "state.json").read_text())
    catalog.check_state(state, state["device"])
    carrier.require(state.get("profile") == transport_canary.EXPECTED, "Unstudied recovery profile")
    path = run / "catalog.zip"
    carrier.require(not path.is_symlink(), "Backup must be a local regular file")
    source = path.read_bytes()
    carrier.require(carrier.digest(source) == state.get("catalog_sha256"), "Backup checksum mismatch")
    expected = carrier.read_catalog(path)
    module = transport_canary.load_transport(require_binaries=False)
    buffer = io.BytesIO(module.build_archive(catalog.PARENT, b"recovery scaffold"))
    with zipfile.ZipFile(buffer, "a") as out, zipfile.ZipFile(io.BytesIO(source)) as original:
        for member in original.infolist():
            # streaming_zip_conduit requires its mode extra field. Unix
            # external_attr alone silently turns symlinks into regular files.
            item = module.zip_info("saved-catalog/" + member.filename, member.external_attr >> 16)
            out.writestr(item, original.read(member))
    result = buffer.getvalue()
    carrier.require(not output.exists(), "Output already exists")
    output.mkdir(mode=0o700, parents=True)
    catalog.durable_bytes(output / "restoration-stage.zip", result)
    catalog.store(output / "plan.json", {
        "device": state["device"], "profile": state["profile"], "target": catalog.TARGET,
        "source_run": str(run.resolve()), "catalog_sha256": state["catalog_sha256"],
        "archive_sha256": carrier.digest(result), "files": len(expected[0]), "links": len(expected[1]),
        "device_access": False, "automatic_restore_supported": False,
        "preserves_permissions_ownership_xattrs": False})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    prepare(args.run, args.out)
    print("Offline restoration archive prepared. No device access. Keep this folder private.")


if __name__ == "__main__":
    main()
