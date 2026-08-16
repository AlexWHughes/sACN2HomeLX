"""Unit tests for Nanoleaf discovery helpers and UDP stream frames."""
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from nanoleaf_client import (
    NanoleafDevice,
    apply_panel_order,
    build_stream_frame,
    merge_panel_order,
    nanoleaf_light_id,
    normalize_map_rotation,
    order_panel_ids,
    parse_layout,
    rotate_point,
)
from nanoleaf_products import (
    infer_side_length,
    layout_product_name,
    product_name,
    shape_kind,
    shape_side_length,
    stream_version_for_model,
)


class TestNanoleafProducts(unittest.TestCase):
    def test_known_models(self):
        self.assertEqual(product_name('NL22'), 'Light Panels')
        self.assertEqual(product_name('nl29'), 'Canvas')
        self.assertEqual(product_name('NL42'), 'Shapes')

    def test_unknown_model_uses_code(self):
        self.assertEqual(product_name('NL99'), 'NL99')

    def test_empty_model_is_blank(self):
        self.assertEqual(product_name(''), '')
        self.assertEqual(product_name(None), '')

    def test_stream_version(self):
        self.assertEqual(stream_version_for_model('NL22'), 'v1')
        self.assertEqual(stream_version_for_model('NL29'), 'v2')
        self.assertEqual(stream_version_for_model('NL52'), 'v2')

    def test_blocks_and_lines_model_names(self):
        self.assertEqual(product_name('NL59'), 'Lines')
        self.assertEqual(product_name('NL81'), 'Blocks')
        self.assertEqual(layout_product_name('NL81', []), 'Blocks: Squares')
        self.assertEqual(layout_product_name('NL42', [8, 8]), 'Shapes: Triangle')
        self.assertEqual(layout_product_name('NL42', [7, 7]), 'Shapes: Hexagon')
        self.assertEqual(layout_product_name('NL42', [8, 7]), 'Shapes: Mixed')
        self.assertEqual(layout_product_name('NL59', [17, 18]), 'Lines')

    def test_shape_kind_and_side(self):
        self.assertEqual(shape_kind(8), 'triangle')
        self.assertEqual(shape_kind(7), 'hex')
        self.assertEqual(shape_kind(17), 'line')
        self.assertEqual(shape_kind(99, 'NL81'), 'square')
        self.assertEqual(shape_side_length(8), 134)
        self.assertEqual(shape_side_length(7), 67)
        self.assertEqual(shape_side_length(18), 77)
        self.assertEqual(shape_side_length(99, 0, 'NL81'), 134)

    def test_skylight_controller_types_keep_180_side(self):
        self.assertEqual(shape_side_length(30), 180)
        for shape_type in (31, 32):
            self.assertEqual(shape_side_length(shape_type, 50, 'NL69'), 180)
            self.assertEqual(shape_kind(shape_type), 'square')
            self.assertEqual(shape_kind(shape_type, 'NL69'), 'square')

    def test_infer_side_length_uses_shape_when_deprecated_zero(self):
        panels = [{'shapeType': 8}, {'shapeType': 7}]
        self.assertEqual(infer_side_length({'sideLength': 0}, panels, 'NL42'), 134)
        self.assertEqual(infer_side_length({'sideLength': 154}, panels, 'NL59'), 154)


class TestNanoleafIdsAndLayout(unittest.TestCase):
    def test_light_id_prefixes_and_strips(self):
        self.assertEqual(nanoleaf_light_id('46:EC:1B:0D'), 'nl_46EC1B0D')
        self.assertEqual(nanoleaf_light_id('nl_abc'), 'nl_abc')

    def test_parse_layout_skips_rhythm_and_orders_top_left(self):
        layout = {
            'positionData': [
                {'panelId': 2, 'x': 100, 'y': 0, 'shapeType': 2},
                {'panelId': 1, 'x': 0, 'y': 100, 'shapeType': 2},
                {'panelId': 99, 'x': 0, 'y': 0, 'shapeType': 1},
                {'panelId': 3, 'x': 100, 'y': 100, 'shapeType': 2},
                {'panelId': 4, 'x': 0, 'y': 0, 'shapeType': 2},
            ]
        }
        panels, width, height, kind = parse_layout(layout)
        self.assertEqual(kind, 'matrix')
        self.assertEqual(width, 2)
        self.assertEqual(height, 2)
        self.assertEqual([panel['id'] for panel in panels], [1, 3, 4, 2])

    def test_parse_layout_linear_when_not_a_grid(self):
        layout = {
            'positionData': [
                {'panelId': 10, 'x': 0, 'y': 0, 'shapeType': 0},
                {'panelId': 11, 'x': 50, 'y': 20, 'shapeType': 0},
                {'panelId': 12, 'x': 10, 'y': 80, 'shapeType': 0},
            ]
        }
        panels, width, height, kind = parse_layout(layout)
        self.assertEqual(kind, 'linear')
        self.assertEqual([panel['id'] for panel in panels], [12, 11, 10])
        self.assertEqual((width, height), (3, 1))

    def test_parse_empty_layout(self):
        self.assertEqual(parse_layout(None), ([], 1, 1, 'single'))

    def test_parse_layout_skips_shapes_controller(self):
        layout = {
            'sideLength': 134,
            'positionData': [
                {'panelId': 8954, 'x': 134, 'y': 361, 'o': 0, 'shapeType': 8},
                {'panelId': 0, 'x': 75, 'y': 356, 'o': 60, 'shapeType': 12},
                {'panelId': 64823, 'x': 0, 'y': 284, 'o': 300, 'shapeType': 8},
            ]
        }
        panels, _width, _height, kind = parse_layout(layout)
        self.assertEqual(kind, 'linear')
        self.assertEqual([panel['id'] for panel in panels], [8954, 64823])
        self.assertEqual(panels[0]['o'], 0)

    def test_parse_layout_keeps_lines_skips_connectors(self):
        layout = {
            'sideLength': 154,
            'positionData': [
                {'panelId': 15376, 'x': 227, 'y': 337, 'o': 0, 'shapeType': 20},
                {'panelId': 10400, 'x': 185, 'y': 313, 'o': 300, 'shapeType': 17},
                {'panelId': 56841, 'x': 227, 'y': 289, 'o': 0, 'shapeType': 18},
                {'panelId': 24127, 'x': 227, 'y': 164, 'o': 120, 'shapeType': 16},
                {'panelId': 12105, 'x': 227, 'y': 212, 'o': 0, 'shapeType': 18},
                {'panelId': 5591, 'x': 227, 'y': 337, 'o': 0, 'shapeType': 19},
            ]
        }
        panels, width, height, kind = parse_layout(layout)
        self.assertEqual(kind, 'linear')
        self.assertEqual((width, height), (3, 1))
        self.assertEqual([panel['id'] for panel in panels], [10400, 56841, 12105])
        self.assertEqual([panel['shapeType'] for panel in panels], [17, 18, 18])

    def test_parse_layout_keeps_shapes_hexagons(self):
        layout = {
            'sideLength': 0,
            'positionData': [
                {'panelId': 10, 'x': 0, 'y': 67, 'o': 0, 'shapeType': 7},
                {'panelId': 11, 'x': 116, 'y': 0, 'o': 0, 'shapeType': 7},
                {'panelId': 0, 'x': 58, 'y': 33, 'o': 0, 'shapeType': 12},
            ]
        }
        panels, _width, _height, kind = parse_layout(layout)
        self.assertEqual(kind, 'linear')
        self.assertEqual([panel['id'] for panel in panels], [10, 11])
        self.assertEqual([panel['shapeType'] for panel in panels], [7, 7])


class TestPanelAddressing(unittest.TestCase):
    GRID = [
        {'id': 1, 'x': 0, 'y': 100},
        {'id': 3, 'x': 100, 'y': 100},
        {'id': 4, 'x': 0, 'y': 0},
        {'id': 2, 'x': 100, 'y': 0},
    ]

    def test_normalize_and_rotate_point(self):
        self.assertEqual(normalize_map_rotation(450), 90)
        self.assertEqual(normalize_map_rotation(22), 15)
        self.assertEqual(normalize_map_rotation(37), 30)
        self.assertEqual(rotate_point(0, 100, 90), (100, 0))
        self.assertEqual(rotate_point(100, 0, 180), (-100, 0))
        x, y = rotate_point(0, 100, 30)
        self.assertAlmostEqual(x, 50)
        self.assertAlmostEqual(y, 86.60254037844386)

    def test_order_follows_rotated_top_left(self):
        self.assertEqual(order_panel_ids(self.GRID, 0), [1, 3, 4, 2])
        self.assertEqual(order_panel_ids(self.GRID, 90), [4, 1, 2, 3])
        self.assertEqual(order_panel_ids(self.GRID, 180), [2, 4, 3, 1])
        self.assertEqual(order_panel_ids(self.GRID, 270), [3, 2, 1, 4])

    def test_merge_keeps_custom_and_appends_new(self):
        self.assertEqual(
            merge_panel_order([2, 1], self.GRID, 0),
            [2, 1, 3, 4],
        )

    def test_apply_panel_order_sets_device(self):
        device = NanoleafDevice('nl_x', '127.0.0.1')
        device.panel_layout = list(self.GRID)
        apply_panel_order(device, [2, 4, 3, 1], 180)
        self.assertEqual(device.panel_ids, [2, 4, 3, 1])
        self.assertEqual(device.map_rotation, 180)


class TestStreamFrames(unittest.TestCase):
    def test_v1_frame(self):
        frame = build_stream_frame('v1', [(123, 255, 0, 0, 9), (67, 0, 0, 255, 32)])
        self.assertEqual(frame[0], 2)
        self.assertEqual(list(frame[1:8]), [123, 1, 255, 0, 0, 0, 9])
        self.assertEqual(list(frame[8:15]), [67, 1, 0, 0, 255, 0, 32])

    def test_v2_frame_big_endian(self):
        frame = build_stream_frame('v2', [(46095, 10, 20, 30, 1)])
        self.assertEqual(frame[0:2], b'\x00\x01')
        self.assertEqual(frame[2:4], (46095).to_bytes(2, 'big'))
        self.assertEqual(list(frame[4:8]), [10, 20, 30, 0])
        self.assertEqual(frame[8:10], b'\x00\x01')

    def test_unknown_version_raises(self):
        with self.assertRaises(ValueError):
            build_stream_frame('v9', [])  # type: ignore[arg-type]


class TestNanoleafDevice(unittest.TestCase):
    def test_model_name_uses_layout_shape(self):
        triangles = NanoleafDevice('nl_tri', '10.0.0.1', model='NL42')
        triangles.panel_layout = [
            {'id': 1, 'x': 0, 'y': 0, 'shapeType': 8},
            {'id': 2, 'x': 67, 'y': 0, 'shapeType': 8},
        ]
        self.assertEqual(triangles.model_name, 'Shapes: Triangle')
        hexes = NanoleafDevice('nl_hex', '10.0.0.2', model='NL42')
        hexes.panel_layout = [{'id': 1, 'x': 0, 'y': 0, 'shapeType': 7}]
        self.assertEqual(hexes.model_name, 'Shapes: Hexagon')
        lines = NanoleafDevice('nl_ln', '10.0.0.3', model='NL59')
        lines.panel_layout = [{'id': 1, 'x': 0, 'y': 0, 'shapeType': 18}]
        self.assertEqual(lines.model_name, 'Lines')
        blocks = NanoleafDevice('nl_blk', '10.0.0.4', model='NL81')
        self.assertEqual(blocks.model_name, 'Blocks: Squares')

    def test_zone_fields_from_panels(self):
        device = NanoleafDevice('abc', '192.168.1.20', model='NL29')
        self.assertEqual(device.id, 'nl_abc')
        self.assertFalse(device.paired)
        self.assertEqual(device.model_name, 'Canvas')
        self.assertEqual(device.stream_version, 'v2')
        device.panel_ids = [1, 2, 3]
        device.layout = 'linear'
        self.assertTrue(device.zone_capable)
        self.assertEqual(device.zone_count, 3)
        self.assertEqual(device.effective_layout, 'linear')

    def test_remember_merges_token_and_layout(self):
        from nanoleaf_client import NanoleafClient
        client = NanoleafClient()
        first = NanoleafDevice('nl_one', '10.0.0.1', label='Old')
        client.remember(first)
        second = NanoleafDevice('nl_one', '10.0.0.2', auth_token='tok', label='New', model='NL29')
        second.panel_ids = [5, 6]
        second.layout = 'linear'
        merged = client.remember(second)
        self.assertEqual(merged.ip, '10.0.0.2')
        self.assertEqual(merged.auth_token, 'tok')
        self.assertEqual(merged.panel_ids, [5, 6])
        client.close()

    def test_remember_merges_same_ip_instead_of_duplicating(self):
        from nanoleaf_client import NanoleafClient
        client = NanoleafClient()
        mdns = NanoleafDevice('nl_DB6E588CD6DB', '192.168.1.115', label='Shapes 1BD0', model='NL42')
        client.remember(mdns)
        probed = NanoleafDevice('nl_192-168-1-115', '192.168.1.115')
        probed.id_is_placeholder = True
        merged = client.remember(probed)
        self.assertEqual(merged.id, 'nl_DB6E588CD6DB')
        self.assertEqual(len(client.get_devices()), 1)
        client.close()

    def test_remember_merges_placeholder_outside_192_subnet(self):
        from nanoleaf_client import NanoleafClient
        client = NanoleafClient()
        self.addCleanup(client.close)
        stable = NanoleafDevice('nl_DB6E588CD6DB', '10.0.0.5', label='Shapes 1BD0', model='NL42')
        client.remember(stable)
        probed = NanoleafDevice('nl_10-0-0-5', '10.0.0.5')
        probed.id_is_placeholder = True
        merged = client.remember(probed)
        self.assertEqual(merged.id, 'nl_DB6E588CD6DB')
        self.assertEqual(len(client.get_devices()), 1)


class TestSendColorLayout(unittest.TestCase):
    def test_send_color_does_not_stream_panel_zero(self):
        from nanoleaf_client import NanoleafClient, NanoleafDevice
        client = NanoleafClient()
        self.addCleanup(client.close)
        device = NanoleafDevice('nl_x', '127.0.0.1', auth_token='tok', model='NL42')
        device.ext_control_active = True
        streamed = []

        def fake_refresh(target):
            target.panel_ids = [8954, 64823]
            target.layout = 'linear'
            return target

        client.refresh_info = fake_refresh  # type: ignore[method-assign]
        client._stream = lambda target, panels: streamed.append((target, list(panels)))  # type: ignore[method-assign]
        client.send_color(device, 1.0, 0.0, 0.0, 1.0)
        self.assertEqual(len(streamed), 1)
        panel_ids = [row[0] for row in streamed[0][1]]
        self.assertEqual(panel_ids, [8954, 64823])
        self.assertNotIn(0, panel_ids)

    def test_stream_drops_when_interval_has_not_elapsed(self):
        from nanoleaf_client import (
            NanoleafClient,
            NanoleafDevice,
            MIN_STREAM_INTERVAL,
            build_stream_frame,
        )
        client = NanoleafClient()
        self.addCleanup(client.close)
        device = NanoleafDevice('nl_x', '127.0.0.1', auth_token='tok', model='NL42')
        device.ext_control_active = True
        sent = []
        original_sock = client._sock
        client._sock = SimpleNamespace(
            sendto=lambda *args, **kwargs: sent.append(args),
            close=original_sock.close,
        )
        client._stream(device, [(1, 255, 0, 0, 1)])
        client._stream(device, [(1, 0, 255, 0, 1)])
        self.assertEqual(len(sent), 1)
        self.assertEqual(client._pending_stream[device.id], [(1, 0, 255, 0, 1)])
        client._last_stream[device.id] = 0.0
        blue = [(1, 0, 0, 255, 1)]
        client._stream(device, blue)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[-1][0], build_stream_frame(device.stream_version, blue))
        self.assertNotIn(device.id, client._pending_stream)
        self.assertGreater(MIN_STREAM_INTERVAL, 0)
        self.assertLess(MIN_STREAM_INTERVAL, 1.0 / 10)


class TestIdentifyPanel(unittest.TestCase):
    def test_identify_panel_streams_only_target_bright(self):
        from nanoleaf_client import NanoleafClient, NanoleafDevice
        client = NanoleafClient()
        self.addCleanup(client.close)
        device = NanoleafDevice('nl_x', '127.0.0.1', auth_token='tok', model='NL59')
        device.ext_control_active = True
        device.panel_layout = [
            {'id': 10, 'x': 0, 'y': 0, 'shapeType': 18},
            {'id': 11, 'x': 77, 'y': 0, 'shapeType': 18},
        ]
        streamed = []

        def capture(_device, panels, force=False):
            streamed.append((list(panels), force))

        with patch.object(client, 'ensure_layout', return_value=device), patch.object(client, 'prepare_streaming'), patch.object(client, '_snapshot_rest_state', return_value=None), patch.object(client, '_restore_rest_state'), patch.object(client, '_stream', side_effect=capture), patch('nanoleaf_client.time.sleep'):
            client.identify_panel(device, 11, wait=True)
        self.assertTrue(streamed)
        self.assertTrue(streamed[0][1])
        by_id = {row[0]: row[1:] for row in streamed[0][0]}
        self.assertEqual(by_id[11][:3], (255, 255, 255))
        self.assertLess(by_id[10][0], 40)

    def test_identify_panel_restores_prior_stream_frame(self):
        from nanoleaf_client import NanoleafClient, NanoleafDevice
        client = NanoleafClient()
        self.addCleanup(client.close)
        device = NanoleafDevice('nl_x', '127.0.0.1', auth_token='tok', model='NL59')
        device.ext_control_active = True
        device.panel_layout = [
            {'id': 10, 'x': 0, 'y': 0, 'shapeType': 18},
            {'id': 11, 'x': 77, 'y': 0, 'shapeType': 18},
        ]
        prior = [(10, 1, 2, 3, 1), (11, 4, 5, 6, 1)]
        client._last_frames[device.id] = list(prior)
        streamed = []

        def capture(_device, panels, force=False):
            streamed.append(list(panels))

        with patch.object(client, 'ensure_layout', return_value=device), patch.object(client, 'prepare_streaming'), patch.object(client, '_stream', side_effect=capture), patch('nanoleaf_client.time.sleep'):
            client.identify_panel(device, 11, wait=True)
        self.assertEqual(streamed[-1], prior)
        self.assertTrue(device.ext_control_active)

    def test_identify_panel_restores_rest_state_without_prior_frame(self):
        from nanoleaf_client import NanoleafClient, NanoleafDevice
        client = NanoleafClient()
        self.addCleanup(client.close)
        device = NanoleafDevice('nl_x', '127.0.0.1', auth_token='tok', model='NL59')
        device.panel_layout = [
            {'id': 10, 'x': 0, 'y': 0, 'shapeType': 18},
            {'id': 11, 'x': 77, 'y': 0, 'shapeType': 18},
        ]
        snapshot = {
            'on': True,
            'brightness': 40,
            'hue': 12,
            'sat': 80,
            'colorMode': 'hs',
        }
        streamed = []

        def capture(_device, panels, force=False):
            streamed.append(list(panels))

        def prepare(dev):
            dev.ext_control_active = True

        with patch.object(client, 'ensure_layout', return_value=device), patch.object(client, 'prepare_streaming', side_effect=prepare), patch.object(client, '_snapshot_rest_state', return_value=snapshot), patch.object(client, '_put_state') as put_state, patch.object(client, '_stream', side_effect=capture), patch('nanoleaf_client.time.sleep'):
            client.identify_panel(device, 11, wait=True)
        put_state.assert_called_once()
        body = put_state.call_args.args[1]
        self.assertEqual(body['on']['value'], True)
        self.assertEqual(body['brightness']['value'], 40)
        self.assertEqual(body['hue']['value'], 12)
        self.assertFalse(device.ext_control_active)
        last_by_id = {row[0]: row[1:] for row in streamed[-1]}
        self.assertNotEqual(last_by_id[11][:3], (255, 255, 255))

    def test_identify_panel_rejects_unknown(self):
        from nanoleaf_client import NanoleafClient, NanoleafDevice, NanoleafError
        client = NanoleafClient()
        self.addCleanup(client.close)
        device = NanoleafDevice('nl_x', '127.0.0.1', auth_token='tok', model='NL59')
        device.panel_layout = [{'id': 10, 'x': 0, 'y': 0, 'shapeType': 18}]
        with patch.object(client, 'ensure_layout', return_value=device):
            with self.assertRaises(NanoleafError):
                client.identify_panel(device, 99)


class TestEnsureLayout(unittest.TestCase):
    def test_refreshes_when_model_missing_even_if_layout_known(self):
        from nanoleaf_client import NanoleafClient, NanoleafDevice
        client = NanoleafClient()
        self.addCleanup(client.close)
        device = NanoleafDevice('nl_x', '127.0.0.1', auth_token='tok')
        device.panel_ids = [1, 2]
        device.panel_layout = [{'id': 1, 'x': 0, 'y': 0}, {'id': 2, 'x': 100, 'y': 0}]
        with patch.object(client, 'refresh_info', return_value=device) as refresh:
            client.ensure_layout(device)
        refresh.assert_called_once_with(device)

    def test_skips_refresh_when_layout_and_model_present(self):
        from nanoleaf_client import NanoleafClient, NanoleafDevice
        client = NanoleafClient()
        self.addCleanup(client.close)
        device = NanoleafDevice('nl_x', '127.0.0.1', auth_token='tok', model='NL42')
        device.panel_ids = [1]
        device.panel_layout = [{'id': 1, 'x': 0, 'y': 0}]
        with patch.object(client, 'refresh_info') as refresh:
            client.ensure_layout(device)
        refresh.assert_not_called()


class TestHttpJsonRedaction(unittest.TestCase):
    def test_http_error_redacts_auth_token(self):
        from nanoleaf_client import _http_json, NanoleafError
        error = urllib.error.HTTPError(
            'http://127.0.0.1:16021/api/v1/secret-token/state',
            401,
            'Unauthorized',
            hdrs=None,
            fp=None,
        )
        with patch('nanoleaf_client.urllib.request.urlopen', side_effect=error):
            with self.assertRaises(NanoleafError) as ctx:
                _http_json('PUT', 'http://127.0.0.1:16021/api/v1/secret-token/state', {'on': True})
        message = str(ctx.exception)
        self.assertNotIn('secret-token', message)
        self.assertIn('<redacted>', message)


class TestNanoleafPairErrors(unittest.TestCase):
    def test_pair_explains_missing_button_press(self):
        from nanoleaf_client import NanoleafClient, NanoleafError
        client = NanoleafClient()
        self.addCleanup(client.close)
        device = NanoleafDevice('nl_x', '127.0.0.1')
        with patch('nanoleaf_client._http_json', side_effect=NanoleafError('nope', status=403)):
            with self.assertRaises(NanoleafError) as ctx:
                client.pair(device)
        self.assertIn('power button', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
