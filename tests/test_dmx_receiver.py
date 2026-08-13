"""Unit tests for DMXReceiver start/stop reuse."""
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dmx_receiver
from dmx_receiver import DMXReceiver


class TestDMXReceiverRestart(unittest.TestCase):
    def test_start_after_stop_restarts_receiver_and_reregisters(self):
        first = MagicMock()
        second = MagicMock()
        with patch.object(dmx_receiver.sacn, 'sACNreceiver', side_effect=[first, second]):
            rx = DMXReceiver()
            callback = MagicMock()
            rx.listen_to_universe(7, callback)
            first.register_listener.assert_called()
            self.assertEqual(first.register_listener.call_args.kwargs.get('universe'), 7)

            rx.stop()
            self.assertFalse(rx.running)
            first.stop.assert_called()

            rx.start()
            try:
                self.assertTrue(rx.running)
                second.start.assert_called()
                second.register_listener.assert_called()
                self.assertEqual(second.register_listener.call_args.kwargs.get('universe'), 7)
                second.join_multicast.assert_called_with(7)
                self.assertEqual(rx.get_stats()['last_start_errors'], [])
            finally:
                rx.close()

    def test_start_after_stop_still_runs_when_reregister_raises(self):
        first = MagicMock()
        second = MagicMock()
        second.register_listener.side_effect = TypeError('register failed')
        with patch.object(dmx_receiver.sacn, 'sACNreceiver', side_effect=[first, second]):
            rx = DMXReceiver()
            rx.listen_to_universe(7, MagicMock())
            rx.stop()
            rx.start()
            try:
                self.assertTrue(rx.running)
                second.register_listener.assert_called()
                self.assertEqual(second.register_listener.call_args.kwargs.get('universe'), 7)
                second.join_multicast.assert_not_called()
                self.assertEqual(
                    rx.get_stats()['last_start_errors'],
                    [{'universe': 7, 'error': 'register failed'}],
                )
            finally:
                rx.close()

    def test_idempotent_start_preserves_degraded_start_errors(self):
        first = MagicMock()
        second = MagicMock()
        second.register_listener.side_effect = TypeError('register failed')
        with patch.object(dmx_receiver.sacn, 'sACNreceiver', side_effect=[first, second]):
            rx = DMXReceiver()
            self.addCleanup(rx.close)
            rx.listen_to_universe(7, MagicMock())
            rx.stop()
            rx.start()
            errors = rx.get_stats()['last_start_errors']
            self.assertEqual(errors, [{'universe': 7, 'error': 'register failed'}])
            rx.start()
            self.assertTrue(rx.running)
            self.assertEqual(rx.get_stats()['last_start_errors'], errors)

    def test_reregister_records_typeerror_and_keeps_successful_universes(self):
        first = MagicMock()
        second = MagicMock()

        def register(_trigger, _func, **kwargs):
            if kwargs.get('universe') == 8:
                raise TypeError('invalid universe registration')

        second.register_listener.side_effect = register
        with patch.object(dmx_receiver.sacn, 'sACNreceiver', side_effect=[first, second]):
            rx = DMXReceiver()
            self.addCleanup(rx.close)
            callback = MagicMock()
            rx.listen_to_universe(7, callback)
            rx.listen_to_universe(8, callback)
            rx.stop()
            rx.start()
            self.assertTrue(rx.running)
            registered = [
                call.kwargs.get('universe') for call in second.register_listener.call_args_list
            ]
            self.assertIn(7, registered)
            self.assertIn(8, registered)
            self.assertEqual(
                rx.get_stats()['last_start_errors'],
                [{'universe': 8, 'error': 'invalid universe registration'}],
            )

    def test_first_start_does_not_recreate_receiver(self):
        sock = MagicMock()
        with patch.object(dmx_receiver.sacn, 'sACNreceiver', return_value=sock) as ctor:
            rx = DMXReceiver()
            self.assertEqual(ctor.call_count, 1)
            rx.start()
            self.assertEqual(ctor.call_count, 1)
            self.assertTrue(rx.running)
            rx.close()

    def test_concurrent_start_after_stop_creates_single_receiver(self):
        receivers = [MagicMock(), MagicMock()]
        with patch.object(dmx_receiver.sacn, 'sACNreceiver', side_effect=receivers) as ctor:
            rx = DMXReceiver()
            rx.stop()
            threads = [threading.Thread(target=rx.start) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(ctor.call_count, 2)
            self.assertTrue(rx.running)
            rx.close()
