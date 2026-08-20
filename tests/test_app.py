"""
Unit tests for app.py - focusing on _restart_dmx_if_running function
"""
import unittest
from unittest.mock import Mock, MagicMock, patch, call
from types import SimpleNamespace
import json
import os
import sys
import tempfile
import threading
import time

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
        
        # Execute - stop() may fail, but teardown must still release resources
        app._restart_dmx_if_running()
        
        mock_receiver.reset_stats.assert_called()
        mock_receiver.close.assert_called()
        mock_dmx_receiver_class.assert_called_once()
        self.assertTrue(app.running)
    
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


class TestMappingVendor(unittest.TestCase):
    def test_prefix_and_stored_vendor(self):
        self.assertEqual(app._mapping_vendor('nl_abc', {}), 'nanoleaf')
        self.assertEqual(app._mapping_vendor('deadbeef', {'vendor': 'nanoleaf'}), 'nanoleaf')
        self.assertEqual(app._mapping_vendor('deadbeef', {}), 'lifx')


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
        self._saved_nl = app.nanoleaf_client
        self._saved_nl_auth = dict(app.nanoleaf_auth)
        app.nanoleaf_client = None
        app.nanoleaf_auth = {}
        app._dmx_mapping_cache_dirty = True
        self._schedule_patch = patch.object(app, '_schedule_nanoleaf_hydrate')
        self._schedule_patch.start()
        self._lifx_schedule_patch = patch.object(app, '_schedule_lifx_hydrate')
        self._lifx_schedule_patch.start()
        app._lifx_hydrate_in_flight = False
        app._lifx_probe_next_attempt.clear()

    def tearDown(self):
        app.light_mappings = self._saved_mappings
        app.lifx_client = self._saved_client
        app.nanoleaf_client = self._saved_nl
        app.nanoleaf_auth = self._saved_nl_auth
        app._dmx_mapping_cache_dirty = True
        app._lifx_hydrate_in_flight = False
        app._lifx_probe_next_attempt.clear()
        self._schedule_patch.stop()
        self._lifx_schedule_patch.stop()

    def test_list_lights_no_client_empty_mappings(self):
        app.light_mappings = {}
        app._dmx_mapping_cache_dirty = True
        app.lifx_client = None
        app.nanoleaf_client = None
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

    def test_list_lights_prefers_mapping_label_over_device_label(self):
        from lifx_client import LifxLight
        light = LifxLight(bytes.fromhex('d073d5aabbcc0000'), '192.168.1.50')
        light.label = 'LIFX Colour'
        light.model_name = 'LIFX Color'
        lid = app.light_id(light)
        mock_client = Mock()
        mock_client.get_lights.return_value = [light]
        mock_client.lights = {light.target: light}
        mock_client.lock = threading.Lock()
        app.lifx_client = mock_client
        app.light_mappings = {
            lid: {
                'universe': 1,
                'start_channel': 1,
                'brightness': 1.0,
                'channel_mode': 'RGB (8bit)',
                'label': 'Stage Wash',
                'model': 'LIFX Color',
                'ip': '192.168.1.50',
            }
        }
        app._dmx_mapping_cache_dirty = True
        resp = self.client.get('/api/lights')
        row = resp.get_json()['all_configured_lights'][0]
        self.assertEqual(row['label'], 'Stage Wash')
        self.assertEqual(row['mapping']['label'], 'Stage Wash')

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

    def test_list_lights_includes_unpaired_nanoleaf(self):
        from nanoleaf_client import NanoleafDevice
        device = NanoleafDevice('nl_canvas1', '192.168.1.80', label='Living Canvas', model='NL42')
        device.panel_ids = [1, 2, 3, 4]
        device.panel_layout = [
            {'id': 1, 'x': 0, 'y': 100, 'o': 0, 'shapeType': 8},
            {'id': 2, 'x': 50, 'y': 50, 'o': 60, 'shapeType': 8},
            {'id': 3, 'x': 0, 'y': 0, 'o': 0, 'shapeType': 8},
            {'id': 4, 'x': 50, 'y': 0, 'o': 60, 'shapeType': 8},
        ]
        device.layout = 'linear'
        mock_nl = Mock()
        mock_nl.get_devices.return_value = [device]
        app.nanoleaf_client = mock_nl
        app.lifx_client = None
        app.light_mappings = {}
        resp = self.client.get('/api/lights')
        data = resp.get_json()
        self.assertEqual(len(data['unconfigured_lights']), 1)
        row = data['unconfigured_lights'][0]
        self.assertEqual(row['vendor'], 'nanoleaf')
        self.assertFalse(row['paired'])
        self.assertTrue(row['zone_capable'])
        self.assertEqual(len(row['panel_layout']), 4)
        self.assertEqual(row['model'], 'Shapes: Triangle')
        self.assertIn('RGB Full Pixel (8bit)', row['supported_modes'])

    def test_list_lights_hydrates_nanoleaf_layout_onto_mapping(self):
        from nanoleaf_client import NanoleafClient, NanoleafDevice
        device = NanoleafDevice('nl_DB6E588CD6DB', '192.168.1.115', auth_token='tok', label='Shapes 1BD0', model='NL42')
        mock_nl = Mock(spec=NanoleafClient)
        mock_nl.get_devices.return_value = [device]
        mock_nl.get_device.return_value = device

        def _ensure_layout(target):
            target.panel_ids = [8954, 64823, 24285]
            target.layout = 'linear'
            target.matrix_width = 3
            target.matrix_height = 1
            return target

        mock_nl.ensure_layout.side_effect = _ensure_layout
        mock_nl.requested_bind_ip = app._normalize_interface_ip(app.lifx_interface)
        app.nanoleaf_client = mock_nl
        app.nanoleaf_auth = {'nl_DB6E588CD6DB': {'auth_token': 'tok', 'ip': '192.168.1.115', 'port': 16021}}
        app.light_mappings = {
            'nl_DB6E588CD6DB': {
                'universe': 1,
                'start_channel': 1,
                'brightness': 1.0,
                'channel_mode': 'RGB (8bit)',
                'label': 'Light 192.168.1.115',
                'model': 'Nanoleaf',
                'ip': '192.168.1.115',
                'vendor': 'nanoleaf',
                'zone_capable': False,
                'zone_count': 1,
                'zone_layout': 'single',
                'panel_ids': [],
            }
        }
        with patch.object(app, 'save_config'):
            app._hydrate_nanoleaf_devices()
            resp = self.client.get('/api/lights')
        row = resp.get_json()['all_configured_lights'][0]
        self.assertTrue(row['zone_capable'])
        self.assertEqual(row['zone_count'], 3)
        self.assertIn('RGB Full Pixel (8bit)', row['supported_modes'])
        self.assertEqual(app.light_mappings['nl_DB6E588CD6DB']['label'], 'Shapes 1BD0')
        self.assertEqual(row['model'], 'Shapes')
        self.assertEqual(app.light_mappings['nl_DB6E588CD6DB']['model'], 'Shapes')
        self.assertNotIn('auth_token', row['mapping'])

    def test_list_lights_hydrates_nanoleaf_model_when_layout_already_known(self):
        from nanoleaf_client import NanoleafDevice
        device = NanoleafDevice('nl_DB6E588CD6DB', '192.168.1.115', auth_token='tok', label='', model='')
        device.panel_ids = [8954, 64823, 24285]
        device.panel_layout = [
            {'id': 8954, 'x': 0, 'y': 0},
            {'id': 64823, 'x': 100, 'y': 0},
            {'id': 24285, 'x': 200, 'y': 0},
        ]
        device.layout = 'linear'
        mock_nl = Mock()
        mock_nl.get_devices.return_value = [device]
        mock_nl.get_device.return_value = device

        def _ensure_layout(target):
            target.model = 'NL42'
            target.label = 'Shapes 1BD0'
            return target

        mock_nl.ensure_layout.side_effect = _ensure_layout
        mock_nl.requested_bind_ip = app._normalize_interface_ip(app.lifx_interface)
        app.nanoleaf_client = mock_nl
        app.nanoleaf_auth = {'nl_DB6E588CD6DB': {'auth_token': 'tok', 'ip': '192.168.1.115', 'port': 16021}}
        app.light_mappings = {
            'nl_DB6E588CD6DB': {
                'universe': 1,
                'start_channel': 1,
                'brightness': 1.0,
                'channel_mode': 'RGB (8bit)',
                'label': 'Light 192.168.1.115',
                'model': 'Nanoleaf',
                'ip': '192.168.1.115',
                'vendor': 'nanoleaf',
                'zone_capable': True,
                'zone_count': 3,
                'zone_layout': 'linear',
                'panel_ids': [8954, 64823, 24285],
                'panel_layout': list(device.panel_layout),
            }
        }
        with patch.object(app, 'save_config'):
            app._hydrate_nanoleaf_devices()
            resp = self.client.get('/api/lights')
        mock_nl.ensure_layout.assert_called()
        row = resp.get_json()['all_configured_lights'][0]
        self.assertEqual(row['model'], 'Shapes')
        self.assertEqual(app.light_mappings['nl_DB6E588CD6DB']['model'], 'Shapes')
        self.assertEqual(app.light_mappings['nl_DB6E588CD6DB']['label'], 'Shapes 1BD0')

    def test_list_lights_keeps_custom_nanoleaf_panel_order(self):
        from nanoleaf_client import NanoleafDevice
        device = NanoleafDevice('nl_DB6E588CD6DB', '192.168.1.115', auth_token='tok', label='Shapes 1BD0', model='NL42')
        mock_nl = Mock()
        mock_nl.get_devices.return_value = [device]
        mock_nl.get_device.return_value = device

        def _ensure_layout(target):
            target.panel_ids = [8954, 64823, 24285]
            target.panel_layout = [
                {'id': 8954, 'x': 0, 'y': 100},
                {'id': 64823, 'x': 50, 'y': 50},
                {'id': 24285, 'x': 0, 'y': 0},
            ]
            target.layout = 'linear'
            return target

        mock_nl.ensure_layout.side_effect = _ensure_layout
        mock_nl.requested_bind_ip = app._normalize_interface_ip(app.lifx_interface)
        app.nanoleaf_client = mock_nl
        app.nanoleaf_auth = {'nl_DB6E588CD6DB': {'auth_token': 'tok', 'ip': '192.168.1.115', 'port': 16021}}
        custom_order = [24285, 8954, 64823]
        app.light_mappings = {
            'nl_DB6E588CD6DB': {
                'universe': 1,
                'start_channel': 1,
                'brightness': 1.0,
                'channel_mode': 'RGB Full Pixel (8bit)',
                'label': 'Shapes 1BD0',
                'model': 'Shapes',
                'ip': '192.168.1.115',
                'vendor': 'nanoleaf',
                'panel_ids': custom_order,
                'map_rotation': 90,
            }
        }
        with patch.object(app, 'save_config'):
            app._hydrate_nanoleaf_devices()
            resp = self.client.get('/api/lights')
        row = resp.get_json()['all_configured_lights'][0]
        self.assertEqual(row['panel_ids'], custom_order)
        self.assertEqual(row['map_rotation'], 90)
        self.assertEqual(device.panel_ids, custom_order)
        self.assertEqual(device.map_rotation, 90)
        self.assertEqual(app.light_mappings['nl_DB6E588CD6DB']['panel_ids'], custom_order)

    def test_display_model_skips_repeated_vendor_brand(self):
        from nanoleaf_client import NanoleafDevice
        device = NanoleafDevice('nl_x', '192.168.1.115', model='')
        self.assertEqual(app._display_model(device, {'model': 'Nanoleaf'}), 'Unknown model')
        device.model = 'NL42'
        self.assertEqual(app._display_model(device, {'model': 'Nanoleaf'}), 'Shapes')
        self.assertEqual(app._display_model(None, {'model': 'Nanoleaf'}), 'Unknown model')

    def test_import_nanoleaf_tokens_strips_mapping_auth_token(self):
        app.light_mappings = {
            'nl_legacy': {
                'vendor': 'nanoleaf',
                'auth_token': 'legacy-token',
                'ip': '10.0.0.8',
                'port': 16021,
            }
        }
        app.nanoleaf_auth = {
            'nl_legacy': {
                'auth_token': 'should-be-replaced',
                'ip': '10.0.0.9',
                'port': 16022,
            }
        }
        app._import_nanoleaf_tokens_from_mappings()
        self.assertNotIn('auth_token', app.light_mappings['nl_legacy'])
        self.assertEqual(app.nanoleaf_auth['nl_legacy']['auth_token'], 'legacy-token')
        self.assertEqual(app.nanoleaf_auth['nl_legacy']['ip'], '10.0.0.8')
        self.assertEqual(app.nanoleaf_auth['nl_legacy']['port'], 16021)

    def test_import_nanoleaf_tokens_keeps_existing_ip_port_fallback(self):
        app.light_mappings = {
            'nl_legacy': {
                'vendor': 'nanoleaf',
                'auth_token': 'legacy-token',
            }
        }
        app.nanoleaf_auth = {
            'nl_legacy': {
                'auth_token': 'old',
                'ip': '10.0.0.9',
                'port': 16022,
            }
        }
        app._import_nanoleaf_tokens_from_mappings()
        self.assertNotIn('auth_token', app.light_mappings['nl_legacy'])
        self.assertEqual(app.nanoleaf_auth['nl_legacy']['ip'], '10.0.0.9')
        self.assertEqual(app.nanoleaf_auth['nl_legacy']['port'], 16022)

    def test_pair_nanoleaf_saves_token(self):
        from nanoleaf_client import NanoleafDevice
        device = NanoleafDevice('nl_canvas1', '192.168.1.80', label='Living Canvas', model='NL29')
        mock_nl = Mock()
        mock_nl.get_device.return_value = device

        def _pair(target):
            target.auth_token = 'secret-token'
            target.panel_ids = [1, 2]
            target.layout = 'linear'

        mock_nl.pair.side_effect = _pair
        app.nanoleaf_auth = {}
        app.light_mappings = {
            'nl_canvas1': {
                'universe': 1,
                'start_channel': 1,
                'channel_mode': 'RGB (8bit)',
                'vendor': 'nanoleaf',
                'auth_token': 'legacy-on-mapping',
                'ip': '192.168.1.80',
            }
        }
        saved = {}

        def _save():
            saved.update(app.nanoleaf_auth)

        with patch.object(app, '_ensure_nanoleaf_client', return_value=mock_nl), \
             patch.object(app, 'save_config', side_effect=_save):
            resp = self.client.post('/api/nanoleaf/pair', json={'light_id': 'nl_canvas1'})
        data = resp.get_json()
        self.assertTrue(data['success'], data)
        self.assertTrue(data['light']['paired'])
        self.assertEqual(saved['nl_canvas1']['auth_token'], 'secret-token')
        self.assertNotIn('auth_token', app.light_mappings['nl_canvas1'])

    def test_pair_nanoleaf_rejects_invalid_port(self):
        mock_nl = Mock()
        mock_nl.get_device.return_value = None
        with patch.object(app, '_ensure_nanoleaf_client', return_value=mock_nl):
            resp = self.client.post(
                '/api/nanoleaf/pair',
                json={'ip': '192.168.1.80', 'port': 'abc'},
            )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['success'])
        mock_nl.probe_by_ip.assert_not_called()

    def test_pair_nanoleaf_rejects_out_of_range_port(self):
        mock_nl = Mock()
        mock_nl.get_device.return_value = None
        with patch.object(app, '_ensure_nanoleaf_client', return_value=mock_nl):
            for port in (0, 65536):
                resp = self.client.post(
                    '/api/nanoleaf/pair',
                    json={'ip': '192.168.1.80', 'port': port},
                )
                self.assertEqual(resp.status_code, 400, port)
                self.assertFalse(resp.get_json()['success'])
        mock_nl.probe_by_ip.assert_not_called()

    def test_update_panel_addressing_rotates_and_reorders(self):
        from nanoleaf_client import NanoleafDevice
        device = NanoleafDevice('nl_shapes', '192.168.1.115', auth_token='tok', label='Shapes', model='NL42')
        device.panel_layout = [
            {'id': 1, 'x': 0, 'y': 100},
            {'id': 3, 'x': 100, 'y': 100},
            {'id': 4, 'x': 0, 'y': 0},
            {'id': 2, 'x': 100, 'y': 0},
        ]
        device.panel_ids = [1, 3, 4, 2]
        mock_nl = Mock()
        mock_nl.get_device.return_value = device
        app.nanoleaf_client = mock_nl
        app.light_mappings = {
            'nl_shapes': {
                'universe': 1,
                'start_channel': 1,
                'channel_mode': 'RGB Full Pixel (8bit)',
                'vendor': 'nanoleaf',
                'panel_ids': [1, 3, 4, 2],
                'panel_layout': list(device.panel_layout),
                'map_rotation': 0,
            }
        }
        with patch.object(app, 'save_config'):
            rotated = self.client.post('/api/lights/addressing', json={
                'light_id': 'nl_shapes',
                'map_rotation': 90,
            })
        self.assertTrue(rotated.get_json()['success'], rotated.get_json())
        self.assertEqual(rotated.get_json()['map_rotation'], 90)
        self.assertEqual(device.map_rotation, 90)
        self.assertEqual(device.panel_ids, [1, 3, 4, 2])
        with patch.object(app, 'save_config'):
            reset = self.client.post('/api/lights/addressing', json={
                'light_id': 'nl_shapes',
                'map_rotation': 90,
                'reset_order': True,
            })
        self.assertEqual(reset.get_json()['panel_ids'], [4, 1, 2, 3])
        self.assertEqual(device.panel_ids, [4, 1, 2, 3])
        with patch.object(app, 'save_config'):
            custom = self.client.post('/api/lights/addressing', json={
                'light_id': 'nl_shapes',
                'panel_ids': [2, 1, 4, 3],
            })
        self.assertEqual(custom.get_json()['panel_ids'], [2, 1, 4, 3])
        self.assertEqual(app.light_mappings['nl_shapes']['panel_ids'], [2, 1, 4, 3])
        with patch.object(app, 'save_config'):
            angled = self.client.post('/api/lights/addressing', json={
                'light_id': 'nl_shapes',
                'panel_orientations': {'2': 45, '9': 15},
            })
        self.assertTrue(angled.get_json()['success'], angled.get_json())
        self.assertEqual(angled.get_json()['panel_orientations'], {'2': 45})
        self.assertEqual(app.light_mappings['nl_shapes']['panel_orientations'], {'2': 45})

    def test_update_panel_addressing_orientations_without_mapping(self):
        from nanoleaf_client import NanoleafDevice
        device = NanoleafDevice('nl_shapes', '192.168.1.115', auth_token='tok', label='Shapes', model='NL42')
        device.panel_layout = [
            {'id': 1, 'x': 0, 'y': 100},
            {'id': 3, 'x': 100, 'y': 100},
            {'id': 4, 'x': 0, 'y': 0},
            {'id': 2, 'x': 100, 'y': 0},
        ]
        device.panel_ids = [1, 3, 4, 2]
        mock_nl = Mock()
        mock_nl.get_device.return_value = device
        app.nanoleaf_client = mock_nl
        app.light_mappings = {}
        with patch.object(app, 'save_config'):
            resp = self.client.post('/api/lights/addressing', json={
                'light_id': 'nl_shapes',
                'panel_orientations': {'2': 45, '9': 15},
            })
        self.assertTrue(resp.get_json()['success'], resp.get_json())
        self.assertEqual(resp.get_json()['panel_orientations'], {'2': 45})
        self.assertEqual(device.panel_orientations, {'2': 45})
        self.assertNotIn('nl_shapes', app.light_mappings)

    def test_update_panel_addressing_rejects_non_numeric_layout_ids(self):
        app.nanoleaf_client = Mock()
        app.nanoleaf_client.get_device.return_value = None
        app.light_mappings = {
            'nl_shapes': {
                'universe': 1,
                'start_channel': 1,
                'channel_mode': 'RGB Full Pixel (8bit)',
                'vendor': 'nanoleaf',
                'panel_layout': [{'id': 'abc', 'x': 0, 'y': 0}, {'id': 2, 'x': 1, 'y': 0}],
            }
        }
        resp = self.client.post('/api/lights/addressing', json={
            'light_id': 'nl_shapes',
            'reset_order': True,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['success'])
        self.assertIn('non-numeric', resp.get_json()['error'])

    def test_identify_light_can_target_one_panel(self):
        from nanoleaf_client import NanoleafDevice
        device = NanoleafDevice('nl_lines', '192.168.1.90', auth_token='tok', label='Lines', model='NL59')
        mock_nl = Mock()
        mock_nl.get_device.return_value = device
        app.nanoleaf_client = mock_nl
        resp = self.client.post('/api/lights/identify', json={
            'light_id': 'nl_lines',
            'panel_id': 18,
        })
        self.assertTrue(resp.get_json()['success'], resp.get_json())
        mock_nl.identify_panel.assert_called_once_with(device, 18)
        mock_nl.identify.assert_not_called()
        whole = self.client.post('/api/lights/identify', json={'light_id': 'nl_lines'})
        self.assertTrue(whole.get_json()['success'])
        mock_nl.identify.assert_called_once_with(device)

    def test_list_lights_keeps_lifx_online_when_nanoleaf_is_present(self):
        from lifx_client import LifxLight
        from nanoleaf_client import NanoleafDevice
        light = LifxLight(bytes.fromhex('d073d5aabbcc0000'), '192.168.1.50')
        light.label = 'Stage Wash'
        light.model_name = 'LIFX Color'
        lid = app.light_id(light)
        device = NanoleafDevice('nl_shapes', '192.168.1.115', auth_token='tok', label='Shapes', model='NL42')
        mock_client = Mock()
        mock_client.get_lights.return_value = [light]
        mock_client.lights = {light.target: light}
        mock_client.lock = threading.Lock()
        mock_nl = Mock()
        mock_nl.get_devices.return_value = [device]
        mock_nl.get_device.return_value = device
        mock_nl.requested_bind_ip = app._normalize_interface_ip(app.lifx_interface)
        app.lifx_client = mock_client
        app.nanoleaf_client = mock_nl
        app.nanoleaf_auth = {'nl_shapes': {'auth_token': 'tok', 'ip': '192.168.1.115', 'port': 16021}}
        app.light_mappings = {
            lid: {
                'universe': 1,
                'start_channel': 1,
                'brightness': 1.0,
                'channel_mode': 'RGB (8bit)',
                'label': 'Stage Wash',
                'model': 'LIFX Color',
                'ip': '192.168.1.50',
            },
            'nl_shapes': {
                'universe': 1,
                'start_channel': 10,
                'brightness': 1.0,
                'channel_mode': 'RGB (8bit)',
                'label': 'Shapes',
                'vendor': 'nanoleaf',
                'ip': '192.168.1.115',
            },
        }
        with patch.object(app, 'save_config'):
            resp = self.client.get('/api/lights')
        rows = {row['id']: row for row in resp.get_json()['all_configured_lights']}
        self.assertTrue(rows[lid]['discovered'])
        self.assertTrue(rows['nl_shapes']['discovered'])

    def test_list_lights_matches_lifx_by_saved_ip(self):
        from lifx_client import LifxLight
        light = LifxLight(bytes.fromhex('d073d5aabbcc0000'), '192.168.1.50')
        light.label = 'Stage Wash'
        mock_client = Mock()
        mock_client.get_lights.return_value = [light]
        mock_client.lights = {light.target: light}
        mock_client.lock = threading.Lock()
        app.lifx_client = mock_client
        app.light_mappings = {
            'stale-mapping-id': {
                'universe': 1,
                'start_channel': 1,
                'brightness': 1.0,
                'channel_mode': 'RGB (8bit)',
                'label': 'Stage Wash',
                'ip': '192.168.1.50',
            }
        }
        resp = self.client.get('/api/lights')
        self.assertTrue(resp.get_json()['all_configured_lights'][0]['discovered'])

    def test_list_lights_reprobes_missing_mapped_lifx(self):
        from lifx_client import LifxLight
        light = LifxLight(bytes.fromhex('d073d5aabbcc0000'), '192.168.1.50')
        lid = app.light_id(light)
        mock_client = Mock()
        mock_client.get_lights.return_value = []
        mock_client.lights = {}
        mock_client.lock = threading.Lock()
        entered = threading.Event()
        release = threading.Event()

        def _probe(ip, timeout=0.6):
            entered.set()
            if not release.wait(timeout=2):
                return None
            mock_client.lights[light.target] = light
            return light

        mock_client.probe_light_by_ip.side_effect = _probe
        app.lifx_client = mock_client
        app.light_mappings = {
            lid: {
                'universe': 1,
                'start_channel': 1,
                'brightness': 1.0,
                'channel_mode': 'RGB (8bit)',
                'label': 'Stage Wash',
                'ip': '192.168.1.50',
            }
        }
        self._lifx_schedule_patch.stop()
        try:
            started = time.monotonic()
            resp = self.client.get('/api/lights')
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertFalse(resp.get_json()['all_configured_lights'][0]['discovered'])
            self.assertTrue(entered.wait(timeout=2))
            mock_client.probe_light_by_ip.assert_called()
        finally:
            release.set()
            self._lifx_schedule_patch.start()

    def test_hydrate_lifx_probes_missing_mapped_lights(self):
        from lifx_client import LifxLight
        light = LifxLight(bytes.fromhex('d073d5aabbcc0000'), '192.168.1.50')
        lid = app.light_id(light)
        mock_client = Mock()
        mock_client.lights = {}
        mock_client.lock = threading.Lock()

        def _probe(ip, timeout=0.6):
            mock_client.lights[light.target] = light
            return light

        mock_client.probe_light_by_ip.side_effect = _probe
        app.lifx_client = mock_client
        app.light_mappings = {
            lid: {
                'universe': 1,
                'start_channel': 1,
                'brightness': 1.0,
                'channel_mode': 'RGB (8bit)',
                'label': 'Stage Wash',
                'ip': '192.168.1.50',
            }
        }
        app._hydrate_lifx_devices()
        mock_client.probe_light_by_ip.assert_called_once_with('192.168.1.50', timeout=0.6)
        app._hydrate_lifx_devices()
        mock_client.probe_light_by_ip.assert_called_once()

    def test_hydrate_lifx_respects_probe_backoff(self):
        mock_client = Mock()
        mock_client.lights = {}
        mock_client.lock = threading.Lock()
        mock_client.probe_light_by_ip.return_value = None
        app.lifx_client = mock_client
        app.light_mappings = {
            'd073d5aabbcc': {
                'universe': 1,
                'start_channel': 1,
                'brightness': 1.0,
                'channel_mode': 'RGB (8bit)',
                'label': 'Stage Wash',
                'ip': '192.168.1.50',
            }
        }
        app._hydrate_lifx_devices()
        app._hydrate_lifx_devices()
        mock_client.probe_light_by_ip.assert_called_once()


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
        shifted = app._pixel_test_commands(light, 'rainbow', 1.0, chase_index=1)
        self.assertEqual(rainbow[0][:3], shifted[1][:3])
        self.assertNotEqual(rainbow[0][:3], shifted[0][:3])
        rows = app._pixel_test_commands(light, 'rows', 1.0)
        self.assertEqual(rows[0][:3], rows[4][:3])
        self.assertNotEqual(rows[0][:3], rows[5][:3])
        cols = app._pixel_test_commands(light, 'columns', 1.0)
        self.assertEqual(cols[0][:3], cols[5][:3])
        self.assertNotEqual(cols[0][:3], cols[1][:3])
        grouped = app._pixel_test_commands(light, '8', 1.0)
        self.assertEqual(len(grouped), 55)
        chase = app._pixel_test_commands(light, 'chase', 1.0, chase_index=3)
        self.assertEqual(chase[3][:3], app.PIXEL_CHASE_HIGHLIGHT)
        self.assertEqual(chase[0][:3], app.PIXEL_CHASE_LOWLIGHT)

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
        self._saved_nl = app.nanoleaf_client
        app.nanoleaf_client = None
        with app._batch_lock:
            self._saved_batch = dict(app._batch_commands_by_id)
            app._batch_commands_by_id.clear()

    def tearDown(self):
        app._stop_lifx_batch_sender_thread()
        app.lifx_client = self._saved_client
        app.nanoleaf_client = self._saved_nl
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

    def test_test_rgb_rejects_invalid_fade_ms(self):
        light, mock_client = self._mock_light()
        lid = app.light_id(light)
        resp = self.client.post('/api/lights/test-rgb', json={
            'light_id': lid, 'r': 1, 'g': 2, 'b': 3, 'brightness': 1.0, 'fade_ms': 45.5,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['success'])
        mock_client.executor.submit.assert_not_called()
        self.assertEqual(app._batch_commands_by_id, {})


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

    @patch('app._restart_dmx_if_running')
    @patch('app.save_config')
    def test_rename_keeps_custom_label_when_device_is_discovered(self, _save, _restart):
        from lifx_client import LifxLight
        light = LifxLight(bytes.fromhex('d073d5aabbcc0000'), '192.168.1.50')
        light.label = 'LIFX Colour'
        light.model_name = 'LIFX Color'
        lid = app.light_id(light)
        mock_client = Mock()
        mock_client.get_lights.return_value = [light]
        app.lifx_client = mock_client
        app.light_mappings[lid] = {
            'universe': 1,
            'start_channel': 1,
            'brightness': 1.0,
            'channel_mode': 'RGB (8bit)',
            'label': 'LIFX Colour',
            'model': 'LIFX Color',
            'ip': '192.168.1.50',
        }
        resp = self.client.post('/api/mappings', json={
            'light_id': lid,
            'universe': 1,
            'start_channel': 1,
            'channel_mode': 'RGB (8bit)',
            'label': 'Stage Wash',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['mapping']['label'], 'Stage Wash')
        row = app._configured_light_row(lid, app.light_mappings[lid], light)
        self.assertEqual(row['label'], 'Stage Wash')
        resp = self.client.post('/api/mappings', json={
            'light_id': lid,
            'universe': 1,
            'start_channel': 1,
            'channel_mode': 'RGB (8bit)',
        })
        self.assertEqual(resp.get_json()['mapping']['label'], 'Stage Wash')


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
        self.assertIn('last_drain_duration_s', data['metrics'])
        self.assertIn('peak_drain_duration_s', data['metrics'])
        self.assertIn('drain_overrun_count', data['metrics'])

    def test_batch_utilization_zero_without_peak_capacity(self):
        with app._perf_lock:
            app._perf_metrics['batch_sizes'].clear()
            app._perf_metrics['peak_fixtures_per_frame'] = 0
            app._perf_metrics['avg_batch_size'] = 3
            app._perf_metrics['total_batches_sent'] = 5
        data = self.client.get('/api/metrics').get_json()
        self.assertEqual(data['metrics']['batch_efficiency'], 0.0)
        self.assertEqual(data['metrics']['batch_utilization_percent'], 0.0)


class TestBatchDrainDuration(unittest.TestCase):
    def setUp(self):
        self._saved_client = app.lifx_client
        self._saved_nl_client = app.nanoleaf_client
        self._last_batch = app._last_batch_time
        self._nl_last_batch = app._nl_last_batch_time
        with app._perf_lock:
            self._overrun = app._perf_metrics['drain_overrun_count']
            self._last_drain = app._perf_metrics['last_drain_duration_s']
            self._peak = app._perf_metrics['peak_drain_duration_s']
            self._batches_sent = app._perf_metrics['total_batches_sent']
            self._commands_sent = app._perf_metrics['total_commands_sent']
            self._batch_sizes = list(app._perf_metrics['batch_sizes'])
            self._avg = app._perf_metrics['avg_batch_size']
            self._nl_overrun = app._nl_perf_metrics['drain_overrun_count']
            self._nl_last_drain = app._nl_perf_metrics['last_drain_duration_s']
            self._nl_peak = app._nl_perf_metrics['peak_drain_duration_s']
            self._nl_batches_sent = app._nl_perf_metrics['total_batches_sent']
            self._nl_commands_sent = app._nl_perf_metrics['total_commands_sent']
            self._nl_batch_sizes = list(app._nl_perf_metrics['batch_sizes'])
            self._nl_avg = app._nl_perf_metrics['avg_batch_size']
        with app._batch_lock:
            self._saved_batch = dict(app._batch_commands_by_id)
            app._batch_commands_by_id.clear()
        with app._nl_batch_lock:
            self._saved_nl_batch = dict(app._nl_batch_commands_by_id)
            app._nl_batch_commands_by_id.clear()

    def tearDown(self):
        app.lifx_client = self._saved_client
        app.nanoleaf_client = self._saved_nl_client
        app._last_batch_time = self._last_batch
        app._nl_last_batch_time = self._nl_last_batch
        with app._perf_lock:
            app._perf_metrics['drain_overrun_count'] = self._overrun
            app._perf_metrics['last_drain_duration_s'] = self._last_drain
            app._perf_metrics['peak_drain_duration_s'] = self._peak
            app._perf_metrics['total_batches_sent'] = self._batches_sent
            app._perf_metrics['total_commands_sent'] = self._commands_sent
            app._perf_metrics['avg_batch_size'] = self._avg
            app._perf_metrics['batch_sizes'].clear()
            app._perf_metrics['batch_sizes'].extend(self._batch_sizes)
            app._nl_perf_metrics['drain_overrun_count'] = self._nl_overrun
            app._nl_perf_metrics['last_drain_duration_s'] = self._nl_last_drain
            app._nl_perf_metrics['peak_drain_duration_s'] = self._nl_peak
            app._nl_perf_metrics['total_batches_sent'] = self._nl_batches_sent
            app._nl_perf_metrics['total_commands_sent'] = self._nl_commands_sent
            app._nl_perf_metrics['avg_batch_size'] = self._nl_avg
            app._nl_perf_metrics['batch_sizes'].clear()
            app._nl_perf_metrics['batch_sizes'].extend(self._nl_batch_sizes)
        with app._batch_lock:
            app._batch_commands_by_id.clear()
            app._batch_commands_by_id.update(self._saved_batch)
        with app._nl_batch_lock:
            app._nl_batch_commands_by_id.clear()
            app._nl_batch_commands_by_id.update(self._saved_nl_batch)

    def test_records_drain_duration_when_send_exceeds_interval(self):
        clock = {'t': 1000.0}
        light = SimpleNamespace(target=b'\x01' * 8, ip='192.168.1.10', label='Lamp')
        mock_client = Mock()

        def send(*_args, **_kwargs):
            clock['t'] += 0.05

        mock_client.send_color_now.side_effect = send
        app.lifx_client = mock_client
        app._last_batch_time = 0
        with app._batch_lock:
            app._batch_commands_by_id['lamp'] = (
                'color', light, 1.0, 0.0, 0.0, 3500, 45, 1.0,
            )
        with patch('app.time.time', side_effect=lambda: clock['t']):
            app._lifx_send_one_batch()
        mock_client.send_color_now.assert_called_once()
        self.assertAlmostEqual(app._perf_metrics['last_drain_duration_s'], 0.05)
        self.assertGreaterEqual(
            app._perf_metrics['peak_drain_duration_s'],
            app._perf_metrics['last_drain_duration_s'],
        )
        self.assertEqual(app._perf_metrics['drain_overrun_count'], self._overrun + 1)

    def test_nl_records_drain_duration_when_send_exceeds_interval(self):
        clock = {'t': 1000.0}
        device = SimpleNamespace(label='Shapes', ip='192.168.1.115')
        mock_client = Mock()

        def send(*_args, **_kwargs):
            clock['t'] += app.NANOLEAF_BATCH_INTERVAL + 0.05

        mock_client.send_color.side_effect = send
        app.nanoleaf_client = mock_client
        app._nl_last_batch_time = 0
        with app._nl_batch_lock:
            app._nl_batch_commands_by_id['nl_x'] = (
                'nl_color', device, 1.0, 0.0, 0.0, 3500, 45, 1.0,
            )
        with patch('app.time.time', side_effect=lambda: clock['t']):
            app._nl_send_one_batch()
        mock_client.send_color.assert_called_once()
        self.assertAlmostEqual(
            app._nl_perf_metrics['last_drain_duration_s'],
            app.NANOLEAF_BATCH_INTERVAL + 0.05,
        )
        self.assertGreaterEqual(
            app._nl_perf_metrics['peak_drain_duration_s'],
            app._nl_perf_metrics['last_drain_duration_s'],
        )
        self.assertEqual(app._nl_perf_metrics['drain_overrun_count'], self._nl_overrun + 1)
        self.assertEqual(app._perf_metrics['drain_overrun_count'], self._overrun)


class TestNanoleafAuthSecrets(unittest.TestCase):
    def setUp(self):
        self._mappings = dict(app.light_mappings)
        self._auth = dict(app.nanoleaf_auth)
        self._auth_config = dict(app._nanoleaf_auth_config)
        self._lifx = app.lifx_interface
        self._sacn = app.sacn_interface

    def tearDown(self):
        app.light_mappings = self._mappings
        app.nanoleaf_auth = self._auth
        app._nanoleaf_auth_config = self._auth_config
        app.lifx_interface = self._lifx
        app.sacn_interface = self._sacn

    def test_load_config_overlays_env_and_secrets_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, 'config.json')
            secrets_path = os.path.join(tmp, 'nanoleaf_auth.json')
            with open(config_path, 'w') as handle:
                json.dump({
                    'mappings': {},
                    'settings': {
                        'nanoleaf_auth': {
                            'nl_config': {
                                'auth_token': 'from-config',
                                'ip': '10.0.0.7',
                                'port': 16021,
                            },
                            'nl_shared': {
                                'auth_token': 'from-config-shared',
                                'ip': '10.0.0.6',
                                'port': 16021,
                            },
                        }
                    },
                }, handle)
            with open(secrets_path, 'w') as handle:
                json.dump({
                    'nl_file': {
                        'auth_token': 'from-file',
                        'ip': '10.0.0.8',
                        'port': 16021,
                    },
                    'nl_shared': {
                        'auth_token': 'from-file-shared',
                        'ip': '10.0.0.5',
                        'port': 16021,
                    },
                }, handle)
            env_auth = json.dumps({
                'nl_env': {
                    'auth_token': 'from-env',
                    'ip': '10.0.0.9',
                    'port': 16021,
                }
            })
            with patch.object(app, 'CONFIG_FILE', config_path), patch.dict(os.environ, {
                'NANOLEAF_AUTH_FILE': secrets_path,
                'NANOLEAF_AUTH': env_auth,
            }):
                app.load_config()
                app.save_config()
            with open(config_path) as handle:
                saved_auth = json.load(handle)['settings']['nanoleaf_auth']
        self.assertEqual(app.nanoleaf_auth['nl_config']['auth_token'], 'from-config')
        self.assertEqual(app.nanoleaf_auth['nl_file']['auth_token'], 'from-file')
        self.assertEqual(app.nanoleaf_auth['nl_env']['auth_token'], 'from-env')
        self.assertEqual(app.nanoleaf_auth['nl_shared']['auth_token'], 'from-file-shared')
        self.assertEqual(saved_auth['nl_config']['auth_token'], 'from-config')
        self.assertEqual(saved_auth['nl_shared']['auth_token'], 'from-config-shared')
        self.assertNotIn('nl_file', saved_auth)
        self.assertNotIn('nl_env', saved_auth)


if __name__ == '__main__':
    unittest.main()