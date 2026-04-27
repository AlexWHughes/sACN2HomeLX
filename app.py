#!/usr/bin/env python3
"""
sACN2LIFX - Control LIFX lights via sACN/E1.31
"""

VERSION = "270426 001"

import json
import os
import hashlib
import threading
import time
import colorsys
import socket
import logging
from typing import Optional, Dict, List, Tuple
from collections import deque
from flask import Flask, render_template, jsonify, request
from lifx_client import LifxLanClient, LifxLight
from dmx_receiver import DMXReceiver

# Set up logging for DMX to LIFX traffic (controlled via environment variables)
# ENABLE_DMX_LOG: Enable basic DMX frame logging (default: false)
# ENABLE_PERF_LOGGING: Enable performance/timing logging (default: false)
# PERF_LOG_SAMPLE_RATE: Log every N frames when sampling (default: 100)
# PERF_SEND_THRESHOLD_MS: Log sends slower than this (default: 5ms)
# PERF_PROCESS_THRESHOLD_MS: Log processing slower than this (default: 10ms)

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

# Frame counter for sampling (thread-local would be better, but simple counter works for single-threaded DMX processing)
_dmx_frame_counter = 0

try:
    import netifaces
    HAS_NETIFACES = True
except ImportError:
    HAS_NETIFACES = False

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
OVERRIDE_MAX_BRIGHT_TOTAL_RGB = 200 * 3
MAX_BRIGHT_OVERRIDE = 1.0
MAX_RGB_PER_COLOUR = 255
DEFAULT_KELVIN = 3500
MAX_HUE = 360  # Degrees
MAX_SATURATION = 100  # Percentage
MAX_INTENSITY = 100  # Percentage
FADE_DURATION_MS = 20  # Smooth fade duration for color transitions (milliseconds) - optimized for 40Hz sACN
VALUE_CHANGE_THRESHOLD = 1  # Only update if DMX value changed by this much (0-255) - lower for smoother transitions

# DMX channel modes (single source for UI + processing). MSB-first 16-bit = coarse then fine per parameter.
CHANNELS_FOR_MODE: Dict[str, int] = {
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
}

ALL_CHANNEL_MODES: List[str] = list(CHANNELS_FOR_MODE.keys())


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
    
    if HAS_NETIFACES:
        try:
            # Get all interfaces
            for iface in netifaces.interfaces():
                addrs = netifaces.ifaddresses(iface)
                if netifaces.AF_INET in addrs:
                    for addr_info in addrs[netifaces.AF_INET]:
                        ip = addr_info.get('addr')
                        if ip and not ip.startswith('127.'):  # Skip loopback
                            interfaces.append({
                                'name': iface,
                                'ip': ip,
                                'display': f"{iface} ({ip})"
                            })
        except Exception as e:
            print(f"Error getting network interfaces: {e}")
    
    # Fallback: try socket method if netifaces failed or not available
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


# Cache: universe -> [(light_id, mapping), ...] — rebuilt when light_mappings change (hot DMX path skips full dict scan)
_dmx_mapping_cache_dirty = True
_mappings_by_universe: Dict[int, List[Tuple[str, Dict]]] = {}


def invalidate_dmx_mapping_cache() -> None:
    """Call after any change to light_mappings."""
    global _dmx_mapping_cache_dirty
    _dmx_mapping_cache_dirty = True


def _rebuild_dmx_mapping_cache_if_dirty() -> None:
    global _mappings_by_universe, _dmx_mapping_cache_dirty
    if not _dmx_mapping_cache_dirty:
        return
    idx: Dict[int, List[Tuple[str, Dict]]] = {}
    for lid, m in light_mappings.items():
        u = m.get('universe')
        if u is None:
            continue
        try:
            ui = int(u)
        except (TypeError, ValueError):
            continue
        idx.setdefault(ui, []).append((lid, m))
    _mappings_by_universe = idx
    _dmx_mapping_cache_dirty = False


# Store last sent values per light to implement change threshold
_last_sent_values: Dict[str, List[int]] = {}  # light_id -> list of channel values

# Performance optimization: coalesced batch — at most one pending command per light (latest wins)
_batch_commands_by_id: Dict[str, Tuple[LifxLight, float, float, float, int, int, float]] = {}
_batch_lock = threading.Lock()
_last_batch_time = 0.0
BATCH_SIZE = 8  # Process up to 8 lights in parallel
BATCH_INTERVAL = 0.02  # 20ms between batches (50Hz)

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
    'batch_sizes': deque(maxlen=50)    # Last 50 batch sizes
}
_perf_lock = threading.Lock()

def _lifx_send_one_batch():
    """Send up to BATCH_SIZE queued LIFX commands if rate interval allows (worker calls this in a loop)."""
    global _batch_commands_by_id, _last_batch_time, _perf_metrics
    
    if not lifx_client:
        return
    
    current_time = time.time()
    if current_time - _last_batch_time < BATCH_INTERVAL:
        return
    
    with _batch_lock:
        if not _batch_commands_by_id:
            return
        keys = list(_batch_commands_by_id.keys())[:BATCH_SIZE]
        batch = [_batch_commands_by_id.pop(k) for k in keys]
    
    if not batch:
        return
    
    batch_size = len(batch)
    
    if hasattr(lifx_client, 'set_rgb_batch'):
        for light, r, g, b, kelvin, duration_ms, brightness in batch:
            lifx_client.set_rgb_batch(light.target, light.ip, r, g, b,
                                    kelvin=kelvin, duration_ms=duration_ms, brightness=brightness)
    else:
        futures = []
        for light, r, g, b, kelvin, duration_ms, brightness in batch:
            future = lifx_client.executor.submit(
                lifx_client.set_rgb, light.target, light.ip, r, g, b,
                kelvin=kelvin, duration_ms=duration_ms, brightness=brightness
            )
            futures.append(future)
        for future in futures:
            try:
                future.result(timeout=0.05)
            except Exception as e:
                if dmx_logger:
                    dmx_logger.warning(f"Error in batch send: {e}")
    
    _last_batch_time = current_time
    
    with _perf_lock:
        _perf_metrics['total_batches_sent'] += 1
        _perf_metrics['total_commands_sent'] += batch_size
        _perf_metrics['batch_sizes'].append(batch_size)
        _perf_metrics['peak_fixtures_per_frame'] = max(_perf_metrics['peak_fixtures_per_frame'], batch_size)
        if len(_perf_metrics['batch_sizes']) > 0:
            _perf_metrics['avg_batch_size'] = sum(_perf_metrics['batch_sizes']) / len(_perf_metrics['batch_sizes'])


def _lifx_batch_sender_worker():
    """Single daemon thread: wake on new DMX commands or interval; drain queue at BATCH_INTERVAL without spawning timers."""
    global running, lifx_client, _batch_sender_stop, _batch_sender_wake
    
    while True:
        if _batch_sender_stop.is_set():
            break
        with _batch_lock:
            pending = len(_batch_commands_by_id) > 0
        if pending:
            wait_for = max(0.0, BATCH_INTERVAL - (time.time() - _last_batch_time))
            timeout = min(max(wait_for, 0.0005), BATCH_INTERVAL)
        else:
            timeout = 0.1
        _batch_sender_wake.wait(timeout=timeout)
        if _batch_sender_stop.is_set():
            break
        _batch_sender_wake.clear()
        while not _batch_sender_stop.is_set():
            if not running or not lifx_client:
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
        _batch_sender_stop = threading.Event()
        _batch_sender_wake.clear()
        t = threading.Thread(target=_lifx_batch_sender_worker, daemon=True, name='lifx_batch_sender')
        _batch_sender_thread = t
        t.start()


def _stop_lifx_batch_sender_thread():
    """Stop background sender (call when DMX processing stops)."""
    global _batch_sender_thread
    
    with _batch_sender_start_lock:
        t = _batch_sender_thread
        if t is None:
            return
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
        channel_mode = mapping.get('channel_mode', 'RGB (8bit)')
        if channel_mode not in CHANNELS_FOR_MODE:
            channel_mode = 'RGB (8bit)'
        
        channels_needed = CHANNELS_FOR_MODE.get(channel_mode, 3)
        
        if start_channel < 0 or start_channel + channels_needed > len(dmx_data):
            continue
        
        # Extract channel values
        channel_values = dmx_data[start_channel:start_channel + channels_needed]
        
        # Check if values have changed significantly (to avoid spamming updates)
        last_values = _last_sent_values.get(mapped_light_id)
        if last_values is not None:
            has_significant_change = False
            max_change = 0
            
            if channel_mode == 'RGB (16bit)' and len(channel_values) >= 6 and len(last_values) >= 6:
                has_significant_change = _u16_pairs_changed(channel_values, last_values, 3, fine_first=False)
            elif channel_mode == 'RGB (16bit, fine first)' and len(channel_values) >= 6 and len(last_values) >= 6:
                has_significant_change = _u16_pairs_changed(channel_values, last_values, 3, fine_first=True)
            elif channel_mode == 'RGBW (16bit)' and len(channel_values) >= 8 and len(last_values) >= 8:
                has_significant_change = _u16_pairs_changed(channel_values, last_values, 4, fine_first=False)
            elif channel_mode == 'RGBW (16bit, fine first)' and len(channel_values) >= 8 and len(last_values) >= 8:
                has_significant_change = _u16_pairs_changed(channel_values, last_values, 4, fine_first=True)
            elif channel_mode == 'HSBK (16bit)' and len(channel_values) >= 8 and len(last_values) >= 8:
                has_significant_change = _u16_pairs_changed(channel_values, last_values, 4, fine_first=False)
            elif channel_mode == 'HSBK (16bit, fine first)' and len(channel_values) >= 8 and len(last_values) >= 8:
                has_significant_change = _u16_pairs_changed(channel_values, last_values, 4, fine_first=True)
            else:
                # For 8-bit modes, check individual channel values
                for i, val in enumerate(channel_values):
                    if i >= len(last_values):
                        has_significant_change = True
                        break
                    change = abs(val - last_values[i])
                    max_change = max(max_change, change)
                    if change >= VALUE_CHANGE_THRESHOLD:
                        has_significant_change = True
                        break
            
            if not has_significant_change:
                if dmx_logger and enable_dmx_log:
                    dmx_logger.debug(f"  SKIP {light.label}: change={max_change:.1f} < threshold, values={channel_values}")
                continue  # Skip update if change is too small
        
        # Store current values
        _last_sent_values[mapped_light_id] = list(channel_values)
        
        # Prepare for batch processing
        cmd_data = None  # (r, g, b, kelvin, duration_ms, brightness)
        if channel_mode == 'RGB (8bit)':
            r = channel_values[0] / MAX_RGB_PER_COLOUR
            g = channel_values[1] / MAX_RGB_PER_COLOUR
            b = channel_values[2] / MAX_RGB_PER_COLOUR
            
            # Clamp RGB values to 0-1
            r = max(0.0, min(1.0, r))
            g = max(0.0, min(1.0, g))
            b = max(0.0, min(1.0, b))
            
            # Apply brightness multiplier from mapping
            # The brightness setting (0-1) acts as a maximum brightness cap
            bright_adj = brightness
            
            if dmx_logger and enable_dmx_log:
                rgb_int = (int(r * 255), int(g * 255), int(b * 255))
                dmx_logger.info(f"  → {light.label}: RGB=({rgb_int[0]},{rgb_int[1]},{rgb_int[2]}), DMX={channel_values}, brightness={bright_adj:.2f}, fade={FADE_DURATION_MS}ms")
            
            # Prepare for batch sending
            cmd_data = (r, g, b, DEFAULT_KELVIN, FADE_DURATION_MS, bright_adj)
        
        elif channel_mode == 'RGB (16bit)':
            # RGB16: 16-bit per channel (6 channels total: MSB+LSB for each color)
            # Channels: R_MSB, R_LSB, G_MSB, G_LSB, B_MSB, B_LSB
            r_16bit = (channel_values[0] << 8) | channel_values[1]  # 0-65535
            g_16bit = (channel_values[2] << 8) | channel_values[3]  # 0-65535
            b_16bit = (channel_values[4] << 8) | channel_values[5]  # 0-65535
            
            # Normalize 16-bit values (0-65535) to 0-1 range
            r = r_16bit / 65535.0
            g = g_16bit / 65535.0
            b = b_16bit / 65535.0
            
            # Clamp RGB values to 0-1
            r = max(0.0, min(1.0, r))
            g = max(0.0, min(1.0, g))
            b = max(0.0, min(1.0, b))
            
            # Apply brightness multiplier from mapping
            bright_adj = brightness
            
            if dmx_logger and enable_dmx_log:
                rgb_int = (int(r * 255), int(g * 255), int(b * 255))
                dmx_logger.info(f"  → {light.label}: RGB16=({r_16bit},{g_16bit},{b_16bit}) → RGB=({rgb_int[0]},{rgb_int[1]},{rgb_int[2]}), brightness={bright_adj:.2f}, fade={FADE_DURATION_MS}ms")
            
            # Prepare for batch sending
            cmd_data = (r, g, b, DEFAULT_KELVIN, FADE_DURATION_MS, bright_adj)
        
        elif channel_mode == 'RGB (16bit, fine first)':
            r_16bit = _dmx_u16(channel_values[0], channel_values[1], fine_first=True)
            g_16bit = _dmx_u16(channel_values[2], channel_values[3], fine_first=True)
            b_16bit = _dmx_u16(channel_values[4], channel_values[5], fine_first=True)
            r = max(0.0, min(1.0, r_16bit / 65535.0))
            g = max(0.0, min(1.0, g_16bit / 65535.0))
            b = max(0.0, min(1.0, b_16bit / 65535.0))
            bright_adj = brightness
            cmd_data = (r, g, b, DEFAULT_KELVIN, FADE_DURATION_MS, bright_adj)
        
        elif channel_mode == 'RGB + Intensity (8bit)':
            intensity = channel_values[3] / MAX_RGB_PER_COLOUR
            intensity = max(0.0, min(1.0, intensity))
            r = max(0.0, min(1.0, (channel_values[0] / MAX_RGB_PER_COLOUR) * intensity))
            g = max(0.0, min(1.0, (channel_values[1] / MAX_RGB_PER_COLOUR) * intensity))
            b = max(0.0, min(1.0, (channel_values[2] / MAX_RGB_PER_COLOUR) * intensity))
            bright_adj = brightness
            cmd_data = (r, g, b, DEFAULT_KELVIN, FADE_DURATION_MS, bright_adj)
        
        elif channel_mode == 'RGBW (8bit)':
            r = channel_values[0] / MAX_RGB_PER_COLOUR
            g = channel_values[1] / MAX_RGB_PER_COLOUR
            b = channel_values[2] / MAX_RGB_PER_COLOUR
            w = channel_values[3] / MAX_RGB_PER_COLOUR
            
            # Clamp values to 0-1
            r = max(0.0, min(1.0, r))
            g = max(0.0, min(1.0, g))
            b = max(0.0, min(1.0, b))
            w = max(0.0, min(1.0, w))
            
            # For RGBW, blend white with RGB
            # Simple approach: mix white into RGB based on white channel
            r = min(1.0, r + w * 0.3)
            g = min(1.0, g + w * 0.3)
            b = min(1.0, b + w * 0.3)
            
            # Apply brightness multiplier from mapping
            bright_adj = brightness
            
            # Prepare for batch sending
            cmd_data = (r, g, b, DEFAULT_KELVIN, FADE_DURATION_MS, bright_adj)
        
        elif channel_mode == 'RGBW (16bit)':
            # RGBW16: 16-bit per channel (8 channels total: MSB+LSB for each color)
            # Channels: R_MSB, R_LSB, G_MSB, G_LSB, B_MSB, B_LSB, W_MSB, W_LSB
            r_16bit = (channel_values[0] << 8) | channel_values[1]  # 0-65535
            g_16bit = (channel_values[2] << 8) | channel_values[3]  # 0-65535
            b_16bit = (channel_values[4] << 8) | channel_values[5]  # 0-65535
            w_16bit = (channel_values[6] << 8) | channel_values[7]  # 0-65535
            
            # Normalize 16-bit values (0-65535) to 0-1 range
            r = r_16bit / 65535.0
            g = g_16bit / 65535.0
            b = b_16bit / 65535.0
            w = w_16bit / 65535.0
            
            # Clamp values to 0-1
            r = max(0.0, min(1.0, r))
            g = max(0.0, min(1.0, g))
            b = max(0.0, min(1.0, b))
            w = max(0.0, min(1.0, w))
            
            # For RGBW, blend white with RGB
            # Simple approach: mix white into RGB based on white channel
            r = min(1.0, r + w * 0.3)
            g = min(1.0, g + w * 0.3)
            b = min(1.0, b + w * 0.3)
            
            # Apply brightness multiplier from mapping
            bright_adj = brightness
            
            if dmx_logger and enable_dmx_log:
                rgb_int = (int(r * 255), int(g * 255), int(b * 255))
                dmx_logger.info(f"  → {light.label}: RGBW16=({r_16bit},{g_16bit},{b_16bit},{w_16bit}) → RGB=({rgb_int[0]},{rgb_int[1]},{rgb_int[2]}), brightness={bright_adj:.2f}, fade={FADE_DURATION_MS}ms")
            
            # Prepare for batch sending
            cmd_data = (r, g, b, DEFAULT_KELVIN, FADE_DURATION_MS, bright_adj)
        
        elif channel_mode == 'RGBW (16bit, fine first)':
            r_16bit = _dmx_u16(channel_values[0], channel_values[1], fine_first=True)
            g_16bit = _dmx_u16(channel_values[2], channel_values[3], fine_first=True)
            b_16bit = _dmx_u16(channel_values[4], channel_values[5], fine_first=True)
            w_16bit = _dmx_u16(channel_values[6], channel_values[7], fine_first=True)
            r = max(0.0, min(1.0, r_16bit / 65535.0))
            g = max(0.0, min(1.0, g_16bit / 65535.0))
            b = max(0.0, min(1.0, b_16bit / 65535.0))
            w = max(0.0, min(1.0, w_16bit / 65535.0))
            r = min(1.0, r + w * 0.3)
            g = min(1.0, g + w * 0.3)
            b = min(1.0, b + w * 0.3)
            bright_adj = brightness
            cmd_data = (r, g, b, DEFAULT_KELVIN, FADE_DURATION_MS, bright_adj)
        
        elif channel_mode == 'HSBK (8bit)':
            # HSBK8: 8-bit HSBK - Hue (0-360), Saturation (0-100), Brightness (0-100), Kelvin (2500-9000)
            hue = (channel_values[0] / 255.0) * MAX_HUE
            saturation = (channel_values[1] / 255.0) * MAX_SATURATION
            brightness_val = (channel_values[2] / 255.0) * MAX_BRIGHTNESS
            kelvin = int(max(2500, min(9000, 2500 + (channel_values[3] / 255.0) * (9000 - 2500))))
            
            # Convert HS to RGB
            h = hue / MAX_HUE
            s = saturation / MAX_SATURATION
            v = brightness_val / MAX_BRIGHTNESS
            
            r, g, b = colorsys.hsv_to_rgb(h, s, 1.0)
            bright_adj = brightness * v
            
            # Prepare for batch sending
            cmd_data = (r, g, b, kelvin, FADE_DURATION_MS, bright_adj)
        
        elif channel_mode == 'HSBK + Intensity (8bit)':
            hue = (channel_values[0] / 255.0) * MAX_HUE
            saturation = (channel_values[1] / 255.0) * MAX_SATURATION
            brightness_val = (channel_values[2] / 255.0) * MAX_BRIGHTNESS
            kelvin = int(max(2500, min(9000, 2500 + (channel_values[3] / 255.0) * (9000 - 2500))))
            intensity = max(0.0, min(1.0, channel_values[4] / 255.0))
            h = hue / MAX_HUE
            s = saturation / MAX_SATURATION
            v = brightness_val / MAX_BRIGHTNESS
            r, g, b = colorsys.hsv_to_rgb(h, s, 1.0)
            bright_adj = brightness * v * intensity
            cmd_data = (r, g, b, kelvin, FADE_DURATION_MS, bright_adj)
        
        elif channel_mode == 'HSBK (16bit)':
            # HSBK16: 16-bit HSBK - 8 channels: MSB+LSB for each parameter
            # Channels: H_MSB, H_LSB, S_MSB, S_LSB, B_MSB, B_LSB, K_MSB, K_LSB
            hue_16bit = (channel_values[0] << 8) | channel_values[1]  # 0-65535 → 0-360°
            saturation_16bit = (channel_values[2] << 8) | channel_values[3]  # 0-65535 → 0-100%
            brightness_16bit = (channel_values[4] << 8) | channel_values[5]  # 0-65535 → 0-100%
            kelvin_16bit = (channel_values[6] << 8) | channel_values[7]  # 0-65535 → 2500-9000K
            
            # Convert 16-bit values to their full ranges
            hue = (hue_16bit / 65535.0) * MAX_HUE  # 0-360°
            saturation = (saturation_16bit / 65535.0) * MAX_SATURATION  # 0-100%
            brightness_val = (brightness_16bit / 65535.0) * MAX_BRIGHTNESS  # 0-100%
            kelvin = int(max(2500, min(9000, 2500 + (kelvin_16bit / 65535.0) * (9000 - 2500))))  # 2500-9000K
            
            # Convert HS to RGB
            h = hue / MAX_HUE
            s = saturation / MAX_SATURATION
            v = brightness_val / MAX_BRIGHTNESS
            
            r, g, b = colorsys.hsv_to_rgb(h, s, 1.0)
            bright_adj = brightness * v
            
            if dmx_logger and enable_dmx_log:
                rgb_int = (int(r * 255), int(g * 255), int(b * 255))
                dmx_logger.info(f"  → {light.label}: HSBK16=({hue_16bit},{saturation_16bit},{brightness_16bit},{kelvin_16bit}) → H={hue:.1f}° S={saturation:.1f}% B={brightness_val:.1f}% K={kelvin}K → RGB=({rgb_int[0]},{rgb_int[1]},{rgb_int[2]}), brightness={bright_adj:.2f}, fade={FADE_DURATION_MS}ms")
            
            # Prepare for batch sending
            cmd_data = (r, g, b, kelvin, FADE_DURATION_MS, bright_adj)
        
        elif channel_mode == 'HSBK (16bit, fine first)':
            hue_16bit = _dmx_u16(channel_values[0], channel_values[1], fine_first=True)
            saturation_16bit = _dmx_u16(channel_values[2], channel_values[3], fine_first=True)
            brightness_16bit = _dmx_u16(channel_values[4], channel_values[5], fine_first=True)
            kelvin_16bit = _dmx_u16(channel_values[6], channel_values[7], fine_first=True)
            hue = (hue_16bit / 65535.0) * MAX_HUE
            saturation = (saturation_16bit / 65535.0) * MAX_SATURATION
            brightness_val = (brightness_16bit / 65535.0) * MAX_BRIGHTNESS
            kelvin = int(max(2500, min(9000, 2500 + (kelvin_16bit / 65535.0) * (9000 - 2500))))
            h = hue / MAX_HUE
            s = saturation / MAX_SATURATION
            v = brightness_val / MAX_BRIGHTNESS
            r, g, b = colorsys.hsv_to_rgb(h, s, 1.0)
            bright_adj = brightness * v
            cmd_data = (r, g, b, kelvin, FADE_DURATION_MS, bright_adj)
        
        if cmd_data:
            frame_batch.append((light, *cmd_data))
    
    if frame_batch:
        with _batch_lock:
            for cmd in frame_batch:
                lid = light_id(cmd[0])
                _batch_commands_by_id[lid] = cmd
        _start_lifx_batch_sender_thread()
        _batch_sender_wake.set()
    
    # Update performance metrics
    with _perf_lock:
        _perf_metrics['total_frames_processed'] += 1
        
        if process_start is not None:
            process_duration = time.time() - process_start
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
            old_receiver.stop()
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
        sacn_bind_ip = None if _normalize_interface_ip(sacn_interface) == '0.0.0.0' else sacn_interface
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
            for target, light in lifx_client.lights.items():
                lid = light_id(light)
                if lid not in discovered_by_id and lid in light_mappings:
                    discovered_by_id[lid] = light
    
    all_configured: List[Dict] = []
    for lid, mapping in light_mappings.items():
        light = discovered_by_id.get(lid)
        if light:
            all_configured.append({
                'id': lid,
                'label': light.label or mapping.get('label') or lid,
                'ip': light.ip,
                'model': light.model_name,
                'discovered': True,
                'supported_modes': ALL_CHANNEL_MODES,
                'mapping': mapping,
            })
        else:
            all_configured.append({
                'id': lid,
                'label': mapping.get('label') or f'Light {lid[:10]}',
                'ip': mapping.get('ip') or '',
                'model': mapping.get('model') or 'Unknown',
                'discovered': False,
                'supported_modes': ALL_CHANNEL_MODES,
                'mapping': mapping,
            })
    
    unconfigured: List[Dict] = []
    for light in lights_list:
        lid = light_id(light)
        if lid not in light_mappings:
            unconfigured.append({
                'id': lid,
                'label': light.label or f"Light {light.ip}",
                'ip': light.ip,
                'model': light.model_name,
                'supported_modes': ALL_CHANNEL_MODES,
            })
    
    lights_summary = [
        {
            'id': light_id(light),
            'label': light.label or f"Light {light.ip}",
            'ip': light.ip,
            'model': light.model_name,
        }
        for light in lights_list
    ]
    
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

@app.route('/api/interfaces', methods=['GET'])
def get_interfaces():
    """Get list of available network interfaces (cached; cold start loads synchronously)."""
    global _interfaces_cache, _interfaces_cache_time
    
    current_time = time.time()
    if _interfaces_cache is not None and (current_time - _interfaces_cache_time) < _interfaces_cache_ttl:
        return jsonify({
            'success': True,
            'interfaces': _interfaces_cache,
            'lifx_interface': lifx_interface,
            'sacn_interface': sacn_interface
        })
    
    def fetch_interfaces():
        global _interfaces_cache, _interfaces_cache_time
        try:
            _interfaces_cache = get_network_interfaces()
            _interfaces_cache_time = time.time()
        except Exception as e:
            print(f"Error fetching network interfaces: {e}")
    
    if _interfaces_cache is None:
        try:
            _interfaces_cache = get_network_interfaces()
            _interfaces_cache_time = time.time()
        except Exception as e:
            print(f"Error fetching network interfaces: {e}")
            _interfaces_cache = []
            _interfaces_cache_time = time.time()
    elif (current_time - _interfaces_cache_time) >= _interfaces_cache_ttl:
        threading.Thread(target=fetch_interfaces, daemon=True).start()
    
    return jsonify({
        'success': True,
        'interfaces': _interfaces_cache if _interfaces_cache is not None else [],
        'lifx_interface': lifx_interface,
        'sacn_interface': sacn_interface
    })


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
    sacn_bind_ip = None if _normalize_interface_ip(sacn_interface) == '0.0.0.0' else sacn_interface
    
    # Recreate LIFX client with new interface if it exists
    if lifx_client:
        lifx_client.close()
        lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
    
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
        lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
    else:
        # Check if we need to recreate with new interface
        try:
            current_bind = lifx_client.sock.getsockname()[0]
            if current_bind != lifx_bind_ip and lifx_bind_ip != '0.0.0.0':
                # Interface changed, recreate client
                lifx_client.close()
                lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
        except Exception as e:
            # Socket might be closed, recreate
            print(f"Warning: Error checking LIFX client socket, recreating client: {e}")
            lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
    
    try:
        lights = lifx_client.discover_lights(timeout=5.0)
        lights_data = [
            {
                'id': light_id(light),
                'label': light.label or f"Light {light.ip}",
                'ip': light.ip,
                'target': light.target.hex(),
                'model': light.model_name,
                'product_id': light.product,
                'supported_modes': ALL_CHANNEL_MODES,
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
        
        # Try to get light info if available
        light_label = existing_mapping.get('label')  # Preserve existing if available
        light_model = existing_mapping.get('model')  # Preserve existing if available
        light_ip = existing_mapping.get('ip')  # Preserve existing if available
        
        if lifx_client:
            lights = lifx_client.get_lights()
            for light in lights:
                if light_id(light) == mapped_light_id:
                    # Update with current discovered info
                    light_label = light.label
                    light_model = light.model_name
                    light_ip = light.ip
                    break
        
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
        
        # Build mapping with explicit values from request
        mode_str = str(channel_mode) if channel_mode else existing_mapping.get('channel_mode', 'RGB (8bit)')
        if mode_str not in CHANNELS_FOR_MODE:
            mode_str = existing_mapping.get('channel_mode', 'RGB (8bit)')
        if mode_str not in CHANNELS_FOR_MODE:
            mode_str = 'RGB (8bit)'
        
        mapping = {
            'universe': universe_int,
            'start_channel': start_channel_int,
            'brightness': brightness_float,
            'channel_mode': mode_str,
            'label': light_label,  # Store label for display when not discovered
            'model': light_model,  # Store model for display when not discovered
            'ip': light_ip  # Store IP for auto-discovery
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
        supported_modes = ALL_CHANNEL_MODES
    else:
        # Create a placeholder ID based on IP (will be updated when discovered)
        # Use a hash of the IP to create a consistent ID
        ip_hash = hashlib.md5(ip.encode()).hexdigest()[:16]
        lid = f"manual_{ip_hash}"
        light_label = label or f"Light {ip}"
        light_model = 'Not discovered'
        supported_modes = ALL_CHANNEL_MODES
    
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
        sacn_bind_ip = None if _normalize_interface_ip(sacn_interface) == '0.0.0.0' else sacn_interface
        
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
        perf_copy = dict(_perf_metrics)
        # Calculate current uptime in seconds
        uptime = time.time() - perf_copy['last_reset_time']
        perf_copy['uptime_seconds'] = uptime
        
        # Calculate commands per second
        if uptime > 0:
            perf_copy['commands_per_second'] = perf_copy['total_commands_sent'] / uptime
            perf_copy['frames_per_second'] = perf_copy['total_frames_processed'] / uptime
        else:
            perf_copy['commands_per_second'] = 0
            perf_copy['frames_per_second'] = 0
        
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
    
    # Find the light
    lights = lifx_client.get_lights()
    light = None
    for l in lights:
        if light_id(l) == requested_light_id:
            light = l
            break
    
    # Also check raw lights for configured lights (e.g., LIFX Switch that was filtered out)
    if not light:
        with lifx_client.lock:
            for target, l in lifx_client.lights.items():
                if light_id(l) == requested_light_id:
                    light = l
                    break
    
    if not light:
        return jsonify({'success': False, 'error': 'Light not found'}), 404
    
    # Convert RGB from 0-255 to 0.0-1.0
    r_norm = r / 255.0
    g_norm = g / 255.0
    b_norm = b / 255.0
    
    # Store light info for the background thread
    light_target = light.target
    light_ip = light.ip
    light_label = light.label
    
    # Send RGB to light asynchronously in a background thread
    def send_rgb_async():
        try:
            lifx_client.set_rgb(
                light_target,
                light_ip,
                r_norm, g_norm, b_norm,
                kelvin=DEFAULT_KELVIN,
                duration_ms=fade_ms,
                brightness=brightness
            )
        except Exception as set_rgb_error:
            import traceback
            print(f"Error in async set_rgb: {set_rgb_error}")
            traceback.print_exc()
    
    # Start the background thread
    threading.Thread(target=send_rgb_async, daemon=True).start()
    
    # Return immediately - command is being sent in background
    return jsonify({
        'success': True,
        'message': f'Sending RGB({r},{g},{b}) to {light_label}',
        'light_label': light_label
    })


@app.route('/api/metrics', methods=['GET'])
def get_metrics():
    """Get detailed performance metrics for multi-fixture setups"""
    with _perf_lock:
        metrics = dict(_perf_metrics)
        
        # Calculate derived metrics
        uptime = time.time() - metrics['last_reset_time']
        metrics['uptime_seconds'] = uptime
        metrics['uptime_formatted'] = f"{uptime/3600:.1f}h" if uptime > 3600 else f"{uptime/60:.1f}m"
        
        if uptime > 0:
            metrics['commands_per_second'] = metrics['total_commands_sent'] / uptime
            metrics['frames_per_second'] = metrics['total_frames_processed'] / uptime
            metrics['batches_per_second'] = metrics['total_batches_sent'] / uptime
        else:
            metrics['commands_per_second'] = 0
            metrics['frames_per_second'] = 0
            metrics['batches_per_second'] = 0
        
        # Calculate efficiency metrics
        if metrics['total_batches_sent'] > 0:
            metrics['batch_efficiency'] = metrics['avg_batch_size'] / BATCH_SIZE
        else:
            metrics['batch_efficiency'] = 0
        
        # Add current system load
        metrics['current_queue_size'] = len(_batch_commands_by_id)
        metrics['batch_utilization_percent'] = (metrics['avg_batch_size'] / BATCH_SIZE) * 100 if BATCH_SIZE > 0 else 0
        
        # Convert deque objects to lists for JSON serialization
        metrics['frame_times'] = list(metrics['frame_times'])
        metrics['batch_sizes'] = list(metrics['batch_sizes'])
    
    return jsonify({
        'success': True,
        'metrics': metrics,
        'batch_config': {
            'batch_size': BATCH_SIZE,
            'batch_interval_ms': BATCH_INTERVAL * 1000,
            'max_workers': lifx_client.executor._max_workers if lifx_client and hasattr(lifx_client, 'executor') else 3
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
        lifx_client = LifxLanClient(bind_ip=lifx_bind_ip)
    
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


if __name__ == '__main__':
    load_config()
    
    print(f"sACN2LIFX v{VERSION} Server starting...")
    
    # Auto-discover configured lights on startup
    auto_discover_configured_lights()
    
    print("Open http://localhost:5001 in your browser")
    
    app.run(host='0.0.0.0', port=5001, debug=True)

