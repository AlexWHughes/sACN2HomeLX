#!/usr/bin/env python3
"""
sACN2LIFX - Control LIFX lights via sACN/E1.31
"""

VERSION = "140826 004"

import json
import os
import hashlib
import threading
import time
import math
import colorsys
import socket
import logging
import re
from typing import Optional, Dict, List, Tuple
from collections import deque
from flask import Flask, render_template, jsonify, request
from werkzeug.serving import WSGIRequestHandler
from lifx_client import (
    CHANNEL_MODES,
    DEFAULT_BATCH_EXECUTOR_WORKERS,
    LifxLanClient,
    LifxLight,
    STANDARD_CHANNEL_MODES,
    ZONE_CHANNEL_MODES,
    clamp01,
)
from dmx_receiver import DMXReceiver

try:
    import ifaddr
except ImportError:
    ifaddr = None

# Set up logging for DMX to LIFX traffic (controlled via environment variables)
# ENABLE_DMX_LOG: Enable basic DMX frame logging (default: false)
# ENABLE_PERF_LOGGING: Enable performance/timing logging (default: false)
# PERF_LOG_SAMPLE_RATE: Log every N frames when sampling (default: 100)
# PERF_SEND_THRESHOLD_MS: Log sends slower than this (default: 5ms)
# PERF_PROCESS_THRESHOLD_MS: Log processing slower than this (default: 10ms)
# RGBW_WHITE_BLEND_COEFF: When mixing the W channel into R/G/B for RGBW DMX modes, fraction of W added to each (0–1, default 0.3)
# FADE_DURATION_MS: LIFX interpolation time (default 45). Longer than the send interval hides jitter.
# LIFX_BATCH_INTERVAL_MS: Minimum ms between LIFX send ticks (default 20).
# FLASK_HOST: Bind address for the development server (default 127.0.0.1).
# The Flask development server and this API are unauthenticated. On untrusted
# networks put them behind a reverse proxy or a production WSGI server.

enable_dmx_log = os.getenv('ENABLE_DMX_LOG', 'false').lower() in ('true', '1', 'yes')
enable_perf_logging = os.getenv('ENABLE_PERF_LOGGING', 'false').lower() in ('true', '1', 'yes')

if enable_dmx_log or enable_perf_logging:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    dmx_logger = logging.getLogger('dmx_lifx')
    dmx_logger.setLevel(logging.INFO)
else:
    dmx_logger = None  # Disabled by default

# Performance logging configuration
try:
    PERF_LOG_SAMPLE_RATE = max(1, int(os.getenv('PERF_LOG_SAMPLE_RATE', '100')))  # Log every N frames, minimum 1
    PERF_SEND_THRESHOLD_MS = max(0.0, float(os.getenv('PERF_SEND_THRESHOLD_MS', '5.0')))  # Log slow sends
    PERF_PROCESS_THRESHOLD_MS = max(0.0, float(os.getenv('PERF_PROCESS_THRESHOLD_MS', '10.0')))  # Log slow processing
except ValueError as e:
    print(f"Warning: Invalid performance logging configuration: {e}. Using defaults.")
    PERF_LOG_SAMPLE_RATE = 100
    PERF_SEND_THRESHOLD_MS = 5.0
    PERF_PROCESS_THRESHOLD_MS = 10.0

try:
    BLEND_WHITE_COEFF = max(0.0, min(1.0, float(os.getenv("RGBW_WHITE_BLEND_COEFF", "0.3"))))
except ValueError:
    print("Warning: Invalid RGBW_WHITE_BLEND_COEFF; using default 0.3")
    BLEND_WHITE_COEFF = 0.3

# Frame counter for sampling (thread-local would be better, but simple counter works for single-threaded DMX processing)
_dmx_frame_counter = 0

app = Flask(__name__)

# Global state
lifx_client: Optional[LifxLanClient] = None
dmx_receiver: Optional[DMXReceiver] = None
light_mappings: Dict[str, Dict] = {}  # light_id -> {universe, start_channel, brightness}
running = False
dmx_thread: Optional[threading.Thread] = None
lifx_interface: Optional[str] = None  # Network interface IP for LIFX
sacn_interface: Optional[str] = None  # Network interface IP for sACN

# Thread synchronization for DMX state mutations
dmx_lock = threading.Lock()

# Configuration
CONFIG_FILE = "config.json"
MAX_BRIGHTNESS = 1.0  # Stored as 0-1 internally, displayed as 0-100%
MAX_LIGHT_LABEL_LEN = 64
OVERRIDE_MAX_BRIGHT_TOTAL_RGB = 200 * 3
MAX_BRIGHT_OVERRIDE = 1.0
MAX_RGB_PER_COLOUR = 255
DEFAULT_KELVIN = 3500
MAX_HUE = 360  # Degrees
MAX_SATURATION = 100  # Percentage
MAX_INTENSITY = 100  # Percentage
try:
    FADE_DURATION_MS = max(0, int(os.getenv('FADE_DURATION_MS', '45')))
except ValueError:
    print("Warning: Invalid FADE_DURATION_MS; using default 45")
    FADE_DURATION_MS = 45
VALUE_CHANGE_THRESHOLD = 1  # Only update if DMX value changed by this much (0-255)

# DMX channel modes. Names come from lifx_client.CHANNEL_MODES; this map adds
# decode spec: (kind, bit_depth, fine_first). 16-bit is coarse then fine unless fine_first.
CHANNEL_MODE_SPEC: Dict[str, Tuple[str, int, bool]] = {
    'RGB (8bit)': ('rgb', 8, False),
    'RGB (16bit)': ('rgb', 16, False),
    'RGB (16bit, fine first)': ('rgb', 16, True),
    'RGB + Intensity (8bit)': ('rgb_intensity', 8, False),
    'RGBW (8bit)': ('rgbw', 8, False),
    'RGBW (16bit)': ('rgbw', 16, False),
    'RGBW (16bit, fine first)': ('rgbw', 16, True),
    'HSBK (8bit)': ('hsbk', 8, False),
    'HSBK (16bit)': ('hsbk', 16, False),
    'HSBK (16bit, fine first)': ('hsbk', 16, True),
    'HSBK + Intensity (8bit)': ('hsbk_intensity', 8, False),
    'RGB Full Pixel (8bit)': ('rgb', 8, False),
    'RGB + Intensity Full Pixel (8bit)': ('rgb_intensity', 8, False),
    'RGBW Full Pixel (8bit)': ('rgbw', 8, False),
}

if tuple(CHANNEL_MODE_SPEC.keys()) != CHANNEL_MODES:
    raise RuntimeError(
        "CHANNEL_MODE_SPEC keys must match lifx_client.CHANNEL_MODES in order"
    )

_KIND_BASE_CHANNELS: Dict[str, int] = {
    'rgb': 3,
    'rgb_intensity': 4,
    'rgbw': 4,
    'hsbk': 4,
    'hsbk_intensity': 5,
}

CHANNELS_FOR_MODE: Dict[str, int] = {
    mode: _KIND_BASE_CHANNELS[kind] * (2 if bits == 16 else 1)
    for mode, (kind, bits, _fine_first) in CHANNEL_MODE_SPEC.items()
}

_PIXEL_MODE_RE = re.compile(
    r'^(?P<kind>RGB(?: \+ Intensity)?|RGBW) (?:Full|(?P<groups>\d+)) Pixel \(8bit\)$'
)
PIXEL_GROUP_PRESETS = (8, 4, 2)
ALL_CHANNEL_MODES: List[str] = list(STANDARD_CHANNEL_MODES)
KELVIN_MIN = 2500
KELVIN_MAX = 9000
U16_MAX = 65535.0


def _dmx_u16(msb: int, lsb: int, fine_first: bool) -> int:
    """Combine two DMX bytes into 0..65535 (big-endian coarse/fine, or fine-first if requested)."""
    if fine_first:
        return ((lsb & 0xFF) << 8) | (msb & 0xFF)
    return ((msb & 0xFF) << 8) | (lsb & 0xFF)


def _u16_pairs_changed(
    channel_values: list,
    last_values: list,
    num_pairs: int,
    fine_first: bool,
) -> bool:
    """True if any 16-bit pair (num_pairs pairs starting at index 0) changed."""
    need = num_pairs * 2
    if len(channel_values) < need or len(last_values) < need:
        return True
    for p in range(num_pairs):
        i = p * 2
        cur = _dmx_u16(channel_values[i], channel_values[i + 1], fine_first)
        prev = _dmx_u16(last_values[i], last_values[i + 1], fine_first)
        if abs(cur - prev) >= 1:
            return True
    return False


def _parse_pixel_mode(mode: Optional[str]) -> Optional[Tuple[str, Optional[int]]]:
    """Return (base kind mode, control-cell count). None count means Full Pixel."""
    if not mode:
        return None
    match = _PIXEL_MODE_RE.fullmatch(mode)
    if not match:
        return None
    kind = match.group('kind')
    groups = match.group('groups')
    return (f'{kind} (8bit)', int(groups) if groups else None)


def _normalize_channel_mode(mode: Optional[str]) -> str:
    if mode in CHANNEL_MODE_SPEC:
        return mode
    if mode and _parse_pixel_mode(mode) is not None:
        return mode
    return 'RGB (8bit)'


def _mode_is_pixel(mode: Optional[str]) -> bool:
    return _parse_pixel_mode(mode) is not None


def _channel_mode_spec(mode: str) -> Tuple[str, int, bool]:
    parsed = _parse_pixel_mode(mode)
    lookup = parsed[0] if parsed else mode
    return CHANNEL_MODE_SPEC.get(lookup) or CHANNEL_MODE_SPEC['RGB (8bit)']


def _channels_per_cell(mode: str) -> int:
    kind, bits, _fine_first = _channel_mode_spec(mode)
    return _KIND_BASE_CHANNELS[kind] * (2 if bits == 16 else 1)


def _int_or(value, default: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _coerce_fade_ms(value) -> Optional[int]:
    """Parse fade_ms as an integer in 0..0xFFFFFFFF, or None if invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        numeric = value
    elif isinstance(value, int):
        numeric = value
    elif isinstance(value, str):
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return None
    else:
        return None
    try:
        parsed = int(numeric)
    except (OverflowError, TypeError, ValueError):
        return None
    if 0 <= parsed <= 0xFFFFFFFF:
        return parsed
    return None


def _geometry_for(light: Optional[LifxLight], mapping: Optional[Dict] = None) -> Tuple[int, int, int]:
    """Physical zone count, matrix width, matrix height."""
    mapping = mapping or {}
    count = _int_or(mapping.get('zone_count'), 1)
    width = _int_or(mapping.get('matrix_width'), 1)
    height = _int_or(mapping.get('matrix_height'), 1)
    if light is not None and getattr(light, 'zone_count', 1) > 1:
        count = int(light.zone_count)
        width = max(1, int(light.matrix_width or 1))
        height = max(1, int(light.matrix_height or 1))
    return count, width, height


def _pixel_group_counts(zone_count: int, width: int = 1, height: int = 1) -> List[int]:
    """Smaller control-cell counts that group physical pixels together."""
    if zone_count <= 2:
        return []
    seen = {zone_count, 1}
    counts: List[int] = []
    if width > 1 and height > 1 and width * height == zone_count:
        for n in (height, width):
            if n not in seen and 1 < n < zone_count:
                counts.append(n)
                seen.add(n)
    for n in PIXEL_GROUP_PRESETS:
        if n not in seen and 1 < n < zone_count:
            counts.append(n)
            seen.add(n)
    counts.sort(reverse=True)
    return counts


def _mapping_is_zone_capable(mapping: Optional[Dict]) -> bool:
    if not mapping:
        return False
    return bool(_light_zone_fields(None, mapping).get('zone_capable'))


def _supported_modes_for(light: Optional[LifxLight] = None, mapping: Optional[Dict] = None) -> List[str]:
    capable = bool(light is not None and light.zone_capable) or _mapping_is_zone_capable(mapping)
    if not capable:
        return list(STANDARD_CHANNEL_MODES)
    zone_count, width, height = _geometry_for(light, mapping)
    modes = list(ZONE_CHANNEL_MODES)
    for groups in _pixel_group_counts(zone_count, width, height):
        modes.append(f'RGB {groups} Pixel (8bit)')
    return modes


def _mode_options_for(light: Optional[LifxLight] = None, mapping: Optional[Dict] = None) -> List[Dict]:
    zone_count, _width, _height = _geometry_for(light, mapping)
    options: List[Dict] = []
    for mode in _supported_modes_for(light, mapping):
        parsed = _parse_pixel_mode(mode)
        if parsed is None:
            options.append({'value': mode, 'label': mode, 'group': 'Whole fixture'})
            continue
        _base, groups = parsed
        cells = zone_count if groups is None else min(groups, zone_count)
        cells = max(1, cells)
        ch = _channels_per_cell(mode) * cells
        group = 'Full Pixel' if groups is None else 'Grouped pixels'
        options.append({'value': mode, 'label': f'{mode} — {ch} ch', 'group': group})
    return options


def _sacn_bind_ip() -> Optional[str]:
    return None if _normalize_interface_ip(sacn_interface) == '0.0.0.0' else sacn_interface


def _dmx_param_unit(values: list, index: int, bits: int, fine_first: bool) -> float:
    """Read one DMX parameter as 0..1 (8-bit channel or 16-bit pair)."""
    if bits == 16:
        i = index * 2
        return clamp01(_dmx_u16(values[i], values[i + 1], fine_first) / U16_MAX)
    return clamp01(values[index] / MAX_RGB_PER_COLOUR)


def _kelvin_from_unit(unit: float) -> int:
    return int(max(KELVIN_MIN, min(KELVIN_MAX, KELVIN_MIN + unit * (KELVIN_MAX - KELVIN_MIN))))


def _blend_white(r: float, g: float, b: float, w: float) -> Tuple[float, float, float]:
    return (
        min(1.0, r + w * BLEND_WHITE_COEFF),
        min(1.0, g + w * BLEND_WHITE_COEFF),
        min(1.0, b + w * BLEND_WHITE_COEFF),
    )


def _hsbk_cmd(
    h_unit: float,
    s_unit: float,
    v_unit: float,
    k_unit: float,
    brightness: float,
    intensity: float,
) -> Tuple[float, float, float, int, int, float]:
    r, g, b = colorsys.hsv_to_rgb(h_unit, s_unit, 1.0)
    return (r, g, b, _kelvin_from_unit(k_unit), FADE_DURATION_MS, brightness * v_unit * intensity)


def _dmx_values_changed(channel_mode: str, channel_values: list, last_values: list) -> bool:
    _kind, bits, fine_first = _channel_mode_spec(channel_mode)
    need = _channels_per_cell(channel_mode)
    if bits == 16 and len(channel_values) >= need and len(last_values) >= need:
        return _u16_pairs_changed(channel_values, last_values, need // 2, fine_first)
    for i, val in enumerate(channel_values):
        if i >= len(last_values) or abs(val - last_values[i]) >= VALUE_CHANGE_THRESHOLD:
            return True
    return False


def _dmx_decode_to_cmd(
    channel_mode: str,
    channel_values: list,
    brightness: float,
) -> Optional[Tuple[float, float, float, int, int, float]]:
    """Map DMX channel bytes to a LIFX (r, g, b, kelvin, duration_ms, brightness) command."""
    kind, bits, fine_first = _channel_mode_spec(channel_mode)

    def unit(i: int) -> float:
        return _dmx_param_unit(channel_values, i, bits, fine_first)

    if kind == 'rgb':
        return (unit(0), unit(1), unit(2), DEFAULT_KELVIN, FADE_DURATION_MS, brightness)
    if kind == 'rgb_intensity':
        intensity = unit(3)
        return (
            unit(0) * intensity,
            unit(1) * intensity,
            unit(2) * intensity,
            DEFAULT_KELVIN,
            FADE_DURATION_MS,
            brightness,
        )
    if kind == 'rgbw':
        r, g, b = _blend_white(unit(0), unit(1), unit(2), unit(3))
        return (r, g, b, DEFAULT_KELVIN, FADE_DURATION_MS, brightness)
    if kind == 'hsbk':
        return _hsbk_cmd(unit(0), unit(1), unit(2), unit(3), brightness, 1.0)
    if kind == 'hsbk_intensity':
        return _hsbk_cmd(unit(0), unit(1), unit(2), unit(3), brightness, unit(4))
    raise ValueError(f'Unhandled channel mode kind: {kind}')


def load_config():
    """Load configuration (mappings and settings) from file"""
    global light_mappings, lifx_interface, sacn_interface
    try:
        with open(CONFIG_FILE, 'r') as f:
            content = f.read().strip()
            # Handle empty file
            if not content:
                light_mappings = {}
                lifx_interface = None
                sacn_interface = None
                invalidate_dmx_mapping_cache()
                return
            
            config = json.loads(content)
            light_mappings = config.get('mappings', {})
            settings = config.get('settings', {})
            lifx_interface = settings.get('lifx_interface', None)
            sacn_interface = settings.get('sacn_interface', None)
    except FileNotFoundError:
        light_mappings = {}
        lifx_interface = None
        sacn_interface = None
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Warning: Error parsing config.json: {e}. Using empty configuration.")
        light_mappings = {}
        lifx_interface = None
        sacn_interface = None
    invalidate_dmx_mapping_cache()


def save_config():
    """Save configuration (mappings and settings) to file"""
    config = {
        'mappings': light_mappings,
        'settings': {
            'lifx_interface': lifx_interface,
            'sacn_interface': sacn_interface
        }
    }
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


# Deprecated functions removed - use load_config() and save_config() directly


def _normalize_interface_ip(ip: Optional[str]) -> str:
    """Normalize interface IP: return '0.0.0.0' if None or '0.0.0.0', otherwise return the IP"""
    return '0.0.0.0' if not ip or ip == '0.0.0.0' else ip


def get_network_interfaces():
    """Get list of available network interfaces with their IP addresses"""
    interfaces = []
    seen = set()

    if ifaddr is not None:
        try:
            for adapter in ifaddr.get_adapters():
                name = adapter.nice_name or adapter.name
                for ip_info in adapter.ips:
                    ip = ip_info.ip
                    if not isinstance(ip, str) or ip.startswith('127.'):
                        continue
                    key = (name, ip)
                    if key in seen:
                        continue
                    seen.add(key)
                    interfaces.append({
                        'name': name,
                        'ip': ip,
                        'display': f"{name} ({ip})"
                    })
        except Exception as e:
            print(f"Error getting network interfaces: {e}")

    # Fallback: try socket method if ifaddr failed or not available
    if not interfaces:
        try:
            hostname = socket.gethostname()
            # Get all IP addresses
            addrinfo = socket.getaddrinfo(hostname, None)
            for info in addrinfo:
                ip = info[4][0]
                if ip and not ip.startswith('127.'):
                    interfaces.append({
                        'name': hostname,
                        'ip': ip,
                        'display': f"{hostname} ({ip})"
                    })
        except Exception as e:
            print(f"Error getting network interfaces (fallback): {e}")
    
    # Add "All Interfaces" option
    interfaces.insert(0, {
        'name': '0.0.0.0',
        'ip': '0.0.0.0',
        'display': 'All Interfaces (0.0.0.0)'
    })
    
    return interfaces


def light_id(light: LifxLight) -> str:
    """Generate unique ID for a light"""
    return light.target.hex()


def _display_label(light: Optional[LifxLight], mapping: Optional[Dict] = None, lid: str = '') -> str:
    stored = (mapping or {}).get('label')
    if isinstance(stored, str):
        stored = stored.strip()
        if stored:
            return stored
    if light is not None and light.label:
        return light.label
    if lid:
        return f'Light {lid[:10]}'
    if light is not None:
        return f'Light {light.ip}'
    return 'Light'


def _light_summary(light: LifxLight, lid: Optional[str] = None) -> Dict:
    resolved_id = lid or light_id(light)
    return {
        'id': resolved_id,
        'label': light.label or f"Light {light.ip}",
        'ip': light.ip,
        'model': light.model_name,
        **_light_zone_fields(light),
    }


def _mapping_uses_zones(mapping: Dict) -> bool:
    return _mode_is_pixel(mapping.get('channel_mode'))


def _physical_zone_count(mapping: Dict, light: Optional[LifxLight]) -> int:
    count, _width, _height = _geometry_for(light, mapping)
    return count if count > 1 else 1


def _mapping_zone_count(mapping: Dict, light: Optional[LifxLight]) -> int:
    """How many sequential DMX pixel slots to consume for pixel modes."""
    if not _mapping_uses_zones(mapping):
        return 1
    physical = _physical_zone_count(mapping, light)
    parsed = _parse_pixel_mode(_normalize_channel_mode(mapping.get('channel_mode')))
    if parsed is None:
        return physical
    _base, groups = parsed
    if groups is None or groups >= physical:
        return physical
    return max(1, groups)


def _mapping_channels_needed(mapping: Dict, light: Optional[LifxLight]) -> int:
    channel_mode = _normalize_channel_mode(mapping.get('channel_mode'))
    return _channels_per_cell(channel_mode) * _mapping_zone_count(mapping, light)


def _expand_control_cells_to_zones(
    cell_cmds: list,
    zone_count: int,
    width: int = 1,
    height: int = 1,
) -> list:
    """Repeat grouped DMX cells across the fixture's physical pixels."""
    n = len(cell_cmds)
    if zone_count < 1 or n < 1:
        return []
    if n == 1:
        return list(cell_cmds) * zone_count
    if n >= zone_count:
        return list(cell_cmds[:zone_count])
    if width > 1 and n == width and height * width == zone_count:
        return [cell_cmds[i % width] for i in range(zone_count)]
    if width > 1 and n == height and height * width == zone_count:
        return [cell_cmds[i // width] for i in range(zone_count)]
    return [cell_cmds[min(n - 1, i * n // zone_count)] for i in range(zone_count)]


def _hsv_cell(hue: float, brightness: float) -> Tuple[float, float, float, int, float]:
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, 1.0, 1.0)
    return (r, g, b, DEFAULT_KELVIN, brightness)


def _rainbow_cells(count: int, brightness: float) -> List[Tuple[float, float, float, int, float]]:
    n = max(1, count)
    return [_hsv_cell(i / n, brightness) for i in range(n)]


def _pixel_test_patterns(light: Optional[LifxLight], mapping: Optional[Dict] = None) -> List[Dict[str, str]]:
    if light is None or not light.zone_capable:
        return []
    count, width, height = _geometry_for(light, mapping)
    if count < 2:
        return []
    patterns = [{'id': 'rainbow', 'label': 'Rainbow'}]
    matrix = width > 1 and height > 1 and width * height == count
    if matrix:
        patterns.append({'id': 'rows', 'label': f'{height} rows'})
        patterns.append({'id': 'columns', 'label': f'{width} cols'})
    for n in _pixel_group_counts(count, width, height):
        if matrix and n in (width, height):
            continue
        patterns.append({'id': str(n), 'label': f'{n} groups'})
    patterns.append({'id': 'chase', 'label': 'Pixel chase'})
    return patterns


def _pixel_test_commands(
    light: LifxLight,
    pattern: str,
    brightness: float,
    chase_index: int = 0,
    chase_rgb: Optional[Tuple[float, float, float]] = None,
    mapping: Optional[Dict] = None,
) -> List[Tuple[float, float, float, int, float]]:
    """Build per-zone colours for Test pixel patterns."""
    count, width, height = _geometry_for(light, mapping)
    if count < 2:
        return []
    bri = clamp01(brightness)
    if pattern == 'rainbow':
        return _rainbow_cells(count, bri)
    if pattern == 'rows':
        cells = _rainbow_cells(height if height > 1 else count, bri)
        return _expand_control_cells_to_zones(cells, count, width, height)
    if pattern == 'columns':
        cells = _rainbow_cells(width if width > 1 else count, bri)
        return _expand_control_cells_to_zones(cells, count, width, height)
    if pattern == 'chase':
        on_r, on_g, on_b = chase_rgb or (1.0, 1.0, 1.0)
        cmds = [(0.0, 0.0, 0.0, DEFAULT_KELVIN, bri) for _ in range(count)]
        cmds[chase_index % count] = (on_r, on_g, on_b, DEFAULT_KELVIN, bri)
        return cmds
    if pattern.isdigit():
        groups = max(1, min(int(pattern), count))
        cells = _rainbow_cells(groups, bri)
        return _expand_control_cells_to_zones(cells, count, width, height)
    return []


def _light_zone_fields(light: Optional[LifxLight], mapping: Optional[Dict] = None) -> Dict:
    if light is not None:
        layout = light.effective_layout
        count, width, height = _geometry_for(light, mapping)
        return {
            'zone_capable': layout in ('linear', 'matrix'),
            'zone_count': count,
            'zone_layout': layout,
            'matrix_width': width,
            'matrix_height': height,
        }
    mapping = mapping or {}
    count, width, height = _geometry_for(None, mapping)
    layout = mapping.get('zone_layout') or 'single'
    capable = bool(mapping.get('zone_capable')) or count > 1 or layout in ('linear', 'matrix')
    return {
        'zone_capable': capable,
        'zone_count': count,
        'zone_layout': layout,
        'matrix_width': width,
        'matrix_height': height,
    }


def _configured_light_row(lid: str, mapping: Dict, light: Optional[LifxLight]) -> Dict:
    if light:
        return {
            **_light_summary(light, lid),
            **_light_zone_fields(light, mapping),
            'label': _display_label(light, mapping, lid),
            'discovered': True,
            'supported_modes': _supported_modes_for(light, mapping),
            'mode_options': _mode_options_for(light, mapping),
            'pixel_test_patterns': _pixel_test_patterns(light, mapping),
            'mapping': mapping,
        }
    return {
        'id': lid,
        'label': _display_label(None, mapping, lid),
        'ip': mapping.get('ip') or '',
        'model': mapping.get('model') or 'Unknown',
        'discovered': False,
        'supported_modes': _supported_modes_for(None, mapping),
        'mode_options': _mode_options_for(None, mapping),
        'pixel_test_patterns': [],
        'mapping': mapping,
        **_light_zone_fields(None, mapping),
    }


# Cache: universe -> [(light_id, mapping), ...] — rebuilt when light_mappings change (hot DMX path skips full dict scan)
_dmx_mapping_cache_lock = threading.Lock()
_dmx_mapping_cache_dirty = True
_mappings_by_universe: Dict[int, List[Tuple[str, Dict]]] = {}


def invalidate_dmx_mapping_cache() -> None:
    """Call after any change to light_mappings."""
    global _dmx_mapping_cache_dirty
    with _dmx_mapping_cache_lock:
        _dmx_mapping_cache_dirty = True


def _rebuild_dmx_mapping_cache_if_dirty() -> None:
    global _mappings_by_universe, _dmx_mapping_cache_dirty
    with _dmx_mapping_cache_lock:
        if not _dmx_mapping_cache_dirty:
            return
        _dmx_mapping_cache_dirty = False
        idx: Dict[int, List[Tuple[str, Dict]]] = {}
        for lid, m in list(light_mappings.items()):
            u = m.get('universe')
            if u is None:
                continue
            try:
                ui = int(u)
            except (TypeError, ValueError):
                continue
            idx.setdefault(ui, []).append((lid, m))
        _mappings_by_universe = idx


# Store last sent values per light to implement change threshold
_last_sent_values: Dict[str, List[int]] = {}  # light_id -> list of channel values

# Performance optimization: coalesced batch — at most one pending command per light (latest wins)
_batch_commands_by_id: Dict[str, tuple] = {}
_batch_lock = threading.Lock()
_last_batch_time = 0.0
try:
    BATCH_INTERVAL = max(0.005, float(os.getenv('LIFX_BATCH_INTERVAL_MS', '20')) / 1000.0)
except ValueError:
    print("Warning: Invalid LIFX_BATCH_INTERVAL_MS; using default 20")
    BATCH_INTERVAL = 0.02

_batch_sender_thread: Optional[threading.Thread] = None
_batch_sender_stop: threading.Event = threading.Event()
_batch_sender_wake = threading.Event()
_batch_sender_start_lock = threading.Lock()

# Performance monitoring for multi-fixture setups
_perf_metrics = {
    'total_frames_processed': 0,
    'total_commands_sent': 0,
    'total_batches_sent': 0,
    'avg_batch_size': 0.0,
    'avg_frame_processing_time': 0.0,
    'peak_fixtures_per_frame': 0,
    'last_reset_time': time.time(),
    'frame_times': deque(maxlen=100),  # Last 100 frame times for rolling average
    'batch_sizes': deque(maxlen=50),   # Last 50 batch sizes
    'last_drain_duration_s': 0.0,
    'peak_drain_duration_s': 0.0,
    'drain_overrun_count': 0,
}
_perf_lock = threading.Lock()
_PERF_RATE_WINDOW_S = 5.0
_perf_rate_frames: deque = deque(maxlen=1000)
_perf_rate_commands: deque = deque(maxlen=1000)  # (timestamp, command_count)
_perf_rate_batches: deque = deque(maxlen=1000)


def _rolling_per_second(events: deque, now: float, counted: bool = False) -> float:
    """Rate over a short rolling window (not lifetime totals / process uptime)."""
    cutoff = now - _PERF_RATE_WINDOW_S
    if counted:
        total = 0
        for t, n in events:
            if t >= cutoff:
                total += n
        count = total
    else:
        count = 0
        for t in events:
            if t >= cutoff:
                count += 1
    elapsed = min(_PERF_RATE_WINDOW_S, now - _perf_metrics['last_reset_time'])
    if elapsed <= 0:
        return 0.0
    return count / elapsed

def _lifx_send_one_batch():
    """Drain every pending LIFX command if the send interval has elapsed."""
    global _batch_commands_by_id, _last_batch_time, _perf_metrics
    
    if not lifx_client:
        return
    
    current_time = time.time()
    if current_time - _last_batch_time < BATCH_INTERVAL:
        return
    
    with _batch_lock:
        if not _batch_commands_by_id:
            return
        batch = list(_batch_commands_by_id.values())
        _batch_commands_by_id.clear()
    
    if not batch:
        return
    
    batch_size = len(batch)
    drain_start = time.time()
    
    for item in batch:
        try:
            if item[0] == 'zones':
                _kind, light, zone_cmds, duration_ms = item
                lifx_client.send_zones_now(
                    light.target, light.ip, zone_cmds, duration_ms=duration_ms
                )
            else:
                _kind, light, r, g, b, kelvin, duration_ms, brightness = item
                lifx_client.send_color_now(
                    light.target, light.ip, r, g, b,
                    kelvin=kelvin, duration_ms=duration_ms, brightness=brightness,
                )
        except Exception as e:
            light = item[1] if len(item) > 1 else None
            label = getattr(light, 'label', None) or getattr(light, 'ip', None) or 'unknown'
            msg = f"Error in batch send for {label}: {e}"
            if dmx_logger:
                dmx_logger.warning(msg)
            else:
                print(msg)
    
    drain_duration = time.time() - drain_start
    _last_batch_time = current_time
    
    with _perf_lock:
        _perf_metrics['total_batches_sent'] += 1
        _perf_metrics['total_commands_sent'] += batch_size
        _perf_metrics['batch_sizes'].append(batch_size)
        _perf_rate_batches.append(current_time)
        _perf_rate_commands.append((current_time, batch_size))
        if len(_perf_metrics['batch_sizes']) > 0:
            _perf_metrics['avg_batch_size'] = sum(_perf_metrics['batch_sizes']) / len(_perf_metrics['batch_sizes'])
        _perf_metrics['last_drain_duration_s'] = drain_duration
        if drain_duration > _perf_metrics['peak_drain_duration_s']:
            _perf_metrics['peak_drain_duration_s'] = drain_duration
        if drain_duration > BATCH_INTERVAL:
            _perf_metrics['drain_overrun_count'] += 1


def _lifx_batch_sender_worker(stop_event: threading.Event):
    """Single daemon thread: wake on new DMX commands or interval; drain queue at BATCH_INTERVAL without spawning timers."""
    global running, lifx_client, _batch_sender_wake
    
    while True:
        if stop_event.is_set():
            break
        with _batch_lock:
            pending = len(_batch_commands_by_id) > 0
        if pending:
            wait_for = max(0.0, BATCH_INTERVAL - (time.time() - _last_batch_time))
            timeout = min(max(wait_for, 0.0005), BATCH_INTERVAL)
        else:
            timeout = 0.1
        _batch_sender_wake.wait(timeout=timeout)
        if stop_event.is_set():
            break
        _batch_sender_wake.clear()
        while not stop_event.is_set():
            if not lifx_client:
                break
            with _batch_lock:
                if not _batch_commands_by_id:
                    break
            if time.time() - _last_batch_time < BATCH_INTERVAL:
                break
            _lifx_send_one_batch()


def _start_lifx_batch_sender_thread():
    """Start background sender if not already running (call when DMX processing starts)."""
    global _batch_sender_thread, _batch_sender_stop, _batch_sender_wake
    
    with _batch_sender_start_lock:
        if _batch_sender_thread is not None and _batch_sender_thread.is_alive():
            return
        stop_event = threading.Event()
        _batch_sender_stop = stop_event
        _batch_sender_wake.clear()
        t = threading.Thread(
            target=_lifx_batch_sender_worker,
            args=(stop_event,),
            daemon=True,
            name='lifx_batch_sender',
        )
        _batch_sender_thread = t
        t.start()


def _stop_lifx_batch_sender_thread():
    """Stop background sender (call when DMX processing stops)."""
    global _batch_sender_thread
    
    with _batch_sender_start_lock:
        t = _batch_sender_thread
        if t is None:
            return
        with _batch_lock:
            _batch_commands_by_id.clear()
        _batch_sender_stop.set()
        _batch_sender_wake.set()
        _batch_sender_thread = None
    t.join(timeout=1.0)


def process_dmx_data(dmx_data: list, universe: int):
    """Process incoming DMX data and update lights"""
    global lifx_client, light_mappings, _last_sent_values, _dmx_frame_counter
    
    if not lifx_client or not running:
        return
    
    _rebuild_dmx_mapping_cache_if_dirty()
    mapping_entries = _mappings_by_universe.get(universe)
    if not mapping_entries:
        return
    
    # Performance logging: track start time and frame counter
    process_start = None
    should_log_frame = False
    if enable_perf_logging:
        process_start = time.time()
        _dmx_frame_counter += 1
        should_log_frame = (_dmx_frame_counter % PERF_LOG_SAMPLE_RATE == 0)
    
    if dmx_logger and enable_dmx_log:
        dmx_logger.info(f"DMX received: universe={universe}, data_len={len(dmx_data)}")
    
    # Single lock, one pass (equivalent to former get_lights() + dict snapshot)
    with lifx_client.lock:
        lights_by_id = {light_id(L): L for L in lifx_client.lights.values()}
    
    frame_batch: List[tuple] = []
    
    for mapped_light_id, mapping in mapping_entries:
        # Find the light for this mapping
        light = lights_by_id.get(mapped_light_id)
        if not light:
            continue  # Light not currently discovered
        
        start_channel = mapping.get('start_channel', 0) - 1  # Convert to 0-based
        brightness = mapping.get('brightness', MAX_BRIGHTNESS)
        channel_mode = _normalize_channel_mode(mapping.get('channel_mode'))
        per_zone = _channels_per_cell(channel_mode)
        zone_count = _mapping_zone_count(mapping, light)
        channels_needed = per_zone * zone_count
        
        if start_channel < 0 or start_channel + channels_needed > len(dmx_data):
            continue
        
        # Extract channel values
        channel_values = dmx_data[start_channel:start_channel + channels_needed]
        
        last_values = _last_sent_values.get(mapped_light_id)
        if last_values is not None:
            if zone_count > 1:
                unchanged = list(channel_values) == last_values
            else:
                unchanged = not _dmx_values_changed(channel_mode, channel_values, last_values)
            if unchanged:
                if dmx_logger and enable_dmx_log:
                    dmx_logger.debug(f"  SKIP {light.label}: values={channel_values}")
                continue
        
        _last_sent_values[mapped_light_id] = list(channel_values)
        
        if zone_count > 1 or _mode_is_pixel(channel_mode):
            zone_cmds = []
            duration_ms = FADE_DURATION_MS
            for z in range(max(1, zone_count)):
                slice_start = z * per_zone
                zone_values = channel_values[slice_start:slice_start + per_zone]
                cmd_data = _dmx_decode_to_cmd(channel_mode, zone_values, brightness)
                if not cmd_data:
                    zone_cmds = []
                    break
                r, g, b, kelvin, duration_ms, zone_bri = cmd_data
                zone_cmds.append((r, g, b, kelvin, zone_bri))
            if zone_cmds:
                physical, width, height = _geometry_for(light, mapping)
                expanded = _expand_control_cells_to_zones(zone_cmds, max(physical, 1), width, height)
                if expanded:
                    frame_batch.append(('zones', light, expanded, duration_ms))
            continue
        
        cmd_data = _dmx_decode_to_cmd(channel_mode, channel_values, brightness)
        if cmd_data:
            if dmx_logger and enable_dmx_log:
                r, g, b = cmd_data[0], cmd_data[1], cmd_data[2]
                rgb_int = (int(r * 255), int(g * 255), int(b * 255))
                dmx_logger.info(
                    f"  → {light.label}: RGB=({rgb_int[0]},{rgb_int[1]},{rgb_int[2]}), "
                    f"DMX={channel_values}, brightness={cmd_data[5]:.2f}, fade={FADE_DURATION_MS}ms"
                )
            frame_batch.append(('color', light, *cmd_data))
    
    if frame_batch:
        with _batch_lock:
            for cmd in frame_batch:
                lid = light_id(cmd[1])
                _batch_commands_by_id[lid] = cmd
        _start_lifx_batch_sender_thread()
        _batch_sender_wake.set()
    
    # Update performance metrics
    with _perf_lock:
        now = time.time()
        _perf_metrics['total_frames_processed'] += 1
        _perf_rate_frames.append(now)
        _perf_metrics['peak_fixtures_per_frame'] = max(
            _perf_metrics['peak_fixtures_per_frame'], len(frame_batch)
        )
        
        if process_start is not None:
            process_duration = now - process_start
            _perf_metrics['frame_times'].append(process_duration)
            
            # Update rolling average
            if len(_perf_metrics['frame_times']) > 0:
                _perf_metrics['avg_frame_processing_time'] = (
                    sum(_perf_metrics['frame_times']) / len(_perf_metrics['frame_times'])
                ) * 1000  # Convert to ms
    
    # Log total processing time for this DMX frame (only if performance logging enabled)
    if enable_perf_logging and process_start is not None:
        process_duration = (time.time() - process_start) * 1000
        # Log if threshold exceeded OR if this is a sampled frame
        if process_duration > PERF_PROCESS_THRESHOLD_MS:
            dmx_logger.warning(f"SLOW process: {process_duration:.1f}ms total for universe {universe}")
        elif should_log_frame:
            dmx_logger.info(f"Frame process: {process_duration:.1f}ms total for universe {universe}")


def dmx_worker():
    """Background thread for DMX processing"""
    global dmx_receiver, running
    
    if not dmx_receiver:
        return
    
    # Start the receiver first
    dmx_receiver.start()
    
    # Get all unique universes from mappings
    universes = set()
    for mapping in light_mappings.values():
        universe = mapping.get('universe')
        if universe is not None:
            universes.add(universe)
    
    # Set up listeners for each universe
    for universe in universes:
        try:
            dmx_receiver.listen_to_universe(universe, process_dmx_data)
            print(f"Listening to universe {universe}")
        except Exception as e:
            print(f"Error setting up listener for universe {universe}: {e}")
    
    while running:
        time.sleep(0.1)


def _restart_dmx_if_running():
    """Restart DMX worker if it's currently running (to pick up mapping changes)"""
    global running, dmx_receiver, dmx_thread, dmx_lock
    
    # Acquire lock and check preconditions inside lock to avoid TOCTOU issues
    with dmx_lock:
        # Check preconditions inside lock
        if not running:
            return  # Not running, nothing to restart
        
        if not dmx_receiver:
            return  # No receiver, nothing to restart
        
        # Store references for operations outside lock
        old_receiver = dmx_receiver
        old_thread = dmx_thread
        
        # Update state atomically
        running = False
        dmx_receiver = None
        dmx_thread = None
    
    # Perform potentially blocking operations outside lock
    try:
        # Stop the old receiver (may block)
        if old_receiver:
            try:
                old_receiver.stop()
            except Exception as e:
                print(f"Error stopping DMX receiver during restart: {e}")
                raise
            old_receiver.reset_stats()
        
        # Wait for thread to finish (may block)
        if old_thread and old_thread.is_alive():
            old_thread.join(timeout=1.0)
            # Check if thread is still alive after timeout
            if old_thread.is_alive():
                print("Warning: DMX worker thread did not finish within timeout, continuing anyway")
        
        # Close old receiver (may block)
        if old_receiver:
            try:
                old_receiver.close()
            except Exception as e:
                print(f"Warning: Error closing DMX receiver: {e}")
        
        # Prepare new receiver (may block)
        sacn_bind_ip = _sacn_bind_ip()
        new_receiver = DMXReceiver(bind_ip=sacn_bind_ip)
        
        # Create new thread
        new_thread = threading.Thread(target=dmx_worker, daemon=True)
        
        # Acquire lock again for final state mutations
        with dmx_lock:
            # Double-check that no other thread has interfered (e.g., called stop_dmx)
            # If dmx_receiver is not None, another thread may have started/stopped it
            if dmx_receiver is not None:
                # Another thread has modified state, clean up and abort
                try:
                    new_receiver.close()
                except Exception as e:
                    print(f"Warning: Error closing new receiver after state conflict: {e}")
                return
            
            # Update state atomically
            dmx_receiver = new_receiver
            running = True
            dmx_thread = new_thread
        
        # Start thread outside lock (may block briefly)
        new_thread.start()
        print("DMX worker restarted successfully")
    except Exception as e:
        print(f"Error restarting DMX worker: {e}")
        import traceback
        traceback.print_exc()
        # Ensure state is consistent on error
        with dmx_lock:
            running = False
            if dmx_receiver:
                dmx_receiver = None
            if dmx_thread:
                dmx_thread = None


# =========================
# WEB UI ROUTES
# =========================

@app.route('/')
def index():
    """Main web interface"""
    return render_template('index.html', version=VERSION)


@app.route('/api/lights', methods=['GET'])
def list_lights():
    """Return discovered lights and configured mappings for the web UI."""
    global lifx_client, light_mappings
    
    discovered_by_id: Dict[str, LifxLight] = {}
    lights_list: List[LifxLight] = []
    if lifx_client:
        lights_list = lifx_client.get_lights()
        for light in lights_list:
            discovered_by_id[light_id(light)] = light
        with lifx_client.lock:
            for _target, light in lifx_client.lights.items():
                lid = light_id(light)
                if lid not in discovered_by_id and lid in light_mappings:
                    discovered_by_id[lid] = light
    
    all_configured = [
        _configured_light_row(lid, mapping, discovered_by_id.get(lid))
        for lid, mapping in light_mappings.items()
    ]
    unconfigured = [
        {
            **_light_summary(light),
            'supported_modes': _supported_modes_for(light),
            'mode_options': _mode_options_for(light),
            'pixel_test_patterns': _pixel_test_patterns(light),
        }
        for light in lights_list
        if light_id(light) not in light_mappings
    ]
    lights_summary = [_light_summary(light) for light in lights_list]
    manual_only = [x for x in all_configured if str(x['id']).startswith('manual_')]
    
    return jsonify({
        'success': True,
        'lights': lights_summary,
        'configured_lights': all_configured,
        'unconfigured_lights': unconfigured,
        'manual_lights': manual_only,
        'all_configured_lights': all_configured,
    })


# Cache for network interfaces to avoid blocking calls
_interfaces_cache = None
_interfaces_cache_time = 0
_interfaces_cache_ttl = 30  # Cache for 30 seconds
_interfaces_refresh_lock = threading.Lock()
_interfaces_refresh_in_flight = False


def _refresh_interfaces_cache(empty_on_error: bool = False) -> None:
    global _interfaces_cache, _interfaces_cache_time, _interfaces_refresh_in_flight
    try:
        _interfaces_cache = get_network_interfaces()
    except Exception as e:
        print(f"Error fetching network interfaces: {e}")
        if empty_on_error:
            _interfaces_cache = []
    finally:
        _interfaces_cache_time = time.time()
        with _interfaces_refresh_lock:
            _interfaces_refresh_in_flight = False


def _interfaces_payload():
    return {
        'success': True,
        'interfaces': _interfaces_cache if _interfaces_cache is not None else [],
        'lifx_interface': lifx_interface,
        'sacn_interface': sacn_interface,
    }


@app.route('/api/interfaces', methods=['GET'])
def get_interfaces():
    """Get list of available network interfaces (cached; cold start loads synchronously)."""
    global _interfaces_refresh_in_flight
    current_time = time.time()
    if _interfaces_cache is not None and (current_time - _interfaces_cache_time) < _interfaces_cache_ttl:
        return jsonify(_interfaces_payload())

    if _interfaces_cache is None:
        should_refresh = False
        with _interfaces_refresh_lock:
            if not _interfaces_refresh_in_flight:
                _interfaces_refresh_in_flight = True
                should_refresh = True
        if should_refresh:
            _refresh_interfaces_cache(empty_on_error=True)
    elif (current_time - _interfaces_cache_time) >= _interfaces_cache_ttl:
        with _interfaces_refresh_lock:
            if not _interfaces_refresh_in_flight:
                _interfaces_refresh_in_flight = True
                try:
                    threading.Thread(target=_refresh_interfaces_cache, daemon=True).start()
                except Exception:
                    _interfaces_refresh_in_flight = False
                    raise

    return jsonify(_interfaces_payload())


@app.route('/api/settings/interfaces', methods=['POST'])
def set_interfaces():
    """Set the network interfaces to use"""
    global lifx_interface, sacn_interface
    
    data = request.json
    lifx_ip = data.get('lifx_interface')
    sacn_ip = data.get('sacn_interface')
    
    if lifx_ip is None or sacn_ip is None:
        return jsonify({'success': False, 'error': 'lifx_interface and sacn_interface required'}), 400
    
    lifx_interface = lifx_ip
    sacn_interface = sacn_ip
    save_config()
    
    return jsonify({
        'success': True,
        'lifx_interface': lifx_interface,
        'sacn_interface': sacn_interface
    })


@app.route('/api/settings/interfaces/apply', methods=['POST'])
def apply_interfaces():
    """Apply the network interface settings (recreate clients)"""
    global lifx_client, dmx_receiver
    
    lifx_bind_ip = _normalize_interface_ip(lifx_interface)
    sacn_bind_ip = _sacn_bind_ip()
    
    # Recreate LIFX client with new interface if it exists
    if lifx_client:
        lifx_client.close()
        try:
            lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
        except OSError as e:
            lifx_client = None
            return jsonify({"success": False, "error": str(e)}), 500
    
    # Recreate DMX receiver with new interface if it exists
    if dmx_receiver:
        dmx_receiver.close()
        dmx_receiver = DMXReceiver(bind_ip=sacn_bind_ip)
    
    return jsonify({
        'success': True,
        'message': 'Network interfaces applied successfully'
    })


@app.route('/api/lights/discover', methods=['POST'])
def discover_lights():
    """Discover LIFX lights on the network"""
    global lifx_client
    
    lifx_bind_ip = _normalize_interface_ip(lifx_interface)
    
    if not lifx_client:
        try:
            lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
        except OSError as e:
            return jsonify({"success": False, "error": str(e)}), 500
    elif getattr(lifx_client, "requested_bind_ip", None) != lifx_bind_ip:
        try:
            lifx_client.close()
            lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
        except OSError as e:
            lifx_client = None
            return jsonify({"success": False, "error": str(e)}), 500
    
    try:
        lights = lifx_client.discover_lights(timeout=5.0)
        lights_data = [
            {
                **_light_summary(light),
                'target': light.target.hex(),
                'product_id': light.product,
                'supported_modes': _supported_modes_for(light),
                'mode_options': _mode_options_for(light),
            }
            for light in lights
        ]
        return jsonify({'success': True, 'lights': lights_data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mappings', methods=['GET'])
def get_mappings():
    """Get current light mappings (non-blocking)"""
    # Create a copy to avoid any potential race conditions
    # Since light_mappings is a dict, we'll create a shallow copy
    try:
        mappings_copy = dict(light_mappings)
    except Exception:
        # If copy fails, return empty dict
        mappings_copy = {}
    return jsonify({'success': True, 'mappings': mappings_copy})


@app.route('/api/config/reload', methods=['POST'])
def reload_config():
    """Reload configuration from file"""
    global light_mappings, lifx_interface, sacn_interface, dmx_receiver, dmx_thread, running
    
    try:
        # Load config from file
        load_config()
        
        # Restart DMX worker if running to pick up mapping changes
        if running:
            _restart_dmx_if_running()
        
        return jsonify({
            'success': True,
            'message': 'Configuration reloaded successfully',
            'mappings_count': len(light_mappings)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mappings', methods=['POST'])
def update_mapping():
    """Update mapping for a light"""
    global light_mappings, dmx_receiver, dmx_thread, lifx_client
    
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Request must be JSON'}), 400
        
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'Request body is required'}), 400
        
        mapped_light_id = data.get('light_id')
        
        if not mapped_light_id:
            return jsonify({'success': False, 'error': 'light_id required'}), 400
        
        # Get existing mapping to preserve label/model/ip if light isn't currently discovered
        existing_mapping = light_mappings.get(mapped_light_id, {})
        
        light_label = existing_mapping.get('label')
        light_model = existing_mapping.get('model')
        light_ip = existing_mapping.get('ip')
        matched_light = None
        
        if lifx_client:
            lights = lifx_client.get_lights()
            for light in lights:
                if light_id(light) == mapped_light_id:
                    light_model = light.model_name or light_model
                    light_ip = light.ip or light_ip
                    matched_light = light
                    if not light_label:
                        light_label = light.label
                    break

        requested_label = data.get('label')
        if isinstance(requested_label, str):
            requested_label = requested_label.strip()[:MAX_LIGHT_LABEL_LEN]
            if requested_label:
                light_label = requested_label
        if not light_label and matched_light is not None:
            light_label = matched_light.label
        if matched_light is not None and light_label:
            matched_light.label = light_label
        
        # Get values from request
        universe = data.get('universe')
        start_channel = data.get('start_channel')
        brightness = data.get('brightness')
        channel_mode = data.get('channel_mode')
        
        # Validate required fields - check for None, empty string, or 0
        if universe is None or universe == '' or universe == 0:
            return jsonify({'success': False, 'error': 'universe is required and must be greater than 0'}), 400
        if start_channel is None or start_channel == '' or start_channel == 0:
            return jsonify({'success': False, 'error': 'start_channel is required and must be greater than 0'}), 400
        
        # Convert values with error handling
        try:
            universe_int = int(universe)
            start_channel_int = int(start_channel)
        except (ValueError, TypeError) as e:
            return jsonify({'success': False, 'error': f'Invalid universe or start_channel: {str(e)}'}), 400
        
        try:
            brightness_float = float(brightness) if brightness is not None else existing_mapping.get('brightness', MAX_BRIGHTNESS)
        except (ValueError, TypeError):
            brightness_float = existing_mapping.get('brightness', MAX_BRIGHTNESS)
        brightness_float = clamp01(brightness_float)
        
        # Build mapping with explicit values from request
        mode_str = channel_mode or existing_mapping.get('channel_mode')
        if mode_str not in CHANNEL_MODE_SPEC and _parse_pixel_mode(mode_str) is None:
            mode_str = existing_mapping.get('channel_mode')
        mode_str = _normalize_channel_mode(mode_str)
        supported_modes = _supported_modes_for(matched_light, existing_mapping)
        if mode_str not in supported_modes:
            existing_mode = _normalize_channel_mode(existing_mapping.get('channel_mode'))
            if existing_mode in supported_modes:
                mode_str = existing_mode
            else:
                mode_str = next((m for m in supported_modes if not _mode_is_pixel(m)), None)
                if mode_str is None:
                    mode_str = supported_modes[0] if supported_modes else 'RGB (8bit)'
        zone_fields = _light_zone_fields(matched_light, existing_mapping)
        mapping = {
            'universe': universe_int,
            'start_channel': start_channel_int,
            'brightness': brightness_float,
            'channel_mode': mode_str,
            'label': light_label,  # Store label for display when not discovered
            'model': light_model,  # Store model for display when not discovered
            'ip': light_ip,  # Store IP for auto-discovery
            'zone_capable': zone_fields['zone_capable'],
            'zone_count': zone_fields['zone_count'],
            'zone_layout': zone_fields['zone_layout'],
            'matrix_width': zone_fields['matrix_width'],
            'matrix_height': zone_fields['matrix_height'],
        }
        
        light_mappings[mapped_light_id] = mapping
        _last_sent_values.pop(mapped_light_id, None)
        invalidate_dmx_mapping_cache()
        save_config()
        
        # Restart DMX worker if running
        _restart_dmx_if_running()
        
        return jsonify({'success': True, 'mapping': mapping})
    
    except Exception as e:
        return jsonify({'success': False, 'error': f'Server error: {str(e)}'}), 500


@app.route('/api/mappings/<light_id>', methods=['DELETE'])
def delete_mapping(light_id):
    """Delete mapping for a light"""
    global light_mappings
    
    if light_id in light_mappings:
        del light_mappings[light_id]
        _last_sent_values.pop(light_id, None)
        invalidate_dmx_mapping_cache()
        save_config()
        
        # Restart DMX worker if running
        _restart_dmx_if_running()
        
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Mapping not found'}), 404


@app.route('/api/lights/manual', methods=['POST'])
def add_manual_light():
    """Manually add a light by IP address"""
    global lifx_client, light_mappings
    
    data = request.json
    ip = data.get('ip', '').strip()
    label = data.get('label', '').strip()
    
    if not ip:
        return jsonify({'success': False, 'error': 'IP address is required'}), 400
    
    # Validate IP format (basic check)
    try:
        socket.inet_aton(ip)
    except socket.error:
        return jsonify({'success': False, 'error': 'Invalid IP address format'}), 400
    
    # Check if this IP is already mapped
    for existing_id, mapping in light_mappings.items():
        if mapping.get('ip') == ip:
            return jsonify({'success': False, 'error': f'Light with IP {ip} is already configured'}), 400
    
    # Try to probe the light to get its target MAC address
    light = None
    if lifx_client:
        try:
            light = lifx_client.probe_light_by_ip(ip, timeout=3.0)
            if light:
                # Request label and version info
                lifx_client._request_label(light)
                lifx_client._request_version(light)
                time.sleep(0.5)  # Wait for responses
        except Exception as e:
            print(f"Error probing light at {ip}: {e}")
    
    # Generate light_id
    if light and light.target:
        # Use the actual target MAC address
        lid = light.target.hex()
        light_label = light.label or label or f"Light {ip}"
        light_model = light.model_name or 'Unknown Model'
        zone_fields = _light_zone_fields(light)
        supported_modes = _supported_modes_for(light)
    else:
        # Create a placeholder ID based on IP (will be updated when discovered)
        # Use a hash of the IP to create a consistent ID
        ip_hash = hashlib.md5(ip.encode(), usedforsecurity=False).hexdigest()[:16]
        lid = f"manual_{ip_hash}"
        light_label = label or f"Light {ip}"
        light_model = 'Not discovered'
        supported_modes = STANDARD_CHANNEL_MODES
        zone_fields = None
    
    # Check if mapping already exists for this light_id
    if lid in light_mappings:
        return jsonify({'success': False, 'error': 'This light is already configured'}), 400
    
    # Create a basic mapping entry (user will need to configure universe/channel separately)
    mapping = {
        'ip': ip,
        'label': light_label,
        'model': light_model,
        'brightness': MAX_BRIGHTNESS,
        'channel_mode': 'RGB (8bit)',
        'universe': None,  # User needs to configure this
        'start_channel': None  # User needs to configure this
    }
    if zone_fields is not None:
        mapping.update({
            'zone_capable': zone_fields['zone_capable'],
            'zone_count': zone_fields['zone_count'],
            'zone_layout': zone_fields['zone_layout'],
            'matrix_width': zone_fields['matrix_width'],
            'matrix_height': zone_fields['matrix_height'],
        })
    
    light_mappings[lid] = mapping
    invalidate_dmx_mapping_cache()
    save_config()
    
    # If light was discovered, add it to the client's lights list
    if light and lifx_client:
        lifx_client.lights[lid] = light
    
    return jsonify({
        'success': True,
        'light_id': lid,
        'light': {
            'id': lid,
            'label': light_label,
            'ip': ip,
            'model': light_model,
            'supported_modes': supported_modes,
            'discovered': light is not None
        }
    })


@app.route('/api/control/start', methods=['POST'])
def start_dmx():
    """Start DMX processing"""
    global running, dmx_receiver, dmx_thread, lifx_client, dmx_lock
    
    with dmx_lock:
        if running:
            return jsonify({'success': False, 'error': 'Already running'}), 400
        
        if not lifx_client:
            return jsonify({'success': False, 'error': 'No lights discovered'}), 400
    
    try:
        sacn_bind_ip = _sacn_bind_ip()
        
        # Close existing receiver if it exists (outside lock to avoid blocking)
        if dmx_receiver:
            try:
                dmx_receiver.close()
            except Exception as e:
                print(f"Warning: Error closing existing DMX receiver: {e}")
            dmx_receiver = None
        
        # Create new receiver
        dmx_receiver = DMXReceiver(bind_ip=sacn_bind_ip)
        
        with dmx_lock:
            running = True
            dmx_thread = threading.Thread(target=dmx_worker, daemon=True)
            dmx_thread.start()
        
        _start_lifx_batch_sender_thread()
        
        return jsonify({'success': True})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/control/stop', methods=['POST'])
def stop_dmx():
    """Stop DMX processing"""
    global running, dmx_receiver, dmx_lock
    
    with dmx_lock:
        running = False
    
    _stop_lifx_batch_sender_thread()
    
    if dmx_receiver:
        dmx_receiver.stop()
        dmx_receiver.reset_stats()
    
    return jsonify({'success': True})


@app.route('/api/control/status', methods=['GET'])
def get_status():
    """Get current status (non-blocking)"""
    dmx_stats = {}
    if dmx_receiver:
        try:
            full = dmx_receiver.get_stats_nonblocking()
            if full is not None:
                dmx_stats = {
                    'packets_received': full['packets_received'],
                    'last_packet_time': full['last_packet_time'],
                    'active_universes': full['active_universes'],
                    'packets_per_universe': full['packets_per_universe'],
                    'receiving': full['receiving'],
                }
        except Exception:
            dmx_stats = {}
    
    # Count discovered lights without blocking (try to get count, but don't wait for lock)
    discovered_count = 0
    if lifx_client:
        try:
            # Try to get count without blocking - use a timeout or just read the dict size
            # Access the lights dict directly with a quick lock check
            if lifx_client.lock.acquire(blocking=False):
                try:
                    discovered_count = len(lifx_client.lights)
                finally:
                    lifx_client.lock.release()
            else:
                # Lock is held, use cached value or skip
                discovered_count = 0  # Will be updated on next successful call
        except Exception:
            # If anything fails, just return 0
            discovered_count = 0
    
    # Get mappings count safely
    try:
        mappings_count = len(light_mappings)
    except Exception:
        mappings_count = 0
    
    # Get running status safely (it's a simple bool, but be safe)
    try:
        running_status = running
    except Exception:
        running_status = False
    
    # Add performance metrics to status
    with _perf_lock:
        now = time.time()
        perf_copy = dict(_perf_metrics)
        perf_copy['uptime_seconds'] = now - perf_copy['last_reset_time']
        perf_copy['commands_per_second'] = _rolling_per_second(_perf_rate_commands, now, counted=True)
        perf_copy['frames_per_second'] = _rolling_per_second(_perf_rate_frames, now)
        
        # Convert deque objects to lists for JSON serialization
        perf_copy['frame_times'] = list(perf_copy['frame_times'])
        perf_copy['batch_sizes'] = list(perf_copy['batch_sizes'])
    
    return jsonify({
        'success': True,
        'running': running_status,
        'lights_count': discovered_count,
        'mappings_count': mappings_count,
        'dmx_stats': dmx_stats,
        'performance_metrics': perf_copy
    })


def _light_from_id(requested_light_id: str) -> Optional[LifxLight]:
    if not lifx_client:
        return None
    for candidate in lifx_client.get_lights():
        if light_id(candidate) == requested_light_id:
            return candidate
    with lifx_client.lock:
        for candidate in lifx_client.lights.values():
            if light_id(candidate) == requested_light_id:
                return candidate
    return None


@app.route('/api/lights/test-rgb', methods=['POST'])
def test_rgb():
    """Test RGB values directly on a light (DMX-less testing)"""
    global lifx_client
    
    if not lifx_client:
        return jsonify({'success': False, 'error': 'LIFX client not initialized'}), 400
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Request body is required'}), 400
    
    requested_light_id = data.get('light_id')
    r = data.get('r', 0)
    g = data.get('g', 0)
    b = data.get('b', 0)
    brightness = data.get('brightness', 1.0)
    fade_ms = data.get('fade_ms', FADE_DURATION_MS)
    
    if not requested_light_id:
        return jsonify({'success': False, 'error': 'light_id is required'}), 400
    
    # Validate RGB values (0-255)
    if not (0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255):
        return jsonify({'success': False, 'error': 'RGB values must be 0-255'}), 400
    
    # Validate brightness (0.0-1.0)
    if not (0.0 <= brightness <= 1.0):
        return jsonify({'success': False, 'error': 'Brightness must be 0.0-1.0'}), 400
    
    fade_ms = _coerce_fade_ms(fade_ms)
    if fade_ms is None:
        return jsonify({'success': False, 'error': 'fade_ms must be an integer from 0 to 4294967295'}), 400
    
    light = _light_from_id(requested_light_id)
    if not light:
        return jsonify({'success': False, 'error': 'Light not found'}), 404
    
    # Convert RGB from 0-255 to 0.0-1.0
    r_norm = r / 255.0
    g_norm = g / 255.0
    b_norm = b / 255.0
    light_label = light.label
    
    try:
        cmd = (
            'color',
            light,
            r_norm, g_norm, b_norm,
            DEFAULT_KELVIN,
            fade_ms,
            brightness,
        )
        with _batch_lock:
            _batch_commands_by_id[light_id(light)] = cmd
        _start_lifx_batch_sender_thread()
        _batch_sender_wake.set()
    except Exception as submit_error:
        print(f"Error submitting set_rgb: {submit_error}")
        return jsonify({
            'success': False,
            'error': str(submit_error),
            'light_label': light_label,
        }), 500

    return jsonify({
        'success': True,
        'message': f'Sending RGB({r},{g},{b}) to {light_label}',
        'light_label': light_label
    })


@app.route('/api/lights/test-pixels', methods=['POST'])
def test_pixels():
    """Send a per-pixel test pattern to a SuperColour / matrix / strip fixture."""
    global lifx_client

    if not lifx_client:
        return jsonify({'success': False, 'error': 'LIFX client not initialized'}), 400

    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Request body is required'}), 400

    requested_light_id = data.get('light_id')
    pattern = str(data.get('pattern') or '')
    brightness = data.get('brightness', 1.0)
    fade_ms = data.get('fade_ms', 0 if pattern == 'chase' else FADE_DURATION_MS)
    r = data.get('r', 255)
    g = data.get('g', 255)
    b = data.get('b', 255)
    chase_index = data.get('index', 0)

    if not requested_light_id:
        return jsonify({'success': False, 'error': 'light_id is required'}), 400
    try:
        brightness = float(brightness)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Brightness must be 0.0-1.0'}), 400
    if not (0.0 <= brightness <= 1.0):
        return jsonify({'success': False, 'error': 'Brightness must be 0.0-1.0'}), 400
    fade_ms = _coerce_fade_ms(fade_ms)
    if fade_ms is None:
        return jsonify({'success': False, 'error': 'fade_ms must be an integer from 0 to 4294967295'}), 400
    try:
        chase_index = int(chase_index)
    except (TypeError, ValueError):
        chase_index = 0
    try:
        r_i, g_i, b_i = int(r), int(g), int(b)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'RGB values must be 0-255'}), 400
    if not (0 <= r_i <= 255 and 0 <= g_i <= 255 and 0 <= b_i <= 255):
        return jsonify({'success': False, 'error': 'RGB values must be 0-255'}), 400

    light = _light_from_id(requested_light_id)
    if not light:
        return jsonify({'success': False, 'error': 'Light not found'}), 404
    if not light.zone_capable:
        return jsonify({'success': False, 'error': 'Light does not support pixel mapping'}), 400

    cmds = _pixel_test_commands(
        light,
        pattern,
        brightness,
        chase_index=chase_index,
        chase_rgb=(r_i / 255.0, g_i / 255.0, b_i / 255.0),
        mapping=light_mappings.get(requested_light_id),
    )
    if not cmds:
        return jsonify({'success': False, 'error': f'Unknown pixel test pattern: {pattern}'}), 400

    try:
        future = lifx_client.executor.submit(
            lifx_client.send_zones_now,
            light.target,
            light.ip,
            cmds,
            fade_ms,
        )
        future.result(timeout=2.0)
    except Exception as send_error:
        print(f"Error sending pixel test: {send_error}")
        return jsonify({
            'success': False,
            'error': str(send_error),
            'light_label': light.label,
        }), 500

    return jsonify({
        'success': True,
        'light_label': light.label,
        'pattern': pattern,
        'zones': len(cmds),
    })


@app.route('/api/metrics', methods=['GET'])
def get_metrics():
    """Get detailed performance metrics for multi-fixture setups"""
    with _perf_lock:
        now = time.time()
        metrics = dict(_perf_metrics)
        
        # Calculate derived metrics
        uptime = now - metrics['last_reset_time']
        metrics['uptime_seconds'] = uptime
        metrics['uptime_formatted'] = f"{uptime/3600:.1f}h" if uptime > 3600 else f"{uptime/60:.1f}m"
        metrics['commands_per_second'] = _rolling_per_second(_perf_rate_commands, now, counted=True)
        metrics['frames_per_second'] = _rolling_per_second(_perf_rate_frames, now)
        metrics['batches_per_second'] = _rolling_per_second(_perf_rate_batches, now)
        
        # Drain utilization over the same rolling window as avg_batch_size (last 50 batches).
        recent_batches = metrics.get('batch_sizes') or ()
        peak_capacity = max(recent_batches) if recent_batches else 0
        commands_per_drain = metrics.get('avg_batch_size') or 0
        if peak_capacity > 0:
            efficiency = min(1.0, commands_per_drain / peak_capacity)
        else:
            efficiency = 0.0
        metrics['batch_efficiency'] = efficiency
        metrics['batch_utilization_percent'] = efficiency * 100.0

        # Add current system load
        metrics['current_queue_size'] = len(_batch_commands_by_id)
        
        # Convert deque objects to lists for JSON serialization
        metrics['frame_times'] = list(metrics['frame_times'])
        metrics['batch_sizes'] = list(metrics['batch_sizes'])
    
    return jsonify({
        'success': True,
        'metrics': metrics,
        'batch_config': {
            'batch_interval_ms': BATCH_INTERVAL * 1000,
            'fade_duration_ms': FADE_DURATION_MS,
            'max_workers': (
                lifx_client.executor_max_workers
                if lifx_client is not None
                else DEFAULT_BATCH_EXECUTOR_WORKERS
            )
        }
    })


def auto_discover_configured_lights():
    """Automatically discover configured lights by their saved IP addresses"""
    global lifx_client, light_mappings
    
    if not light_mappings:
        return
    
    # Initialize LIFX client if needed
    if not lifx_client:
        lifx_bind_ip = _normalize_interface_ip(lifx_interface)
        try:
            lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
        except OSError as e:
            print(f"Failed to create LIFX client (bind {lifx_bind_ip!r}): {e}")
            return
    
    print(f"Auto-discovering {len(light_mappings)} configured light(s)...")
    
    # Probe each configured light by its saved IP
    for light_id, mapping in light_mappings.items():
        saved_ip = mapping.get('ip')
        if saved_ip and saved_ip != 'Not discovered':
            try:
                print(f"  Probing {saved_ip} ({mapping.get('label', light_id[:8])})...")
                discovered_light = lifx_client.probe_light_by_ip(saved_ip, timeout=1.5)
                if discovered_light:
                    print(f"    [OK] Found: {discovered_light.label} ({discovered_light.model_name})")
                else:
                    print(f"    [ERROR] Not found at {saved_ip}")
            except Exception as e:
                print(f"    [ERROR] Error probing {saved_ip}: {e}")
    
    print("Auto-discovery complete.")


class _ErrorOnlyRequestHandler(WSGIRequestHandler):
    """Log HTTP requests only when the status is an error (4xx/5xx)."""

    def log_request(self, code='-', size='-'):
        try:
            status = int(str(code).split(None, 1)[0])
        except (TypeError, ValueError):
            super().log_request(code, size)
            return
        if status >= 400:
            super().log_request(code, size)


if __name__ == '__main__':
    load_config()
    
    print(f"sACN2LIFX v{VERSION} Server starting...")
    
    # Auto-discover configured lights on startup
    auto_discover_configured_lights()
    
    print("Open http://localhost:5001 in your browser")
    
    debug = os.getenv('FLASK_DEBUG', 'false').lower() in ('true', '1', 'yes')
    # Development server only. Bind defaults to loopback; the API has no auth.
    # On untrusted networks use a reverse proxy or a production WSGI server.
    host = os.getenv('FLASK_HOST', '127.0.0.1')
    app.run(host=host, port=5001, debug=debug, request_handler=_ErrorOnlyRequestHandler)

