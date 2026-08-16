"""
Unit tests for lifx_client.py - focusing on color_set_time and state handling
"""
import unittest
from unittest.mock import Mock, MagicMock, patch
import time
import colorsys
import socket
import struct
import sys
import os

# Add parent directory to path to import lifx_client module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lifx_client
from lifx_client import LifxLight, LifxLanClient, clamp01, rgb01_to_hsbk


class TestLifxLight(unittest.TestCase):
    """Test suite for LifxLight class with color_set_time attribute"""
    
    def test_light_initialization_with_color_set_time(self):
        """Test that LifxLight initializes with color_set_time attribute"""
        light = LifxLight(b'\x00' * 8, '192.168.1.100')
        
        # Verify color_set_time is initialized to 0
        self.assertEqual(light.color_set_time, 0)
        self.assertIsInstance(light.color_set_time, (int, float))
    
    def test_light_all_attributes_initialized(self):
        """Test that all light attributes are properly initialized"""
        target = b'\x01\x02\x03\x04\x05\x06\x07\x08'
        ip = '10.0.0.50'
        light = LifxLight(target, ip)
        
        self.assertEqual(light.target, target)
        self.assertEqual(light.ip, ip)
        self.assertEqual(light.label, "")
        self.assertEqual(light.power, 0)
        self.assertEqual(light.vendor, 0)
        self.assertEqual(light.product, 0)
        self.assertEqual(light.version, 0)
        self.assertEqual(light.model_name, "Discovering...")
        self.assertTrue(light.is_light)
        self.assertEqual(light.supported_modes, ["RGB"])
        self.assertEqual(light.current_hue, 0)
        self.assertEqual(light.current_saturation, 0)
        self.assertEqual(light.current_brightness, 0)
        self.assertEqual(light.current_kelvin, lifx_client.DEFAULT_KELVIN)
        self.assertEqual(light.current_rgb, (0, 0, 0))
        self.assertEqual(light.color_set_time, 0)
        self.assertEqual(light.state_requested_time, 0)


class TestSetRgbColorSetTime(unittest.TestCase):
    """Test suite for set_rgb method color_set_time tracking"""
    
    def setUp(self):
        """Set up test fixtures"""
        with patch('socket.socket'):
            self.client = LifxLanClient(bind_ip="0.0.0.0")
            self.client.listening = False  # Don't start listener thread
            # Stop listener thread if it was started
            if self.client.listener_thread and self.client.listener_thread.is_alive():
                self.client.listening = False
                self.client.listener_thread.join(timeout=0.1)
    
    def tearDown(self):
        """Clean up"""
        self.client.listening = False
    
    @patch('time.time')
    def test_set_rgb_updates_color_set_time(self, mock_time):
        """Test that set_rgb updates color_set_time to current time"""
        # Setup
        mock_time.return_value = 1234567890.5
        target = b'\x01' * 8
        ip = '192.168.1.100'
        light = LifxLight(target, ip)
        light.color_set_time = 0
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Execute
        self.client.set_rgb(target, ip, 1.0, 0.5, 0.25, brightness=0.8)
        
        # Verify color_set_time was updated
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.color_set_time, 1234567890.5)
    
    def test_set_rgb_updates_current_rgb_with_brightness(self):
        """Test that set_rgb stores RGB values that reflect brightness"""
        # Setup
        target = b'\x02' * 8
        ip = '192.168.1.101'
        light = LifxLight(target, ip)
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Execute - set pure red at 50% brightness
        self.client.set_rgb(target, ip, 1.0, 0.0, 0.0, brightness=0.5)
        
        # Verify stored RGB reflects brightness
        with self.client.lock:
            updated_light = self.client.lights[target]
            # With 50% brightness, red should be approximately 127-128
            self.assertGreater(updated_light.current_rgb[0], 0)
            self.assertLess(updated_light.current_rgb[0], 255)
            # Green and blue should be 0
            self.assertEqual(updated_light.current_rgb[1], 0)
            self.assertEqual(updated_light.current_rgb[2], 0)
    
    def test_set_rgb_hsbk_conversion_accuracy(self):
        """Test that RGB to HSBK and back maintains color accuracy"""
        # Setup
        target = b'\x03' * 8
        ip = '192.168.1.102'
        light = LifxLight(target, ip)
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Test with various RGB values
        test_colors = [
            (1.0, 0.0, 0.0),  # Red
            (0.0, 1.0, 0.0),  # Green
            (0.0, 0.0, 1.0),  # Blue
            (1.0, 1.0, 0.0),  # Yellow
            (0.5, 0.5, 0.5),  # Gray
            (1.0, 0.5, 0.25), # Orange
        ]
        
        for r, g, b in test_colors:
            self.client.set_rgb(target, ip, r, g, b, brightness=1.0)
            
            with self.client.lock:
                updated_light = self.client.lights[target]
                # Verify RGB is stored
                self.assertIsNotNone(updated_light.current_rgb)
                self.assertEqual(len(updated_light.current_rgb), 3)
                # Values should be in 0-255 range
                for val in updated_light.current_rgb:
                    self.assertGreaterEqual(val, 0)
                    self.assertLessEqual(val, 255)
    
    def test_set_rgb_with_zero_brightness(self):
        """Test set_rgb with zero brightness (should result in black)"""
        # Setup
        target = b'\x04' * 8
        ip = '192.168.1.103'
        light = LifxLight(target, ip)
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Execute - bright red but with 0% brightness
        self.client.set_rgb(target, ip, 1.0, 0.0, 0.0, brightness=0.0)
        
        # Verify RGB is all zeros (black)
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.current_rgb, (0, 0, 0))
    
    def test_set_rgb_with_full_brightness(self):
        """Test set_rgb with full brightness"""
        # Setup
        target = b'\x05' * 8
        ip = '192.168.1.104'
        light = LifxLight(target, ip)
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Execute - set white at full brightness
        self.client.set_rgb(target, ip, 1.0, 1.0, 1.0, brightness=1.0)
        
        # Verify RGB is maximum (white)
        with self.client.lock:
            updated_light = self.client.lights[target]
            # Should be close to (255, 255, 255)
            for val in updated_light.current_rgb:
                self.assertGreater(val, 250)
    
    def test_set_rgb_nonexistent_light(self):
        """Test set_rgb when light doesn't exist in client (should not crash)"""
        target = b'\xFF' * 8
        ip = '192.168.1.200'
        
        # Should not raise exception
        self.client.set_rgb(target, ip, 0.5, 0.5, 0.5)


class TestStateLightHandling(unittest.TestCase):
    """Test suite for STATE_LIGHT message handling with color_set_time"""
    
    def setUp(self):
        """Set up test fixtures"""
        with patch('socket.socket'):
            self.client = LifxLanClient(bind_ip="0.0.0.0")
            self.client.listening = False
            # Stop listener thread if it was started
            if hasattr(self.client, 'listener_thread') and self.client.listener_thread and self.client.listener_thread.is_alive():
                self.client.listening = False
                self.client.listener_thread.join(timeout=0.1)
    
    def tearDown(self):
        """Clean up"""
        self.client.listening = False
    
    def _create_state_light_packet(self, hue, sat, bri, kel):
        """Helper to create a STATE_LIGHT packet"""
        # Build a minimal packet (36 byte header + 9 byte payload)
        header = b'\x00' * 36
        payload = struct.pack("<BHHHH", 0, hue, sat, bri, kel)
        return header + payload
    
    @patch('time.time')
    def test_state_light_updates_when_color_not_recently_set(self, mock_time):
        """Test that STATE_LIGHT updates light when color wasn't recently set"""
        # Setup
        mock_time.return_value = 1000.0
        target = b'\x06' * 8
        ip = '192.168.1.105'
        light = LifxLight(target, ip)
        light.color_set_time = 997.0  # Set 3 seconds ago (more than 1 second threshold)
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Simulate STATE_LIGHT packet values
        hue, sat, bri, kel = 32768, 65535, 32768, 3500
        
        # Call the actual STATE_LIGHT processing method
        with self.client.lock:
            light = self.client.lights[target]
            self.client._process_state_light(light, hue, sat, bri, kel)
        
        # Verify values were updated
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.current_hue, hue)
            self.assertEqual(updated_light.current_saturation, sat)
            self.assertEqual(updated_light.current_brightness, bri)
            self.assertEqual(updated_light.current_kelvin, kel)
            self.assertIsNotNone(updated_light.current_rgb)
    
    @patch('time.time')
    def test_state_light_ignored_when_color_recently_set(self, mock_time):
        """Test that STATE_LIGHT is ignored when color was recently set"""
        # Setup
        mock_time.return_value = 1000.0
        target = b'\x07' * 8
        ip = '192.168.1.106'
        light = LifxLight(target, ip)
        light.color_set_time = 999.5  # Set 0.5 seconds ago (less than 1 second threshold)
        light.current_hue = 10000
        light.current_saturation = 20000
        light.current_brightness = 30000
        light.current_kelvin = 4000
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Simulate STATE_LIGHT packet with different values
        hue, sat, bri, kel = 50000, 60000, 40000, 5000
        
        # Call the actual STATE_LIGHT processing method
        with self.client.lock:
            light = self.client.lights[target]
            self.client._process_state_light(light, hue, sat, bri, kel)
        
        # Verify values were NOT updated (kept original)
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.current_hue, 10000)
            self.assertEqual(updated_light.current_saturation, 20000)
            self.assertEqual(updated_light.current_brightness, 30000)
            self.assertEqual(updated_light.current_kelvin, 4000)
    
    @patch('time.time')
    def test_state_light_exact_threshold_boundary(self, mock_time):
        """Test STATE_LIGHT at exactly 1.0 second boundary"""
        # Setup
        mock_time.return_value = 1000.0
        target = b'\x08' * 8
        ip = '192.168.1.107'
        light = LifxLight(target, ip)
        light.color_set_time = 999.0  # Exactly 1.0 seconds ago
        light.current_hue = 1000
        
        with self.client.lock:
            self.client.lights[target] = light
        
        hue, sat, bri, kel = 5000, 30000, 40000, 3500
        
        # Call the actual STATE_LIGHT processing method
        # At exactly 1.0, should NOT update (condition is > 1.0, but protection_time logic applies)
        with self.client.lock:
            light = self.client.lights[target]
            self.client._process_state_light(light, hue, sat, bri, kel)
        
        # Verify value was NOT updated at exact boundary (protection time prevents update)
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.current_hue, 1000)
    
    @patch('time.time')
    def test_state_light_just_after_threshold(self, mock_time):
        """Test STATE_LIGHT just after protection time threshold"""
        # Setup
        mock_time.return_value = 1000.0
        target = b'\x09' * 8
        ip = '192.168.1.108'
        light = LifxLight(target, ip)
        # Set color 6 seconds ago (past the 5 second DMX protection time)
        # This tests the case where time_since_set > COLOR_SET_PROTECTION_TIME_DMX
        light.color_set_time = 994.0  # 6.0 seconds ago
        light.current_hue = 1000
        
        with self.client.lock:
            self.client.lights[target] = light
        
        hue, sat, bri, kel = 5000, 30000, 40000, 3500
        
        # Call the actual STATE_LIGHT processing method
        with self.client.lock:
            light = self.client.lights[target]
            self.client._process_state_light(light, hue, sat, bri, kel)
        
        # Verify value WAS updated after protection time
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.current_hue, 5000)
    
    @patch('time.time')
    def test_state_light_with_missing_color_set_time(self, mock_time):
        """Test STATE_LIGHT handling when color_set_time attribute is missing"""
        # Setup
        mock_time.return_value = 1000.0
        target = b'\x0A' * 8
        ip = '192.168.1.109'
        light = LifxLight(target, ip)
        # Simulate old light object without color_set_time (set to 0, which is old enough)
        light.color_set_time = 0
        
        with self.client.lock:
            self.client.lights[target] = light
        
        hue, sat, bri, kel = 5000, 30000, 40000, 3500
        
        # Call the actual STATE_LIGHT processing method
        # color_set_time of 0 means time_since_set will be large, should update
        with self.client.lock:
            light = self.client.lights[target]
            self.client._process_state_light(light, hue, sat, bri, kel)
        
        # Verify value was updated (color_set_time of 0 makes time_since_set large)
        with self.client.lock:
            updated_light = self.client.lights[target]
            self.assertEqual(updated_light.current_hue, 5000)
    
    def test_state_light_rgb_conversion_accuracy(self):
        """Test that HSBK to RGB conversion in STATE_LIGHT is accurate"""
        target = b'\x0B' * 8
        ip = '192.168.1.110'
        light = LifxLight(target, ip)
        light.color_set_time = 0  # Old enough to allow update
        
        with self.client.lock:
            self.client.lights[target] = light
        
        # Test various HSBK values
        test_cases = [
            (0, 65535, 65535, 3500),      # Red at full saturation and brightness
            (21845, 65535, 65535, 3500),  # Green
            (43690, 65535, 65535, 3500),  # Blue
            (0, 0, 65535, 3500),          # White (no saturation)
            (32768, 32768, 32768, 3500),  # Mid-tone
        ]
        
        for hue, sat, bri, kel in test_cases:
            with self.client.lock:
                light = self.client.lights[target]
                # Ensure old color_set_time to allow update
                light.color_set_time = 0
                
                # Call the actual STATE_LIGHT processing method
                self.client._process_state_light(light, hue, sat, bri, kel)
                
                # Verify RGB conversion
                self.assertIsNotNone(light.current_rgb)
                self.assertEqual(len(light.current_rgb), 3)
                for val in light.current_rgb:
                    self.assertGreaterEqual(val, 0)
                    self.assertLessEqual(val, 255)


class TestHelperFunctions(unittest.TestCase):
    """Test suite for helper functions"""
    
    def test_clamp01_valid_values(self):
        """Test clamp01 with valid 0-1 values"""
        self.assertEqual(clamp01(0.0), 0.0)
        self.assertEqual(clamp01(0.5), 0.5)
        self.assertEqual(clamp01(1.0), 1.0)
    
    def test_clamp01_below_zero(self):
        """Test clamp01 with values below 0"""
        self.assertEqual(clamp01(-0.1), 0.0)
        self.assertEqual(clamp01(-1.0), 0.0)
        self.assertEqual(clamp01(-100.0), 0.0)
    
    def test_clamp01_above_one(self):
        """Test clamp01 with values above 1"""
        self.assertEqual(clamp01(1.1), 1.0)
        self.assertEqual(clamp01(2.0), 1.0)
        self.assertEqual(clamp01(100.0), 1.0)
    
    def test_clamp01_edge_cases(self):
        """Test clamp01 with edge case values"""
        self.assertEqual(clamp01(0.0000001), 0.0000001)
        self.assertEqual(clamp01(0.9999999), 0.9999999)
    
    def test_rgb01_to_hsbk_pure_colors(self):
        """Test rgb01_to_hsbk with pure RGB colors"""
        # Red
        hue, sat, bri, _kel = rgb01_to_hsbk(1.0, 0.0, 0.0)
        self.assertEqual(hue, 0)
        self.assertEqual(sat, 65535)
        self.assertEqual(bri, 65535)
        
        # Green
        hue, sat, bri, _kel = rgb01_to_hsbk(0.0, 1.0, 0.0)
        self.assertGreater(hue, 20000)  # Approximately 1/3 of 65535
        self.assertLess(hue, 23000)
        self.assertEqual(sat, 65535)
        self.assertEqual(bri, 65535)
        
        # Blue
        hue, sat, bri, _kel = rgb01_to_hsbk(0.0, 0.0, 1.0)
        self.assertGreater(hue, 40000)  # Approximately 2/3 of 65535
        self.assertLess(hue, 46000)
        self.assertEqual(sat, 65535)
        self.assertEqual(bri, 65535)
    
    def test_rgb01_to_hsbk_white(self):
        """Test rgb01_to_hsbk with white (no saturation)"""
        _hue, sat, bri, _kel = rgb01_to_hsbk(1.0, 1.0, 1.0)
        self.assertEqual(sat, 0)  # No saturation for white
        self.assertEqual(bri, 65535)  # Full brightness
    
    def test_rgb01_to_hsbk_black(self):
        """Test rgb01_to_hsbk with black"""
        _hue, _sat, bri, _kel = rgb01_to_hsbk(0.0, 0.0, 0.0)
        self.assertEqual(bri, 0)  # No brightness for black

    def test_rgb01_to_hsbk_hold_hue_when_dim(self):
        """Near-black should keep the previous hue instead of snapping to red."""
        blue_hue, _sat, _bri, _kel = rgb01_to_hsbk(0.0, 0.0, 1.0)
        held_hue, sat, bri, _kel2 = rgb01_to_hsbk(0.0, 0.0, 0.0, hold_hue=blue_hue)
        self.assertEqual(held_hue, blue_hue)
        self.assertEqual(sat, 0)
        self.assertEqual(bri, 0)
    
    def test_rgb01_to_hsbk_gray(self):
        """Test rgb01_to_hsbk with gray (no saturation, mid brightness)"""
        hue, sat, bri, kel = rgb01_to_hsbk(0.5, 0.5, 0.5)
        self.assertEqual(sat, 0)  # No saturation for gray
        self.assertGreater(bri, 30000)  # Approximately half brightness
        self.assertLess(bri, 35000)
    
    def test_rgb01_to_hsbk_kelvin_default(self):
        """Test that rgb01_to_hsbk uses default kelvin"""
        hue, sat, bri, kel = rgb01_to_hsbk(0.5, 0.5, 0.5)
        self.assertEqual(kel, lifx_client.DEFAULT_KELVIN)
    
    def test_rgb01_to_hsbk_kelvin_custom(self):
        """Test rgb01_to_hsbk with custom kelvin value"""
        custom_kelvin = 5000
        hue, sat, bri, kel = rgb01_to_hsbk(0.5, 0.5, 0.5, kelvin=custom_kelvin)
        self.assertEqual(kel, custom_kelvin)
    
    def test_rgb01_to_hsbk_clamping(self):
        """Test that rgb01_to_hsbk clamps input values"""
        # Values above 1.0 should be clamped
        _hue, _sat, bri, _kel = rgb01_to_hsbk(2.0, 2.0, 2.0)
        self.assertEqual(bri, 65535)  # Should be clamped to max
        
        # Values below 0.0 should be clamped
        _hue, _sat, bri, _kel = rgb01_to_hsbk(-1.0, -1.0, -1.0)
        self.assertEqual(bri, 0)  # Should be clamped to min
    
    def test_rgb01_to_hsbk_return_types(self):
        """Test that rgb01_to_hsbk returns correct types"""
        result = rgb01_to_hsbk(0.5, 0.5, 0.5)
        self.assertEqual(len(result), 4)
        # All values should be integers
        for val in result:
            self.assertIsInstance(val, int)
    
    def test_rgb01_to_hsbk_range_bounds(self):
        """Test that rgb01_to_hsbk returns values in correct ranges"""
        hue, sat, bri, kel = rgb01_to_hsbk(0.7, 0.3, 0.9, kelvin=4500)
        
        # All HSBK values should be 16-bit (0-65535)
        self.assertGreaterEqual(hue, 0)
        self.assertLessEqual(hue, 65535)
        self.assertGreaterEqual(sat, 0)
        self.assertLessEqual(sat, 65535)
        self.assertGreaterEqual(bri, 0)
        self.assertLessEqual(bri, 65535)
        self.assertEqual(kel, 4500)


class TestDiscoveryBroadcast(unittest.TestCase):
    """UDP broadcast discovery requires SO_BROADCAST (Errno 13 otherwise)."""

    def _mock_client(self):
        mock_sock = MagicMock()
        mock_sock.getsockname.return_value = ('0.0.0.0', 12345)
        mock_sock.recvfrom.side_effect = socket.timeout
        with patch('socket.socket', return_value=mock_sock):
            client = LifxLanClient(bind_ip="0.0.0.0")
        client.listening = False
        client.batch_running = False
        return client, mock_sock

    def test_socket_enables_broadcast(self):
        client, mock_sock = self._mock_client()
        try:
            mock_sock.setsockopt.assert_any_call(
                socket.SOL_SOCKET, socket.SO_BROADCAST, 1
            )
        finally:
            client.close()

    @patch('time.sleep')
    def test_discover_sends_limited_broadcast(self, _mock_sleep):
        client, mock_sock = self._mock_client()
        try:
            client.discover_lights(timeout=0)
            dests = [call.args[1] for call in mock_sock.sendto.call_args_list]
            self.assertIn(('255.255.255.255', lifx_client.LIFX_PORT), dests)
        finally:
            client.close()

    @patch('time.sleep')
    def test_discover_keeps_existing_lights(self, _mock_sleep):
        client, _mock_sock = self._mock_client()
        try:
            existing = LifxLight(bytes.fromhex('d073d5aabbcc0000'), '192.168.1.50')
            existing.label = 'Keep Me'
            client.lights[existing.target] = existing
            found = client.discover_lights(timeout=0)
            self.assertIn(existing.target, client.lights)
            self.assertTrue(any(light.target == existing.target for light in found))
        finally:
            client.close()

    def test_live_socket_broadcast_does_not_raise_permission_denied(self):
        client = LifxLanClient(bind_ip="0.0.0.0")
        try:
            self.assertNotEqual(
                client.sock.getsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST), 0
            )
            try:
                client.sock.sendto(b'\x00', ('255.255.255.255', lifx_client.LIFX_PORT))
            except PermissionError:
                self.fail(
                    "Broadcast send raised PermissionError; SO_BROADCAST is not effective"
                )
            except OSError as exc:
                self.skipTest(
                    f"Broadcast send unavailable in this environment: {exc}"
                )
        finally:
            client.close()


class TestProductRegistry(unittest.TestCase):
    """New SuperColour products must resolve from the LIFX product catalog."""

    def setUp(self):
        mock_sock = MagicMock()
        mock_sock.getsockname.return_value = ('0.0.0.0', 12345)
        mock_sock.recvfrom.side_effect = socket.timeout
        with patch('socket.socket', return_value=mock_sock):
            self.client = LifxLanClient(bind_ip="0.0.0.0")
        self.client.listening = False
        self.client.batch_running = False

    def tearDown(self):
        self.client.close()

    def test_supercolour_tube_intl_product_218(self):
        self.assertEqual(
            self.client._get_model_name(1, 218),
            "LIFX SuperColour Tube",
        )
        self.assertFalse(self.client._is_switch_product(218, "LIFX SuperColour Tube"))

    def test_supercolour_tube_product_217(self):
        self.assertEqual(
            self.client._get_model_name(1, 217),
            "LIFX SuperColour Tube",
        )

    def test_supercolour_luna_products(self):
        self.assertEqual(self.client._get_model_name(1, 219), "LIFX SuperColour Luna")
        self.assertEqual(self.client._get_model_name(1, 220), "LIFX SuperColour Luna")

    def test_switch_products_are_not_lights(self):
        self.assertEqual(self.client._get_model_name(1, 70), "LIFX Switch")
        self.assertTrue(self.client._is_switch_product(70, "LIFX Switch"))
        self.assertTrue(self.client._is_switch_product(226, "LIFX Dimmer Switch"))

    def test_candle_is_not_classified_as_switch(self):
        self.assertEqual(self.client._get_model_name(1, 68), "LIFX Candle C")
        self.assertFalse(self.client._is_switch_product(68, "LIFX Candle C"))

    def test_unknown_product_keeps_id(self):
        self.assertEqual(
            self.client._get_model_name(1, 99999),
            "Unknown (product=99999)",
        )


class TestMultizonePackets(unittest.TestCase):
    """Optional matrix/linear zone control packets."""

    def setUp(self):
        mock_sock = MagicMock()
        mock_sock.getsockname.return_value = ('0.0.0.0', 12345)
        mock_sock.recvfrom.side_effect = socket.timeout
        with patch('socket.socket', return_value=mock_sock):
            self.client = LifxLanClient(bind_ip="0.0.0.0")
        self.client.listening = False
        self.client.batch_running = False

    def tearDown(self):
        self.client.close()

    def test_supercolour_tube_is_matrix(self):
        from lifx_products import product_layout
        self.assertEqual(product_layout(218), 'matrix')
        light = LifxLight(b'\x00' * 8, '192.168.1.122')
        light.layout = 'matrix'
        light.zone_count = 52
        self.assertTrue(light.zone_capable)

    def test_supercolour_tube_zone_capable_from_product_without_layout(self):
        light = LifxLight(b'\x00' * 8, '192.168.1.122')
        light.product = 218
        self.assertEqual(light.layout, 'single')
        self.assertEqual(light.effective_layout, 'matrix')
        self.assertTrue(light.zone_capable)
        modes = self.client._get_supported_modes(218)
        self.assertIn('RGB Full Pixel (8bit)', modes)
        self.assertNotIn('HSBK (16bit)', modes)

    def test_standard_bulb_not_zone_capable_from_product(self):
        light = LifxLight(b'\x00' * 8, '192.168.1.50')
        light.product = 27
        self.assertFalse(light.zone_capable)
        self.assertNotIn('RGB Full Pixel (8bit)', self.client._get_supported_modes(27))

    def test_set64_packet_size_for_tube(self):
        colors = [(100, 200, 300, 3500)] * 52
        packets = self.client._build_set64_packets(b'\x01' * 8, colors, 4, 13, 20)
        self.assertEqual(len(packets), 1)
        self.assertEqual(len(packets[0]), lifx_client.HEADER_SIZE + 522)
        msg_type = struct.unpack_from("<H", packets[0], 32)[0]
        self.assertEqual(msg_type, lifx_client.SET_64)

    def test_extended_multizone_packet_size(self):
        colors = [(1, 2, 3, 3500)] * 10
        packets = self.client._build_extended_mz_packets(b'\x02' * 8, colors, 20)
        self.assertEqual(len(packets), 1)
        self.assertEqual(len(packets[0]), lifx_client.HEADER_SIZE + 664)
        apply_flag = packets[0][lifx_client.HEADER_SIZE + 4]
        self.assertEqual(apply_flag, lifx_client.MULTI_ZONE_APPLY)

    def test_parse_device_chain_sets_zone_count(self):
        light = LifxLight(b'\x03' * 8, '192.168.1.10')
        tile = bytearray(lifx_client.TILE_DEVICE_SIZE)
        tile[16] = 4
        tile[17] = 13
        payload = bytes([0]) + bytes(tile) + bytes(lifx_client.TILE_DEVICE_SIZE * 15) + bytes([1])
        self.client._parse_state_device_chain(light, b'\x00' * lifx_client.HEADER_SIZE + payload)
        self.assertEqual(light.layout, 'matrix')
        self.assertEqual(light.matrix_width, 4)
        self.assertEqual(light.matrix_height, 13)
        self.assertEqual(light.zone_count, 52)

    def _header_payload(self, payload: bytes) -> bytes:
        return b'\x00' * lifx_client.HEADER_SIZE + payload

    def _set64_colors(self, packet: bytes, count: int):
        offset = lifx_client.HEADER_SIZE + 10
        colors = []
        for i in range(count):
            colors.append(struct.unpack_from("<HHHH", packet, offset + i * 8))
        return colors

    def test_parse_linear_zone_count_extended_uses_uint16_at_offset_zero(self):
        light = LifxLight(b'\x04' * 8, '192.168.1.11')
        # 300 = 0x012C little-endian; a 1-byte read would yield 0x2C (44).
        payload = struct.pack("<HHB", 300, 0, 0)
        self.client._parse_linear_zone_count(
            light, lifx_client.STATE_EXTENDED_COLOR_ZONES, self._header_payload(payload)
        )
        self.assertEqual(light.layout, 'linear')
        self.assertEqual(light.zone_count, 300)

    def test_parse_linear_zone_count_extended_ignores_short_payload(self):
        light = LifxLight(b'\x04' * 8, '192.168.1.11')
        self.client._parse_linear_zone_count(
            light, lifx_client.STATE_EXTENDED_COLOR_ZONES, self._header_payload(b'\x05')
        )
        self.assertEqual(light.layout, 'single')
        self.assertEqual(light.zone_count, 1)

    def test_parse_linear_zone_count_multizone_uses_first_byte(self):
        light = LifxLight(b'\x05' * 8, '192.168.1.12')
        payload = bytes([10, 2])  # uint16 read would be 522
        self.client._parse_linear_zone_count(
            light, lifx_client.STATE_MULTIZONE, self._header_payload(payload)
        )
        self.assertEqual(light.layout, 'linear')
        self.assertEqual(light.zone_count, 10)

    def test_parse_linear_zone_count_zone_uses_first_byte(self):
        light = LifxLight(b'\x06' * 8, '192.168.1.13')
        payload = bytes([8, 99])
        self.client._parse_linear_zone_count(
            light, lifx_client.STATE_ZONE, self._header_payload(payload)
        )
        self.assertEqual(light.layout, 'linear')
        self.assertEqual(light.zone_count, 8)

    def test_matrix_default_tile_dims_truncate_without_zone_count(self):
        light = LifxLight(b'\x07' * 8, '192.168.1.14')
        light.layout = 'matrix'
        self.assertEqual(light.matrix_width, 1)
        self.assertEqual(light.matrix_height, 1)
        self.assertEqual(light.zone_count, 1)
        colors = [(i * 10, 1, 2, 3500) for i in range(8)]
        packets = self.client._zone_packets_for_light(light, colors, 20)
        self.assertEqual(len(packets), 1)
        packed = self._set64_colors(packets[0], 2)
        self.assertEqual(packed[0], (0, 1, 2, 3500))
        self.assertEqual(packed[1], (0, 0, 0, 0))

    def test_matrix_default_tile_dims_keep_colours_when_zone_count_known(self):
        light = LifxLight(b'\x08' * 8, '192.168.1.15')
        light.layout = 'matrix'
        light.zone_count = 8
        colors = [(i * 10, 1, 2, 3500) for i in range(8)]
        packets = self.client._zone_packets_for_light(light, colors, 20)
        self.assertEqual(len(packets), 1)
        packed = self._set64_colors(packets[0], 8)
        self.assertEqual(packed, [(i * 10, 1, 2, 3500) for i in range(8)])

    def test_legacy_linear_emits_set_color_zones_per_run(self):
        light = LifxLight(b'\x09' * 8, '192.168.1.16')
        light.layout = 'linear'
        light.product = 31
        colors = [(1, 2, 3, 3500), (1, 2, 3, 3500), (4, 5, 6, 3500)]
        packets = self.client._zone_packets_for_light(light, colors, 20)
        self.assertEqual(len(packets), 2)
        self.assertEqual(
            struct.unpack_from('<H', packets[0], 32)[0],
            lifx_client.SET_COLOR_ZONES,
        )
        start0, end0 = packets[0][lifx_client.HEADER_SIZE:lifx_client.HEADER_SIZE + 2]
        start1, end1 = packets[1][lifx_client.HEADER_SIZE:lifx_client.HEADER_SIZE + 2]
        self.assertEqual((start0, end0), (0, 1))
        self.assertEqual((start1, end1), (2, 2))
        apply0 = packets[0][lifx_client.HEADER_SIZE + 14]
        apply1 = packets[1][lifx_client.HEADER_SIZE + 14]
        self.assertEqual(apply0, lifx_client.MULTI_ZONE_NO_APPLY)
        self.assertEqual(apply1, lifx_client.MULTI_ZONE_APPLY)

    def test_legacy_linear_truncates_past_8bit_zone_range(self):
        light = LifxLight(b'\x09' * 8, '192.168.1.16')
        light.layout = 'linear'
        light.product = 31
        colors = [(i, 1, 2, 3500) for i in range(lifx_client.LEGACY_MZ_MAX_ZONES + 10)]
        packets = self.client._zone_packets_for_light(light, colors, 20)
        last_start, last_end = packets[-1][lifx_client.HEADER_SIZE:lifx_client.HEADER_SIZE + 2]
        self.assertLess(last_end, lifx_client.LEGACY_MZ_MAX_ZONES)
        self.assertLessEqual(len(packets), lifx_client.LEGACY_MZ_PACKET_BUDGET)

    def test_legacy_linear_coalesces_runs_to_packet_budget(self):
        light = LifxLight(b'\x09' * 8, '192.168.1.16')
        light.layout = 'linear'
        light.product = 31
        colors = [(i, 1, 2, 3500) for i in range(40)]
        packets = self.client._zone_packets_for_light(light, colors, 20)
        self.assertLessEqual(len(packets), lifx_client.LEGACY_MZ_PACKET_BUDGET)
        self.assertLess(len(packets), 40)
        start0, end0 = packets[0][lifx_client.HEADER_SIZE:lifx_client.HEADER_SIZE + 2]
        self.assertEqual(start0, 0)
        self.assertGreater(end0, 0)

    def test_extended_linear_still_uses_message_510(self):
        light = LifxLight(b'\x0a' * 8, '192.168.1.17')
        light.layout = 'linear'
        light.product = 56
        colors = [(1, 2, 3, 3500)] * 3
        packets = self.client._zone_packets_for_light(light, colors, 20)
        self.assertEqual(len(packets), 1)
        self.assertEqual(
            struct.unpack_from('<H', packets[0], 32)[0],
            lifx_client.SET_EXTENDED_COLOR_ZONES,
        )

    def test_supported_modes_come_from_exported_tuples(self):
        from lifx_client import STANDARD_CHANNEL_MODES, ZONE_CHANNEL_MODES
        self.assertEqual(
            self.client._get_supported_modes(27),
            list(STANDARD_CHANNEL_MODES),
        )
        self.assertEqual(
            self.client._get_supported_modes(218),
            [mode for mode in lifx_client.CHANNEL_MODES if mode in ZONE_CHANNEL_MODES],
        )


if __name__ == '__main__':
    unittest.main()