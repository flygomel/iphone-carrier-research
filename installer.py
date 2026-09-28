#!/usr/bin/env python3
"""Install only hash-pinned original A1 or Docomo IPCC; journal every response."""
import argparse
import asyncio
import fcntl
import io
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import sys
import zipfile

import assets
import carrier
import catalog
import transport_canary


def package_files(path, kind, sources):
    data = path.read_bytes()
    carrier.require(len(data) <= carrier.MAX_TOTAL, 'Package too large')
    bundle = 'mobilkom_by.bundle' if kind == 'a1' else 'Docomo_jp.bundle'
    if kind == 'docomo':
        carrier.require(carrier.digest(data) == sources['docomo']['sha256'], 'Wrong Docomo bytes')
    files = {}
    seen = set()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        carrier.require(len(entries) <= 4096 and sum(e.file_size for e in entries) <= carrier.MAX_TOTAL, 'Archive too large')
        for e in entries:
            name = e.filename.rstrip('/')
            p = carrier.safe_name(name)
            carrier.require(name not in seen and not e.flag_bits & 1, 'Duplicate or encrypted entry')
            seen.add(name)
            carrier.require(p.parts == ('Payload',) and e.is_dir() or p.parts[:2] == ('Payload', bundle), 'Unexpected package path')
            mode = stat.S_IFMT(e.external_attr >> 16)
            carrier.require(mode in (0, stat.S_IFDIR if e.is_dir() else stat.S_IFREG), 'Special archive entry')
            if not e.is_dir(): files[name] = archive.read(e)
    carrier.require(files, 'Empty IPCC')
    if kind == 'a1':
        observed = {n.removeprefix('Payload/mobilkom_by.bundle/'): carrier.digest(v) for n, v in files.items()}
        carrier.require(observed == sources['bundles'][bundle], 'A1 contents differ from pinned originals')
    return data, files


def no_pending():
    for p in catalog.PRIVATE.glob('catalog-*/state.json'):
        carrier.require(json.loads(p.read_text())['phase'] == 'complete', 'Recover pending catalog first: ' + p.parent.name)
    for p in catalog.PRIVATE.glob('install-*/state.json'):
        carrier.require(json.loads(p.read_text()).get('phase') == 'complete', 'Inspect pending installation: ' + p.parent.name)


async def install(path, kind):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
    from pymobiledevice3.services.installation_proxy import InstallationProxyService
    sources = json.loads(assets.SOURCES.read_text())
    data, files = package_files(path, kind, sources)
    no_pending()
    serial, profile = await transport_canary.identify()
    token = secrets.token_hex(10)
    directory = catalog.PRIVATE / ('install-' + token); directory.mkdir(mode=0o700)
    stage = '/PublicStaging/carrier-research-' + token + '.ipcc'
    state = {'device': serial, 'profile': profile, 'kind': kind, 'package_sha256': carrier.digest(data),
             'stage': stage, 'phase': 'created', 'dispatched': False, 'responses': []}
    def save(): catalog.store(directory / 'state.json', state)
    save()
    class LoggedInstaller(InstallationProxyService):
        async def _watch_completion(self, handler=None, *args):
            while True:
                response = await self.service.recv_plist()
                state['responses'].append(response); save()
                carrier.require(bool(response), 'Installation connection ended without Complete')
                if response.get('Error'):
                    state['conclusive_error'] = True; save()
                    raise ValueError('iOS rejected package: ' + str(response['Error']))
                if response.get('Status') == 'Complete':
                    state['installation_proxy_complete'] = True; save(); return
    async with await create_using_usbmux(serial=serial, autopair=False, connection_type='USB') as dev:
        try:
            sims = await dev.get_value(key='CarrierBundleInfoArray') or []
            carrier.require({(c.get('MCC'), c.get('MNC')) for c in sims} == {('257','01'), ('257','04')}, 'Expected A1 and life research SIMs')
            async with AfcService(dev) as afc:
                carrier.require(await catalog.info(afc, stage) is None, 'Staging collision')
                await afc.makedirs(stage)
                state['owns_stage'] = True; save()
                state['phase'] = 'staging'; save()
                for name, value in files.items():
                    target = stage + '/' + name
                    await afc.makedirs(str(PurePosixPath(target).parent))
                    await afc.set_file_contents(target, value)
                    carrier.require(await afc.get_file_contents(target) == value, 'Staging readback differs')
            async with LoggedInstaller(dev) as service:
                state.update(phase='install_intent', dispatched=True); save()
                await asyncio.wait_for(service.send_package('Install', {'PackageType':'CarrierBundle'}, None, stage), 90)
        except BaseException as error:
            state['error_type'] = type(error).__name__; save(); raise
        finally:
            conclusive = not state['dispatched'] or state.get('installation_proxy_complete') or state.get('conclusive_error')
            if conclusive:
                if state.get('owns_stage'):
                    async with AfcService(dev) as afc:
                        if await catalog.info(afc, stage) is not None: await afc.rm(stage)
                        carrier.require(await catalog.info(afc, stage) is None, 'Staging cleanup incomplete')
                state.update(phase='complete', staging_removed=True); save()
    # InstallationProxy Complete precedes CommCenter's asynchronous reload.
    # Close the installation connection before allowing a new AirTraffic sync.
    await asyncio.sleep(20)
    state['reload_settle_wait_completed'] = True; save()
    return {'run': str(directory), 'installation_proxy_complete': state.get('installation_proxy_complete', False),
            'network_5g_verified': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('kind', choices=['a1','docomo']); p.add_argument('--package', type=Path, required=True)
    p.add_argument('--confirmed-device-write', action='store_true')
    a = p.parse_args()
    if not a.confirmed_device_write: p.error('Explicit device-write opt-in required')
    os.umask(0o077); catalog.PRIVATE.mkdir(exist_ok=True)
    try:
        with (catalog.PRIVATE/'device-operation.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
            print(json.dumps(asyncio.run(install(a.package,a.kind))))
    except Exception as e:
        print('Stopped: ' + (str(e) if isinstance(e,ValueError) else type(e).__name__), file=sys.stderr); return 2
    return 0


if __name__ == '__main__': raise SystemExit(main())
