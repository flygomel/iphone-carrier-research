#!/usr/bin/env python3
"""Discover the active Belarus overlay, change one key and verify its return."""
import argparse
import asyncio
import fcntl
import json
import os
import plistlib
import posixpath
import re
from functools import partial
import secrets
import subprocess

import device
import file_transport

PARENT = '/var/mobile/Library/CountryBundles/Overlay'
REFERENCES = '/var/mobile/Library/CountryBundles/Library/Preferences'


def validate_target(path):
    device.require(isinstance(path, str) and posixpath.dirname(path) == PARENT
                   and re.fullmatch(r'device\+carrier\+com\.apple\.Belarus\+[A-Za-z0-9.+_-]+\.plist', posixpath.basename(path)),
                   'Нет подходящего странового overlay Belarus')
    return path


def parse_country(value):
    try:
        data = plistlib.loads(value)
    except Exception as error:
        raise ValueError('Некорректный страновой plist') from error
    device.require(isinstance(data, dict) and type(data.get('Show5GSwitch')) is bool
                   and data.get('CountryName') == 'Belarus'
                   and data.get('ISOAlpha2CountryCode') == ['by']
                   and isinstance(data.get('SupportedCountryIds'), list)
                   and '257' in data['SupportedCountryIds']
                   and 'com.apple.Belarus' in data['SupportedCountryIds'],
                   'Структура странового файла не поддерживается')
    return data


def transform(value, mode, original_backup=None):
    device.require(mode in ('apply', 'inspect', 'restore'), 'Unknown mode')
    data = parse_country(value)
    if mode == 'inspect': return value
    if mode == 'restore':
        device.require(original_backup is not None, 'Нужна резервная копия этого устройства и сборки')
        original = parse_country(original_backup)
        current = dict(data)
        current['Show5GSwitch'] = original['Show5GSwitch']
        device.require(current == original, 'Файл изменился вне нашего ключа; возврат отменён')
        return original_backup
    if data['Show5GSwitch']: return value
    data['Show5GSwitch'] = True
    fmt = plistlib.FMT_BINARY if value.startswith(b'bplist00') else plistlib.FMT_XML
    return plistlib.dumps(data, fmt=fmt, sort_keys=False)


def check_report(report, mode="apply"):
    device.require(isinstance(report.get('ProductType'), str)
                   and report['ProductType'].startswith('iPhone')
                   and all(isinstance(report.get(k), str) and report[k] for k in device.PROFILE_KEYS),
                   'Не удалось определить подключённый iPhone')


def reference_paths(report):
    rows = [c for c in report.get('carriers', []) if c.get('MCC') == '257']
    device.require(rows, 'Не найдена белорусская SIM; вставьте или включите её')
    slots = {'kOne': '1', 'kTwo': '2'}
    device.require(all(c.get('Slot') in slots for c in rows), 'Неизвестный слот SIM')
    return sorted({'com.apple.country.carrier_' + slots[c['Slot']] + '.plist' for c in rows})


def saved_original(serial, profile):
    for saved in sorted(file_transport.PRIVATE.glob('country-*/state.json'), key=lambda p:p.stat().st_mtime, reverse=True):
        state = json.loads(saved.read_text())
        if (state.get('device') != serial or state.get('profile') != profile
                or state.get('mode') != 'apply' or state.get('phase') != 'complete'):
            continue
        target = validate_target(state.get('target'))
        original = (saved.parent/'original.plist').read_bytes()
        device.require(device.digest(original) == state.get('original_sha256'), 'Повреждена резервная копия')
        if not parse_country(original)['Show5GSwitch']:
            return target, original
    raise ValueError('Нет исходной резервной копии для этого устройства и сборки')


def new_session(module, serial, profile, mode, target):
    token = secrets.token_hex(10)
    directory = file_transport.PRIVATE / ('country-' + token)
    directory.mkdir(mode=0o700)
    state = dict(device=serial, profile=profile, target=target, token=token,
                 source='airlift-src-' + token, link='airlift-link-' + token,
                 exported='airlift-recovered-' + token, phase='created', mode=mode)
    file_transport.store(directory/'state.json', state)
    return CountrySession(module, serial, directory, state)


class CountrySession(file_transport.Session):
    async def read_file(self, afc):
        path = self.state['exported']+'/'+posixpath.basename(self.state['target'])
        meta = await file_transport.info(afc, path)
        device.require(meta and meta.get('st_ifmt') == 'S_IFREG'
                        and 0 < int(meta['st_size']) <= 65536, 'Unexpected exported object')
        first = await afc.get_file_contents(path)
        device.require(first == await afc.get_file_contents(path), 'Unstable country file')
        return first

    async def perform(self, afc, mode):
        s, d = self.state, self.directory
        original = None
        target = s['target']
        container = target if mode == 'discovery' else PARENT
        parent, leaf = posixpath.split(container)
        if mode == 'discovery':
            device.require(target == REFERENCES, 'Invalid reference directory')
        else:
            validate_target(target)
        await self.backup_books(afc)
        pairs = [('../../'+s['source']+'/p0/p1/p2/link', s['link']),
                 (posixpath.relpath(container, file_transport.AIRLOCK), s['exported']),
                 ('../../'+s['exported'], s['link']+'/'+leaf)]
        file_transport.durable_bytes(d/'payload.zip', self.module.build_archive(parent, b'unused'))
        file_transport.durable_bytes(d/'Books.plist', self.module.build_books([p[0] for p in pairs]))
        self.phase('stage_intent')
        try:
            await self.native('stage', s['source'], s['link'], s['exported'],
                              d/'payload.zip', d/'Books.plist', d/'books-native')
            self.phase('export_intent')
            await self.export_paused(pairs)
            await self.await_export(afc, True)
            self.phase('exported')
            if mode == 'discovery':
                meta = await file_transport.info(afc, s['exported'])
                device.require(meta and meta.get('st_ifmt') == 'S_IFDIR', 'Country preferences is not a directory')
                async def links():
                    names = sorted(await afc.listdir(s['exported']))
                    device.require(len(names) <= 128, 'Unexpected country preferences size')
                    result = {}
                    for name in names:
                        device.safe_name(name)
                        device.require('/' not in name, 'Invalid reference name')
                        if re.fullmatch(r'com\.apple\.country\.carrier_[12]\.plist', name):
                            node = await file_transport.info(afc, s['exported']+'/'+name)
                            device.require(node and node.get('st_ifmt') == 'S_IFLNK'
                                           and isinstance(node.get('LinkTarget'), str), 'Country reference is not a symlink')
                            result[name] = node['LinkTarget']
                    return result
                references = await links()
                device.require(references == await links(), 'Unstable country references')
                file_transport.store(d/'references.json', references)
                await self.return_file(afc)
                await self.cleanup(afc)
                return references
            snapshot = await file_transport.tree(afc, s['exported'])
            files, links, dirs = snapshot
            device.require(not links, 'Unexpected overlay links')
            for index, (name, value) in enumerate(sorted(files.items())):
                file_transport.durable_bytes(d/('overlay-'+str(index)+'.bin'), value)
            file_transport.store(d/'overlay-backup.json', {'files':{name:{'file':'overlay-'+str(index)+'.bin','sha256':device.digest(value)} for index,(name,value) in enumerate(sorted(files.items()))},'directories':sorted(dirs)})
            original = await self.read_file(afc)
            file_transport.durable_bytes(d/'original.plist', original)
            self.phase('backup_verified', original_sha256=device.digest(original))
            candidate = transform(original, mode, getattr(self, 'restore_bytes', None))
            file_transport.durable_bytes(d/'desired.plist', candidate)
            self.phase('write_intent', desired_sha256=device.digest(candidate))
            if original != candidate:
                await afc.set_file_contents(s['exported']+'/'+posixpath.basename(target), candidate)
            device.require(await self.read_file(afc) == candidate, 'Country write readback failed')
            expected_files = dict(files)
            expected_files[posixpath.basename(target)] = candidate
            device.require(await file_transport.tree(afc, s['exported']) == (expected_files, links, dirs), 'Overlay changed outside selected file')
            self.phase('write_verified')
            await self.return_file(afc)
            await self.cleanup(afc)
            return candidate
        except BaseException:
            if file_transport.pre_export_recovery_allowed(self.state, file_transport.read_events(self.journal.path)):
                await self.recover_before_export(afc)
                raise
            # Return original in the still-paused session whenever possible.
            # Unknown/interrupted returns remain blocked with backups preserved.
            if self.worker is not None and self.worker.returncode is None:
                if await file_transport.info(afc, s['exported']) is not None:
                    if original is not None:
                        await afc.set_file_contents(s['exported']+'/'+posixpath.basename(target), original)
                        device.require(await self.read_file(afc) == original, 'Original restoration failed')
                    await self.return_file(afc)
                    await self.cleanup(afc)
            raise


def pending():
    device.check_legacy_canaries(file_transport.PRIVATE)
    for pattern in ('country-*/state.json', 'catalog-*/state.json', 'install-*/state.json'):
        for p in file_transport.PRIVATE.glob(pattern):
            state = json.loads(p.read_text())
            device.require(state.get('phase') == 'complete',
                            'Незавершённая операция: '+str(p.parent)+'. Сохраните private; повторная запись заблокирована.')


async def execute(mode, serial, report):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
    profile = {key: report[key] for key in device.PROFILE_KEYS}
    module = device.load_transport()
    module.native = partial(module.native, binding=dict(device=serial, **profile))
    restore_bytes = None
    target = None
    if mode == 'restore':
        target, restore_bytes = saved_original(serial, profile)
    async with await create_using_usbmux(serial=serial, autopair=False, connection_type='USB') as dev:
        device.require(await device.read_report(dev) == report,
                       'Устройство или настройки SIM изменились после подтверждения; запись отменена')
        async with AfcService(dev) as afc:
            if target is None:
                selected = reference_paths(report)
                discovery = new_session(module, serial, profile, 'discovery', REFERENCES)
                references = await discovery.perform(afc, 'discovery')
                device.require(all(name in references for name in selected), 'Нет страновой ссылки для активной SIM')
                targets = {validate_target(references[name]) for name in selected}
                device.require(len(targets) == 1, 'SIM используют разные страновые файлы; запись не начата')
                target = targets.pop()
            session = new_session(module, serial, profile, mode, target)
            session.restore_bytes = restore_bytes
            desired = await session.perform(afc, mode)
            if mode != 'inspect':
                session.phase('verification_pending')
                proof = new_session(module, serial, profile, 'inspect', target)
                observed = await proof.perform(afc, 'inspect')
                device.require(observed == desired, 'iOS изменила файл после записи; результат не подтверждён')
                session.phase('complete', verified_by=proof.directory.name)
        after = await device.read_report(dev)
    device.require(after == report, 'Метаданные SIM изменились; проверьте связь')
    result = dict(phase='complete', mode=mode, Show5GSwitch=plistlib.loads(desired)['Show5GSwitch'],
                  network_5g_verified=False, reboot_verified=False,
                  before=report, after=after, **{'run':str(session.directory)})
    file_transport.store(session.directory/'result.json', result)
    if mode == 'inspect':
        print('Настройка меню 5G: '+('включена.' if result['Show5GSwitch'] else 'выключена.'))
    elif mode == 'restore':
        print('Исходный файл восстановлен.')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--yes', action='store_true')
    modes = p.add_mutually_exclusive_group()
    modes.add_argument('--inspect', action='store_true', help='Export/return without content edits; not read-only')
    modes.add_argument('--restore', action='store_true', help='Restore known original country bytes')
    a = p.parse_args(argv)
    os.umask(0o077)
    file_transport.PRIVATE.mkdir(exist_ok=True)
    serial, report = asyncio.run(device.inspect_device())
    mode = 'inspect' if a.inspect else 'restore' if a.restore else 'apply'
    check_report(report, mode)
    prompts = {'apply': 'Включить меню 5G?', 'inspect': 'Проверить настройку 5G?',
               'restore': 'Вернуть исходный файл?'}
    if not a.yes and input(prompts[mode]+' Введите ДА: ').strip().upper() != 'ДА': return 0
    print('Выполняю… Не отключайте iPhone.', flush=True)
    with (file_transport.PRIVATE/'device-operation.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
        pending()
        with (file_transport.PRIVATE/'build.log').open('w') as log:
            built = subprocess.run(['make','-C',str(file_transport.ROOT/'vendor/airlift')],
                                   stdout=log, stderr=subprocess.STDOUT)
        device.require(built.returncode == 0, 'Не удалось подготовить программу. Подробности: private/build.log')
        asyncio.run(execute(mode, serial, report))
    if not a.inspect and not a.restore:
        print('Готово. Перезагрузите iPhone и проверьте меню 5G.')
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception as e:
        print('Остановлено: '+str(e)); raise SystemExit(2)
