import socket
import struct
import time
import random
import colorsys
import threading
import sys
import traceback
from typing import Dict, Optional, List, Tuple
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from lifx_products import (
    EXTENDED_MULTIZONE_PRODUCT_IDS,
    PRODUCT_NAMES,
    SWITCH_PRODUCT_IDS,
    product_layout,
)

# =========================
# LIFX CONSTANTS
# =========================

LIFX_PORT = 56700
PROTO = 1024
HEADER_SIZE = 36
DEFAULT_BATCH_EXECUTOR_WORKERS = 3
MIN_SEND_INTERVAL = 0.02  # seconds (rate limit) - allows up to 50Hz updates for smooth 40Hz sACN
DEFAULT_KELVIN = 3500
COLOR_SET_PROTECTION_TIME = 1.0  # seconds - prevent stale STATE_LIGHT responses from overwriting recently set colors
COLOR_SET_PROTECTION_TIME_DMX = 5.0  # seconds - longer protection when DMX is actively running
COLOR_SET_DMX_THRESHOLD_SECONDS = 2.0  # seconds - threshold to determine if color was set "very recently" (DMX active)
COLOR_SET_RESET_INTERVAL = 2.0  # seconds - interval for resetting color_set_count when tracking DMX update frequency
STATE_REQUEST_WINDOW = 2.0  # seconds - window for accepting explicitly requested state responses

# Message types
GET_SERVICE = 2
STATE_SERVICE = 3
GET_LABEL = 23
STATE_LABEL = 25
GET_POWER = 20
STATE_POWER = 22
GET_VERSION = 32
STATE_VERSION = 33
GET_LIGHT_STATE = 101
STATE_LIGHT = 107
SET_COLOR = 102
SET_POWER = 21
GET_COLOR_ZONES = 502
STATE_ZONE = 503
STATE_MULTIZONE = 506
SET_EXTENDED_COLOR_ZONES = 510
GET_EXTENDED_COLOR_ZONES = 511
STATE_EXTENDED_COLOR_ZONES = 512
GET_DEVICE_CHAIN = 701
STATE_DEVICE_CHAIN = 702
SET_64 = 715
MULTI_ZONE_APPLY = 1
MULTI_ZONE_NO_APPLY = 0
EXTENDED_MZ_COLORS_PER_PACKET = 82
SET64_COLORS_PER_PACKET = 64
TILE_DEVICE_SIZE = 55

# Canonical DMX channel-mode names (shared with app.py CHANNEL_MODE_SPEC).
CHANNEL_MODES: Tuple[str, ...] = (
    "RGB (8bit)",
    "RGB (16bit)",
    "RGB (16bit, fine first)",
    "RGB + Intensity (8bit)",
    "RGBW (8bit)",
    "RGBW (16bit)",
    "RGBW (16bit, fine first)",
    "HSBK (8bit)",
    "HSBK (16bit)",
    "HSBK (16bit, fine first)",
    "HSBK + Intensity (8bit)",
    "RGB Full Pixel (8bit)",
    "RGB + Intensity Full Pixel (8bit)",
    "RGBW Full Pixel (8bit)",
)

# Zone fixtures: whole-fixture RGB modes plus per-pixel variants. Standard
# fixtures keep every non-pixel layout. app.py derives API lists from these.
ZONE_CHANNEL_MODES: Tuple[str, ...] = (
    "RGB (8bit)",
    "RGB + Intensity (8bit)",
    "RGBW (8bit)",
    "RGB Full Pixel (8bit)",
    "RGB + Intensity Full Pixel (8bit)",
    "RGBW Full Pixel (8bit)",
)
STANDARD_CHANNEL_MODES: Tuple[str, ...] = tuple(
    mode for mode in CHANNEL_MODES if "Pixel" not in mode
)
_ZONE_CHANNEL_MODE_SET = frozenset(ZONE_CHANNEL_MODES)

# =========================
# HELPERS
# =========================

def clamp01(x):
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else float(x)


def _scaled_brightness(bri: int, brightness: float) -> int:
    """Scale a 0–65535 brightness by a 0–1 multiplier and clamp to the LIFX range."""
    return max(0, min(65535, int(bri * clamp01(brightness))))


def rgb01_to_hsbk(r, g, b, kelvin=DEFAULT_KELVIN, hold_hue: Optional[int] = None):
    r = clamp01(r)
    g = clamp01(g)
    b = clamp01(b)

    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    # Near black/white, HSV hue is undefined and snaps to 0 (red). Keep the last
    # hue so sinewaves and chases through dim values do not flash a wrong colour.
    if hold_hue is not None and (s < 0.05 or v < 0.02):
        h = (hold_hue & 0xFFFF) / 65535.0

    return (
        int(h * 65535) & 0xFFFF,
        int(s * 65535) & 0xFFFF,
        int(v * 65535) & 0xFFFF,
        int(kelvin) & 0xFFFF
    )


def hsbk_to_rgb8(hue: int, sat: int, bri: int) -> Tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(hue / 65535.0, sat / 65535.0, bri / 65535.0)
    return (int(r * 255), int(g * 255), int(b * 255))


def _pack_hsbk_colors(colors: List[Tuple[int, int, int, int]], count: int) -> bytes:
    """Pack HSBK tuples into a fixed-length little-endian color array."""
    packed = bytearray()
    for i in range(count):
        if i < len(colors):
            hue, sat, bri, kel = colors[i]
            packed.extend(struct.pack("<HHHH", hue & 0xFFFF, sat & 0xFFFF, bri & 0xFFFF, kel & 0xFFFF))
        else:
            packed.extend(b"\x00" * 8)
    return bytes(packed)


# =========================
# LIFX LIGHT REPRESENTATION
# =========================

class LifxLight:
    def __init__(self, target: bytes, ip: str, label: str = ""):
        self.target = target
        self.ip = ip
        self.label = label
        self.power = 0
        self.colour = None
        self.last_seen = time.time()
        self.vendor = 0
        self.product = 0
        self.version = 0
        self.model_name = "Discovering..."
        self.is_light = True  # Will be set based on product type
        self.supported_modes = ["RGB"]  # Default, will be updated based on product
        # Current state
        self.current_hue = 0
        self.current_saturation = 0
        self.current_brightness = 0
        self.current_kelvin = DEFAULT_KELVIN
        # Display RGB as 8-bit ints (0–255), same shape as updates from set_rgb / STATE_LIGHT.
        self.current_rgb = (0, 0, 0)
        self.color_set_time = 0.0  # Timestamp when color was last set via set_rgb
        self.state_requested_time = 0.0  # Timestamp when we requested state after setting color
        self.color_set_count = 0  # Count of recent color sets (for detecting active DMX updates)
        self.last_color_set_check = 0.0  # Timestamp of last color set count check
        self.layout = "single"  # 'single', 'linear', or 'matrix'
        self.zone_count = 1
        self.matrix_width = 1
        self.matrix_height = 1
        self.tile_count = 1

    @property
    def effective_layout(self) -> str:
        """Live geometry if known, otherwise the product catalog layout."""
        if self.layout in ("linear", "matrix"):
            return self.layout
        return product_layout(self.product)

    @property
    def zone_capable(self) -> bool:
        return self.effective_layout in ("linear", "matrix")

    def __repr__(self):
        return f"LifxLight(label='{self.label}', ip='{self.ip}', model='{self.model_name}')"


# =========================
# LIFX CLIENT
# =========================

class LifxLanClient:
    def __init__(self, bind_ip: str = "0.0.0.0"):
        self.source = random.randint(2, 0xFFFFFFFF)
        self.requested_bind_ip = bind_ip

        # Performance optimization: batch processing
        self.command_queue = deque()  # Queue of commands to batch
        self.batch_size = 10  # Max commands per batch
        self.batch_timeout = 0.005  # 5ms max wait for batch accumulation
        self.last_batch_send = 0.0
        self.batch_thread = None
        self.batch_running = False
        self._queue_wake = threading.Event()
        self.executor_max_workers = DEFAULT_BATCH_EXECUTOR_WORKERS
        self.executor = ThreadPoolExecutor(
            max_workers=self.executor_max_workers,
            thread_name_prefix="lifx_batch",
        )
        self.sequence = random.randint(0, 255)
        self.lights: Dict[bytes, LifxLight] = {}
        self.last_send = 0.0
        self.lock = threading.Lock()
        self.pending_state_requests: Dict[bytes, threading.Event] = {}

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.5)
        # Limited broadcast (255.255.255.255) requires SO_BROADCAST. Without it,
        # sendto() raises PermissionError [Errno 13] on macOS/Linux even as root.
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((bind_ip, 0))
        except OSError as e:
            self.executor.shutdown(wait=False)
            self.sock.close()
            raise OSError(f"Could not bind LIFX UDP socket to {bind_ip!r} (port 0): {e}") from e
        sa = self.sock.getsockname()
        if isinstance(sa, tuple) and len(sa) >= 2:
            self.bound_ip, self.bound_port = sa[0], int(sa[1])
        else:
            # e.g. unit tests with a patched socket that does not implement getsockname()
            self.bound_ip, self.bound_port = bind_ip, 0
        
        self.listening = True
        self.listener_thread = threading.Thread(target=self._listen, daemon=True)
        self.listener_thread.start()
        
        # Start batch processing thread
        self.batch_running = True
        self.batch_thread = threading.Thread(target=self._batch_worker, daemon=True)
        self.batch_thread.start()

    def _next_seq(self):
        self.sequence = (self.sequence + 1) & 0xFF
        return self.sequence

    def _rate_limit(self):
        dt = time.time() - self.last_send
        if dt < MIN_SEND_INTERVAL:
            time.sleep(MIN_SEND_INTERVAL - dt)
        self.last_send = time.time()

    def _batch_worker(self):
        """Background thread for any leftover queued packets (discovery/test paths)."""
        while self.batch_running:
            try:
                queued = len(self.command_queue)
                if queued >= self.batch_size:
                    self._send_batch()
                elif queued > 0 and (time.time() - self.last_batch_send > self.batch_timeout):
                    self._send_batch()
                else:
                    timeout = self.batch_timeout if queued else 0.05
                    self._queue_wake.wait(timeout=timeout)
                    self._queue_wake.clear()
            except Exception as e:
                print(f"Error in batch worker: {e}")
                time.sleep(0.01)

    def _send_batch(self):
        """Send a batch of commands efficiently"""
        if not self.command_queue:
            return
            
        # Get a batch of commands
        batch = []
        while len(batch) < self.batch_size and self.command_queue:
            batch.append(self.command_queue.popleft())
        
        if not batch:
            return
            
        # Sequential UDP sends (avoid thread-pool overhead for tiny packets)
        for cmd_data in batch:
            try:
                self._send_command_raw(*cmd_data)
            except Exception as e:
                print(f"Error sending batch command: {e}")
        
        self.last_batch_send = time.time()

    def _send_command_raw(self, packet: bytes, ip: str):
        """Send raw packet without rate limiting (used in batches)"""
        try:
            self.sock.sendto(packet, (ip, LIFX_PORT))
        except Exception as e:
            print(f"Error sending command to {ip}: {e}")

    def _ensure_light_powered(self, target: bytes, ip: str) -> None:
        with self.lock:
            if target not in self.lights or self.lights[target].power != 0:
                return
        try:
            self.set_power(target, ip, True)
        except OSError as e:
            print(f"Error powering on light at {ip}: {e}")
            return
        with self.lock:
            if target in self.lights:
                self.lights[target].power = 65535

    def _build_set_color_packet(
        self,
        target: bytes,
        r: float,
        g: float,
        b: float,
        kelvin: int,
        duration_ms: int,
        brightness: float,
        hold_hue: Optional[int] = None,
    ) -> Tuple[bytes, int, int, int, int]:
        hue, sat, bri, kel = rgb01_to_hsbk(r, g, b, kelvin, hold_hue=hold_hue)
        bri = _scaled_brightness(bri, brightness)
        header = self._build_header(SET_COLOR, target=target, tagged=False)
        payload = struct.pack("<BHHHHI", 0, hue, sat, bri, kel, int(duration_ms))
        return self._finalise(header + payload), hue, sat, bri, kel

    def _write_local_color_locked(
        self,
        target: bytes,
        hue: int,
        sat: int,
        bri: int,
        kel: int,
        now: float,
    ) -> Optional[LifxLight]:
        """Caller must hold self.lock."""
        if target not in self.lights:
            return None
        light = self.lights[target]
        light.current_hue = hue
        light.current_saturation = sat
        light.current_brightness = bri
        light.current_kelvin = kel
        light.current_rgb = hsbk_to_rgb8(hue, sat, bri)
        light.color_set_time = now
        if now - light.last_color_set_check > COLOR_SET_RESET_INTERVAL:
            light.color_set_count = 0
            light.last_color_set_check = now
        light.color_set_count += 1
        return light

    def send_color_now(
        self,
        target: bytes,
        ip: str,
        r: float,
        g: float,
        b: float,
        kelvin: int = DEFAULT_KELVIN,
        duration_ms: int = 0,
        brightness: float = 1.0,
    ) -> None:
        """Build and send SET_COLOR immediately (DMX hot path; no extra queue)."""
        self._ensure_light_powered(target, ip)
        with self.lock:
            light = self.lights.get(target)
            hold_hue = light.current_hue if light is not None else None
        packet, hue, sat, bri, kel = self._build_set_color_packet(
            target, r, g, b, kelvin, duration_ms, brightness, hold_hue=hold_hue
        )
        self._send_command_raw(packet, ip)
        with self.lock:
            self._write_local_color_locked(target, hue, sat, bri, kel, time.time())

    def send_zones_now(
        self,
        target: bytes,
        ip: str,
        zone_cmds: List[Tuple[float, float, float, int, float]],
        duration_ms: int = 0,
    ) -> None:
        """Build and send multizone packets immediately (DMX hot path)."""
        self._ensure_light_powered(target, ip)
        hsbk = [
            self._hsbk_from_rgb(r, g, b, kelvin, brightness)
            for r, g, b, kelvin, brightness in zone_cmds
        ]
        with self.lock:
            light = self.lights.get(target)
            if light is None:
                return
            packets = self._zone_packets_for_light(light, hsbk, duration_ms)
        for packet in packets:
            self._send_command_raw(packet, ip)
        if hsbk:
            hue, sat, bri, kel = hsbk[0]
            with self.lock:
                self._write_local_color_locked(target, hue, sat, bri, kel, time.time())

    def set_rgb_batch(self, target: bytes, ip: str, r: float, g: float, b: float, 
                     kelvin: int = DEFAULT_KELVIN, duration_ms: int = 0, brightness: float = 1.0):
        """Send RGB immediately. Name kept for callers that queued through the old batch path."""
        self.send_color_now(target, ip, r, g, b, kelvin, duration_ms, brightness)

    def _hsbk_from_rgb(
        self, r: float, g: float, b: float, kelvin: int, brightness: float
    ) -> Tuple[int, int, int, int]:
        hue, sat, bri, kel = rgb01_to_hsbk(r, g, b, kelvin)
        return hue, sat, _scaled_brightness(bri, brightness), kel

    def _build_set64_packets(
        self,
        target: bytes,
        colors: List[Tuple[int, int, int, int]],
        width: int,
        height: int,
        duration_ms: int,
        tile_index: int = 0,
    ) -> List[bytes]:
        """Build Set64 (715) packets for one matrix tile (row-major from DMX)."""
        width = max(1, width)
        height = max(1, height)
        rows_per_packet = max(1, SET64_COLORS_PER_PACKET // width)
        packets = []
        for y in range(0, height, rows_per_packet):
            start = y * width
            chunk = colors[start:start + rows_per_packet * width]
            payload = struct.pack(
                "<BBBBBBI",
                tile_index,
                1,
                0,  # visible framebuffer
                0,  # x
                y,
                width,
                int(duration_ms),
            )
            payload += _pack_hsbk_colors(chunk, SET64_COLORS_PER_PACKET)
            header = self._build_header(SET_64, target=target, tagged=False)
            packets.append(self._finalise(header + payload))
        return packets

    def _build_extended_mz_packets(
        self,
        target: bytes,
        colors: List[Tuple[int, int, int, int]],
        duration_ms: int,
    ) -> List[bytes]:
        """Build SetExtendedColorZones (510) packets for a linear strip."""
        packets = []
        total = len(colors)
        for index in range(0, total, EXTENDED_MZ_COLORS_PER_PACKET):
            chunk = colors[index:index + EXTENDED_MZ_COLORS_PER_PACKET]
            apply = (
                MULTI_ZONE_APPLY
                if index + EXTENDED_MZ_COLORS_PER_PACKET >= total
                else MULTI_ZONE_NO_APPLY
            )
            payload = struct.pack("<IBHB", int(duration_ms), apply, index, len(chunk))
            payload += _pack_hsbk_colors(chunk, EXTENDED_MZ_COLORS_PER_PACKET)
            header = self._build_header(SET_EXTENDED_COLOR_ZONES, target=target, tagged=False)
            packets.append(self._finalise(header + payload))
        return packets

    def _zone_packets_for_light(
        self,
        light: LifxLight,
        colors: List[Tuple[int, int, int, int]],
        duration_ms: int,
    ) -> List[bytes]:
        if light.effective_layout == "matrix":
            packets: List[bytes] = []
            width = max(1, light.matrix_width)
            height = max(1, light.matrix_height)
            tile_count = max(1, light.tile_count)
            default_geometry = width == 1 and height == 1
            if default_geometry:
                per_tile = max(1, light.zone_count // tile_count)
                send_width = min(per_tile, SET64_COLORS_PER_PACKET)
                send_height = max(1, (per_tile + send_width - 1) // send_width)
            else:
                per_tile = width * height
                send_width = width
                send_height = height
            for tile_index in range(tile_count):
                start = tile_index * per_tile
                chunk = colors[start:start + per_tile]
                packets.extend(
                    self._build_set64_packets(
                        light.target,
                        chunk,
                        send_width,
                        send_height,
                        duration_ms,
                        tile_index=tile_index,
                    )
                )
            return packets
        return self._build_extended_mz_packets(light.target, colors, duration_ms)

    def set_zones_batch(
        self,
        target: bytes,
        ip: str,
        zone_cmds: List[Tuple[float, float, float, int, float]],
        duration_ms: int = 0,
    ) -> None:
        """Send per-zone colours immediately. Name kept for older callers."""
        self.send_zones_now(target, ip, zone_cmds, duration_ms)

    def _build_header(self, msg_type: int, target: Optional[bytes] = None, tagged: bool = False):
        addressable = 1
        origin = 0

        frame_bits = (
            (PROTO & 0x0FFF)
            | (addressable << 12)
            | ((1 if tagged else 0) << 13)
            | (origin << 14)
        )

        frame = struct.pack(
            "<HHI",
            HEADER_SIZE,
            frame_bits,
            self.source
        )

        target_bytes = target if target else b"\x00" * 8

        address = struct.pack(
            "<8s6sBB",
            target_bytes,
            b"\x00" * 6,
            0,
            self._next_seq()
        )

        protocol = struct.pack(
            "<QH2s",
            0,
            msg_type,
            b"\x00\x00"
        )

        return frame + address + protocol

    def _finalise(self, packet: bytes) -> bytes:
        size = len(packet)
        return struct.pack("<H", size) + packet[2:]

    def _process_state_light(self, light: LifxLight, hue: int, sat: int, bri: int, kel: int):
        """
        Process a STATE_LIGHT message and update light state if appropriate.
        
        This method implements the protection logic to prevent stale state responses
        from overwriting recently set colours.
        
        Args:
            light: The LifxLight object to update
            hue: Hue value (0-65535)
            sat: Saturation value (0-65535)
            bri: Brightness value (0-65535)
            kel: Kelvin value
        """
        # Only update from STATE_LIGHT if we haven't set a color recently
        # OR if we explicitly requested the state after setting a color
        # This prevents stale state responses from overwriting colors we just set via DMX
        time_since_set = time.time() - light.color_set_time
        time_since_request = time.time() - light.state_requested_time if light.state_requested_time > 0 else float('inf')
        
        # Use longer protection time if color was set very recently (within COLOR_SET_DMX_THRESHOLD_SECONDS, likely DMX is actively running)
        # This prevents stale refresh responses from overwriting actively updated colors
        protection_time = COLOR_SET_PROTECTION_TIME_DMX if time_since_set < COLOR_SET_DMX_THRESHOLD_SECONDS else COLOR_SET_PROTECTION_TIME
        
        # Update if: color wasn't set recently (with appropriate protection time), OR we requested state recently
        if time_since_set > protection_time or (light.state_requested_time > 0 and time_since_request < STATE_REQUEST_WINDOW):
            light.current_hue = hue
            light.current_saturation = sat
            light.current_brightness = bri
            light.current_kelvin = kel
            
            # Convert HSBK to RGB for display
            light.current_rgb = hsbk_to_rgb8(hue, sat, bri)

    def _parse_state_device_chain(self, light: LifxLight, data: bytes) -> None:
        """Read matrix width/height from StateDeviceChain (702)."""
        payload = data[HEADER_SIZE:]
        count_offset = 1 + 16 * TILE_DEVICE_SIZE
        if len(payload) <= count_offset:
            return
        tile_count = payload[count_offset]
        if tile_count < 1:
            return
        first = payload[1:1 + TILE_DEVICE_SIZE]
        width, height = first[16], first[17]
        if width < 1 or height < 1:
            return
        light.layout = "matrix"
        light.matrix_width = width
        light.matrix_height = height
        light.tile_count = tile_count
        light.zone_count = width * height * tile_count

    def _parse_linear_zone_count(self, light: LifxLight, msg_type: int, data: bytes) -> None:
        """Read zone_count from linear multizone state packets."""
        payload = data[HEADER_SIZE:]
        zone_count = 0
        if msg_type == STATE_EXTENDED_COLOR_ZONES:
            if len(payload) >= 2:
                zone_count = struct.unpack_from("<H", payload, 0)[0]
        elif msg_type in (STATE_MULTIZONE, STATE_ZONE):
            if len(payload) >= 1:
                zone_count = payload[0]
        if zone_count < 1:
            return
        light.layout = "linear"
        light.zone_count = zone_count

    def _listen(self):
        """Background thread to listen for LIFX responses"""
        while self.listening:
            try:
                data, (ip, port) = self.sock.recvfrom(4096)
                if len(data) < HEADER_SIZE:
                    continue

                # Parse header
                size = struct.unpack("<H", data[:2])[0]
                frame_bits = struct.unpack("<H", data[2:4])[0]
                source = struct.unpack("<I", data[4:8])[0]
                target = data[8:16]
                msg_type = struct.unpack("<H", data[32:34])[0]

                # Only process responses to our messages
                if source != self.source:
                    continue

                with self.lock:
                    # Update or create light entry
                    if target not in self.lights:
                        light = LifxLight(target, ip)
                        self.lights[target] = light
                    else:
                        light = self.lights[target]
                    light.last_seen = time.time()

                    # Handle different message types
                    if msg_type == STATE_SERVICE:
                        # Service response - light is responding
                        pass
                    elif msg_type == STATE_LABEL:
                        # Extract label (starts at byte 36)
                        if len(data) >= 36:
                            label_bytes = data[36:].split(b'\x00')[0]
                            light.label = label_bytes.decode('utf-8', errors='ignore')
                    elif msg_type == STATE_POWER:
                        # Extract power state (byte 36)
                        if len(data) >= 37:
                            light.power = struct.unpack("<H", data[36:38])[0]
                    elif msg_type == STATE_VERSION:
                        # Extract version info (vendor, product, version)
                        # STATE_VERSION payload: vendor (4 bytes), product (4 bytes), version (4 or 8 bytes)
                        # Total: 36 (header) + 4 + 4 + 4/8 = 48 or 52 bytes
                        if len(data) >= 48:
                            try:
                                vendor_bytes = data[36:40]
                                product_bytes = data[40:44]
                                # Version might be 4 or 8 bytes depending on device
                                if len(data) >= 52:
                                    version_bytes = data[44:52]
                                    light.version = struct.unpack("<Q", version_bytes)[0]
                                else:
                                    version_bytes = data[44:48]
                                    light.version = struct.unpack("<I", version_bytes)[0]
                                
                                light.vendor = struct.unpack("<I", vendor_bytes)[0]
                                light.product = struct.unpack("<I", product_bytes)[0]
                                if light.layout not in ("linear", "matrix"):
                                    light.layout = product_layout(light.product)
                                light.model_name = self._get_model_name(light.vendor, light.product)
                                light.supported_modes = self._get_supported_modes(light.product)
                                is_switch = self._is_switch_product(light.product, light.model_name)
                                light.is_light = not is_switch
                                
                                # Remove switch from lights dictionary if it's a switch
                                if is_switch:
                                    if target in self.lights:
                                        del self.lights[target]
                            except Exception as e:
                                print(f"Error parsing STATE_VERSION for {light.ip}: {e}, data_len={len(data)}")
                    elif msg_type == STATE_LIGHT:
                        # STATE_LIGHT payload: HSBK (Hue, Saturation, Brightness, Kelvin) + reserved
                        # Format: reserved (1 byte), hue (2 bytes), saturation (2 bytes), brightness (2 bytes), kelvin (2 bytes)
                        # Total: 36 (header) + 9 = 45 bytes
                        if len(data) >= 45:
                            try:
                                hue = struct.unpack("<H", data[37:39])[0]
                                sat = struct.unpack("<H", data[39:41])[0]
                                bri = struct.unpack("<H", data[41:43])[0]
                                kel = struct.unpack("<H", data[43:45])[0]
                                
                                # Process STATE_LIGHT using the extracted method
                                self._process_state_light(light, hue, sat, bri, kel)
                            except Exception as e:
                                print(f"Error parsing STATE_LIGHT for {light.ip}: {e}, data_len={len(data)}")
                    elif msg_type == STATE_DEVICE_CHAIN:
                        self._parse_state_device_chain(light, data)
                    elif msg_type in (STATE_EXTENDED_COLOR_ZONES, STATE_MULTIZONE, STATE_ZONE):
                        self._parse_linear_zone_count(light, msg_type, data)
                    else:
                        # Debug: log unhandled message types
                        if msg_type not in [STATE_SERVICE, STATE_LABEL, STATE_POWER, STATE_VERSION, STATE_LIGHT]:
                            print(f"Unhandled message type {msg_type} from {ip}")

            except socket.timeout:
                continue
            except Exception as e:
                if self.listening:
                    print(f"Error in listener: {e}")

    # =========================
    # DISCOVERY
    # =========================

    def discover_lights(self, timeout: float = 5.0) -> List[LifxLight]:
        """Discover all LIFX lights on the network"""
        # Clear existing lights at start of discovery to avoid duplicates
        with self.lock:
            self.lights.clear()
        
        # Send broadcast discovery
        header = self._build_header(GET_SERVICE, tagged=True)
        packet = self._finalise(header)

        self._rate_limit()
        self.sock.sendto(packet, ("255.255.255.255", LIFX_PORT))

        # Wait for responses - increased timeout to allow more lights to respond
        time.sleep(timeout)

        # Request labels and version info for discovered lights
        with self.lock:
            lights_list = list(self.lights.values())
            for light in lights_list:
                self._request_label(light)
                time.sleep(0.05)  # Small delay between requests
                self._request_version(light)
                time.sleep(0.05)  # Small delay between requests

        # Wait longer for label and version responses - some lights respond slower
        time.sleep(1.5)

        with self.lock:
            zoned = [light for light in self.lights.values() if light.zone_capable]
        for light in zoned:
            self._request_zone_geometry(light)
            time.sleep(0.05)
        if zoned:
            time.sleep(0.5)

        # Filter out non-light devices (switches, etc.)
        # Also double-check by model name in case product ID wasn't set
        with self.lock:
            filtered_lights = []
            for light in self.lights.values():
                # Check product ID first
                if not light.is_light:
                    continue
                # Also check model name as fallback
                if light.model_name and "Switch" in light.model_name:
                    continue
                filtered_lights.append(light)
            return filtered_lights

    def _request_label(self, light: LifxLight):
        """Request label from a specific light"""
        header = self._build_header(GET_LABEL, target=light.target, tagged=False)
        packet = self._finalise(header)

        self._rate_limit()
        self.sock.sendto(packet, (light.ip, LIFX_PORT))
    
    def _request_version(self, light: LifxLight):
        """Request version info from a specific light"""
        header = self._build_header(GET_VERSION, target=light.target, tagged=False)
        packet = self._finalise(header)

        self._rate_limit()
        self.sock.sendto(packet, (light.ip, LIFX_PORT))

    def _request_zone_geometry(self, light: LifxLight) -> None:
        """Ask a matrix or linear fixture how many zones it has."""
        layout = light.effective_layout
        if layout == "matrix":
            header = self._build_header(GET_DEVICE_CHAIN, target=light.target, tagged=False)
            packet = self._finalise(header)
        elif light.product in EXTENDED_MULTIZONE_PRODUCT_IDS:
            header = self._build_header(GET_EXTENDED_COLOR_ZONES, target=light.target, tagged=False)
            packet = self._finalise(header)
        elif layout == "linear":
            header = self._build_header(GET_COLOR_ZONES, target=light.target, tagged=False)
            packet = self._finalise(header + struct.pack("<BB", 0, 255))
        else:
            return
        self._rate_limit()
        self.sock.sendto(packet, (light.ip, LIFX_PORT))
    
    def _request_light_state(self, light: LifxLight):
        """Request current light state (color) from a specific light"""
        header = self._build_header(GET_LIGHT_STATE, target=light.target, tagged=False)
        packet = self._finalise(header)

        self._rate_limit()
        self.sock.sendto(packet, (light.ip, LIFX_PORT))
    
    def refresh_light_states(self):
        """Request current state from all discovered lights"""
        with self.lock:
            lights_list = [light for light in self.lights.values() 
                          if light.is_light and not (light.model_name and "Switch" in light.model_name)]
            for light in lights_list:
                self._request_light_state(light)
                time.sleep(0.05)  # Small delay between requests
        # Wait for responses
        time.sleep(0.5)
    
    def probe_light_by_ip(self, ip: str, timeout: float = 2.0) -> Optional[LifxLight]:
        """Probe a specific IP address to discover a LIFX light"""
        self._start_listener()
        
        # Send GET_SERVICE to specific IP (not broadcast)
        # We use tagged=True to get a response even if we don't know the target
        header = self._build_header(GET_SERVICE, tagged=True)
        packet = self._finalise(header)
        
        self._rate_limit()
        self.sock.sendto(packet, (ip, LIFX_PORT))
        
        # Wait for response
        time.sleep(timeout)
        
        found = None
        with self.lock:
            for light in self.lights.values():
                if light.ip == ip:
                    found = light
                    break
        if not found:
            return None
        self._request_label(found)
        time.sleep(0.05)
        self._request_version(found)
        time.sleep(0.2)
        if found.zone_capable:
            self._request_zone_geometry(found)
            time.sleep(0.3)
        if found.is_light:
            return found
        return None

    def _start_listener(self) -> None:
        if self.listening and self.listener_thread and self.listener_thread.is_alive():
            return
        self.listening = True
        self.listener_thread = threading.Thread(target=self._listen, daemon=True)
        self.listener_thread.start()
    
    def _get_model_name(self, vendor: int, product: int) -> str:
        """Map product ID to model name from the LIFX product registry."""
        if vendor != 1:
            return f"Unknown (vendor={vendor})"
        return PRODUCT_NAMES.get(product, f"Unknown (product={product})")

    def _is_switch_product(self, product: int, model_name: str = "") -> bool:
        """True for LIFX switches/relays, which are not controllable lights."""
        if product in SWITCH_PRODUCT_IDS:
            return True
        return bool(model_name) and "Switch" in model_name
    
    def _get_supported_modes(self, product: int = 0) -> List[str]:
        """DMX channel layouts; zone fixtures get a shorter whole-fixture + pixel list."""
        if product_layout(product) in ("linear", "matrix"):
            return [mode for mode in CHANNEL_MODES if mode in _ZONE_CHANNEL_MODE_SET]
        return list(STANDARD_CHANNEL_MODES)

    def refresh_lights(self):
        """Refresh the list of discovered lights"""
        return self.discover_lights()

    def get_lights(self) -> List[LifxLight]:
        """Get current list of discovered lights"""
        with self.lock:
            return list(self.lights.values())

    # =========================
    # COLOR CONTROL
    # =========================

    def set_rgb(self, target: bytes, ip: str, r: float, g: float, b: float, 
                kelvin: int = DEFAULT_KELVIN, duration_ms: int = 0, brightness: float = 1.0):
        """Set RGB colour for a specific light"""
        self._ensure_light_powered(target, ip)
        with self.lock:
            existing = self.lights.get(target)
            hold_hue = existing.current_hue if existing is not None else None
        packet, hue, sat, bri, kel = self._build_set_color_packet(
            target, r, g, b, kelvin, duration_ms, brightness, hold_hue=hold_hue
        )

        self._rate_limit()
        self.sock.sendto(packet, (ip, LIFX_PORT))
        
        # Update the light's current state so UI can display it
        current_time = time.time()
        cancel_event = None
        with self.lock:
            light = self._write_local_color_locked(target, hue, sat, bri, kel, current_time)
            if light is None or light.color_set_count > 2:
                return
            # Cancel any pending state request for this light to prevent thread accumulation
            if target in self.pending_state_requests:
                self.pending_state_requests[target].set()
            cancel_event = threading.Event()
            self.pending_state_requests[target] = cancel_event

        def request_state_after_fade():
            # Wait for fade to complete plus a small buffer
            fade_time = max(duration_ms / 1000.0, 0.1) + 0.2

            # Sleep in small increments to allow cancellation
            sleep_interval = 0.05  # Check for cancellation every 50ms
            elapsed = 0.0
            while elapsed < fade_time:
                if cancel_event.is_set():
                    self._discard_pending_request(target, cancel_event)
                    return
                sleep_duration = min(sleep_interval, fade_time - elapsed)
                time.sleep(sleep_duration)
                elapsed += sleep_duration

            if cancel_event.is_set():
                self._discard_pending_request(target, cancel_event)
                return

            try:
                light_to_poll = None
                with self.lock:
                    self._discard_pending_request(target, cancel_event, already_locked=True)
                    if target in self.lights:
                        light = self.lights[target]
                        color_set_time = getattr(light, 'color_set_time', 0)
                        color_set_count = getattr(light, 'color_set_count', 0)
                        time_since_set = time.time() - color_set_time
                        if time_since_set >= fade_time - 0.1 and color_set_count <= 2:
                            light.state_requested_time = time.time()
                            light_to_poll = light
                if light_to_poll is not None:
                    self._request_light_state(light_to_poll)
            except Exception as e:
                try:
                    target_str = target.hex() if target else "unknown"
                except Exception as fmt_err:
                    target_str = str(target) if target else "unknown"
                    print(
                        f"Warning: request_state_after_fade could not hex-format target ({fmt_err!r}); "
                        f"using target_str={target_str!r}",
                        file=sys.stderr,
                    )
                print(f"Error in request_state_after_fade for target {target_str}: {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                try:
                    self._discard_pending_request(target, cancel_event)
                except Exception as cleanup_err:
                    print(
                        f"Warning: request_state_after_fade pending_state_requests cleanup failed "
                        f"(target={target_str!r}, cancel_event={cancel_event!r}): {cleanup_err!r}",
                        file=sys.stderr,
                    )

        threading.Thread(target=request_state_after_fade, daemon=True).start()

    def _discard_pending_request(
        self,
        target: Optional[bytes],
        cancel_event: threading.Event,
        already_locked: bool = False,
    ) -> None:
        """Remove a pending state request only if it still refers to this cancel_event."""
        if not target:
            return

        def _drop() -> None:
            if self.pending_state_requests.get(target) is cancel_event:
                del self.pending_state_requests[target]

        if already_locked:
            _drop()
            return
        with self.lock:
            _drop()

    def set_power(self, target: bytes, ip: str, power: bool):
        """Set power state for a specific light"""
        header = self._build_header(SET_POWER, target=target, tagged=False)
        power_value = 65535 if power else 0
        payload = struct.pack("<H", power_value)
        packet = self._finalise(header + payload)

        self._rate_limit()
        self.sock.sendto(packet, (ip, LIFX_PORT))

    def close(self):
        """Close the client and cleanup"""
        self.listening = False
        self.batch_running = False
        self._queue_wake.set()
        
        # Cancel all pending state requests
        with self.lock:
            for cancel_event in self.pending_state_requests.values():
                cancel_event.set()
            self.pending_state_requests.clear()
        
        # Stop batch thread
        if self.batch_thread and self.batch_thread.is_alive():
            self.batch_thread.join(timeout=1.0)
        
        # Shutdown thread pool
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=True)
        
        if self.sock:
            self.sock.close()

