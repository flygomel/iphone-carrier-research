#!/usr/bin/env python3
"""Opt-in untested catalog writes, bound to one device/build/SIM and Apple IPSW."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request

import assets
import carrier
import catalog
import experimental_context as context
import installer
import transaction

ROOT = Path(__file__).resolve().parent


def apple_url(url):
    parsed = urllib.parse.urlparse(url)
    carrier.require(parsed.scheme == 'https' and parsed.hostname in
                    ('updates.cdn-apple.com', 'updates-http.cdn-apple.com', 'appldnld.apple.com')
                    and not parsed.username and not parsed.password, 'An official Apple HTTPS IPSW URL is required')
    return url


async def inspect_device():
    from pymobiledevice3.usbmux import list_devices
    from pymobiledevice3.lockdown import create_using_usbmux
    devices = [d for d in await list_devices() if d.connection_type == 'USB']
    carrier.require(len(devices) == 1, 'Connect exactly one unlocked USB iPhone')
    serial = devices[0].serial
    async with await create_using_usbmux(serial=serial, autopair=False, connection_type='USB') as dev:
        profile = {k: await dev.get_value(key=k) for k in context.FIELDS}
        board = await dev.get_value(key='HardwareModel')
        sims = await dev.get_value(key='CarrierBundleInfoArray') or []
    carrier.require(re.fullmatch(r'iPhone\d+,\d+', profile['ProductType']), 'An iPhone is required')
    carrier.require(int(profile['ProductType'].split(',')[0][6:]) >= 13, 'This iPhone generation has no 5G hardware')
    carrier.require(isinstance(board, str) and re.fullmatch(r'[A-Za-z0-9]+AP', board, re.I), 'Unknown hardware board')
    choices = sorted({str(s.get('MCC', '')) + str(s.get('MNC', '')) for s in sims
                      if re.fullmatch(r'\d{3}', str(s.get('MCC', ''))) and re.fullmatch(r'\d{2,3}', str(s.get('MNC', '')))})
    carrier.require(choices, 'No unambiguous SIM MCC/MNC found')
    carrier.require(len(choices) == len(sims), 'Cannot distinguish lines sharing the same MCC/MNC')
    return serial, profile, board, choices


def firmware_url(profile):
    pinned = json.loads(assets.SOURCES.read_text())
    if profile == pinned['profile']: return pinned['ipsw_url']
    # The index is discovery only. Apple-hosted BuildManifest is authoritative.
    url = 'https://api.ipsw.me/v4/device/' + urllib.parse.quote(profile['ProductType'], safe='') + '?type=ipsw'
    with urllib.request.urlopen(url, context=assets.tls_context(), timeout=30) as response:
        data = json.load(response)
    matches = {r['url'] for r in data.get('firmwares', [])
               if r.get('buildid') == profile['BuildVersion'] and r.get('version') == profile['ProductVersion']}
    carrier.require(len(matches) == 1, 'Exact firmware not found; supply --ipsw-url with its official Apple URL')
    return apple_url(matches.pop())


def check_manifest(manifest, profile, board):
    carrier.require(manifest.get('ProductBuildVersion') == profile['BuildVersion']
                    and manifest.get('ProductVersion') == profile['ProductVersion']
                    and profile['ProductType'] in manifest.get('SupportedProductTypes', [])
                    and any(i.get('Info', {}).get('DeviceClass', '').lower() == board.lower()
                            for i in manifest.get('BuildIdentities', [])), 'IPSW does not match device, build and board')


def read_manifest(url, profile, board):
    import requests
    import remotezip2
    class AppleSession(requests.Session):
        def request(self, method, url, **kwargs):
            apple_url(url)
            response = super().request(method, url, **kwargs)
            apple_url(response.url)
            return response
    with AppleSession() as session, remotezip2.RemoteZip(url, session=session, timeout=30) as archive:
        entry = archive.getinfo('BuildManifest.plist')
        carrier.require(entry.file_size <= 16 * 1024**2, 'BuildManifest too large')
        raw = archive.read(entry)
    check_manifest(plistlib.loads(raw), profile, board)
    return raw


def board_bundle(directory, board):
    all_files = {}
    total = 0
    for path in directory.rglob('*'):
        carrier.require(not path.is_symlink(), 'Symlink in source bundle')
        if path.is_dir(): continue
        carrier.require(path.is_file(), 'Special source file')
        total += path.stat().st_size
        carrier.require(total <= carrier.MAX_TOTAL, 'Source bundle too large')
        name = path.relative_to(directory).as_posix(); carrier.safe_name(name)
        all_files[name] = path.read_bytes()
    token = board[:-2].lower()
    candidates = [n for n in all_files if '/' not in n and n.startswith('overrides_') and n.endswith('.plist')
                  and token in n[:-6].lower().split('_')]
    carrier.require(len(candidates) == 1, 'No unique CarrierLab override for this hardware board')
    override = candidates[0]
    names = {'Info.plist', 'carrier.plist', override, override[:-6] + '.der.pri',
             'signatures/common.plist', 'signatures/' + override}
    carrier.require(names <= set(all_files), 'CarrierLab layout is not supported by this experimental adapter')
    info = plistlib.loads(all_files['Info.plist'])
    carrier.require(info.get('CFBundleIdentifier') == 'com.apple.CarrierLab', 'Not CarrierLab')
    return {'CarrierLab.bundle/' + n: all_files[n] for n in sorted(names)}


def ipsw_tool():
    locks = json.loads(assets.SOURCES.read_text())['ipsw_tool']
    work = catalog.PRIVATE/'experimental-tools'; work.mkdir(exist_ok=True)
    archive = work/'ipsw.tar.gz'
    if not archive.exists():
        with urllib.request.urlopen(locks['url'], context=assets.tls_context(), timeout=60) as r:
            data = r.read(locks['size']+1)
        carrier.require(len(data) == locks['size'] and carrier.digest(data) == locks['sha256'], 'ipsw download differs')
        catalog.durable_bytes(archive, data)
    carrier.require(carrier.digest(archive.read_bytes()) == locks['sha256'], 'ipsw cache differs')
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if m.name in ('ipsw', './ipsw') and m.isfile()]
        carrier.require(len(members) == 1 and members[0].size < 200_000_000, 'Invalid tool archive')
        binary = tar.extractfile(members[0]).read()
    path = work/'ipsw'
    if not path.exists(): catalog.durable_bytes(path, binary)
    carrier.require(path.read_bytes() == binary, 'Tool binary differs'); path.chmod(0o700)
    return path


def prepare(serial, profile, board, sim, url):
    raw = read_manifest(apple_url(url), profile, board)
    folder = catalog.PRIVATE/('experimental-'+secrets.token_hex(10)); folder.mkdir(mode=0o700)
    catalog.durable_bytes(folder/'BuildManifest.plist', raw)
    locks = json.loads(assets.SOURCES.read_text())
    if profile == locks['profile'] and (catalog.PRIVATE/'assets/CarrierLab.bundle').exists():
        source = catalog.PRIVATE/'assets/CarrierLab.bundle'
        assets.locked_bundle(source.parent, source.name, locks['bundles'][source.name])
    else:
        carrier.require(shutil.disk_usage(folder).free >= 40*1024**3, 'Need 40 GiB free for firmware extraction')
        print('Извлекаю CarrierLab из точной прошивки Apple; возможна загрузка десятков ГБ.', flush=True)
        subprocess.run([str(ipsw_tool()), 'extract', '--remote', '--files', '--pattern',
                        r'System/Library/Carrier Bundles/iPhone/CarrierLab\.bundle/',
                        '--output', str(folder/'extracted'), url], check=True)
        sources = list((folder/'extracted').rglob('CarrierLab.bundle'))
        carrier.require(sources, 'CarrierLab not found in this firmware')
        source = sources[0]
    lab = board_bundle(source, board)
    carrier.write_catalog(folder/'carrierlab.zip', lab, {})
    state = {'device':serial, 'profile':profile, 'board':board, 'sim':sim, 'ipsw_url':url,
             'manifest_sha256':carrier.digest(raw), 'lab_sha256':carrier.digest((folder/'carrierlab.zip').read_bytes()),
             'experimental':True, 'compatibility_verified':False, 'phase':'prepared'}
    state['apple_signature_verified'] = False
    catalog.store(folder/'state.json', state)
    return folder, state


def candidate_for(original, lab, sim):
    files, links = original
    carrier.require(sim in links and links[sim].endswith('.bundle'), 'Selected SIM has no existing catalog mapping')
    existing = {n:v for n,v in files.items() if n.startswith('CarrierLab.bundle/')}
    carrier.require(not existing or existing == lab, 'A different CarrierLab is already present; preserve it')
    return {**files, **lab}, {**links, sim:'CarrierLab.bundle'}


async def operate(folder, state, rollback=False):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
    serial, profile = await catalog.canary.identify()
    carrier.require(serial == state['device'] and profile == state['profile'], 'Prepared device changed')
    module = catalog.canary.load_transport()
    async with await create_using_usbmux(serial=serial, autopair=False, connection_type='USB') as dev:
        async with AfcService(dev) as afc:
            baseline = catalog.new_session(module, serial, profile)
            catalog.store(folder/'progress.json', {'snapshot':baseline.directory.name})
            try: await baseline.snapshot(afc)
            except BaseException:
                if catalog.pre_export_recovery_allowed(baseline.state, catalog.read_events(baseline.journal.path)):
                    await baseline.recover_before_export(afc)
                elif baseline.state['phase'] in ('exported','backup_verified'):
                    await baseline.return_directory(afc); await baseline.cleanup(afc)
                raise
            original = catalog.saved_catalog(baseline.directory, baseline.state)
            if rollback:
                path = catalog.PRIVATE/state['transaction_run']
                saved = json.loads((path/'state.json').read_text()); catalog.check_state(saved, serial)
                carrier.require(saved.get('operation')=='catalog_transaction' and saved.get('outcome')=='applied', 'No verified apply to undo')
                carrier.require(carrier.digest((path/'candidate.zip').read_bytes()) == saved['candidate_sha256'], 'Saved candidate damaged')
                carrier.require(original == carrier.read_catalog(path/'candidate.zip'), 'Current catalog changed; no blind rollback')
                candidate = catalog.saved_catalog(path, saved)
            else:
                lab = carrier.read_catalog(folder/'carrierlab.zip')[0]
                candidate = candidate_for(original, lab, state['sim'])
            created = catalog.new_session(module, serial, profile, operation='catalog_transaction', experimental_run=folder.name,
                                          action='experimental_rollback' if rollback else 'experimental_apply')
            catalog.store(folder/'progress.json', {'snapshot':baseline.directory.name, 'transaction':created.directory.name})
            session = transaction.Transaction(module,serial,created.directory,created.state)
            try: await session.apply(afc, original, candidate)
            except BaseException:
                if catalog.pre_export_recovery_allowed(session.state, catalog.read_events(session.journal.path)):
                    await session.recover_before_export(afc)
                elif session.state['phase'] in ('exported','backup_verified'):
                    await session.return_directory(afc);await session.cleanup(afc)
                raise
            carrier.require(session.state.get('outcome')=='applied', 'Candidate was not applied')
            return created.directory.name


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ipsw-url'); parser.add_argument('--sim', help='Target MCC+MNC, e.g. 25701')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--prepared', type=Path)
    parser.add_argument('--rollback', type=Path)
    parser.add_argument('--recover', type=Path, help='Interrupted catalog run; use with --prepared')
    parser.add_argument('--accept-experimental', action='store_true', help='Explicit opt-in for untested device writes; separate from --yes')
    args = parser.parse_args(argv); os.umask(0o077); catalog.PRIVATE.mkdir(exist_ok=True)
    serial, profile, board, choices = asyncio.run(inspect_device())
    print('Экспериментальный режим:',profile,'SIM:',', '.join(choices),flush=True)
    folder = args.rollback or args.prepared
    if folder:
        folder = folder.resolve()
        carrier.require(folder.parent == catalog.PRIVATE and re.fullmatch(r'experimental-[0-9a-f]{20}', folder.name), 'Use a local prepared experimental folder')
        state = json.loads((folder/'state.json').read_text())
        carrier.require(state['device']==serial and state['profile']==profile and state['board']==board and state['sim'] in choices, 'Prepared target changed')
        carrier.require(carrier.digest((folder/'carrierlab.zip').read_bytes())==state['lab_sha256'], 'Prepared files changed')
        raw = (folder/'BuildManifest.plist').read_bytes()
        carrier.require(carrier.digest(raw)==state['manifest_sha256'], 'Firmware manifest changed')
        check_manifest(plistlib.loads(raw),profile,board)
    else:
        carrier.require(not args.recover, '--recover requires --prepared')
        sim = args.sim or (choices[0] if len(choices)==1 else None)
        if sim is None and sys.stdin.isatty():
            sim = input('Выберите MCC+MNC оператора ('+', '.join(choices)+'): ').strip()
        carrier.require(sim in choices, 'Select exactly one SIM using --sim '+ ' or --sim '.join(choices))
        folder,state = prepare(serial,profile,board,sim,args.ipsw_url or firmware_url(profile))
    print('Подготовлено:',folder,flush=True)
    if args.prepare_only: return 0
    carrier.require(not state.get('transaction_run') or args.rollback or args.recover,
                    'This prepared run was already applied; use --rollback instead of overwriting its recovery history')
    print('Целевой оператор MCC+MNC: '+state['sim']+'; плата: '+board,flush=True)
    print('Запись и откат на этой конфигурации НЕ проверены. Возможна потеря мобильной связи. Резервная копия не гарантирует восстановление.\nМеняется привязка только выбранной SIM; доступ к 5G не гарантируется.',flush=True)
    if not args.accept_experimental and input('Для записи введите ЭКСПЕРИМЕНТ: ').strip()!='ЭКСПЕРИМЕНТ': return 0
    current_serial, current_profile, current_board, current_sims = asyncio.run(inspect_device())
    carrier.require((current_serial, current_profile, current_board) == (serial, profile, board)
                    and state['sim'] in current_sims, 'Device or SIM changed during preparation')
    binding = {**profile,'device':serial}; previous = os.environ.get(context.KEY)
    os.environ[context.KEY] = json.dumps(binding)
    try:
        subprocess.run(['make','-C',str(ROOT/'vendor/airlift')],check=True)
        if args.recover:
            run = args.recover.resolve()
            carrier.require(run.parent==catalog.PRIVATE, 'Use a local catalog run')
            recovery = json.loads((run/'state.json').read_text()); catalog.check_state(recovery,serial)
            carrier.require(recovery['profile']==profile and recovery.get('experimental'), 'Not this experimental target')
            script = 'transaction.py' if recovery.get('operation')=='catalog_transaction' else 'catalog.py'
            subprocess.run([sys.executable,str(ROOT/script),'recover','--run',str(run),'--confirmed-device-write'],check=True)
            restored = json.loads((run/'state.json').read_text())
            if restored.get('experimental_run') == folder.name and restored.get('outcome') == 'applied':
                key = 'rollback_run' if restored.get('action') == 'experimental_rollback' else 'transaction_run'
                state.update(phase='catalog_verified', network_5g_verified=False, reload_verified=False, **{key:run.name})
                catalog.store(folder/'state.json', state)
            return 0
        installer.no_pending()
        # Canary is a prerequisite, never a declaration of supported firmware.
        subprocess.run([sys.executable,str(ROOT/'transport_canary.py'),'--confirmed-device-write'],check=True)
        with (catalog.PRIVATE/'device-operation.lock').open('a') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            installer.no_pending()
            run = asyncio.run(operate(folder,state,bool(args.rollback)))
        result = {**state, 'phase':'catalog_verified', 'network_5g_verified':False, 'reload_verified':False}
        if args.rollback: result['rollback_run']=run
        else: result['transaction_run']=run
        catalog.store(folder/'state.json',result)
        print('Каталог записан и проверен. Перезагрузите iPhone вручную и проверьте связь и Field Test.\nРезультат: '+str(folder/'state.json'),flush=True)
        return 0
    finally:
        if previous is None: os.environ.pop(context.KEY,None)
        else: os.environ[context.KEY]=previous


if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as error:
        print('Stopped: '+str(error),file=sys.stderr);raise SystemExit(2)
