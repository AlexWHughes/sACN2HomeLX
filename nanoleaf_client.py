"""Nanoleaf Open API client: mDNS discovery, pairing, REST, and UDP streaming."""

from __future__ import annotations

import colorsys
import json
import math
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple

from nanoleaf_products import (
    NON_LIGHT_SHAPE_TYPES,
    StreamVersion,
    product_name,
    stream_version_for_model,
)

NANOLEAF_SERVICE_TYPE = '_nanoleafapi._tcp.local.'
DEFAULT_API_PORT = 16021
DEFAULT_STREAM_PORT = 60222
DEFAULT_HTTP_TIMEOUT = 3.0
DISCOVERY_TIMEOUT = 4.0
MAX_STREAM_HZ = 10
MIN_STREAM_INTERVAL = 1.0 / MAX_STREAM_HZ
STREAM_TRANSITION_TENTHS = 1  # 100ms; matches the 10Hz stream cap

try:
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
except ImportError:
    ServiceBrowser = None  # type: ignore[misc, assignment]
    ServiceListener = object  # type: ignore[misc, assignment]
    Zeroconf = None  # type: ignore[misc, assignment]


class NanoleafError(Exception):
    """HTTP or protocol error from a Nanoleaf controller."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class NanoleafDevice:
    """One Nanoleaf controller (Light Panels, Canvas, Shapes, …)."""

    vendor = 'nanoleaf'

    def __init__(
        self,
        device_id: str,
        ip: str,
        port: int = DEFAULT_API_PORT,
        auth_token: Optional[str] = None,
        label: str = '',
        model: str = '',
    ):
        self.id = device_id if device_id.startswith('nl_') else f'nl_{device_id}'
        self.ip = ip
        self.port = int(port) if port else DEFAULT_API_PORT
        self.auth_token = auth_token
        self.label = label
        self.model = model
        self.serial = ''
        self.firmware = ''
        self.panel_ids: List[int] = []
        self.panel_layout: List[Dict[str, int]] = []
        self.map_rotation = 0
        self.side_length = 100
        self.matrix_width = 1
        self.matrix_height = 1
        self.layout = 'single'
        self.stream_port = DEFAULT_STREAM_PORT
        self.ext_control_active = False
        self.last_seen = time.time()

    @property
    def paired(self) -> bool:
        return bool(self.auth_token)

    @property
    def model_name(self) -> str:
        return product_name(self.model)

    @property
    def stream_version(self) -> StreamVersion:
        return stream_version_for_model(self.model)

    @property
    def zone_count(self) -> int:
        return max(1, len(self.panel_ids))

    @property
    def zone_capable(self) -> bool:
        return self.zone_count > 1

    @property
    def effective_layout(self) -> str:
        if self.layout in ('linear', 'matrix'):
            return self.layout
        return 'linear' if self.zone_capable else 'single'

    @property
    def base_url(self) -> str:
        return f'http://{self.ip}:{self.port}/api/v1'

    def __repr__(self) -> str:
        return (
            f"NanoleafDevice(id={self.id!r}, label={self.label!r}, "
            f"ip={self.ip!r}, model={self.model_name!r})"
        )


def nanoleaf_light_id(raw_id: str) -> str:
    """Stable mapping id: nl_ plus the mDNS / serial identifier."""
    cleaned = (raw_id or '').strip()
    if cleaned.startswith('nl_'):
        return cleaned
    cleaned = cleaned.replace(':', '').replace(' ', '')
    return f'nl_{cleaned}' if cleaned else 'nl_unknown'


def _txt_str(properties: Dict, key: str, default: str = '') -> str:
    raw = properties.get(key)
    if raw is None:
        raw = properties.get(key.encode())
    if raw is None:
        return default
    if isinstance(raw, bytes):
        return raw.decode('utf-8', errors='ignore')
    return str(raw)


def parse_layout(layout: Optional[dict]) -> Tuple[List[Dict[str, int]], int, int, str]:
    """Return (panels, width, height, layout) from panelLayout.layout JSON.

    Each panel is {id, x, y, o, shapeType}, ordered top-to-bottom then left-to-right.
    Nanoleaf Y increases upward.
    """
    if not layout:
        return [], 1, 1, 'single'
    positions = layout.get('positionData') or layout.get('positionLayout') or []
    panels: List[Dict[str, int]] = []
    for item in positions:
        try:
            shape = int(item.get('shapeType', 0))
        except (TypeError, ValueError):
            shape = 0
        if shape in NON_LIGHT_SHAPE_TYPES:
            continue
        try:
            panel_id = int(item['panelId'])
            x = int(item.get('x', 0))
            y = int(item.get('y', 0))
            orientation = int(item.get('o', 0))
        except (KeyError, TypeError, ValueError):
            continue
        panels.append({
            'id': panel_id,
            'x': x,
            'y': y,
            'o': orientation,
            'shapeType': shape,
        })
    if not panels:
        return [], 1, 1, 'single'
    panels.sort(key=lambda p: (-p['y'], p['x'], p['id']))
    xs = sorted({p['x'] for p in panels})
    ys = sorted({p['y'] for p in panels})
    width = max(1, len(xs))
    height = max(1, len(ys))
    if width > 1 and height > 1 and width * height == len(panels):
        return panels, width, height, 'matrix'
    if len(panels) > 1:
        return panels, len(panels), 1, 'linear'
    return panels, 1, 1, 'single'


MAP_ROTATION_STEP = 15


def normalize_map_rotation(value) -> int:
    """Snap a clockwise map rotation to the nearest 15°."""
    try:
        degrees = int(round(float(value))) % 360
    except (TypeError, ValueError):
        return 0
    if degrees < 0:
        degrees += 360
    return int(round(degrees / MAP_ROTATION_STEP) * MAP_ROTATION_STEP) % 360


def rotate_point(x: float, y: float, degrees: int) -> Tuple[float, float]:
    """Rotate a Nanoleaf (Y-up) point clockwise around the origin."""
    snapped = normalize_map_rotation(degrees)
    if snapped == 0:
        return float(x), float(y)
    if snapped == 90:
        return float(y), float(-x)
    if snapped == 180:
        return float(-x), float(-y)
    if snapped == 270:
        return float(-y), float(x)
    rad = math.radians(snapped)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    return (x * cos_a + y * sin_a, -x * sin_a + y * cos_a)


def order_panel_ids(panels: Sequence[dict], rotation: int = 0) -> List[int]:
    """Pixel order: top-to-bottom, then left-to-right after map rotation."""
    rotation = normalize_map_rotation(rotation)

    def sort_key(panel: dict) -> Tuple[int, int, int]:
        x, y = rotate_point(int(panel.get('x', 0)), int(panel.get('y', 0)), rotation)
        return (-y, x, int(panel['id']))

    return [int(panel['id']) for panel in sorted(panels, key=sort_key)]


def merge_panel_order(
    saved_ids: Sequence[int],
    panels: Sequence[dict],
    rotation: int = 0,
) -> List[int]:
    """Keep a saved pixel order, dropping unknown IDs and appending new ones."""
    known = {int(panel['id']) for panel in panels}
    if not known:
        cleaned: List[int] = []
        seen = set()
        for raw in saved_ids:
            try:
                panel_id = int(raw)
            except (TypeError, ValueError):
                continue
            if panel_id not in seen:
                cleaned.append(panel_id)
                seen.add(panel_id)
        return cleaned
    kept: List[int] = []
    seen = set()
    for raw in saved_ids:
        try:
            panel_id = int(raw)
        except (TypeError, ValueError):
            continue
        if panel_id in known and panel_id not in seen:
            kept.append(panel_id)
            seen.add(panel_id)
    for panel_id in order_panel_ids(panels, rotation):
        if panel_id not in seen:
            kept.append(panel_id)
            seen.add(panel_id)
    return kept


def apply_panel_order(
    device: NanoleafDevice,
    panel_ids: Sequence[int],
    rotation: Optional[int] = None,
) -> List[int]:
    """Set streaming pixel order on a device. Rotation is visual + default sort."""
    if rotation is not None:
        device.map_rotation = normalize_map_rotation(rotation)
    source = device.panel_layout or [
        {'id': panel_id, 'x': 0, 'y': 0} for panel_id in device.panel_ids
    ]
    ordered = merge_panel_order(panel_ids, source, device.map_rotation)
    if ordered:
        device.panel_ids = ordered
    return list(device.panel_ids)


def build_stream_frame(
    version: StreamVersion,
    panels: Sequence[Tuple[int, int, int, int, int]],
) -> bytes:
    """Build one external-control UDP datagram.

    Each panel tuple is (panel_id, r, g, b, transition_tenths).
    White is sent as 0; controllers ignore it and white-balance internally.
    """
    if version == 'v1':
        payload = bytearray()
        payload.append(len(panels) & 0xFF)
        for panel_id, red, green, blue, tenths in panels:
            payload.extend(struct.pack(
                'BBBBBBB',
                panel_id & 0xFF,
                1,  # nFrames
                red & 0xFF,
                green & 0xFF,
                blue & 0xFF,
                0,
                tenths & 0xFF,
            ))
        return bytes(payload)
    if version == 'v2':
        payload = bytearray()
        payload.extend(struct.pack('>H', len(panels) & 0xFFFF))
        for panel_id, red, green, blue, tenths in panels:
            payload.extend(struct.pack(
                '>HBBBBH',
                panel_id & 0xFFFF,
                red & 0xFF,
                green & 0xFF,
                blue & 0xFF,
                0,
                tenths & 0xFFFF,
            ))
        return bytes(payload)
    unreachable: StreamVersion = version
    raise ValueError(f'Unhandled Nanoleaf stream version: {unreachable}')


def _safe_request_url(url: str) -> str:
    """Redact the Open API auth token from a request URL used in error text."""
    marker = '/api/v1/'
    idx = url.find(marker)
    if idx < 0:
        return url
    rest = url[idx + len(marker):]
    if not rest or rest == 'new' or rest.startswith('new/') or rest.startswith('new?'):
        return url
    slash = rest.find('/')
    prefix = url[: idx + len(marker)]
    if slash == -1:
        return prefix + '<redacted>'
    return prefix + '<redacted>' + rest[slash:]


def _http_json(
    method: str,
    url: str,
    body: Optional[dict] = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
) -> Tuple[int, Optional[dict]]:
    data = None if body is None else json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    safe_url = _safe_request_url(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read() if exc.fp else b''
        if status >= 400:
            snippet = raw.decode('utf-8', errors='ignore')[:160]
            raise NanoleafError(
                f'{method} {safe_url} failed ({status}){": " + snippet if snippet else ""}',
                status=status,
            ) from exc
    except urllib.error.URLError as exc:
        raise NanoleafError(f'{method} {safe_url} failed: {exc.reason}') from exc
    if not raw:
        return status, None
    try:
        parsed = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NanoleafError(f'{method} {safe_url} returned non-JSON') from exc
    if not isinstance(parsed, dict):
        raise NanoleafError(f'{method} {safe_url} returned a non-object JSON body')
    return status, parsed


class _MdnsListener(ServiceListener):
    def __init__(self) -> None:
        self.found: List[NanoleafDevice] = []
        self._lock = threading.Lock()

    def add_service(self, zc, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info is None:
            return
        addresses = []
        parsed = getattr(info, 'parsed_addresses', None)
        if callable(parsed):
            addresses = [addr for addr in parsed() if ':' not in addr]
        if not addresses and info.addresses:
            for raw in info.addresses:
                try:
                    addresses.append(socket.inet_ntoa(raw))
                except (OSError, TypeError, ValueError):
                    continue
        if not addresses:
            return
        props = info.properties or {}
        raw_id = _txt_str(props, 'id') or name.split('.')[0]
        model = _txt_str(props, 'md')
        device = NanoleafDevice(
            device_id=nanoleaf_light_id(raw_id),
            ip=addresses[0],
            port=info.port or DEFAULT_API_PORT,
            label=name.split('.')[0],
            model=model,
        )
        with self._lock:
            if any(existing.id == device.id for existing in self.found):
                return
            self.found.append(device)

    def update_service(self, zc, type_: str, name: str) -> None:
        return

    def remove_service(self, zc, type_: str, name: str) -> None:
        return


class NanoleafClient:
    def __init__(self, bind_ip: str = '0.0.0.0'):
        self.requested_bind_ip = bind_ip
        self.devices: Dict[str, NanoleafDevice] = {}
        self.lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        bind_host = bind_ip or '0.0.0.0'
        try:
            self._sock.bind((bind_host, DEFAULT_STREAM_PORT))
        except OSError:
            try:
                self._sock.bind((bind_host, 0))
            except OSError as exc:
                print(f'Nanoleaf UDP bind failed for {bind_host}: {exc}')
        self._last_stream: Dict[str, float] = {}

    def get_devices(self) -> List[NanoleafDevice]:
        with self.lock:
            return list(self.devices.values())

    def get_device(self, device_id: str) -> Optional[NanoleafDevice]:
        with self.lock:
            return self.devices.get(device_id)

    def paired_devices(self) -> List[NanoleafDevice]:
        return [device for device in self.get_devices() if device.paired]

    def remember(self, device: NanoleafDevice) -> NanoleafDevice:
        with self.lock:
            existing = self.devices.get(device.id)
            if existing is None and device.ip:
                for candidate in self.devices.values():
                    if candidate.ip == device.ip and candidate.port == device.port:
                        existing = candidate
                        break
            if existing is None:
                self.devices[device.id] = device
                return device
            if existing.id != device.id and device.id.startswith('nl_') and not device.id.startswith('nl_192-'):
                self.devices.pop(existing.id, None)
                existing.id = device.id
                self.devices[existing.id] = existing
            existing.ip = device.ip or existing.ip
            existing.port = device.port or existing.port
            existing.last_seen = time.time()
            if device.auth_token:
                existing.auth_token = device.auth_token
            if device.label:
                existing.label = device.label
            if device.model:
                existing.model = device.model
            if device.panel_ids:
                existing.panel_ids = list(device.panel_ids)
                existing.matrix_width = device.matrix_width
                existing.matrix_height = device.matrix_height
                existing.layout = device.layout
            if device.panel_layout:
                existing.panel_layout = [dict(panel) for panel in device.panel_layout]
                existing.side_length = device.side_length
            return existing

    def apply_auth(self, saved: Dict[str, Dict]) -> None:
        """Attach persisted tokens (and IPs) from config settings."""
        for device_id, record in saved.items():
            token = (record or {}).get('auth_token')
            if not token:
                continue
            ip = (record or {}).get('ip') or ''
            port = int((record or {}).get('port') or DEFAULT_API_PORT)
            device = self.get_device(device_id)
            if device is None:
                device = NanoleafDevice(device_id, ip, port, auth_token=token)
                self.remember(device)
            else:
                device.auth_token = token
                if ip:
                    device.ip = ip
                device.port = port

    def discover(self, timeout: float = DISCOVERY_TIMEOUT) -> List[NanoleafDevice]:
        if Zeroconf is None or ServiceBrowser is None:
            print('Nanoleaf discovery skipped: install zeroconf (pip install zeroconf)')
            return self.get_devices()
        bind_ip = self.requested_bind_ip
        interfaces = None if not bind_ip or bind_ip == '0.0.0.0' else [bind_ip]
        zc = None
        try:
            zc = Zeroconf(interfaces=interfaces) if interfaces else Zeroconf()
            listener = _MdnsListener()
            browser = ServiceBrowser(zc, NANOLEAF_SERVICE_TYPE, listener)
            time.sleep(max(0.2, timeout))
            browser.cancel()
        except Exception as exc:
            print(f'Nanoleaf mDNS discovery failed: {exc}')
            return self.get_devices()
        finally:
            if zc is not None:
                zc.close()
        for device in listener.found:
            self.remember(device)
        return self.get_devices()

    def pair(self, device: NanoleafDevice) -> str:
        """Create an auth token. Hold the controller power button 5–7s first."""
        url = f'{device.base_url}/new'
        try:
            _status, body = _http_json('POST', url, timeout=5.0)
        except NanoleafError as exc:
            if exc.status in (401, 403):
                raise NanoleafError(
                    'Pairing window closed or not started. Hold the controller '
                    'power button for 5–7 seconds until the LED flashes, then try again.',
                    status=exc.status,
                ) from exc
            raise
        token = (body or {}).get('auth_token')
        if not token:
            raise NanoleafError('Pairing succeeded but no auth_token was returned')
        device.auth_token = str(token)
        self.remember(device)
        self.refresh_info(device)
        return device.auth_token

    def refresh_info(self, device: NanoleafDevice) -> NanoleafDevice:
        if not device.auth_token:
            raise NanoleafError('Device is not paired')
        _status, body = _http_json('GET', f'{device.base_url}/{device.auth_token}')
        if not body:
            raise NanoleafError('Controller returned empty info')
        device.label = str(body.get('name') or device.label)
        device.serial = str(body.get('serialNo') or device.serial)
        device.model = str(body.get('model') or device.model)
        device.firmware = str(body.get('firmwareVersion') or device.firmware)
        layout = (body.get('panelLayout') or {}).get('layout') or {}
        positions, width, height, kind = parse_layout(layout)
        if positions:
            previous_ids = list(device.panel_ids)
            device.panel_layout = positions
            device.matrix_width = width
            device.matrix_height = height
            device.layout = kind
            device.panel_ids = merge_panel_order(previous_ids, positions, device.map_rotation)
            try:
                device.side_length = int(layout.get('sideLength') or device.side_length)
            except (TypeError, ValueError):
                pass
        self.remember(device)
        return device

    def ensure_layout(self, device: NanoleafDevice) -> NanoleafDevice:
        """Fetch panel IDs, coordinates, and product info when the device is incomplete."""
        if not device.auth_token:
            return device
        if device.panel_ids and device.panel_layout and device.model:
            return device
        try:
            return self.refresh_info(device)
        except NanoleafError as exc:
            print(f'Nanoleaf layout refresh failed for {device.ip}: {exc}')
            return device

    def probe_by_ip(
        self,
        ip: str,
        port: int = DEFAULT_API_PORT,
        auth_token: Optional[str] = None,
        timeout: float = 2.0,
    ) -> Optional[NanoleafDevice]:
        """Return a device if something answers on the Open API port."""
        if not _port_open(ip, port, timeout=min(timeout, 1.0)):
            return None
        device_id = nanoleaf_light_id(ip.replace('.', '-'))
        device = NanoleafDevice(device_id, ip, port, auth_token=auth_token)
        if auth_token:
            try:
                self.refresh_info(device)
                if device.serial:
                    stable_id = nanoleaf_light_id(device.serial)
                    if stable_id != device.id:
                        with self.lock:
                            self.devices.pop(device.id, None)
                        device.id = stable_id
            except NanoleafError:
                device.auth_token = None
        return self.remember(device)

    def set_power(self, device: NanoleafDevice, on: bool) -> None:
        self._put_state(device, {'on': {'value': bool(on)}})

    def set_brightness(self, device: NanoleafDevice, brightness: float) -> None:
        value = max(0, min(100, int(round(brightness * 100))))
        self._put_state(device, {'brightness': {'value': value}})

    def set_hs(
        self,
        device: NanoleafDevice,
        hue: float,
        saturation: float,
        brightness: float,
    ) -> None:
        """Whole-fixture HS colour via REST (0–1 units)."""
        self._put_state(device, {
            'on': {'value': True},
            'hue': {'value': int(round(max(0.0, min(1.0, hue)) * 360)) % 360},
            'sat': {'value': int(round(max(0.0, min(1.0, saturation)) * 100))},
            'brightness': {'value': int(round(max(0.0, min(1.0, brightness)) * 100))},
        })

    def identify(self, device: NanoleafDevice) -> None:
        if not device.auth_token:
            raise NanoleafError('Device is not paired')
        _http_json('PUT', f'{device.base_url}/{device.auth_token}/identify')

    def enable_ext_control(self, device: NanoleafDevice) -> None:
        if not device.auth_token:
            raise NanoleafError('Device is not paired')
        version = device.stream_version
        body = {
            'write': {
                'command': 'display',
                'animType': 'extControl',
                'extControlVersion': version,
            }
        }
        _status, response = _http_json(
            'PUT',
            f'{device.base_url}/{device.auth_token}/effects',
            body,
        )
        if response:
            port = response.get('streamControlPort')
            if port:
                device.stream_port = int(port)
        device.ext_control_active = True

    def prepare_streaming(self, device: NanoleafDevice) -> None:
        """Enable external control once so UDP streaming does not block on HTTP."""
        if device.ext_control_active or not device.auth_token:
            return
        try:
            self.set_power(device, True)
            self.set_brightness(device, 1.0)
            self.enable_ext_control(device)
        except NanoleafError as exc:
            print(f'Nanoleaf extControl failed for {device.label or device.ip}: {exc}')

    def send_color(
        self,
        device: NanoleafDevice,
        r: float,
        g: float,
        b: float,
        brightness: float = 1.0,
        transition_tenths: int = STREAM_TRANSITION_TENTHS,
    ) -> None:
        self.ensure_layout(device)
        red, green, blue = _rgb8(r, g, b, brightness)
        if not device.panel_ids:
            hue, sat, value = colorsys.rgb_to_hsv(
                max(0.0, min(1.0, r)),
                max(0.0, min(1.0, g)),
                max(0.0, min(1.0, b)),
            )
            self.set_hs(device, hue, sat, brightness * value)
            return
        panels = [
            (panel_id, red, green, blue, transition_tenths)
            for panel_id in device.panel_ids
        ]
        self.prepare_streaming(device)
        self._stream(device, panels)

    def send_zones(
        self,
        device: NanoleafDevice,
        zone_cmds: Sequence[Tuple[float, float, float, int, float]],
        transition_tenths: int = STREAM_TRANSITION_TENTHS,
    ) -> None:
        self.ensure_layout(device)
        if not device.panel_ids:
            if zone_cmds:
                r, g, b, _kelvin, zone_bri = zone_cmds[0]
                self.send_color(device, r, g, b, zone_bri, transition_tenths)
            return
        panel_ids = device.panel_ids
        panels: List[Tuple[int, int, int, int, int]] = []
        for index, panel_id in enumerate(panel_ids):
            if index < len(zone_cmds):
                r, g, b, _kelvin, zone_bri = zone_cmds[index]
            else:
                r, g, b, zone_bri = 0.0, 0.0, 0.0, 0.0
            red, green, blue = _rgb8(r, g, b, zone_bri)
            panels.append((panel_id, red, green, blue, transition_tenths))
        self.prepare_streaming(device)
        self._stream(device, panels)

    def _put_state(self, device: NanoleafDevice, body: dict) -> None:
        if not device.auth_token:
            raise NanoleafError('Device is not paired')
        _http_json('PUT', f'{device.base_url}/{device.auth_token}/state', body)

    def _stream(
        self,
        device: NanoleafDevice,
        panels: Sequence[Tuple[int, int, int, int, int]],
    ) -> None:
        if not device.ext_control_active:
            return
        now = time.time()
        with self.lock:
            last = self._last_stream.get(device.id, 0.0)
            if now - last < MIN_STREAM_INTERVAL:
                return
            self._last_stream[device.id] = now
        frame = build_stream_frame(device.stream_version, panels)
        try:
            self._sock.sendto(frame, (device.ip, device.stream_port))
        except OSError as exc:
            print(f'Nanoleaf UDP send failed for {device.ip}: {exc}')

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _rgb8(r: float, g: float, b: float, brightness: float) -> Tuple[int, int, int]:
    scale = max(0.0, min(1.0, brightness))
    return (
        max(0, min(255, int(round(r * scale * 255)))),
        max(0, min(255, int(round(g * scale * 255)))),
        max(0, min(255, int(round(b * scale * 255)))),
    )


def _port_open(ip: str, port: int, timeout: float = 1.0) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()
