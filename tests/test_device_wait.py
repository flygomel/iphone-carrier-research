import unittest
from unittest.mock import AsyncMock, Mock, patch
from device_wait import wait_for_confirmation


class WaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_enter_is_not_queued_and_disconnect_updates(self):
        ready=('device',{'ProductVersion':'27.2'})
        missing=ValueError('Connect exactly one unlocked USB iPhone')
        ui=Mock()
        with patch('device_wait.device.inspect_device',AsyncMock(side_effect=[missing,ready,missing,ready,ready])) as inspect:
            result=await wait_for_confirmation(ui,'Start?',read_input=Mock(side_effect=['',None,None,None,'']),interval=0)
        self.assertEqual(result,ready)
        self.assertEqual(inspect.await_count,5)
        self.assertEqual(ui.status.call_count,5)
        ui.done.assert_called_once()

    async def test_cancel_without_connection(self):
        with patch('device_wait.device.inspect_device',AsyncMock(side_effect=ValueError('Connect exactly one unlocked USB iPhone'))):
            self.assertIsNone(await wait_for_confirmation(Mock(),'Start?',read_input=lambda:'n',interval=0))

    async def test_unexpected_errors_not_hidden(self):
        with patch('device_wait.device.inspect_device',AsyncMock(side_effect=RuntimeError('broken'))):
            with self.assertRaisesRegex(RuntimeError,'broken'):
                await wait_for_confirmation(Mock(),'Start?',read_input=lambda:None,interval=0)
