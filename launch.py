#!/usr/bin/env python3
"""Guided, build-specific CarrierLab experiment with a durable result and rollback."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

import assets
import carrier
import catalog
import installer
import transaction

ROOT = Path(__file__).resolve().parent


def command(script, *arguments):
    result = subprocess.run([sys.executable, str(ROOT/script), *map(str,arguments)], cwd=ROOT,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError((result.stderr.strip() or result.stdout.strip() or script + ' failed')[-1500:])
    return json.loads(result.stdout)


def snapshot():
    return Path(command('catalog.py','snapshot','--confirmed-device-write')['run'])


def contents(path):
    state = json.loads((path/'state.json').read_text())
    return catalog.saved_catalog(path,state)


def selected(report, identifier):
    carriers = report['carriers']
    return (report.get('SIMStatus') == 'kCTSIMSupportSIMStatusReady'
            and {(c.get('MCC'), c.get('MNC')) for c in carriers} == {('257', '01'), ('257', '04')}
            and any(c.get('MCC') == '257' and c.get('MNC') == '01'
                    and c.get('CFBundleIdentifier') == identifier and c.get('CFBundleVersion') == '72.7.1'
                    for c in carriers)
            and any(c.get('MCC') == '257' and c.get('MNC') == '04'
                    and c.get('CFBundleIdentifier') == 'com.apple.life_by' and c.get('CFBundleVersion') == '72.7'
                    for c in carriers))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--yes',action='store_true',help='Confirm the described device writes')
    p.add_argument('--assets',type=Path,default=ROOT/'private/assets')
    p.add_argument('--rollback',type=Path,help='Completed launch folder to roll back')
    a=p.parse_args();os.umask(0o077);catalog.PRIVATE.mkdir(exist_ok=True)
    os.chdir(ROOT)
    report=asyncio.run(carrier.doctor())
    carrier.require(all(report.get(k)==v for k,v in catalog.canary.EXPECTED.items()),'Поддерживается только iPhone18,2 / 27.2 / 24B5084k')
    carrier.require({(c.get('MCC'),c.get('MNC')) for c in report['carriers']}=={('257','01'),('257','04')},'Нужны A1 и life исследовательского профиля')
    installer.no_pending()
    prior = None
    if a.rollback:
        parent = a.rollback.resolve()
        carrier.require(parent.parent == catalog.PRIVATE and parent.name.startswith('launch-'), 'Use a local launch folder')
        prior = json.loads((parent/'result.json').read_text())
        carrier.require(prior.get('transaction_run'), 'В этом запуске нет записи каталога для отката')
    print('iPhone найден. Операция меняет операторский каталог и временно данные синхронизации Books.\nРезервные копии сохранятся в private. 5G в сети оператора не гарантируется.',flush=True)
    if not a.yes and input('Начать? Введите ДА: ').strip()!='ДА': return 0
    with (catalog.PRIVATE/'launch.lock').open('a') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        sources=json.loads(assets.SOURCES.read_text())
        assets.fetch_all(a.assets,sources)
        subprocess.run(['make','-C','vendor/airlift'],check=True)
        run=catalog.PRIVATE/('launch-'+secrets.token_hex(10));run.mkdir(mode=0o700)
        state={'phase':'started','before':report,'network_5g_verified':False}
        def save(): catalog.store(run/'result.json',state)
        save();print('Папка результата:',run,flush=True)
        try:
            print('Проверяю транспорт на временном файле…',flush=True)
            command('transport_canary.py','--confirmed-device-write')
            if a.rollback:
                print('Возвращаю сохранённый каталог…',flush=True)
                result=command('transaction.py','rollback','--run',prior['transaction_run'],'--confirmed-device-write')
                state['transaction_run']=result['run'];state['action']='rollback';save()
                expected=contents(Path(prior['transaction_run']))
            else:
                print('Сохраняю каталог телефона…',flush=True)
                baseline=snapshot();state['initial_snapshot']=str(baseline);save()
                original=contents(baseline)
                if original[1].get('25701')=='CarrierLab.bundle':
                    transaction.original_mapping(original)
                    lab=carrier.bundle_files(a.assets/'CarrierLab.bundle')
                    carrier.require({n:v for n,v in original[0].items() if n.startswith('CarrierLab.bundle/')}==lab,'Installed CarrierLab bytes differ')
                    expected=original;state['action']='already_applied';save()
                else:
                    carrier.require(original[1].get('25701')=='mobilkom_by.bundle','Неизвестная исходная привязка A1; ничего не перезаписываю')
                    print('Устанавливаю оригинальные пакеты A1 и Docomo…',flush=True)
                    for kind,name in [('a1','A1-72.7.1.ipcc'),('docomo','Docomo-69.1.ipcc')]:
                        command('installer.py',kind,'--package',a.assets/name,'--confirmed-device-write')
                    baseline=snapshot();state['baseline_snapshot']=str(baseline);save()
                    original=contents(baseline)
                    fresh=asyncio.run(carrier.doctor())
                    expected=carrier.make_plan(original,carrier.bundle_files(a.assets/'CarrierLab.bundle'),fresh)
                    plan=run/'plan';plan.mkdir()
                    carrier.write_catalog(plan/'original-catalog.zip',*original)
                    carrier.write_catalog(plan/'candidate-catalog.zip',*expected)
                    print('Применяю CarrierLab и проверяю запись…',flush=True)
                    result=command('transaction.py','apply','--run',baseline,'--plan',plan,'--confirmed-device-write')
                    carrier.require(result['outcome']=='applied','Каталог не применён')
                    state.update(transaction_run=result['run'],action='apply');save()
            print('Запрашиваю перечитывание операторских настроек…',flush=True)
            command('installer.py','docomo','--package',a.assets/'Docomo-69.1.ipcc','--confirmed-device-write')
            after=asyncio.run(carrier.doctor());state['after']=after;save()
            observed=snapshot();carrier.require(contents(observed)==expected,'Каталог изменился после перечитывания; копии сохранены')
            # The following fresh cycle independently proves the preceding return.
            proof=snapshot();carrier.require(contents(proof)==expected,'Повторное чтение отличается')
            after=asyncio.run(carrier.doctor());state['after']=after;save()
            identifier='com.apple.mobilkom_by' if a.rollback else 'com.apple.CarrierLab'
            carrier.require(selected(after,identifier),'iOS ещё не подтвердила выбор ожидаемого пакета; проверьте result.json')
            state.update(phase='complete',protected_catalog_verified=True,verification_run=str(proof),carrier_selected=True)
            save()
            print('\nГотово: каталог проверен, iOS выбрала '+identifier+'.\nПроверьте интернет, звонки и 5G в Field Test. Результат: '+str(run/'result.json'),flush=True)
            if state.get('transaction_run') and not a.rollback:
                print('Откат: python launch.py --rollback '+str(run),flush=True)
            return 0
        except BaseException as e:
            state.update(phase='stopped',error_type=type(e).__name__);save()
            print('Остановлено. Копии сохранены: '+str(run)+'\nНе запускайте применение повторно до восстановления незавершённой операции.',file=sys.stderr)
            raise


if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as e:
        print(str(e),file=sys.stderr);raise SystemExit(2)
