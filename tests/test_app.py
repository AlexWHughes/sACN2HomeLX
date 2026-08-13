"""
Unit tests for app.py - focusing on _restart_dmx_if_running function
"""
import unittest
from unittest.mock import Mock, MagicMock, patch, call
from types import SimpleNamespace
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


class TestChannelModeSpec(unittest.TestCase):
    def test_channel_counts(self):
        self.assertEqual(app.CHANNELS_FOR_MODE, {
            'RGB (8bit)': 3,
            'RGB (16bit)': 6,
            'RGB (16bit, fine first)': 6,
            'RGB + Intensity (8bit)': 4,
            'RGBW (8bit)': 4,
            'RGBW (16bit)': 8,
            'RGBW (16bit, fine first)': 8,
            'HSBK (8bit)': 4,
            'HSBK (16bit)': 8,
            'HSBK (16bit, fine first)': 8,
            'HSBK + Intensity (8bit)': 5,
            'RGB Full Pixel (8bit)': 3,
            'RGB + Intensity Full Pixel (8bit)': 4,
            'RGBW Full Pixel (8bit)': 4,
        })

    def test_grouped_pixel_mode_is_pixel(self):
        self.assertTrue(app._mode_is_pixel('RGB 8 Pixel (8bit)'))
        self.assertEqual(app._channels_per_cell('RGB 8 Pixel (8bit)'), 3)
        self.assertEqual(app._normalize_channel_mode('RGB 8 Pixel (8bit)'), 'RGB 8 Pixel (8bit)')

    def test_unknown_mode_falls_back_to_rgb8(self):
        self.assertEqual(app._normalize_channel_mode('nope'), 'RGB (8bit)')
        self.assertEqual(app._normalize_channel_mode(None), 'RGB (8bit)')


class TestDmxDecode(unittest.TestCase):
    def test_rgb_8bit_full_red(self):
        r, g, b, kelvin, duration, brightness = app._dmx_decode_to_cmd(
            'RGB (8bit)', [255, 0, 0], 1.0
        )
        self.assertAlmostEqual(r, 1.0)
        self.assertAlmostEqual(g, 0.0)
        self.assertAlmostEqual(b, 0.0)
        self.assertEqual(kelvin, app.DEFAULT_KELVIN)
        self.assertEqual(duration, app.FADE_DURATION_MS)
        self.assertEqual(brightness, 1.0)

    def test_rgb_16bit_msb_first(self):
        r, g, b, *_rest = app._dmx_decode_to_cmd(
            'RGB (16bit)', [255, 255, 0, 0, 0, 0], 0.5
        )
        self.assertAlmostEqual(r, 1.0)
        self.assertAlmostEqual(g, 0.0)
        self.assertAlmostEqual(b, 0.0)

    def test_rgb_16bit_fine_first_differs_from_msb(self):
        values = [0x34, 0x12, 0, 0, 0, 0]
        r_msb, *_a = app._dmx_decode_to_cmd('RGB (16bit)', values, 1.0)
        r_fine, *_b = app._dmx_decode_to_cmd('RGB (16bit, fine first)', values, 1.0)
        self.assertNotAlmostEqual(r_msb, r_fine)
        self.assertAlmostEqual(r_fine, 0x1234 / 65535.0)

    def test_rgb_plus_intensity(self):
        r, g, b, *_rest = app._dmx_decode_to_cmd(
            'RGB + Intensity (8bit)', [255, 255, 255, 128], 1.0
        )
        self.assertAlmostEqual(r, 128 / 255.0, places=5)
        self.assertAlmostEqual(g, 128 / 255.0, places=5)
        self.assertAlmostEqual(b, 128 / 255.0, places=5)

    def test_rgbw_blends_white(self):
        r, g, b, *_rest = app._dmx_decode_to_cmd(
            'RGBW (8bit)', [255, 0, 0, 255], 1.0
        )
        self.assertAlmostEqual(r, 1.0)
        self.assertAlmostEqual(g, app.BLEND_WHITE_COEFF)
        self.assertAlmostEqual(b, app.BLEND_WHITE_COEFF)

    def test_hsbk_8bit_full_value_white(self):
        r, g, b, kelvin, _duration, brightness = app._dmx_decode_to_cmd(
            'HSBK (8bit)', [0, 0, 255, 0], 1.0
        )
        self.assertAlmostEqual(r, 1.0)
        self.assertAlmostEqual(g, 1.0)
        self.assertAlmostEqual(b, 1.0)
        self.assertEqual(kelvin, 2500)
        self.assertAlmostEqual(brightness, 1.0)

    def test_hsbk_intensity_scales_brightness(self):
        *_rgb, _kelvin, _duration, brightness = app._dmx_decode_to_cmd(
            'HSBK + Intensity (8bit)', [0, 0, 255, 0, 128], 1.0
        )
        self.assertAlmostEqual(brightness, 128 / 255.0, places=5)

    def test_8bit_change_threshold(self):
        self.assertFalse(app._dmx_values_changed('RGB (8bit)', [10, 10, 10], [10, 10, 10]))
        self.assertTrue(app._dmx_values_changed('RGB (8bit)', [11, 10, 10], [10, 10, 10]))

    def test_16bit_change_uses_combined_value(self):
        prev = [0x12, 0x34, 0, 0, 0, 0]
        same = [0x12, 0x34, 0, 0, 0, 0]
        changed = [0x12, 0x35, 0, 0, 0, 0]
        self.assertFalse(app._dmx_values_changed('RGB (16bit)', same, prev))
        self.assertTrue(app._dmx_values_changed('RGB (16bit)', changed, prev))

    def test_16bit_truncated_last_values_falls_back_to_per_byte(self):
        same = [0x12, 0x34, 0]
        prev = [0x12, 0x34, 0]
        changed = [0x12, 0x35, 0]
        self.assertFalse(app._dmx_values_changed('RGB (16bit)', same, prev))
        self.assertTrue(app._dmx_values_changed('RGB (16bit)', changed, prev))
        self.assertTrue(app._dmx_values_changed('RGB (16bit)', [10, 10, 10, 10, 10, 10], [10, 10]))


class TestListLightsApi(unittest.TestCase):
    """GET /api/lights returns configured and unconfigured lists"""

    def setUp(self):
        self.flask_app = app.app
        self.flask_app.config['TESTING'] = True
        self.client = self.flask_app.test_client()
        self._saved_mappings = dict(app.light_mappings)
        self._saved_client = app.lifx_client
        app._dmx_mapping_cache_dirty = True

    def tearDown(self):
        app.light_mappings = self._saved_mappings
        app.lifx_client = self._saved_client
        app._dmx_mapping_cache_dirty = True

    def test_list_lights_no_client_empty_mappings(self):
        app.light_mappings = {}
        app._dmx_mapping_cache_dirty = True
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
        app._dmx_mapping_cache_dirty = True
        resp = self.client.get('/api/lights')
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['all_configured_lights']), 1)
        row = data['all_configured_lights'][0]
        self.assertFalse(row['discovered'])
        self.assertEqual(row['label'], 'Stage Left')
        self.assertEqual(row['mapping']['channel_mode'], 'HSBK (16bit)')

    def test_list_lights_supercolour_shows_pixel_modes(self):
        from lifx_client import LifxLight
        light = LifxLight(bytes.fromhex('d073d5a29dd40000'), '192.168.1.122')
        light.product = 218
        light.model_name = 'LIFX SuperColour Tube'
        light.label = 'Living Room Forest Lamp'
        light.layout = 'matrix'
        light.zone_count = 55
        light.matrix_width = 5
        light.matrix_height = 11
        mock_client = Mock()
        mock_client.get_lights.return_value = [light]
        mock_client.lights = {light.target: light}
        mock_client.lock = threading.Lock()
        app.lifx_client = mock_client
        app.light_mappings = {}
        app._dmx_mapping_cache_dirty = True
        resp = self.client.get('/api/lights')
        data = resp.get_json()
        self.assertEqual(len(data['unconfigured_lights']), 1)
        row = data['unconfigured_lights'][0]
        self.assertEqual(row['model'], 'LIFX SuperColour Tube')
        self.assertTrue(row['zone_capable'])
        self.assertEqual(row['zone_layout'], 'matrix')
        self.assertIn('RGB Full Pixel (8bit)', row['supported_modes'])
        self.assertIn('RGB 8 Pixel (8bit)', row['supported_modes'])
        self.assertIn('RGB 11 Pixel (8bit)', row['supported_modes'])
        self.assertNotIn('HSBK (16bit)', row['supported_modes'])
        labels = {opt['value']: opt['label'] for opt in row['mode_options']}
        self.assertEqual(labels['RGB Full Pixel (8bit)'], 'RGB Full Pixel (8bit) — 165 ch')
        self.assertEqual(labels['RGB 8 Pixel (8bit)'], 'RGB 8 Pixel (8bit) — 24 ch')
        pattern_ids = [item['id'] for item in row['pixel_test_patterns']]
        self.assertEqual(pattern_ids[0], 'rainbow')
        self.assertIn('chase', pattern_ids)


class TestGetNetworkInterfaces(unittest.TestCase):
    """Interface listing via ifaddr, with socket fallback."""

    def test_ifaddr_keeps_ipv4_and_skips_loopback_and_ipv6(self):
        adapters = [
            SimpleNamespace(
                name='lo0',
                nice_name='lo0',
                ips=[SimpleNamespace(ip='127.0.0.1')],
            ),
            SimpleNamespace(
                name='en0',
                nice_name='en0',
                ips=[
                    SimpleNamespace(ip=('fe80::1', 0, 14)),
                    SimpleNamespace(ip='192.168.1.82'),
                    SimpleNamespace(ip='192.168.1.82'),
                ],
            ),
        ]
        mock_ifaddr = SimpleNamespace(get_adapters=lambda: adapters)
        with patch.object(app, 'ifaddr', mock_ifaddr):
            result = app.get_network_interfaces()

        ips = [row['ip'] for row in result]
        self.assertEqual(result[0]['ip'], '0.0.0.0')
        self.assertEqual(ips.count('192.168.1.82'), 1)
        self.assertNotIn('127.0.0.1', ips)
        self.assertEqual(result[1]['display'], 'en0 (192.168.1.82)')

    def test_socket_fallback_when_ifaddr_missing(self):
        addrinfo = [(None, None, None, None, ('10.0.0.5', 0))]
        with patch.object(app, 'ifaddr', None), \
             patch('app.socket.gethostname', return_value='testhost'), \
             patch('app.socket.getaddrinfo', return_value=addrinfo):
            result = app.get_network_interfaces()

        self.assertEqual(result[0]['ip'], '0.0.0.0')
        self.assertEqual(result[1]['ip'], '10.0.0.5')
        self.assertEqual(result[1]['display'], 'testhost (10.0.0.5)')


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


class TestPixelMapping(unittest.TestCase):
    """Pixel and grouped SuperColour DMX modes."""

    def test_zone_count_defaults_to_one(self):
        mapping = {'channel_mode': 'RGB (8bit)'}
        self.assertEqual(app._mapping_zone_count(mapping, None), 1)
        self.assertEqual(app._mapping_channels_needed(mapping, None), 3)

    def test_whole_fixture_ignores_zone_count(self):
        from lifx_client import LifxLight
        light = LifxLight(b'\x00' * 8, '192.168.1.122')
        light.zone_count = 52
        mapping = {'channel_mode': 'RGB (8bit)'}
        self.assertEqual(app._mapping_channels_needed(mapping, light), 3)

    def test_full_pixel_uses_physical_zone_count(self):
        from lifx_client import LifxLight
        light = LifxLight(b'\x00' * 8, '192.168.1.122')
        light.layout = 'matrix'
        light.zone_count = 55
        mapping = {'channel_mode': 'RGB Full Pixel (8bit)'}
        self.assertEqual(app._mapping_zone_count(mapping, light), 55)
        self.assertEqual(app._mapping_channels_needed(mapping, light), 165)

    def test_grouped_pixel_mode_uses_control_cells(self):
        from lifx_client import LifxLight
        light = LifxLight(b'\x00' * 8, '192.168.1.122')
        light.layout = 'matrix'
        light.zone_count = 55
        light.matrix_width = 5
        light.matrix_height = 11
        mapping = {'channel_mode': 'RGB 8 Pixel (8bit)'}
        self.assertEqual(app._mapping_zone_count(mapping, light), 8)
        self.assertEqual(app._mapping_channels_needed(mapping, light), 24)
        self.assertEqual(app._physical_zone_count(mapping, light), 55)

    def test_zone_fixture_gets_pixel_mode_options(self):
        from lifx_client import LifxLight
        light = LifxLight(b'\x00' * 8, '192.168.1.122')
        light.layout = 'matrix'
        light.zone_count = 55
        light.matrix_width = 5
        light.matrix_height = 11
        modes = app._supported_modes_for(light)
        self.assertIn('RGB Full Pixel (8bit)', modes)
        self.assertIn('RGBW Full Pixel (8bit)', modes)
        self.assertIn('RGB + Intensity Full Pixel (8bit)', modes)
        self.assertIn('RGB 11 Pixel (8bit)', modes)
        self.assertIn('RGB 8 Pixel (8bit)', modes)
        self.assertIn('RGB 5 Pixel (8bit)', modes)
        self.assertIn('RGB 4 Pixel (8bit)', modes)
        self.assertIn('RGB 2 Pixel (8bit)', modes)
        self.assertNotIn('HSBK (16bit)', modes)
        labels = {opt['value']: opt['label'] for opt in app._mode_options_for(light)}
        self.assertEqual(labels['RGB Full Pixel (8bit)'], 'RGB Full Pixel (8bit) — 165 ch')
        self.assertEqual(labels['RGB 8 Pixel (8bit)'], 'RGB 8 Pixel (8bit) — 24 ch')

    def test_standard_fixture_does_not_get_pixel_modes(self):
        from lifx_client import LifxLight
        light = LifxLight(b'\x00' * 8, '192.168.1.50')
        self.assertNotIn('RGB Full Pixel (8bit)', app._supported_modes_for(light))
        self.assertNotIn('RGB 8 Pixel (8bit)', app._supported_modes_for(light))

    def test_supercolour_product_gets_pixel_modes_without_layout(self):
        from lifx_client import LifxLight
        light = LifxLight(b'\x00' * 8, '192.168.1.122')
        light.product = 218
        light.model_name = 'LIFX SuperColour Tube'
        modes = app._supported_modes_for(light)
        self.assertIn('RGB Full Pixel (8bit)', modes)
        self.assertIn('RGBW Full Pixel (8bit)', modes)
        self.assertNotIn('HSBK (16bit)', modes)
        fields = app._light_zone_fields(light)
        self.assertTrue(fields['zone_capable'])
        self.assertEqual(fields['zone_layout'], 'matrix')

    def test_offline_mapping_uses_stored_layout_not_model_name(self):
        mapping = {
            'model': 'LIFX SuperColour Tube',
            'channel_mode': 'RGB (8bit)',
            'ip': '192.168.1.122',
        }
        modes = app._supported_modes_for(None, mapping)
        self.assertNotIn('RGB Full Pixel (8bit)', modes)
        self.assertFalse(app._mapping_is_zone_capable(mapping))

        stored = dict(mapping)
        stored.update({
            'zone_capable': True,
            'zone_count': 55,
            'zone_layout': 'matrix',
        })
        modes = app._supported_modes_for(None, stored)
        self.assertIn('RGB Full Pixel (8bit)', modes)
        self.assertNotIn('HSBK (16bit)', modes)
        self.assertTrue(app._mapping_is_zone_capable(stored))

    def test_expand_rows_and_columns_for_5x11(self):
        rows = [(i, 0, 0, 3500, 1.0) for i in range(11)]
        row_out = app._expand_control_cells_to_zones(rows, 55, 5, 11)
        self.assertEqual(len(row_out), 55)
        self.assertEqual(row_out[0], rows[0])
        self.assertEqual(row_out[4], rows[0])
        self.assertEqual(row_out[5], rows[1])
        cols = [(i, 0, 0, 3500, 1.0) for i in range(5)]
        col_out = app._expand_control_cells_to_zones(cols, 55, 5, 11)
        self.assertEqual(col_out[0], cols[0])
        self.assertEqual(col_out[1], cols[1])
        self.assertEqual(col_out[5], cols[0])

    def test_expand_eight_cells_covers_all_pixels(self):
        cells = [(i, 0, 0, 3500, 1.0) for i in range(8)]
        out = app._expand_control_cells_to_zones(cells, 55, 5, 11)
        self.assertEqual(len(out), 55)
        self.assertEqual(out[0], cells[0])
        self.assertEqual(out[-1], cells[7])

    def test_pixel_test_patterns_for_tube(self):
        from lifx_client import LifxLight
        light = LifxLight(b'\x00' * 8, '192.168.1.122')
        light.layout = 'matrix'
        light.zone_count = 55
        light.matrix_width = 5
        light.matrix_height = 11
        ids = [row['id'] for row in app._pixel_test_patterns(light)]
        self.assertEqual(ids, ['rainbow', 'rows', 'columns', '8', '4', '2', 'chase'])

    def test_pixel_test_rainbow_and_rows(self):
        from lifx_client import LifxLight
        light = LifxLight(b'\x00' * 8, '192.168.1.122')
        light.layout = 'matrix'
        light.zone_count = 55
        light.matrix_width = 5
        light.matrix_height = 11
        rainbow = app._pixel_test_commands(light, 'rainbow', 1.0)
        self.assertEqual(len(rainbow), 55)
        self.assertNotEqual(rainbow[0][:3], rainbow[-1][:3])
        rows = app._pixel_test_commands(light, 'rows', 1.0)
        self.assertEqual(rows[0][:3], rows[4][:3])
        self.assertNotEqual(rows[0][:3], rows[5][:3])
        cols = app._pixel_test_commands(light, 'columns', 1.0)
        self.assertEqual(cols[0][:3], cols[5][:3])
        self.assertNotEqual(cols[0][:3], cols[1][:3])
        grouped = app._pixel_test_commands(light, '8', 1.0)
        self.assertEqual(len(grouped), 55)
        chase = app._pixel_test_commands(light, 'chase', 1.0, chase_index=3, chase_rgb=(1.0, 0.0, 0.0))
        self.assertEqual(chase[3][:3], (1.0, 0.0, 0.0))
        self.assertEqual(chase[0][:3], (0.0, 0.0, 0.0))

    def test_standard_fixture_has_no_pixel_tests(self):
        from lifx_client import LifxLight
        light = LifxLight(b'\x00' * 8, '192.168.1.50')
        self.assertEqual(app._pixel_test_patterns(light), [])

    def test_pixel_test_commands_use_mapping_geometry(self):
        from lifx_client import LifxLight
        light = LifxLight(b'\x00' * 8, '192.168.1.122')
        light.product = 218
        light.model_name = 'LIFX SuperColour Tube'
        mapping = {
            'zone_capable': True,
            'zone_count': 55,
            'zone_layout': 'matrix',
            'matrix_width': 5,
            'matrix_height': 11,
        }
        ids = [row['id'] for row in app._pixel_test_patterns(light, mapping)]
        self.assertEqual(ids, ['rainbow', 'rows', 'columns', '8', '4', '2', 'chase'])
        self.assertEqual(app._pixel_test_commands(light, 'rainbow', 1.0), [])
        rainbow = app._pixel_test_commands(light, 'rainbow', 1.0, mapping=mapping)
        self.assertEqual(len(rainbow), 55)


class TestGetInterfaces(unittest.TestCase):
    def setUp(self):
        self.flask_app = app.app
        self.flask_app.config['TESTING'] = True
        self.client = self.flask_app.test_client()
        self._cache = app._interfaces_cache
        self._cache_time = app._interfaces_cache_time
        self._in_flight = app._interfaces_refresh_in_flight
        app._interfaces_cache = None
        app._interfaces_cache_time = 0
        app._interfaces_refresh_in_flight = False

    def tearDown(self):
        app._interfaces_cache = self._cache
        app._interfaces_cache_time = self._cache_time
        app._interfaces_refresh_in_flight = self._in_flight

    @patch('app.get_network_interfaces', return_value=[{'ip': '192.168.1.82', 'display': 'en0 (192.168.1.82)'}])
    def test_cold_start_does_not_raise_unbound_local(self, _mock_ifaces):
        resp = self.client.get('/api/interfaces')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['interfaces']), 1)

    @patch('app.get_network_interfaces', return_value=[{'ip': '192.168.1.82', 'display': 'en0 (192.168.1.82)'}])
    def test_stale_cache_background_refresh_does_not_raise(self, _mock_ifaces):
        app._interfaces_cache = [{'ip': '10.0.0.1', 'display': 'stale'}]
        app._interfaces_cache_time = 0
        resp = self.client.get('/api/interfaces')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        deadline = time.time() + 2.0
        while app._interfaces_refresh_in_flight and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(
            app._interfaces_refresh_in_flight,
            'background interface refresh did not finish before deadline',
        )


class TestTestRgbApi(unittest.TestCase):
    def setUp(self):
        self.flask_app = app.app
        self.flask_app.config['TESTING'] = True
        self.client = self.flask_app.test_client()
        self._saved_client = app.lifx_client
        with app._batch_lock:
            self._saved_batch = dict(app._batch_commands_by_id)
            app._batch_commands_by_id.clear()

    def tearDown(self):
        app._stop_lifx_batch_sender_thread()
        app.lifx_client = self._saved_client
        with app._batch_lock:
            app._batch_commands_by_id.clear()
            app._batch_commands_by_id.update(self._saved_batch)

    def _mock_light(self):
        from lifx_client import LifxLight
        light = LifxLight(bytes.fromhex('d073d5aabbcc0000'), '192.168.1.10')
        light.label = 'Test Lamp'
        mock_client = Mock()
        mock_client.get_lights.return_value = [light]
        mock_client.lights = {light.target: light}
        mock_client.lock = threading.Lock()
        mock_client.executor = Mock()
        app.lifx_client = mock_client
        return light, mock_client

    @patch('app._start_lifx_batch_sender_thread')
    def test_test_rgb_coalesces_latest_color_without_blocking(self, _mock_start):
        light, mock_client = self._mock_light()
        lid = app.light_id(light)
        first = self.client.post('/api/lights/test-rgb', json={
            'light_id': lid, 'r': 1, 'g': 2, 'b': 3, 'brightness': 1.0,
        })
        second = self.client.post('/api/lights/test-rgb', json={
            'light_id': lid, 'r': 10, 'g': 20, 'b': 30, 'brightness': 0.5,
        })
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.get_json()['success'])
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()['success'])
        mock_client.executor.submit.assert_not_called()
        self.assertEqual(len(app._batch_commands_by_id), 1)
        cmd = app._batch_commands_by_id[lid]
        self.assertEqual(cmd[0], 'color')
        self.assertAlmostEqual(cmd[2], 10 / 255.0)
        self.assertAlmostEqual(cmd[3], 20 / 255.0)
        self.assertAlmostEqual(cmd[4], 30 / 255.0)
        self.assertAlmostEqual(cmd[7], 0.5)

    def test_test_rgb_missing_light_keeps_error_contract(self):
        _light, mock_client = self._mock_light()
        resp = self.client.post('/api/lights/test-rgb', json={
            'light_id': 'missing', 'r': 1, 'g': 2, 'b': 3,
        })
        self.assertEqual(resp.status_code, 404)
        data = resp.get_json()
        self.assertFalse(data['success'])
        self.assertEqual(data['error'], 'Light not found')
        mock_client.executor.submit.assert_not_called()


class TestUpdateMappingChannelMode(unittest.TestCase):
    def setUp(self):
        self.flask_app = app.app
        self.flask_app.config['TESTING'] = True
        self.client = self.flask_app.test_client()
        self._saved_mappings = dict(app.light_mappings)
        self._saved_client = app.lifx_client
        app.light_mappings = {}
        app.lifx_client = None
        app._dmx_mapping_cache_dirty = True

    def tearDown(self):
        app.light_mappings = self._saved_mappings
        app.lifx_client = self._saved_client
        app._dmx_mapping_cache_dirty = True

    @patch('app._restart_dmx_if_running')
    @patch('app.save_config')
    def test_rejects_pixel_mode_for_standard_fixture(self, _save, _restart):
        from lifx_client import LifxLight
        light = LifxLight(bytes.fromhex('d073d5aabbcc0000'), '192.168.1.50')
        light.label = 'Bulb'
        light.model_name = 'LIFX Color'
        lid = app.light_id(light)
        mock_client = Mock()
        mock_client.get_lights.return_value = [light]
        app.lifx_client = mock_client
        app.light_mappings[lid] = {
            'universe': 1,
            'start_channel': 1,
            'brightness': 1.0,
            'channel_mode': 'HSBK (8bit)',
            'label': 'Bulb',
            'model': 'LIFX Color',
            'ip': '192.168.1.50',
        }
        resp = self.client.post('/api/mappings', json={
            'light_id': lid,
            'universe': 1,
            'start_channel': 1,
            'channel_mode': 'RGB Full Pixel (8bit)',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['mapping']['channel_mode'], 'HSBK (8bit)')

    @patch('app._restart_dmx_if_running')
    @patch('app.save_config')
    def test_keeps_valid_pixel_mode_for_zone_fixture(self, _save, _restart):
        from lifx_client import LifxLight
        light = LifxLight(bytes.fromhex('d073d5aabbcc0000'), '192.168.1.122')
        light.layout = 'matrix'
        light.zone_count = 55
        light.matrix_width = 5
        light.matrix_height = 11
        light.label = 'Tube'
        lid = app.light_id(light)
        mock_client = Mock()
        mock_client.get_lights.return_value = [light]
        app.lifx_client = mock_client
        resp = self.client.post('/api/mappings', json={
            'light_id': lid,
            'universe': 2,
            'start_channel': 10,
            'channel_mode': 'RGB 8 Pixel (8bit)',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['mapping']['channel_mode'], 'RGB 8 Pixel (8bit)')


class TestTestPixelsValidation(unittest.TestCase):
    def setUp(self):
        self.flask_app = app.app
        self.flask_app.config['TESTING'] = True
        self.client = self.flask_app.test_client()
        self._saved_client = app.lifx_client
        mock_client = Mock()
        mock_client.get_lights.return_value = []
        mock_client.lights = {}
        mock_client.lock = threading.Lock()
        app.lifx_client = mock_client

    def tearDown(self):
        app.lifx_client = self._saved_client

    def test_invalid_brightness_returns_400(self):
        resp = self.client.post('/api/lights/test-pixels', json={
            'light_id': 'abc', 'pattern': 'rainbow', 'brightness': 'bright',
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['error'], 'Brightness must be 0.0-1.0')

    def test_invalid_fade_ms_returns_400(self):
        resp = self.client.post('/api/lights/test-pixels', json={
            'light_id': 'abc', 'pattern': 'rainbow', 'fade_ms': 'slow',
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn('fade_ms', resp.get_json()['error'])

    def test_numeric_strings_are_accepted_before_light_lookup(self):
        resp = self.client.post('/api/lights/test-pixels', json={
            'light_id': 'missing', 'pattern': 'rainbow',
            'brightness': '0.5', 'fade_ms': '45',
        })
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()['error'], 'Light not found')

    def test_fade_ms_rejects_non_integral_and_non_finite(self):
        self.assertIsNone(app._coerce_fade_ms(45.5))
        self.assertIsNone(app._coerce_fade_ms(float('inf')))
        self.assertIsNone(app._coerce_fade_ms(float('nan')))
        self.assertIsNone(app._coerce_fade_ms(0xFFFFFFFF + 1))
        self.assertEqual(app._coerce_fade_ms('45'), 45)
        self.assertEqual(app._coerce_fade_ms(45.0), 45)
        self.assertEqual(app._coerce_fade_ms(0), 0)
        self.assertEqual(app._coerce_fade_ms(0xFFFFFFFF), 0xFFFFFFFF)

    def test_non_integral_fade_ms_returns_400(self):
        resp = self.client.post('/api/lights/test-pixels', json={
            'light_id': 'abc', 'pattern': 'rainbow', 'fade_ms': 45.5,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn('fade_ms', resp.get_json()['error'])


class TestErrorOnlyRequestHandler(unittest.TestCase):
    def test_unparsable_status_delegates(self):
        with patch.object(app.WSGIRequestHandler, 'log_request') as parent:
            handler = app._ErrorOnlyRequestHandler.__new__(app._ErrorOnlyRequestHandler)
            handler.log_request('-', '-')
            parent.assert_called_once_with('-', '-')

    def test_success_status_is_silent(self):
        with patch.object(app.WSGIRequestHandler, 'log_request') as parent:
            handler = app._ErrorOnlyRequestHandler.__new__(app._ErrorOnlyRequestHandler)
            handler.log_request(200, '12')
            parent.assert_not_called()

    def test_error_status_delegates(self):
        with patch.object(app.WSGIRequestHandler, 'log_request') as parent:
            handler = app._ErrorOnlyRequestHandler.__new__(app._ErrorOnlyRequestHandler)
            handler.log_request(404, '12')
            parent.assert_called_once_with(404, '12')


class TestMetricsUtilization(unittest.TestCase):
    def setUp(self):
        self.flask_app = app.app
        self.flask_app.config['TESTING'] = True
        self.client = self.flask_app.test_client()
        self._metrics = dict(app._perf_metrics)
        self._peak = app._perf_metrics['peak_fixtures_per_frame']
        self._avg = app._perf_metrics['avg_batch_size']
        self._batch_sizes = list(app._perf_metrics['batch_sizes'])

    def tearDown(self):
        app._perf_metrics['peak_fixtures_per_frame'] = self._peak
        app._perf_metrics['avg_batch_size'] = self._avg
        app._perf_metrics['total_batches_sent'] = self._metrics['total_batches_sent']
        app._perf_metrics['batch_sizes'].clear()
        app._perf_metrics['batch_sizes'].extend(self._batch_sizes)

    def test_batch_utilization_uses_peak_capacity(self):
        with app._perf_lock:
            app._perf_metrics['batch_sizes'].clear()
            app._perf_metrics['batch_sizes'].extend([8, 2, 2])
            app._perf_metrics['avg_batch_size'] = 4
            app._perf_metrics['peak_fixtures_per_frame'] = 99
            app._perf_metrics['total_batches_sent'] = 10
        data = self.client.get('/api/metrics').get_json()
        self.assertTrue(data['success'])
        self.assertAlmostEqual(data['metrics']['batch_efficiency'], 0.5)
        self.assertAlmostEqual(data['metrics']['batch_utilization_percent'], 50.0)
        self.assertIn('current_queue_size', data['metrics'])

    def test_batch_utilization_zero_without_peak_capacity(self):
        with app._perf_lock:
            app._perf_metrics['batch_sizes'].clear()
            app._perf_metrics['peak_fixtures_per_frame'] = 0
            app._perf_metrics['avg_batch_size'] = 3
            app._perf_metrics['total_batches_sent'] = 5
        data = self.client.get('/api/metrics').get_json()
        self.assertEqual(data['metrics']['batch_efficiency'], 0.0)
        self.assertEqual(data['metrics']['batch_utilization_percent'], 0.0)


if __name__ == '__main__':
    unittest.main()