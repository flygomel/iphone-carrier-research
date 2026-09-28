"""Wait for a usable USB connection and explicit keyboard confirmation."""
import asyncio
import select
import sys

import device


def read_key():
    if select.select([sys.stdin], [], [], 0)[0]:
        line = sys.stdin.readline()
        return 'n' if not line else line.strip().lower()
    return None


def connection_hint(error):
    if str(error) == 'Connect exactly one unlocked USB iPhone':
        return 'Подключите один iPhone кабелем.'
    if isinstance(error, (OSError, EOFError, asyncio.TimeoutError)) or type(error).__name__ in {
        'NoDeviceConnectedError', 'ConnectionFailedError', 'DeviceNotFoundError',
        'MuxException', 'MuxError', 'ConnectionTerminatedError',
        'NotPairedError', 'InvalidHostIDError', 'PairingError',
        'PairingDialogResponsePendingError', 'PasswordRequiredError',
        'UserDeniedPairingError', 'GetProhibitedError',
    }:
        return 'Разблокируйте iPhone и подтвердите доверие к Mac.'
    return None


async def wait_for_confirmation(ui, prompt, *, read_input=read_key, interval=0.5):
    previous = None
    while True:
        ready = None
        try:
            ready = await asyncio.wait_for(device.inspect_device(), timeout=3)
            text = 'iPhone подключён · iOS '+ready[1]['ProductVersion']
        except Exception as error:
            text = connection_hint(error)
            if text is None:
                raise
        if text != previous:
            ui.waiting(text, prompt if ready is not None else None)
            previous = text
        answer = read_input()
        if answer == 'n':
            ui.finish('Отменено.')
            return None
        if answer in ('', 'д', 'да', 'y', 'yes'):
            # Enter while disconnected is never remembered as authorization.
            if ready is not None:
                ui.done('iPhone подключён · iOS '+ready[1]['ProductVersion'])
                return ready
            ui.waiting(text, prompt if ready is not None else None)
        await asyncio.sleep(interval)
