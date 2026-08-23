"""Constant-folding a UE3 material expression graph.

The rest of `material.py` asks "which texture does this material draw?". This
asks the other question -- "what does the graph compute when no texture is
involved?" -- and it matters because a great many of UT3's non-opaque materials
have no texture in their colour path at all. `M_EV_FogSheet_Master_01` states
its EmissiveColor as `VectorParameter "Color" * VectorParameter "Color"` and
nothing else; the whole appearance of a goo pit is one instance overriding that
parameter with green. Resolve it as "a texture" and you get whatever turns up
in the opacity chain, drawn white.

Only nodes that can be folded are folded. Anything else -- a texture sample, a
Fresnel, a CameraVector -- returns None and the caller falls back to what it
did before. That is the whole safety story: this never guesses, it either
evaluates the graph exactly or declines.

Values are 4-tuples in UE3's linear colour space. Converting to a UT2004 byte
colour is `linear_to_srgb`, because UE3 authors parameters linearly and gamma-
corrects on output while UE2 stores what it displays.
"""

import struct

from ..props import Struct, read_object_properties

# Nodes whose value is a constant stated on the node itself.
_CONSTANTS = {
    "MaterialExpressionConstant": ("R",),
    "MaterialExpressionConstant2Vector": ("R", "G"),
    "MaterialExpressionConstant3Vector": ("R", "G", "B"),
    "MaterialExpressionConstant4Vector": ("R", "G", "B", "A"),
}
MAX_DEPTH = 16





def _expression(value):
    if isinstance(value, Struct) and value.value is not None and hasattr(value.value, "get"):
        return value.value.get("Expression")
    return None


def _mask(value):
    """The component mask an input carries, as indices, or None for all of it.

    A UE3 material input is a struct that can select channels of whatever it is
    wired to: `Mask` turns it on and MaskR/G/B/A say which. A one-channel mask
    is how a graph pulls a scalar out of a colour.
    """
    if not isinstance(value, Struct) or value.value is None or not hasattr(value.value, "get"):
        return None
    inner = value.value
    if not inner.get("Mask"):
        return None
    picked = [i for i, key in enumerate(("MaskR", "MaskG", "MaskB", "MaskA"))
              if inner.get(key)]
    return picked or None


def _as_tuple(value, size=4, fill=0.0):
    if value is None:
        return None
    out = list(value[:size])
    while len(out) < size:
        out.append(fill)
    return tuple(float(v) for v in out)


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Parameters:
    """Effective parameter values for a material instance chain.

    Leaf first: an instance that restates a parameter wins over the parent it
    inherits everything else from, which is how UT3 builds a family of goo,
    steam and smoke sheets out of one master material.
    """

    def __init__(self):
        self.scalars = {}
        self.vectors = {}
        self.switches = {}
        # parameter name -> channel index (0..3) for R, G, B, A
        self.masks = {}

    def __repr__(self):
        return "Parameters(%d scalar, %d vector, %d switch)" % (
            len(self.scalars), len(self.vectors), len(self.switches))


# One FStaticSwitchParameter: FName, its value, whether this instance set it,
# and the expression GUID. The mask array that follows has the same shape with
# four channel flags in place of the value.
_SWITCH_STRIDE = 8 + 4 + 4 + 16
_MASK_STRIDE = 8 + 16 + 4 + 16


def read_static_parameters(pkg, export):
    """(switches, masks) from a material instance's static parameter set.

    `masks` maps a StaticComponentMaskParameter's name to the channel index it
    selects -- 0..3 for R, G, B, A -- which is how a UE3 material says which
    channel of a texture drives something. TOXIKK's two-tone panels state it as
    `DiffuseColorMaskChannel`.
    """
    return _read_static(pkg, export)


def read_static_switches(pkg, export):
    """{parameter name: value} from a material instance's static parameter set.

    UE3 does not put these in the tagged property list. An instance says
    `bHasStaticPermutationResource=True` and then writes an
    `FStaticParameterSet` into the native data after its properties, which is
    why `collect_parameters` used to come back with no switches at all and
    every static branch was evaluated at the *master's* default.

    That matters because a static switch is not a runtime branch -- UE3
    compiles one side into the shader and discards the other -- so getting it
    wrong means reading a texture out of a branch the material never samples.

    Found by scanning rather than by offset: the set sits after an
    FMaterialResource whose layout this reader does not know, so the switch
    array is located by shape and confirmed by the component-mask array that
    must follow it. Both constraints together are strong -- every element has
    to carry a valid FName with number zero and flags that are only ever 0 or 1.

    Checked against UDK's own browser for
    `M_TechPanel_simple_FloorPanels_RUNTIME_MainHallLow_INST`: 33 switches and
    4 masks, with `bUseBoxCorrectedCubemap` and `bUseCubemapCorrection` the two
    the instance overrides and every other value agreeing.
    """
    from ..props import read_object_properties

    return _read_static(pkg, export)[0]


def _read_static(pkg, export):
    cached = getattr(export, "_static_parameters", None)
    if cached is not None:
        return cached
    result = ({}, {})
    try:
        data = pkg.export_data(export)
        _props, start, end = read_object_properties(pkg, export)
    except (ValueError, IndexError, KeyError, struct.error):
        return result
    if start is not None and end is not None and end < len(data):
        result = _scan_static_parameters(pkg, data[end:])
    try:
        export._static_parameters = result
    except AttributeError:
        pass
    return result


def _array_at(pkg, tail, off, stride, flags):
    """(entries, end offset) for an array of parameters at `off`, or None."""
    n = len(tail)
    if off + 4 > n:
        return None
    count = struct.unpack_from("<i", tail, off)[0]
    if not (0 <= count <= 512) or off + 4 + count * stride > n:
        return None
    out = []
    for k in range(count):
        base = off + 4 + k * stride
        name_index, number = struct.unpack_from("<ii", tail, base)
        if not (0 <= name_index < len(pkg.names)) or number != 0:
            return None
        values = struct.unpack_from("<%di" % flags, tail, base + 8)
        if any(v not in (0, 1) for v in values):
            return None
        out.append((pkg.names[name_index], values))
    return out, off + 4 + count * stride


def _scan_static_parameters(pkg, tail):
    for off in range(0, max(0, len(tail) - 4), 4):
        found = _array_at(pkg, tail, off, _SWITCH_STRIDE, 2)
        if not found or not found[0]:
            continue
        # The mask array has to follow, which is what makes this safe: a run of
        # bytes that merely looks like one array almost never has a second,
        # differently shaped one immediately after it.
        masks = _array_at(pkg, tail, found[1], _MASK_STRIDE, 5)
        if masks is None:
            continue
        channels = {}
        for name, values in masks[0]:
            picked = [i for i in range(4) if values[i]]
            if len(picked) == 1:
                channels[name] = picked[0]
        return {name: bool(values[0]) for name, values in found[0]}, channels
    return {}, {}


def collect_parameters(pkg, index, ref, depth=12, into=None):
    """Every scalar, vector and static switch an instance chain overrides."""
    params = into if into is not None else Parameters()
    if depth <= 0 or ref is None or ref.is_null:
        return params
    owner, export = index.resolve(pkg, ref)
    if export is None:
        return params
    if owner.class_name_of(export) not in ("MaterialInstanceConstant",
                                           "MaterialInstanceTimeVarying"):
        return params
    props, start, _end = read_object_properties(owner, export)
    if start is None:
        return params

    array = props.get("ScalarParameterValues")
    if array is not None and len(array):
        for entry in array.as_props():
            name = str(entry.get("ParameterName", ""))
            value = _number(entry.get("ParameterValue"))
            if name and value is not None:
                params.scalars.setdefault(name, value)
    array = props.get("VectorParameterValues")
    if array is not None and len(array):
        for entry in array.as_props():
            name = str(entry.get("ParameterName", ""))
            value = entry.get("ParameterValue")
            if name and isinstance(value, Struct) and value.value is not None:
                params.vectors.setdefault(name, _as_tuple(value.value, 4, 1.0))
    # Static switches usually live in native data after the properties rather
    # than beside them -- see read_static_switches. The tagged form is kept as
    # well, since a package that does write it costs nothing to read.
    statics = props.get("StaticParameters")
    if isinstance(statics, Struct) and statics.value is not None \
            and hasattr(statics.value, "get"):
        array = statics.value.get("StaticSwitchParameters")
        if array is not None and len(array):
            for entry in array.as_props():
                name = str(entry.get("ParameterName", ""))
                if name:
                    params.switches.setdefault(name, entry.get("Value") is True)
    if props.get("bHasStaticPermutationResource") is True:
        found_switches, found_masks = read_static_parameters(owner, export)
        for name, value in found_switches.items():
            params.switches.setdefault(name, value)
        for name, channel in found_masks.items():
            params.masks.setdefault(name, channel)

    parent = props.get("Parent")
    if parent is not None and not parent.is_null:
        collect_parameters(owner, index, parent, depth - 1, params)
    return params


def fold(pkg, index, ref, params=None, depth=MAX_DEPTH):
    """Evaluate an expression to a constant (r, g, b, a), or None.

    None means "this graph does something UE2 cannot be told about", which is
    the answer for anything sampling a texture or reading the camera.

    An *opacity* chain will almost never fold, and that is the correct answer
    rather than a gap. UT3's fog sheets drive opacity from PixelDepth through a
    DotProduct and a Divide -- a per-pixel depth fade with no UE2 counterpart
    at all. Standing those nodes at their strongest and folding anyway was
    tried: `M_EV_FogSheet_Master_01` came out at 0.0002, because the depth
    arithmetic is in world units and there is no sensible depth to put in.
    What converts is the *colour*, and the texture supplies the shape.
    """
    if depth <= 0 or ref is None or ref.is_null:
        return None
    owner, export = index.resolve(pkg, ref)
    if export is None:
        return None
    return _fold_export(owner, index, export, params or Parameters(), depth)


def _input(pkg, index, props, key, params, depth):
    """Fold one named input, applying any component mask it carries."""
    value = props.get(key)
    ref = _expression(value)
    if ref is None or ref.is_null:
        return None
    got = fold(pkg, index, ref, params, depth - 1)
    if got is None:
        return None
    picked = _mask(value)
    if picked:
        # A masked input broadcasts its single channel, which is what a scalar
        # wired out of a colour does in UE3.
        if len(picked) == 1:
            v = got[picked[0]]
            return (v, v, v, v)
        # Padded opaque, not with zero: a colour input masked to RGB has no
        # alpha of its own, and zero would multiply the alpha out of whatever
        # it is combined with. `M_EV_FogSheet_Master_01` is Color.rgb * Color.a
        # and needs the rgb side to leave the alpha alone.
        return _as_tuple([got[i] for i in picked], 4, 1.0)
    return got


def _fold_export(pkg, index, export, params, depth):
    cls = pkg.class_name_of(export)
    props, start, _end = read_object_properties(pkg, export)
    if start is None:
        return None

    if cls in _CONSTANTS:
        keys = _CONSTANTS[cls]
        values = [_number(props.get(k, 0.0)) or 0.0 for k in keys]
        # A scalar constant broadcasts; a colour constant is opaque by default.
        if len(values) == 1:
            return (values[0],) * 4
        return _as_tuple(values, 4, 1.0)

    if cls in ("MaterialExpressionScalarParameter",
               "MaterialExpressionStaticComponentMaskParameter"):
        name = str(props.get("ParameterName", ""))
        value = params.scalars.get(name)
        if value is None:
            value = _number(props.get("DefaultValue"))
        if value is None:
            return None
        return (value,) * 4

    if cls == "MaterialExpressionVectorParameter":
        name = str(props.get("ParameterName", ""))
        value = params.vectors.get(name)
        if value is None:
            default = props.get("DefaultValue")
            if isinstance(default, Struct) and default.value is not None:
                value = _as_tuple(default.value, 4, 1.0)
        return value

    if cls == "MaterialExpressionStaticSwitchParameter":
        name = str(props.get("ParameterName", ""))
        on = params.switches.get(name)
        if on is None:
            on = props.get("DefaultValue") is True
        return _input(pkg, index, props, "A" if on else "B", params, depth)

    if cls in ("MaterialExpressionMultiply", "MaterialExpressionAdd",
               "MaterialExpressionSubtract", "MaterialExpressionDivide"):
        a = _input(pkg, index, props, "A", params, depth)
        b = _input(pkg, index, props, "B", params, depth)
        # UE3 lets either side be a constant stated on the node itself.
        if a is None:
            a = _const_operand(props, "ConstA")
        if b is None:
            b = _const_operand(props, "ConstB")
        if a is None or b is None:
            return None
        if cls == "MaterialExpressionMultiply":
            return tuple(a[i] * b[i] for i in range(4))
        if cls == "MaterialExpressionAdd":
            return tuple(a[i] + b[i] for i in range(4))
        if cls == "MaterialExpressionSubtract":
            return tuple(a[i] - b[i] for i in range(4))
        if any(v == 0.0 for v in b):
            return None
        return tuple(a[i] / b[i] for i in range(4))

    if cls == "MaterialExpressionAbs":
        a = _input(pkg, index, props, "Input", params, depth)
        return None if a is None else tuple(abs(v) for v in a)

    if cls in ("MaterialExpressionMin", "MaterialExpressionMax"):
        a = _input(pkg, index, props, "A", params, depth)
        b = _input(pkg, index, props, "B", params, depth)
        if a is None:
            a = _const_operand(props, "ConstA")
        if b is None:
            b = _const_operand(props, "ConstB")
        if a is None or b is None:
            return None
        pick = min if cls == "MaterialExpressionMin" else max
        return tuple(pick(a[i], b[i]) for i in range(4))

    if cls == "MaterialExpressionOneMinus":
        a = _input(pkg, index, props, "Input", params, depth)
        return None if a is None else tuple(1.0 - v for v in a)

    if cls == "MaterialExpressionComponentMask":
        return _input(pkg, index, props, "Input", params, depth)

    if cls in ("MaterialExpressionClamp", "MaterialExpressionConstantClamp"):
        a = _input(pkg, index, props, "Input", params, depth)
        if a is None:
            return None
        low = _input(pkg, index, props, "Min", params, depth)
        high = _input(pkg, index, props, "Max", params, depth)
        lo = low[0] if low is not None else (_number(props.get("MinDefault")) or 0.0)
        hi = high[0] if high is not None else (_number(props.get("MaxDefault")) or 1.0)
        return tuple(min(max(v, lo), hi) for v in a)

    if cls == "MaterialExpressionPower":
        base = _input(pkg, index, props, "Base", params, depth)
        if base is None:
            return None
        exponent = _input(pkg, index, props, "Exponent", params, depth)
        e = exponent[0] if exponent is not None else \
            (_number(props.get("ConstExponent")) or 1.0)
        return tuple(max(v, 0.0) ** e for v in base)

    if cls == "MaterialExpressionLinearInterpolate":
        a = _input(pkg, index, props, "A", params, depth)
        b = _input(pkg, index, props, "B", params, depth)
        alpha = _input(pkg, index, props, "Alpha", params, depth)
        if a is None or b is None or alpha is None:
            return None
        return tuple(a[i] + (b[i] - a[i]) * alpha[i] for i in range(4))

    if cls == "MaterialExpressionDesaturation":
        a = _input(pkg, index, props, "Input", params, depth)
        if a is None:
            return None
        fraction = _input(pkg, index, props, "Percent", params, depth)
        f = fraction[0] if fraction is not None else 1.0
        grey = 0.30 * a[0] + 0.59 * a[1] + 0.11 * a[2]
        return tuple(a[i] + (grey - a[i]) * f if i < 3 else a[i] for i in range(4))

    return None


def _const_operand(props, key):
    value = _number(props.get(key))
    return None if value is None else (value,) * 4


def linear_to_srgb(value):
    """UE3 authors colour linearly and gamma-corrects on output; UE2 stores
    what it displays. Without this a 0.43 linear green writes as 110 and reads
    as a muddy olive rather than the colour UT3 shows."""
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    if value <= 0.0031308:
        return value * 12.92
    return 1.055 * (value ** (1.0 / 2.4)) - 0.055


def to_color(linear, alpha=1.0):
    """A folded linear colour as a UT2004 (R, G, B, A) byte tuple.

    Alpha is passed in rather than taken from the fold: a UE3 *colour* input
    carries three meaningful channels and whatever happens to be in the fourth,
    and opacity is a separate input entirely.
    """
    if linear is None:
        return None
    rgb = [max(0, min(255, int(round(linear_to_srgb(v) * 255.0)))) for v in linear[:3]]
    return tuple(rgb) + (max(0, min(255, int(round(alpha * 255.0)))),)


def find_panner(pkg, index, ref, depth=MAX_DEPTH, seen=None):
    """(SpeedX, SpeedY) of the first Panner feeding this expression, or None.

    UT3 scrolls a fog sheet, a waterfall or a conveyor with a `Panner` node on
    the texture coordinates. UT2004 has `TexPanner`, which is the same idea
    stated as a rotator and a rate, so this is one of the few parts of a UE3
    graph that converts rather than approximates.
    """
    if depth <= 0 or ref is None or ref.is_null:
        return None
    seen = seen if seen is not None else set()
    owner, export = index.resolve(pkg, ref)
    if export is None or export.index in seen:
        return None
    seen.add(export.index)
    props, start, _end = read_object_properties(owner, export)
    if start is None:
        return None
    if owner.class_name_of(export) == "MaterialExpressionPanner":
        return (_number(props.get("SpeedX")) or 0.0, _number(props.get("SpeedY")) or 0.0)
    for key, _i, _t, value in props:
        sub = _expression(value)
        if sub is None and hasattr(value, "is_null") and not value.is_null \
                and key not in ("Texture", "Material", "Parent"):
            sub = value
        if sub is None or sub.is_null:
            continue
        found = find_panner(owner, index, sub, depth - 1, seen)
        if found is not None:
            return found
    return None


# Wrappers on the *spine* of an opacity chain -- the nodes between the
# material's Opacity input and the product underneath it. Each is followed to
# reach that product and for no other purpose:
#
#   StaticSwitchParameter  picking the live branch is exact -- the other side
#                          is not compiled into the shader at all.
#   DepthBiasedAlpha       UE2 has no depth-biased alpha, so the alpha under it
#                          is the whole of what survives (see `fold`).
#   Clamp / ConstantClamp  the outermost clamp of an opacity chain caps the
#                          result at 1; the shape under it already reaches 1 at
#                          most, so a factor pulled out from under it is exact.
#
# The Clamp entry is *only* safe on the spine, and that restriction is the
# whole design. `M_UN_Volumetrics_Lightbeam_Cheap_02` also clamps deep inside
# its product, around `PixelDepth * 0.0025` -- a depth ramp deliberately
# saturated at 1. Treating that clamp as transparent and taking the 0.0025
# gives a scale of 0.000125 and an invisible beam. Once the walk is inside the
# product it descends nothing but Multiply.
_SPINE = {
    "MaterialExpressionDepthBiasedAlpha": "Alpha",
    "MaterialExpressionClamp": "Input",
    "MaterialExpressionConstantClamp": "Input",
}


def constant_scale(pkg, index, ref, params=None, depth=MAX_DEPTH):
    """The product of the constant factors multiplying into an expression.

    `fold` asks whether a whole graph is constant, and for an opacity chain the
    answer is almost always no -- UT3 drives those from PixelDepth and texture
    samples. But the *level* usually is constant, stated as one scalar
    parameter multiplied into the product:

        Opacity = DepthBiasedAlpha( shape * ScalarParameter"Opacity" )

    and that scalar is the difference between a light beam and a searchlight.
    `M_UN_Volumetrics_Lightbeam_Cheap_02_Windows` sets it to 0.05, the fog
    sheets to 0.65, `M_EV_Lightbeam_Master_01_INST` to 0.1. Dropping it drew
    every one of them at full strength.

    Two phases. The spine (`_SPINE`) is followed down to the product; then the
    product's Multiply tree is descended, folding whichever sides are constant
    and multiplying what comes back. What it returns is exact: the product of
    the factors that genuinely fold, with the texture and the depth term left
    alone to supply the shape. 1.0 when there are none.
    """
    if depth <= 0 or ref is None or ref.is_null:
        return 1.0
    owner, export = index.resolve(pkg, ref)
    if export is None:
        return 1.0
    params = params or Parameters()
    props, start, _end = read_object_properties(owner, export)
    if start is None:
        return 1.0
    cls = owner.class_name_of(export)

    if cls == "MaterialExpressionStaticSwitchParameter":
        name = str(props.get("ParameterName", ""))
        on = params.switches.get(name)
        if on is None:
            on = props.get("DefaultValue") is True
        return _spine_of(owner, index, props, "A" if on else "B", params, depth)

    if cls in _SPINE:
        return _spine_of(owner, index, props, _SPINE[cls], params, depth)

    if cls == "MaterialExpressionMultiply":
        return _product_scale(owner, index, export, props, params, depth)

    # Not a product and not on the way to one: if the whole thing folds it *is*
    # the level, otherwise there is no constant here to take.
    whole = _fold_export(owner, index, export, params, depth)
    return 1.0 if whole is None else whole[0]


def _product_scale(pkg, index, export, props, params, depth):
    """The constant factors of one Multiply and everything multiplied into it."""
    a = _input(pkg, index, props, "A", params, depth)
    b = _input(pkg, index, props, "B", params, depth)
    if a is not None and b is not None:
        return a[0] * b[0]
    if a is not None:
        return a[0] * _factor_in(pkg, index, props, "B", params, depth)
    if b is not None:
        return b[0] * _factor_in(pkg, index, props, "A", params, depth)
    # Neither side is constant, but one of them may be a product with a
    # constant deeper inside. `M_EV_Lightbeam_Master_01` puts its Opacity
    # scalar one level down, which is why stopping here missed it entirely.
    return (_factor_in(pkg, index, props, "A", params, depth)
            * _factor_in(pkg, index, props, "B", params, depth))


def _factor_in(pkg, index, props, key, params, depth):
    """Continue the product through `key`, if what is there is another Multiply.

    Nothing else is entered. Inside the product a Clamp is a saturating depth
    ramp rather than a level, and a texture sample is shape.
    """
    if depth <= 0:
        return 1.0
    ref = _expression(props.get(key))
    if ref is None or ref.is_null:
        return 1.0
    owner, export = index.resolve(pkg, ref)
    if export is None or owner.class_name_of(export) != "MaterialExpressionMultiply":
        return 1.0
    node, start, _end = read_object_properties(owner, export)
    if start is None:
        return 1.0
    return _product_scale(owner, index, export, node, params, depth - 1)


def _spine_of(pkg, index, props, key, params, depth):
    ref = _expression(props.get(key))
    if ref is None or ref.is_null:
        return 1.0
    return constant_scale(pkg, index, ref, params, depth - 1)
