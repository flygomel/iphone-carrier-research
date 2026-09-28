#!/usr/bin/env python3
"""Prepare hash-pinned original Apple assets on the Mac. No iPhone access."""
import argparse
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
import urllib.parse
import urllib.request
import zipfile

import carrier
import catalog

SOURCES = Path(__file__).with_name('sources.json')


def locked_bundle(root, name, locks):
    directory = root / name
    carrier.require(directory.is_dir() and not directory.is_symlink(), 'Missing regular bundle: ' + name)
    files = {}
    for p in sorted(directory.rglob('*')):
        carrier.require(not p.is_symlink(), 'Source bundle contains a symlink')
        if p.is_dir(): continue
        carrier.require(p.is_file(), 'Unexpected source object')
        key = p.relative_to(directory).as_posix()
        carrier.safe_name(key)
        carrier.require(key in locks, 'Unexpected source file: ' + key)
        carrier.require(p.stat().st_size <= carrier.MAX_TOTAL, 'Source file exceeds size limit')
        value = p.read_bytes()
        carrier.require(carrier.digest(value) == locks[key], 'Wrong source bytes: ' + key)
        files[key] = value
    carrier.require(set(files) == set(locks), 'Missing source files')
    return files


def a1_ipcc(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, value in sorted(files.items()):
            item = zipfile.ZipInfo('Payload/mobilkom_by.bundle/' + name)
            item.create_system = 3; item.external_attr = 0o100644 << 16
            archive.writestr(item, value)
    return buffer.getvalue()


def prepare(root, output, sources):
    bundles = {name: locked_bundle(root, name, locks) for name, locks in sources['bundles'].items()}
    carrier.require(not output.exists(), 'Output exists')
    output.mkdir(mode=0o700, parents=True)
    for name, files in bundles.items():
        for relative, value in files.items():
            p = output / name / relative; p.parent.mkdir(parents=True, exist_ok=True)
            catalog.durable_bytes(p, value)
    catalog.durable_bytes(output / 'A1-72.7.1.ipcc', a1_ipcc(bundles['mobilkom_by.bundle']))
    catalog.store(output / 'assets.json', {'profile': sources['profile'], 'files_verified': True,
                                         'apple_signature_verified': False, 'device_access': False})


class AppleRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urllib.parse.urlparse(newurl)
        carrier.require(url.scheme == 'https' and url.hostname == 'updates.cdn-apple.com', 'Unexpected download redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_docomo(output, source):
    carrier.require(not output.exists(), 'Output exists')
    url = urllib.parse.urlparse(source['url'])
    carrier.require(url.scheme == 'https' and url.hostname == 'updates.cdn-apple.com', 'Untrusted asset host')
    with urllib.request.build_opener(AppleRedirects).open(source['url'], timeout=30) as response:
        data = response.read(source['size'] + 1)
    carrier.require(len(data) == source['size'] and carrier.digest(data) == source['sha256'], 'Downloaded package differs')
    output.parent.mkdir(parents=True, exist_ok=True)
    catalog.durable_bytes(output, data)


def fetch_all(output, sources, *, ipsw_binary=None):
    """Download from official sources, extract on Mac, verify every retained byte."""
    output = output.resolve()
    work = output.parent / (output.name + '-download')
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.exists():
        bundles = {name: locked_bundle(output, name, locks) for name, locks in sources['bundles'].items()}
        a1 = output / 'A1-72.7.1.ipcc'
        expected = a1_ipcc(bundles['mobilkom_by.bundle'])
        if not a1.exists(): catalog.durable_bytes(a1, expected)
        carrier.require(a1.read_bytes() == expected, 'Cached A1 package differs')
        package = output / 'Docomo-69.1.ipcc'
        if not package.exists(): fetch_docomo(package, sources['docomo'])
        carrier.require(carrier.digest(package.read_bytes()) == sources['docomo']['sha256'], 'Cached Docomo differs')
        return
    if ipsw_binary is None:
        tool = sources['ipsw_tool']
        archive = work / 'ipsw.tar.gz'
        if not archive.exists():
            print('Скачиваю закреплённую версию ipsw (около 55 МБ)…', flush=True)
            with urllib.request.urlopen(tool['url'], timeout=60) as response:
                data = response.read(tool['size'] + 1)
            carrier.require(len(data) == tool['size'] and carrier.digest(data) == tool['sha256'], 'ipsw download differs')
            catalog.durable_bytes(archive, data)
        carrier.require(carrier.digest(archive.read_bytes()) == tool['sha256'], 'Cached ipsw archive differs')
        with tarfile.open(archive) as tar:
            members = [m for m in tar.getmembers() if m.name in ('ipsw', './ipsw') and m.isfile()]
            carrier.require(len(members) == 1 and members[0].size < 200_000_000, 'Unexpected ipsw archive')
            binary_data = tar.extractfile(members[0]).read()
        ipsw_binary = work / 'ipsw'
        if not ipsw_binary.exists(): catalog.durable_bytes(ipsw_binary, binary_data)
        carrier.require(ipsw_binary.read_bytes() == binary_data, 'Cached ipsw executable differs')
        ipsw_binary.chmod(0o700)
    extracted = work / 'extracted'
    roots = list(extracted.rglob('CarrierLab.bundle')) if extracted.exists() else []
    valid = []
    for lab in roots:
        try:
            for name, locks in sources['bundles'].items(): locked_bundle(lab.parent, name, locks)
            valid.append(lab.parent)
        except ValueError: pass
    if not valid:
        carrier.require(shutil.disk_usage(work).free >= 40 * 1024**3, 'Для извлечения прошивки нужно не менее 40 ГБ свободного места')
        print('Извлекаю оригинальные пакеты из прошивки Apple. Это может скачать несколько ГБ и занять десятки минут.', flush=True)
        subprocess.run([str(Path(ipsw_binary).resolve()), 'extract', '--remote', '--files',
                        '--pattern', r'System/Library/Carrier Bundles/iPhone/(CarrierLab|mobilkom_by)\.bundle/',
                        '--output', str(extracted), sources['ipsw_url']], check=True)
        for lab in extracted.rglob('CarrierLab.bundle'):
            try:
                for name, locks in sources['bundles'].items(): locked_bundle(lab.parent, name, locks)
                valid.append(lab.parent)
            except ValueError: pass
    carrier.require(valid, 'Matching Apple bundle files were not extracted')
    prepare(valid[0], output, sources)
    fetch_docomo(output / 'Docomo-69.1.ipcc', sources['docomo'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    prepare_cmd = sub.add_parser('prepare')
    prepare_cmd.add_argument('--carrier-root', type=Path, required=True)
    prepare_cmd.add_argument('--out', type=Path, required=True)
    docomo = sub.add_parser('fetch-docomo'); docomo.add_argument('--out', type=Path, required=True)
    fetch = sub.add_parser('fetch'); fetch.add_argument('--out', type=Path, required=True)
    args = p.parse_args(); os.umask(0o077)
    sources = json.loads(SOURCES.read_text())
    if args.command == 'prepare': prepare(args.carrier_root, args.out, sources)
    elif args.command == 'fetch': fetch_all(args.out, sources)
    else: fetch_docomo(args.out, sources['docomo'])
    print('Original asset bytes verified. No device access.')


if __name__ == '__main__':
    main()
