"""Unit tests for Home Assistant discovery helpers and colour payloads."""
import json
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from homeassistant_client import (
    HomeAssistantClient,
    HomeAssistantError,
    HomeAssistantLight,
    entity_id_from_ha_id,
    ha_light_id,
    host_from_url,
    light_from_state,
    normalize_base_url,
)


class TestHomeAssistantIds(unittest.TestCase):
    def test_light_id_prefixes(self):
        self.assertEqual(ha_light_id('light.kitchen'), 'ha_light.kitchen')
        self.assertEqual(ha_light_id('ha_light.kitchen'), 'ha_light.kitchen')

    def test_entity_id_from_ha_id(self):
        self.assertEqual(entity_id_from_ha_id('ha_light.kitchen'), 'light.kitchen')
        self.assertEqual(entity_id_from_ha_id('light.kitchen'), 'light.kitchen')

    def test_normalize_base_url(self):
        self.assertEqual(
            normalize_base_url('http://homeassistant.local:8123/'),
            'http://homeassistant.local:8123',
        )
        with self.assertRaises(HomeAssistantError):
            normalize_base_url('homeassistant.local:8123')

    def test_host_from_url(self):
        self.assertEqual(host_from_url('http://192.168.1.50:8123'), '192.168.1.50:8123')


class TestLightFromState(unittest.TestCase):
    def test_parses_rgb_light(self):
        light = light_from_state({
            'entity_id': 'light.living_room',
            'state': 'on',
            'attributes': {
                'friendly_name': 'Living Room',
                'supported_color_modes': ['rgb', 'color_temp'],
                'brightness': 200,
                'rgb_color': [10, 20, 30],
            },
        }, ha_host='ha.local:8123')
        self.assertIsNotNone(light)
        self.assertEqual(light.id, 'ha_light.living_room')
        self.assertEqual(light.label, 'Living Room')
        self.assertEqual(light.model_name, 'HA RGB light')
        self.assertTrue(light.supports_rgb)
        self.assertEqual(light.ip, 'ha.local:8123')

    def test_skips_non_lights(self):
        self.assertIsNone(light_from_state({'entity_id': 'switch.fan', 'state': 'on'}))

    def test_brightness_only_model(self):
        light = light_from_state({
            'entity_id': 'light.closet',
            'state': 'off',
            'attributes': {'supported_color_modes': ['brightness']},
        })
        self.assertEqual(light.model_name, 'HA brightness light')
        self.assertFalse(light.supports_rgb)


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        if isinstance(self._payload, bytes):
            return self._payload
        return json.dumps(self._payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestHomeAssistantClient(unittest.TestCase):
    def test_configured_requires_url_and_token(self):
        client = HomeAssistantClient('http://ha.local:8123', 'token')
        self.assertTrue(client.configured)
        client.configure('', 'token')
        self.assertFalse(client.configured)

    def test_discover_filters_lights(self):
        client = HomeAssistantClient('http://ha.local:8123', 'secret')
        states = [
            {
                'entity_id': 'light.a',
                'state': 'on',
                'attributes': {'friendly_name': 'A', 'supported_color_modes': ['hs']},
            },
            {'entity_id': 'sensor.temp', 'state': '21', 'attributes': {}},
            {
                'entity_id': 'light.b',
                'state': 'off',
                'attributes': {'friendly_name': 'B', 'supported_color_modes': ['onoff']},
            },
        ]
        with patch('homeassistant_client.urllib.request.urlopen', return_value=_FakeResponse(states)):
            lights = client.discover()
        self.assertEqual([light.entity_id for light in lights], ['light.a', 'light.b'])
        self.assertEqual(client.get_light('ha_light.a').label, 'A')

    def test_send_color_rgb_turn_on(self):
        client = HomeAssistantClient('http://ha.local:8123', 'secret')
        light = HomeAssistantLight(
            'light.desk',
            label='Desk',
            supported_color_modes=['rgb'],
            ha_host='ha.local:8123',
        )
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['url'] = request.full_url
            captured['method'] = request.get_method()
            captured['body'] = json.loads(request.data.decode('utf-8'))
            return _FakeResponse([])

        with patch('homeassistant_client.urllib.request.urlopen', side_effect=fake_urlopen):
            client.send_color(light, 1.0, 0.0, 0.0, brightness=0.5, transition=0.1)
        self.assertTrue(captured['url'].endswith('/api/services/light/turn_on'))
        self.assertEqual(captured['method'], 'POST')
        self.assertEqual(captured['body']['entity_id'], 'light.desk')
        self.assertEqual(captured['body']['rgb_color'], [128, 0, 0])
        self.assertEqual(captured['body']['brightness'], 128)
        self.assertEqual(captured['body']['transition'], 0.1)

    def test_send_color_off_uses_turn_off(self):
        client = HomeAssistantClient('http://ha.local:8123', 'secret')
        light = HomeAssistantLight('light.desk', supported_color_modes=['rgb'])
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['url'] = request.full_url
            captured['body'] = json.loads(request.data.decode('utf-8'))
            return _FakeResponse([])

        with patch('homeassistant_client.urllib.request.urlopen', side_effect=fake_urlopen):
            client.send_color(light, 0.0, 0.0, 0.0, brightness=1.0)
        self.assertTrue(captured['url'].endswith('/api/services/light/turn_off'))
        self.assertEqual(captured['body']['entity_id'], 'light.desk')

    def test_http_error_raises(self):
        client = HomeAssistantClient('http://ha.local:8123', 'secret')

        def boom(request, timeout=None):
            raise HTTPError(
                request.full_url, 401, 'Unauthorized', hdrs=None, fp=BytesIO(b'bad token')
            )

        with patch('homeassistant_client.urllib.request.urlopen', side_effect=boom):
            with self.assertRaises(HomeAssistantError) as ctx:
                client.ping()
        self.assertEqual(ctx.exception.status, 401)

    def test_url_error_raises(self):
        client = HomeAssistantClient('http://ha.local:8123', 'secret')
        with patch(
            'homeassistant_client.urllib.request.urlopen',
            side_effect=URLError('down'),
        ):
            with self.assertRaises(HomeAssistantError):
                client.discover()

    def test_apply_saved_hydrates_offline_light(self):
        client = HomeAssistantClient('http://ha.local:8123', 'secret')
        client.apply_saved({
            'ha_light.office': {
                'entity_id': 'light.office',
                'label': 'Office',
                'supported_color_modes': ['color_temp'],
            }
        })
        light = client.get_light('ha_light.office')
        self.assertIsNotNone(light)
        self.assertEqual(light.label, 'Office')
        self.assertTrue(light.supports_color_temp)


if __name__ == '__main__':
    unittest.main()
