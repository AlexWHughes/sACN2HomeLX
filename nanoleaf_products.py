"""Nanoleaf model codes from the Open API mDNS TXT `md` field."""

from typing import Dict, Iterable, List, Literal, NamedTuple, Sequence

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
    'NL81': 'Blocks',
}

# Light Panels (Aurora) use stream control v1 (1-byte panel IDs).
# Canvas, Shapes, Elements, Lines, and later products use v2.
STREAM_V1_MODELS = frozenset({'NL22'})

ShapeKind = Literal['triangle', 'hex', 'square', 'line']
StreamVersion = Literal['v1', 'v2']


class ShapeSpec(NamedTuple):
    label: str
    kind: ShapeKind
    side: int
    light: bool


# Canonical side lengths from Open API chapter 3.3. layout.sideLength is
# deprecated (often 0) for Connect+ mixed layouts; infer size from shapeType.
SHAPE_TYPES: Dict[int, ShapeSpec] = {
    0: ShapeSpec('Light Panels', 'triangle', 150, True),
    1: ShapeSpec('Rhythm', 'triangle', 0, False),
    2: ShapeSpec('Canvas', 'square', 100, True),
    3: ShapeSpec('Canvas', 'square', 100, True),
    4: ShapeSpec('Canvas', 'square', 100, True),
    7: ShapeSpec('Shapes: Hexagon', 'hex', 67, True),
    8: ShapeSpec('Shapes: Triangle', 'triangle', 134, True),
    9: ShapeSpec('Shapes: Mini Triangle', 'triangle', 67, True),
    12: ShapeSpec('Shapes Controller', 'triangle', 0, False),
    14: ShapeSpec('Elements: Hexagon', 'hex', 134, True),
    15: ShapeSpec('Elements: Corner', 'hex', 58, True),
    16: ShapeSpec('Lines Connector', 'line', 11, False),
    17: ShapeSpec('Lines', 'line', 154, True),
    18: ShapeSpec('Lines', 'line', 77, True),
    19: ShapeSpec('Controller Cap', 'line', 11, False),
    20: ShapeSpec('Power Connector', 'line', 11, False),
    29: ShapeSpec('4D', 'line', 50, True),
    30: ShapeSpec('Skylight', 'square', 180, True),
    31: ShapeSpec('Skylight', 'square', 180, True),
    32: ShapeSpec('Skylight', 'square', 180, True),
}

NON_LIGHT_SHAPE_TYPES = frozenset(
    shape_id for shape_id, spec in SHAPE_TYPES.items() if not spec.light
)

_MODEL_KIND: Dict[str, ShapeKind] = {
    'NL29': 'square',
    'NL45': 'square',
    'NL59': 'line',
    'NL67': 'line',
    'NL81': 'square',
}

_MODEL_DEFAULT_SIDE: Dict[str, int] = {
    'NL45': 134,
    'NL59': 154,
    'NL81': 134,
}


def _model_code(model: str) -> str:
    return (model or '').strip().upper()


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def product_name(model: str) -> str:
    """Human-readable name for a Nanoleaf model code. Empty codes stay empty."""
    code = _model_code(model)
    if not code:
        return ''
    return PRODUCT_NAMES.get(code, code)


def stream_version_for_model(model: str) -> StreamVersion:
    """UDP external-control protocol version for a model code."""
    if _model_code(model) in STREAM_V1_MODELS:
        return 'v1'
    return 'v2'


def shape_kind(shape_type, model: str = '') -> ShapeKind:
    """Draw kind for a layout shapeType, with a model fallback for unknown ids."""
    spec = SHAPE_TYPES.get(_as_int(shape_type))
    if spec:
        return spec.kind
    return _MODEL_KIND.get(_model_code(model), 'triangle')


def shape_side_length(shape_type, fallback: int = 0, model: str = '') -> int:
    """Canonical panel side in layout units, or a reported/model fallback."""
    spec = SHAPE_TYPES.get(_as_int(shape_type))
    if spec and spec.side > 0:
        return spec.side
    if fallback > 0:
        return fallback
    return _MODEL_DEFAULT_SIDE.get(_model_code(model), 100)


def infer_side_length(layout, panels: Sequence[dict], model: str = '', current: int = 100) -> int:
    """Prefer the controller's sideLength; if it is 0, infer from the first panel."""
    try:
        reported = int((layout or {}).get('sideLength') or 0)
    except (TypeError, ValueError):
        reported = 0
    if reported > 0:
        return reported
    for panel in panels:
        side = shape_side_length(panel.get('shapeType', 0), 0, model)
        if side > 0:
            return side
    return current if current > 0 else 100


def layout_product_name(model: str, shape_types: Iterable) -> str:
    """Product name refined by the light-emitting panels actually present."""
    labels: List[str] = []
    for raw in shape_types:
        spec = SHAPE_TYPES.get(_as_int(raw))
        if spec and spec.light:
            labels.append(spec.label)
    unique = list(dict.fromkeys(labels))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        families = {label.split(':', 1)[0].strip() for label in unique}
        if len(families) == 1:
            return f'{next(iter(families))}: Mixed'
        return 'Mixed'
    family = product_name(model)
    if family == 'Blocks':
        return 'Blocks: Squares'
    return family
