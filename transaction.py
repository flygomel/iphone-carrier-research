#!/usr/bin/env python3
"""Narrow CarrierLab/A1 catalog transaction with fresh readback and rollback."""
import argparse
import asyncio
import fcntl
import io
import json
import os
from pathlib import Path
import plistlib
import posixpath
import sys
import zipfile

import carrier
import catalog
import assets
import transport_canary


def validate_transition(original, candidate):
    files, links = original
    new_files, new_links = candidate
    carrier.require(links.get('25701') == 'mobilkom_by.bundle', 'Expected original A1 mapping')
    carrier.require(new_links == {**links, '25701': 'CarrierLab.bundle'}, 'Unrelated mapping change')
    carrier.require(not any(n.startswith('CarrierLab.bundle/') for n in files), 'Original already contains CarrierLab')
    added = {n: v for n, v in new_files.items() if n not in files}
    carrier.require(added and all(n.startswith('CarrierLab.bundle/') for n in added), 'Unrelated added files')
    carrier.require(new_files == {**files, **added}, 'Original files modified or removed')
    for name in ('Info.plist', 'carrier.plist', 'overrides_V53_V54_V57.plist', 'overrides_V53_V54_V57.der.pri'):
        carrier.require('CarrierLab.bundle/' + name in added, 'Incomplete CarrierLab')
    carrier.require(any(n.startswith('CarrierLab.bundle/signatures/') for n in added), 'Missing signatures')
    lab = plistlib.loads(added['CarrierLab.bundle/Info.plist'])
    a1 = plistlib.loads(files['mobilkom_by.bundle/Info.plist'])
    carrier.require(lab.get('CFBundleIdentifier') == 'com.apple.CarrierLab' and lab.get('CFBundleVersion') == '72.7.1', 'Wrong CarrierLab')
    carrier.require(a1.get('CFBundleIdentifier') == 'com.apple.mobilkom_by' and a1.get('CFBundleVersion') == '72.7.1', 'Wrong A1')


def original_mapping(current):
    files, links = current
    carrier.require(links.get('25701') == 'CarrierLab.bundle', 'CarrierLab is not mapped to A1')
    original = ({n: v for n, v in files.items() if not n.startswith('CarrierLab.bundle/')},
                {**links, '25701': 'mobilkom_by.bundle'})
    carrier.require(not any(t == 'CarrierLab.bundle' for t in original[1].values()), 'Other SIM uses CarrierLab')
    validate_transition(original, current)
    return original


def save_archive(path, contents):
    carrier.write_catalog(path, *contents)
    with path.open('rb') as f: os.fsync(f.fileno())
    return carrier.digest(path.read_bytes())


def stage_archive(module, candidate_path):
    buffer = io.BytesIO(module.build_archive(catalog.PARENT, b'catalog transaction scaffold'))
    with zipfile.ZipFile(buffer, 'a') as target, zipfile.ZipFile(candidate_path) as source:
        for entry in source.infolist():
            target.writestr(module.zip_info('candidate/' + entry.filename, entry.external_attr >> 16), source.read(entry))
    return buffer.getvalue()


def load_bound_run(path, serial):
    path = path.resolve()
    carrier.require(path.parent == catalog.PRIVATE.resolve(), 'Use a local private run')
    state = json.loads((path / 'state.json').read_text())
    catalog.check_state(state, serial)
    carrier.require(state.get('profile') == transport_canary.EXPECTED, 'Wrong source profile')
    return path, state


class Transaction(catalog.Session):
    async def apply(self, afc, original, candidate):
        s, d = self.state, self.directory
        s.update(catalog_sha256=save_archive(d / 'catalog.zip', original),
                 candidate_sha256=save_archive(d / 'candidate.zip', candidate))
        self.phase('planned')
        await self.backup_books(afc)
        pairs = [('../../' + s['source'] + '/p0/p1/p2/link', s['link']),
                 (posixpath.relpath(catalog.TARGET, catalog.AIRLOCK), s['exported']),
                 ('../../' + s['exported'], s['link'] + '/iPhone'),
                 ('../../' + s['source'] + '/candidate', s['link'] + '/iPhone')]
        catalog.durable_bytes(d / 'payload.zip', stage_archive(self.module, d / 'candidate.zip'))
        catalog.durable_bytes(d / 'Books.plist', self.module.build_books([p[0] for p in pairs]))
        self.phase('stage_intent')
        await self.native('stage', s['source'], s['link'], s['exported'], d / 'payload.zip', d / 'Books.plist', d / 'books-native')
        staged = await catalog.tree(afc, s['source'] + '/candidate', allow_links=True)
        carrier.require(staged is not None and staged[:2] == candidate, 'Staged candidate differs')
        self.phase('export_intent')
        await self.export_paused(pairs, allow_candidate=True)
        await self.await_export(afc, True)
        self.phase('exported')
        actual = await catalog.tree(afc, s['exported'], allow_links=True)
        carrier.require(actual is not None and actual == await catalog.tree(afc, s['exported'], allow_links=True), 'Unstable original')
        if actual[:2] != original:
            # Preserve concurrent changes; the predeclared default returns this
            # exact observed original, never the planned candidate.
            self.phase('stale_baseline')
            await self.return_directory(afc)
            await self.cleanup(afc)
            self.phase('complete', outcome='stale_baseline_returned')
            raise ValueError('Catalog changed since snapshot; candidate not applied')
        self.phase('backup_verified')
        self.phase('apply_intent')
        result = await self.finish_worker('candidate')
        carrier.require(result.get('placement') == 'candidate', 'Worker did not select candidate')
        carrier.require(await catalog.info(afc, s['source'] + '/candidate') is None, 'Candidate not consumed')
        self.phase('placement_transport_only')
        await self.verify(afc)

    async def verify(self, afc):
        s, d = self.state, self.directory
        catalog.worker_stopped(s)
        original = catalog.saved_catalog(d, s)
        candidate_path = d / 'candidate.zip'
        carrier.require(carrier.digest(candidate_path.read_bytes()) == s['candidate_sha256'], 'Candidate backup corrupted')
        candidate = carrier.read_catalog(candidate_path)
        before = self.validate_books_backup()
        link = await catalog.info(afc, s['link'])
        carrier.require(link is None or link.get('st_ifmt') == 'S_IFLNK', 'Unexpected scaffold directory')
        child_name = s.get('verification_run')
        if child_name is None:
            self.phase('transaction_verify_prepare')
            await self.native('restore-books', d / 'books-native')
            carrier.require(before == await catalog.tree(afc, 'Books'), 'Books mismatch')
            if link is not None: await catalog.remove_generated(afc, s['link'])
            child = catalog.new_session(self.module, self.serial, s['profile'], transaction_parent=d.name)
            self.phase('transaction_verification_started', verification_run=child.directory.name)
            try:
                await child.snapshot(afc)
            except BaseException:
                if child.state['phase'] in ('exported', 'backup_verified'):
                    await child.return_directory(afc); await child.cleanup(afc)
                raise
        else:
            path, state = load_bound_run(catalog.PRIVATE / child_name, self.serial)
            carrier.require(state.get('transaction_parent') == d.name, 'Verification parent mismatch')
            child = catalog.Session(self.module, self.serial, path, state)
        carrier.require(child.state['phase'] == 'complete', 'Recover verification child first')
        observed = catalog.saved_catalog(child.directory, child.state)
        carrier.require(observed == original or observed == candidate, 'Unexpected protected catalog; all backups preserved')
        remaining = await catalog.tree(afc, s['exported'], allow_links=True)
        if remaining is not None:
            carrier.require(remaining[:2] == original, 'Retained original changed')
            await catalog.remove_generated(afc, s['exported'])
        for key in ('link', 'source'): await catalog.remove_generated(afc, s[key])
        carrier.require(before == await catalog.tree(afc, 'Books'), 'Books mismatch after verification')
        self.phase('complete', outcome='applied' if observed == candidate else 'original_returned',
                   verified_by=child.directory.name)


async def execute(args):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
    module = transport_canary.load_transport()
    serial, profile = await transport_canary.identify()
    if args.command == 'recover':
        path, state = load_bound_run(args.run, serial)
        carrier.require(state.get('operation') == 'catalog_transaction', 'Not a transaction run')
        session = Transaction(module, serial, path, state)
        if state['phase'] == 'complete':
            print(json.dumps({'run': str(path), 'phase': 'complete', 'outcome': state.get('outcome')})); return
    else:
        transport_canary.require_completed_canaries(catalog.PRIVATE)
        for p in catalog.PRIVATE.glob('catalog-*/state.json'):
            carrier.require(json.loads(p.read_text())['phase'] == 'complete', 'Resolve previous run first')
        path, state = load_bound_run(args.run, serial)
        carrier.require(state['phase'] == 'complete', 'Source run incomplete')
        if args.command == 'rollback':
            carrier.require(state.get('operation') == 'catalog_transaction' and state.get('outcome') == 'applied', 'No verified transaction to roll back')
            original = carrier.read_catalog(path / 'candidate.zip')
            carrier.require(carrier.digest((path / 'candidate.zip').read_bytes()) == state['candidate_sha256'], 'Candidate backup corrupted')
            candidate = catalog.saved_catalog(path, state)
        else:
            original = catalog.saved_catalog(path, state)
            if args.command == 'restore-a1':
                candidate = original_mapping(original)
            else:
                carrier.require(carrier.read_catalog(args.plan / 'original-catalog.zip') == original, 'Plan baseline differs from bound snapshot')
                candidate = carrier.read_catalog(args.plan / 'candidate-catalog.zip')
                validate_transition(original, candidate)
                locked = json.loads(assets.SOURCES.read_text())['bundles']['CarrierLab.bundle']
                observed = {n.removeprefix('CarrierLab.bundle/'): carrier.digest(v)
                            for n, v in candidate[0].items() if n.startswith('CarrierLab.bundle/')}
                carrier.require(observed == locked, 'CarrierLab differs from pinned original files')
        created = catalog.new_session(module, serial, profile, operation='catalog_transaction',
                                      source_run=path.name, action=args.command)
        session = Transaction(module, serial, created.directory, created.state)
    async with await create_using_usbmux(serial=serial, autopair=False, connection_type='USB') as dev:
        async with AfcService(dev) as afc:
            try:
                if args.command != 'recover': await session.apply(afc, original, candidate)
                elif catalog.pre_export_recovery_allowed(session.state, catalog.read_events(session.journal.path)):
                    await session.recover_before_export(afc)
                elif session.state['phase'] in ('backup_verified', 'return_intent', 'recovery_verify_prepare', 'recovery_verification_started'):
                    await session.recover_return_by_readback(afc)
                elif session.state['phase'] in ('returned_transport_only', 'cleanup_intent'):
                    await session.cleanup(afc)
                else:
                    carrier.require(session.state['phase'] in ('apply_intent', 'placement_transport_only', 'transaction_verify_prepare', 'transaction_verification_started'),
                                    'Pre-commit interrupted export requires original recovery; do not overwrite')
                    await session.verify(afc)
            except BaseException as error:
                session.journal.append('stopped', error_type=type(error).__name__)
                if catalog.pre_export_recovery_allowed(session.state, catalog.read_events(session.journal.path)):
                    await session.recover_before_export(afc)
                elif session.state['phase'] in ('exported', 'backup_verified') and session.worker is not None and session.worker.returncode is None:
                    await session.return_directory(afc); await session.cleanup(afc)
                raise
    print(json.dumps({'run': str(session.directory), 'phase': session.state['phase'],
                      'outcome': session.state.get('outcome'), 'verified_by': session.state.get('verified_by')}))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('apply', 'restore-a1', 'rollback', 'recover'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--run', type=Path, required=True)
        cmd.add_argument('--confirmed-device-write', action='store_true')
        if name == 'apply': cmd.add_argument('--plan', type=Path, required=True)
    args = p.parse_args()
    if not args.confirmed_device_write: p.error('Explicit device-write opt-in required')
    os.umask(0o077)
    try:
        with (catalog.PRIVATE / 'device-operation.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            asyncio.run(execute(args))
    except Exception as error:
        print('Stopped: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
