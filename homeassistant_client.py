"""Home Assistant REST client: discover light entities and push RGB/brightness."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

DEFAULT_HTTP_TIMEOUT = 5.0
DEFAULT_BATCH_INTERVAL_S = 0.1
MIN_TRANSITION_S = 0.0
MAX_TRANSITION_S = 5.0

# Colour modes that accept an RGB (or convertible) payload via light.turn_on.
RGB_CAPABLE_MODES = frozenset({
    'rgb',
    'rgbw',
    'rgbww',
    'hs',
    'xy',
})
COLOR_TEMP_MODES = frozenset({'color_temp', 'color_temp_kelvin'})
BRIGHTNESS_MODES = frozenset({'brightness'}) | RGB_CAPABLE_MODES | COLOR_TEMP_MODES


class HomeAssistantError(Exception):
    """HTTP or protocol error talking to Home Assistant."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class HomeAssistantLight:
    """One Home Assistant ``light.*`` entity."""

    vendor = 'homeassistant'

    def __init__(
        self,
        entity_id: str,
        label: str = '',
        state: str = 'unknown',
        supported_color_modes: Optional[Sequence[str]] = None,
        brightness: Optional[int] = None,
        rgb_color: Optional[Tuple[int, int, int]] = None,
        color_temp_kelvin: Optional[int] = None,
        ha_host: str = '',
    ):
        cleaned = ha_light_id(entity_id)
        self.id = cleaned
        self.entity_id = entity_id_from_ha_id(cleaned)
        self.label = label or self.entity_id
        self.state = state
        self.supported_color_modes: List[str] = [
            str(mode) for mode in (supported_color_modes or []) if mode
        ]
        self.brightness = brightness
        self.rgb_color = rgb_color
        self.color_temp_kelvin = color_temp_kelvin
        self.ha_host = ha_host
        self.ip = ha_host or self.entity_id
        self.last_seen = time.time()

    @property
    def model_name(self) -> str:
        modes = {mode.lower() for mode in self.supported_color_modes}
        if not modes:
            return 'HA light'
        if modes & RGB_CAPABLE_MODES:
            return 'HA RGB light'
        if modes & COLOR_TEMP_MODES:
            return 'HA color-temp light'
        if modes & BRIGHTNESS_MODES:
            return 'HA brightness light'
        if 'onoff' in modes:
            return 'HA on/off light'
        return 'HA light'

    @property
    def paired(self) -> bool:
        return True

    @property
    def zone_capable(self) -> bool:
        return False

    @property
    def zone_count(self) -> int:
        return 1

    @property
    def matrix_width(self) -> int:
        return 1

    @property
    def matrix_height(self) -> int:
        return 1

    @property
    def layout(self) -> str:
        return 'single'

    @property
    def effective_layout(self) -> str:
        return 'single'

    @property
    def panel_ids(self) -> List[int]:
        return []

    @property
    def panel_layout(self) -> List[Dict]:
        return []

    @property
    def supports_rgb(self) -> bool:
        modes = {mode.lower() for mode in self.supported_color_modes}
        if not modes:
            # Unknown capability — try RGB; HA ignores unsupported fields.
            return True
        return bool(modes & RGB_CAPABLE_MODES)

    @property
    def supports_color_temp(self) -> bool:
        modes = {mode.lower() for mode in self.supported_color_modes}
        return bool(modes & COLOR_TEMP_MODES)

    @property
    def supports_brightness(self) -> bool:
        modes = {mode.lower() for mode in self.supported_color_modes}
        if not modes:
            return True
        return bool(modes & BRIGHTNESS_MODES) or self.supports_rgb or self.supports_color_temp

    def __repr__(self) -> str:
        return (
            f"HomeAssistantLight(id={self.id!r}, label={self.label!r}, "
            f"state={self.state!r})"
        )


def ha_light_id(entity_id: str) -> str:
    """Stable mapping id: ``ha_`` plus the Home Assistant entity_id."""
    cleaned = (entity_id or '').strip()
    if cleaned.startswith('ha_'):
        return cleaned
    return f'ha_{cleaned}' if cleaned else 'ha_light.unknown'


def entity_id_from_ha_id(light_id: str) -> str:
    cleaned = (light_id or '').strip()
    if cleaned.startswith('ha_'):
        return cleaned[3:]
    return cleaned


def normalize_base_url(url: str) -> str:
    """Strip trailing slash and require an http(s) scheme."""
    cleaned = (url or '').strip().rstrip('/')
    if not cleaned:
        return ''
    parsed = urlparse(cleaned)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise HomeAssistantError(
            'Home Assistant URL must be an absolute http(s) URL '
            '(e.g. http://homeassistant.local:8123)'
        )
    return cleaned


def host_from_url(url: str) -> str:
    try:
        cleaned = normalize_base_url(url)
    except HomeAssistantError:
        return ''
    return urlparse(cleaned).netloc or ''


def _rgb8(r: float, g: float, b: float, brightness: float) -> Tuple[int, int, int]:
    scale = max(0.0, min(1.0, brightness))
    return (
        max(0, min(255, int(round(r * scale * 255)))),
        max(0, min(255, int(round(g * scale * 255)))),
        max(0, min(255, int(round(b * scale * 255)))),
    )


def _brightness8(r: float, g: float, b: float, brightness: float) -> int:
    red, green, blue = _rgb8(r, g, b, brightness)
    return max(red, green, blue)


def light_from_state(state: dict, ha_host: str = '') -> Optional[HomeAssistantLight]:
    """Build a light from a Home Assistant ``/api/states`` entry."""
    if not isinstance(state, dict):
        return None
    entity_id = state.get('entity_id')
    if not isinstance(entity_id, str) or not entity_id.startswith('light.'):
        return None
    attributes = state.get('attributes') or {}
    if not isinstance(attributes, dict):
        attributes = {}
    friendly = attributes.get('friendly_name')
    label = str(friendly).strip() if isinstance(friendly, str) and friendly.strip() else entity_id
    modes_raw = attributes.get('supported_color_modes') or attributes.get('supported_features')
    modes: List[str] = []
    if isinstance(modes_raw, (list, tuple, set)):
        for mode in modes_raw:
            if isinstance(mode, str) and mode:
                modes.append(mode)
    brightness = attributes.get('brightness')
    try:
        bri_int = int(brightness) if brightness is not None else None
    except (TypeError, ValueError):
        bri_int = None
    rgb = attributes.get('rgb_color')
    rgb_tuple: Optional[Tuple[int, int, int]] = None
    if isinstance(rgb, (list, tuple)) and len(rgb) >= 3:
        try:
            rgb_tuple = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        except (TypeError, ValueError):
            rgb_tuple = None
    kelvin = attributes.get('color_temp_kelvin')
    if kelvin is None:
        kelvin = attributes.get('color_temp')
    try:
        kelvin_int = int(kelvin) if kelvin is not None else None
    except (TypeError, ValueError):
        kelvin_int = None
    return HomeAssistantLight(
        entity_id=entity_id,
        label=label,
        state=str(state.get('state') or 'unknown'),
        supported_color_modes=modes,
        brightness=bri_int,
        rgb_color=rgb_tuple,
        color_temp_kelvin=kelvin_int,
        ha_host=ha_host,
    )


class HomeAssistantClient:
    """Discover and control Home Assistant lights over the REST API."""

    def __init__(
        self,
        base_url: str = '',
        token: str = '',
        timeout: float = DEFAULT_HTTP_TIMEOUT,
    ):
        self._lock = threading.Lock()
        self.lights: Dict[str, HomeAssistantLight] = {}
        self.timeout = float(timeout)
        self._base_url = ''
        self._token = ''
        self.configure(base_url, token)

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._token)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def token_configured(self) -> bool:
        return bool(self._token)

    @property
    def ha_host(self) -> str:
        return host_from_url(self._base_url)

    def configure(self, base_url: str = '', token: str = '') -> None:
        url = (base_url or '').strip()
        tok = (token or '').strip()
        if url:
            self._base_url = normalize_base_url(url)
        else:
            self._base_url = ''
        self._token = tok

    def close(self) -> None:
        with self._lock:
            self.lights.clear()

    def get_lights(self) -> List[HomeAssistantLight]:
        with self._lock:
            return list(self.lights.values())

    def get_light(self, light_id: str) -> Optional[HomeAssistantLight]:
        lid = ha_light_id(light_id)
        with self._lock:
            found = self.lights.get(lid)
            if found is not None:
                return found
            entity = entity_id_from_ha_id(lid)
            for light in self.lights.values():
                if light.entity_id == entity:
                    return light
        return None

    def remember(self, light: HomeAssistantLight) -> HomeAssistantLight:
        with self._lock:
            existing = self.lights.get(light.id)
            if existing is None:
                self.lights[light.id] = light
                return light
            existing.label = light.label or existing.label
            existing.state = light.state or existing.state
            existing.supported_color_modes = list(light.supported_color_modes) or existing.supported_color_modes
            existing.brightness = light.brightness if light.brightness is not None else existing.brightness
            existing.rgb_color = light.rgb_color or existing.rgb_color
            existing.color_temp_kelvin = (
                light.color_temp_kelvin
                if light.color_temp_kelvin is not None
                else existing.color_temp_kelvin
            )
            existing.ha_host = light.ha_host or existing.ha_host
            existing.ip = existing.ha_host or existing.entity_id
            existing.last_seen = time.time()
            return existing

    def apply_saved(self, records: Dict[str, Dict]) -> None:
        """Rehydrate mapped lights that are not currently discovered."""
        host = self.ha_host
        for light_id, record in (records or {}).items():
            if not isinstance(record, dict):
                continue
            entity = record.get('entity_id') or entity_id_from_ha_id(light_id)
            if not isinstance(entity, str) or not entity.startswith('light.'):
                continue
            if self.get_light(light_id) is not None:
                continue
            label = record.get('label') or entity
            modes = record.get('supported_color_modes') or []
            light = HomeAssistantLight(
                entity_id=entity,
                label=str(label),
                state='unknown',
                supported_color_modes=modes if isinstance(modes, list) else [],
                ha_host=str(record.get('ip') or host),
            )
            self.remember(light)

    def _headers(self) -> Dict[str, str]:
        if not self._token:
            raise HomeAssistantError('Home Assistant access token is not configured')
        return {
            'Authorization': f'Bearer {self._token}',
            'Content-Type': 'application/json',
        }

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> object:
        if not self._base_url:
            raise HomeAssistantError('Home Assistant URL is not configured')
        url = f'{self._base_url}{path}'
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
        request = urllib.request.Request(
            url,
            data=data,
            headers=self._headers(),
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
                if not raw:
                    return None
                return json.loads(raw.decode('utf-8'))
        except urllib.error.HTTPError as exc:
            detail = ''
            try:
                detail = exc.read().decode('utf-8', errors='ignore')
            except Exception:
                detail = ''
            message = f'Home Assistant HTTP {exc.code} for {path}'
            if detail:
                message = f'{message}: {detail[:300]}'
            raise HomeAssistantError(message, status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise HomeAssistantError(f'Home Assistant unreachable: {exc.reason}') from exc
        except TimeoutError as exc:
            raise HomeAssistantError('Home Assistant request timed out') from exc
        except json.JSONDecodeError as exc:
            raise HomeAssistantError(f'Home Assistant returned invalid JSON for {path}') from exc

    def ping(self) -> dict:
        """GET /api/ — confirms URL + token."""
        result = self._request('GET', '/api/')
        if not isinstance(result, dict):
            raise HomeAssistantError('Unexpected /api/ response')
        return result

    def discover(self) -> List[HomeAssistantLight]:
        """Fetch all states and keep ``light.*`` entities."""
        if not self.configured:
            raise HomeAssistantError(
                'Configure Home Assistant URL and long-lived access token first'
            )
        payload = self._request('GET', '/api/states')
        if not isinstance(payload, list):
            raise HomeAssistantError('Unexpected /api/states response')
        host = self.ha_host
        lights: List[HomeAssistantLight] = []
        for entry in payload:
            light = light_from_state(entry, ha_host=host)
            if light is None:
                continue
            remembered = self.remember(light)
            lights.append(remembered)
        return lights

    def send_color(
        self,
        light: HomeAssistantLight,
        r: float,
        g: float,
        b: float,
        brightness: float = 1.0,
        kelvin: int = 3500,
        transition: Optional[float] = None,
    ) -> None:
        """Push colour/brightness via ``light.turn_on`` / ``turn_off``."""
        red, green, blue = _rgb8(r, g, b, brightness)
        bri = _brightness8(r, g, b, brightness)
        transition_s = _coerce_transition(transition)

        if bri <= 0 or (red, green, blue) == (0, 0, 0):
            body: Dict[str, object] = {'entity_id': light.entity_id}
            if transition_s is not None:
                body['transition'] = transition_s
            self._request('POST', '/api/services/light/turn_off', body)
            light.state = 'off'
            light.brightness = 0
            return

        body = {'entity_id': light.entity_id}
        if transition_s is not None:
            body['transition'] = transition_s

        if light.supports_rgb:
            body['rgb_color'] = [red, green, blue]
            body['brightness'] = bri
        elif light.supports_color_temp:
            body['brightness'] = bri
            try:
                body['color_temp_kelvin'] = int(kelvin)
            except (TypeError, ValueError):
                body['color_temp_kelvin'] = 3500
        elif light.supports_brightness:
            body['brightness'] = bri
        else:
            # on/off only
            pass

        self._request('POST', '/api/services/light/turn_on', body)
        light.state = 'on'
        light.brightness = bri
        if light.supports_rgb:
            light.rgb_color = (red, green, blue)


def _coerce_transition(value: Optional[float]) -> Optional[float]:
    if value is None:
        raw = os.getenv('HOMEASSISTANT_TRANSITION_S', '0.05')
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.05
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.05
    if parsed < MIN_TRANSITION_S:
        return MIN_TRANSITION_S
    if parsed > MAX_TRANSITION_S:
        return MAX_TRANSITION_S
    return parsed


def load_settings_from_env() -> Dict[str, str]:
    """Optional URL/token overrides from the environment."""
    url = (os.getenv('HOMEASSISTANT_URL') or '').strip()
    token = (os.getenv('HOMEASSISTANT_TOKEN') or '').strip()
    out: Dict[str, str] = {}
    if url:
        out['url'] = url
    if token:
        out['token'] = token
    return out
