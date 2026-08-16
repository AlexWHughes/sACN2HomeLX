"""Nanoleaf model codes from the Open API mDNS TXT `md` field."""

from typing import Literal

PRODUCT_NAMES = {
    'NL22': 'Light Panels',
    'NL29': 'Canvas',
    'NL42': 'Shapes',
    'NL45': 'Blocks',
    'NL52': 'Elements',
    'NL59': 'Lines',
    'NL64': 'Essentials',
    'NL67': '4D',
    'NL69': 'Skylight',
}

# Light Panels (Aurora) use stream control v1 (1-byte panel IDs).
# Canvas, Shapes, Elements, Lines, and later products use v2.
STREAM_V1_MODELS = frozenset({'NL22'})

# Layout positionData shapeType values that are not light-emitting panels.
NON_LIGHT_SHAPE_TYPES = frozenset({
    1,   # Rhythm module
    12,  # Shapes controller
    16,  # Lines connector
    19,  # Controller cap
    20,  # Power connector
})

StreamVersion = Literal['v1', 'v2']


def product_name(model: str) -> str:
    """Human-readable name for a Nanoleaf model code. Empty codes stay empty."""
    code = (model or '').strip().upper()
    if not code:
        return ''
    return PRODUCT_NAMES.get(code, code)


def stream_version_for_model(model: str) -> StreamVersion:
    """UDP external-control protocol version for a model code."""
    code = (model or '').strip().upper()
    if code in STREAM_V1_MODELS:
        return 'v1'
    return 'v2'
