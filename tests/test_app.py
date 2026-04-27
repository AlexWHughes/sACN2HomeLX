"""
Unit tests for app.py - focusing on _restart_dmx_if_running function
"""
import unittest
from unittest.mock import Mock, MagicMock, patch, call
import threading
import time
import sys
import os

# Add parent directory to path to import app module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


class TestRestartDmxIfRunning(unittest.TestCase):
    """Test suite for _restart_dmx_if_running function"""
    
    def setUp(self):
        """Set up test fixtures"""
        # Store original global state
        self.original_running = app.running
        self.original_dmx_receiver = app.dmx_receiver
        self.original_dmx_thread = app.dmx_thread
        self.original_sacn_interface = app.sacn_interface
        
        # Reset to safe defaults
        app.running = False
        app.dmx_receiver = None
        app.dmx_thread = None
        app.sacn_interface = None
    
    def tearDown(self):
        """Restore original state"""
        app.running = self.original_running
        app.dmx_receiver = self.original_dmx_receiver
        app.dmx_thread = self.original_dmx_thread
        app.sacn_interface = self.original_sacn_interface
    
    def test_restart_when_not_running(self):
        """Test that restart does nothing when DMX is not running"""
        app.running = False
        app.dmx_receiver = Mock()
        app.dmx_thread = Mock()
        
        # Should return early without doing anything
        app._restart_dmx_if_running()
        
        # Verify nothing was called
        app.dmx_receiver.stop.assert_not_called()
        app.dmx_receiver.reset_stats.assert_not_called()
    
    def test_restart_when_no_receiver(self):
        """Test that restart does nothing when receiver is None"""
        app.running = True
        app.dmx_receiver = None
        app.dmx_thread = Mock()
        
        # Should return early
        app._restart_dmx_if_running()
        
        # Thread should not be touched
        app.dmx_thread.is_alive.assert_not_called()
    
    @patch('app.DMXReceiver')
    @patch('app.threading.Thread')
    def test_restart_successful(self, mock_thread_class, mock_dmx_receiver_class):
        """Test successful restart of DMX worker"""
        # Setup
        app.running = True
        mock_receiver = Mock()
        mock_receiver.stop = Mock()
        mock_receiver.reset_stats = Mock()
        mock_receiver.close = Mock()
        app.dmx_receiver = mock_receiver
        
        mock_thread = Mock()
        mock_thread.is_alive.return_value = True
        mock_thread.join = Mock()
        app.dmx_thread = mock_thread
        
        app.sacn_interface = "192.168.1.100"
        
        # Mock the new receiver
        new_receiver = Mock()
        mock_dmx_receiver_class.return_value = new_receiver
        
        # Mock the new thread
        new_thread = Mock()
        mock_thread_class.return_value = new_thread
        
        # Execute
        app._restart_dmx_if_running()
        
        # Verify old receiver was stopped
        mock_receiver.stop.assert_called_once()
        mock_receiver.reset_stats.assert_called_once()
        
        # Verify thread was joined (is_alive may be called multiple times)
        self.assertGreaterEqual(mock_thread.is_alive.call_count, 1)
        mock_thread.join.assert_called_once_with(timeout=1.0)
        
        # Verify old receiver was closed
        mock_receiver.close.assert_called_once()
        
        # Verify new receiver was created with correct bind_ip
        mock_dmx_receiver_class.assert_called_once_with(bind_ip="192.168.1.100")
        
        # Verify new thread was created and started
        mock_thread_class.assert_called_once()
        new_thread.start.assert_called_once()
        
        # Verify running flag is still True
        self.assertTrue(app.running)
    
    @patch('app.DMXReceiver')
    @patch('app.threading.Thread')
    def test_restart_with_default_interface(self, mock_thread_class, mock_dmx_receiver_class):
        """Test restart with 0.0.0.0 interface (should use None for bind_ip)"""
        # Setup
        app.running = True
        mock_receiver = Mock()
        app.dmx_receiver = mock_receiver
        
        mock_thread = Mock()
        mock_thread.is_alive.return_value = False
        app.dmx_thread = mock_thread
        
        app.sacn_interface = "0.0.0.0"
        
        new_receiver = Mock()
        mock_dmx_receiver_class.return_value = new_receiver
        
        # Execute
        app._restart_dmx_if_running()
        
        # Verify new receiver was created with bind_ip=None
        mock_dmx_receiver_class.assert_called_once_with(bind_ip=None)
    
    @patch('app.DMXReceiver')
    @patch('app.threading.Thread')
    def test_restart_with_none_interface(self, mock_thread_class, mock_dmx_receiver_class):
        """Test restart with None interface (should use None for bind_ip)"""
        # Setup
        app.running = True
        mock_receiver = Mock()
        app.dmx_receiver = mock_receiver
        
        mock_thread = Mock()
        mock_thread.is_alive.return_value = False
        app.dmx_thread = mock_thread
        
        app.sacn_interface = None
        
        new_receiver = Mock()
        mock_dmx_receiver_class.return_value = new_receiver
        
        # Execute
        app._restart_dmx_if_running()
        
        # Verify new receiver was created with bind_ip=None
        mock_dmx_receiver_class.assert_called_once_with(bind_ip=None)
    
    @patch('app.DMXReceiver')
    @patch('app.threading.Thread')
    def test_restart_thread_timeout(self, mock_thread_class, mock_dmx_receiver_class):
        """Test that restart handles thread join timeout gracefully"""
        # Setup
        app.running = True
        mock_receiver = Mock()
        app.dmx_receiver = mock_receiver
        
        # Thread that takes longer than timeout
        mock_thread = Mock()
        mock_thread.is_alive.return_value = True
        mock_thread.join = Mock()  # Will complete within timeout
        app.dmx_thread = mock_thread
        
        app.sacn_interface = "192.168.1.1"
        
        new_receiver = Mock()
        mock_dmx_receiver_class.return_value = new_receiver
        
        # Execute - should not raise exception
        app._restart_dmx_if_running()
        
        # Verify join was called with timeout
        mock_thread.join.assert_called_once_with(timeout=1.0)
        
        # Verify restart continued despite potential timeout
        mock_dmx_receiver_class.assert_called_once()
    
    @patch('app.DMXReceiver')
    @patch('app.threading.Thread')
    def test_restart_receiver_close_exception(self, mock_thread_class, mock_dmx_receiver_class):
        """Test that restart handles receiver close exceptions gracefully"""
        # Setup
        app.running = True
        mock_receiver = Mock()
        mock_receiver.close.side_effect = Exception("Close failed")
        app.dmx_receiver = mock_receiver
        
        mock_thread = Mock()
        mock_thread.is_alive.return_value = False
        app.dmx_thread = mock_thread
        
        app.sacn_interface = "192.168.1.1"
        
        new_receiver = Mock()
        mock_dmx_receiver_class.return_value = new_receiver
        
        # Execute - should not raise exception
        app._restart_dmx_if_running()
        
        # Verify restart continued despite close exception
        mock_dmx_receiver_class.assert_called_once()
    
    @patch('app.DMXReceiver')
    @patch('app.threading.Thread')
    def test_restart_general_exception(self, mock_thread_class, mock_dmx_receiver_class):
        """Test that restart handles general exceptions and sets running to False"""
        # Setup
        app.running = True
        mock_receiver = Mock()
        mock_receiver.stop.side_effect = Exception("Stop failed badly")
        app.dmx_receiver = mock_receiver
        
        mock_thread = Mock()
        mock_thread.is_alive.return_value = False
        app.dmx_thread = mock_thread
        
        app.sacn_interface = "192.168.1.1"
        
        # Execute - should not raise exception
        app._restart_dmx_if_running()
        
        # Verify running flag was set to False due to exception
        self.assertFalse(app.running)
    
    @patch('app.DMXReceiver')
    @patch('app.threading.Thread')
    def test_restart_thread_not_alive(self, mock_thread_class, mock_dmx_receiver_class):
        """Test restart when thread is not alive (should not call join)"""
        # Setup
        app.running = True
        mock_receiver = Mock()
        app.dmx_receiver = mock_receiver
        
        mock_thread = Mock()
        mock_thread.is_alive.return_value = False
        app.dmx_thread = mock_thread
        
        app.sacn_interface = "192.168.1.1"
        
        new_receiver = Mock()
        mock_dmx_receiver_class.return_value = new_receiver
        
        # Execute
        app._restart_dmx_if_running()
        
        # Verify join was not called since thread was not alive
        mock_thread.join.assert_not_called()
        
        # Verify restart still completed
        mock_dmx_receiver_class.assert_called_once()
    
    @patch('app.DMXReceiver')
    @patch('app.threading.Thread')
    def test_restart_thread_is_none(self, mock_thread_class, mock_dmx_receiver_class):
        """Test restart when thread is None"""
        # Setup
        app.running = True
        mock_receiver = Mock()
        app.dmx_receiver = mock_receiver
        app.dmx_thread = None
        
        app.sacn_interface = "192.168.1.1"
        
        new_receiver = Mock()
        mock_dmx_receiver_class.return_value = new_receiver
        
        # Execute - should not raise exception
        app._restart_dmx_if_running()
        
        # Verify restart completed
        mock_dmx_receiver_class.assert_called_once()
    
    @patch('app.DMXReceiver')
    @patch('app.threading.Thread')
    @patch('app.dmx_worker')
    def test_restart_thread_target_is_dmx_worker(self, mock_dmx_worker, mock_thread_class, mock_dmx_receiver_class):
        """Test that new thread targets dmx_worker function"""
        # Setup
        app.running = True
        mock_receiver = Mock()
        app.dmx_receiver = mock_receiver
        
        mock_thread = Mock()
        mock_thread.is_alive.return_value = False
        app.dmx_thread = mock_thread
        
        app.sacn_interface = "192.168.1.1"
        
        new_receiver = Mock()
        mock_dmx_receiver_class.return_value = new_receiver
        
        # Execute
        app._restart_dmx_if_running()
        
        # Verify thread was created with correct target and daemon flag
        call_kwargs = mock_thread_class.call_args[1]
        self.assertEqual(call_kwargs['target'], app.dmx_worker)
        self.assertTrue(call_kwargs['daemon'])
    
    def test_restart_idempotency(self):
        """Test that multiple restart calls don't cause issues"""
        # This is more of an integration-style test
        # Setup minimal state
        app.running = False
        app.dmx_receiver = None
        
        # Multiple calls should all return early without error
        app._restart_dmx_if_running()
        app._restart_dmx_if_running()
        app._restart_dmx_if_running()
        
        # No assertion needed - just verify no exception is raised


class TestDmxU16Helpers(unittest.TestCase):
    """16-bit DMX coarse/fine byte order helpers"""

    def test_msb_first(self):
        self.assertEqual(app._dmx_u16(0x12, 0x34, False), 0x1234)

    def test_fine_first(self):
        self.assertEqual(app._dmx_u16(0x34, 0x12, True), 0x1234)


class TestListLightsApi(unittest.TestCase):
    """GET /api/lights returns configured and unconfigured lists"""

    def setUp(self):
        self.flask_app = app.app
        self.flask_app.config['TESTING'] = True
        self.client = self.flask_app.test_client()
        self._saved_mappings = dict(app.light_mappings)
        self._saved_client = app.lifx_client

    def tearDown(self):
        app.light_mappings = self._saved_mappings
        app.lifx_client = self._saved_client

    def test_list_lights_no_client_empty_mappings(self):
        app.light_mappings = {}
        app.lifx_client = None
        resp = self.client.get('/api/lights')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['all_configured_lights'], [])
        self.assertEqual(data['unconfigured_lights'], [])

    def test_list_lights_shows_configured_offline(self):
        app.lifx_client = None
        app.light_mappings = {
            'deadbeef': {
                'universe': 1,
                'start_channel': 10,
                'brightness': 1.0,
                'channel_mode': 'HSBK (16bit)',
                'label': 'Stage Left',
                'ip': '192.168.1.50',
                'model': 'LIFX Color',
            }
        }
        resp = self.client.get('/api/lights')
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['all_configured_lights']), 1)
        row = data['all_configured_lights'][0]
        self.assertFalse(row['discovered'])
        self.assertEqual(row['label'], 'Stage Left')
        self.assertEqual(row['mapping']['channel_mode'], 'HSBK (16bit)')


class TestNormalizeInterfaceIp(unittest.TestCase):
    """Test suite for _normalize_interface_ip helper function"""
    
    def test_normalize_none(self):
        """Test that None returns 0.0.0.0"""
        result = app._normalize_interface_ip(None)
        self.assertEqual(result, '0.0.0.0')
    
    def test_normalize_zero_ip(self):
        """Test that 0.0.0.0 returns 0.0.0.0"""
        result = app._normalize_interface_ip('0.0.0.0')
        self.assertEqual(result, '0.0.0.0')
    
    def test_normalize_valid_ip(self):
        """Test that valid IP is returned unchanged"""
        test_ip = '192.168.1.100'
        result = app._normalize_interface_ip(test_ip)
        self.assertEqual(result, test_ip)
    
    def test_normalize_localhost(self):
        """Test that localhost IP is returned unchanged"""
        result = app._normalize_interface_ip('127.0.0.1')
        self.assertEqual(result, '127.0.0.1')
    
    def test_normalize_empty_string(self):
        """Test that empty string returns 0.0.0.0"""
        result = app._normalize_interface_ip('')
        self.assertEqual(result, '0.0.0.0')


if __name__ == '__main__':
    unittest.main()