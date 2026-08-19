"""Find the diffuse texture behind a UE3 material.

UE3 materials are expression graphs with no UE2 equivalent, so this walks the
graph from the diffuse input until it reaches a texture sample. Where that
fails, it falls back to the material's own texture expressions, preferring names
that do not look like normal/specular maps.
"""

from ..props import Struct, read_object_properties

DIFFUSE_INPUTS = ("DiffuseColor", "DiffusePower", "EmissiveColor", "BaseColor")
TEXTURE_EXPRESSIONS = (
    "MaterialExpressionTextureSample",
    "MaterialExpressionTextureSampleParameter2D",
    "MaterialExpressionTextureSampleParameter",
    "MaterialExpressionTextureSampleParameterMeshSubUV",
    "MaterialExpressionFlipBookSample",
)
# Expression inputs worth following when hunting for a texture.
FOLLOW_INPUTS = ("A", "B", "Input", "Texture", "Coordinates", "X", "Y")

# Single-letter channel markers, which only mean anything at the end of a name.
# Matching them anywhere is what once made a cubemap outscore a real diffuse:
# "T_UN_CubeMaps_Robot_Paint01" contains "_c" and so read as a colour map.
_SUFFIX_PENALTY = (("_n", 100), ("_s", 40), ("_m", 30), ("_e", 20), ("_g", 15))
_SUFFIX_BONUS = ("_d", "_c")

# Words, which mean the same wherever they appear. A cubemap is never the
# diffuse -- it is a reflection probe, and a material that multiplies one into
# its diffuse chain still gets its colour from a flat texture further along.
_WORD_PENALTY = (
    ("normal", 100), ("_norm", 100), ("cubemap", 200), ("cube_map", 200),
    ("falloff", 60), ("spec", 40), ("mask", 30), ("emis", 20), ("gloss", 15),
)
_WORD_BONUS = ("diff", "albedo", "color", "colour", "base")


# Markers that settle it outright. A normal map or a cubemap is never a base
# colour whatever else the name says, and letting a bonus offset the penalty is
# how CTF-LostCause's water came to be painted with T_Base_Tile_DetailNormal:
# "normal" scores +100, "base" -- part of the asset's name, not a claim about
# what it is -- took 20 back off, and 80 slipped under the threshold that
# refuses a non-colour map. A normal map drawn as diffuse renders iridescent
# blue and magenta, which is what the water sheet looked like.
_DISQUALIFY = ("normal", "_norm", "cubemap", "cube_map")
DISQUALIFIED = 1000


def score_texture_name(name):
    """Lower is better: how likely this name is the diffuse map."""
    low = name.lower()
    if any(token in low for token in _DISQUALIFY) or low.endswith("_n"):
        return DISQUALIFIED
    score = 0
    for token, penalty in _SUFFIX_PENALTY:
        if low.endswith(token):
            score += penalty
    for token in _SUFFIX_BONUS:
        if low.endswith(token):
            score -= 20
    for token, penalty in _WORD_PENALTY:
        if token in low:
            score += penalty
    for token in _WORD_BONUS:
        if token in low:
            score -= 20
    return score


def _expression_ref(value):
    """Pull the Expression object out of a material input struct."""
    if isinstance(value, Struct) and value.value is not None and hasattr(value.value, "get"):
        return value.value.get("Expression")
    return None


def _texture_of(pkg, index, export):
    """If `export` is a texture-sample expression, return its texture."""
    if pkg.class_name_of(export) not in TEXTURE_EXPRESSIONS:
        return None, None
    props, start, _end = read_object_properties(pkg, export)
    if start is None:
        return None, None
    ref = props.get("Texture")
    if ref is None or ref.is_null:
        return None, None
    owner, tex = index.resolve(pkg, ref)
    if tex is None or owner.class_name_of(tex) != "Texture2D":
        return None, None
    return owner, tex


def sample_coordinates(pkg, index, sample_export):
    """(uv_channel, u_tiling, v_tiling) that a texture sample reads.

    A UE3 sample's Coordinates input is usually empty, meaning UV channel 0
    untiled. When it is wired to a MaterialExpressionTextureCoordinate, that
    node names the channel -- and UT3 sky domes rely on it: the dome carries a
    polar map in channel 0 and a disc projection in channel 1, and its material
    asks for channel 1. Export the wrong one and the sky pinches into a fan of
    wedges at the apex, since a polar map collapses every apex vertex onto one
    line of the texture.
    """
    props, start, _end = read_object_properties(pkg, sample_export)
    if start is None:
        return 0, 1.0, 1.0
    ref = _expression_ref(props.get("Coordinates"))
    if ref is None or ref.is_null:
        return 0, 1.0, 1.0
    owner, node = index.resolve(pkg, ref)
    if node is None or owner.class_name_of(node) != "MaterialExpressionTextureCoordinate":
        return 0, 1.0, 1.0
    node_props, node_start, _e = read_object_properties(owner, node)
    if node_start is None:
        return 0, 1.0, 1.0
    try:
        channel = int(node_props.get("CoordinateIndex", 0))
    except (TypeError, ValueError):
        channel = 0
    def _tiling(key):
        try:
            return float(node_props.get(key, 1.0))
        except (TypeError, ValueError):
            return 1.0
    return channel, _tiling("UTiling"), _tiling("VTiling")


def _collect(pkg, index, export, depth, seen, found):
    """Every texture sample reachable from `export`, in graph order."""
    if depth <= 0 or export.index in seen:
        return
    seen.add(export.index)
    owner, tex = _texture_of(pkg, index, export)
    if tex is not None:
        found.append((owner, tex, export))
        return
    props, start, _end = read_object_properties(pkg, export)
    if start is None:
        return
    for key in FOLLOW_INPUTS:
        for value in props.get_all(key):
            ref = _expression_ref(value)
            if ref is None and hasattr(value, "is_null"):
                ref = value
            if ref is None or ref.is_null:
                continue
            sub_owner, sub_export = index.resolve(pkg, ref)
            if sub_export is None:
                continue
            _collect(sub_owner, index, sub_export, depth - 1, seen, found)


def _stem(name):
    """A material or texture name with its type prefix removed."""
    low = (name or "").lower()
    for prefix in ("mi_", "m_", "t_"):
        if low.startswith(prefix):
            return low[len(prefix):]
    return low


def names_the_texture(material_name, texture_name):
    """Is the material named after this texture?

    UE3 assets are named in pairs -- `M_UN_Foliage_SM_Bark01_Fresnel` is built
    on `T_UN_Foliage_SM_Bark01` -- and that is the one signal strong enough to
    beat the name scoring when a base colour carries no `_D` suffix at all. That
    material reaches two samples: its own bark, unsuffixed and scoring 0, and
    `T_UN_Foliage_SM_Tree_TilingBark_02_D`, a tiling overlay that scores -20 for
    its suffix and wins on name alone. The tree then renders in the wrong bark.

    Two things this deliberately does not match.

    **Equal names.** `M_UN_Sky_SM_Invasion2` samples both
    `T_UN_Sky_SM_Invasion2` and `T_UN_Sky_SM_CloudsSun`, and it is CloudsSun the
    dome is painted with -- through UV channel 1, which is the whole reason the
    sky does not pinch into a fan of wedges at its apex. A shared asset name
    says nothing there. Only a material named *for* a texture and then qualified
    (`..._Fresnel`) is making a claim, so a strict prefix is required.

    **A suffixed texture.** The test runs on the texture's full name with no
    suffix stripped, so `T_UN_Rock_SM_Cliffs01_D` cannot claim to be what
    `M_UN_Rock_SM_Cliffs01_Master` is named after -- it is WAR-PowerSurge's
    per-mesh relief bake, and the pixel test exists to reject it. Stripping `_D`
    first would match it and undo that. So this fires only where the base colour
    is unsuffixed, which is exactly the case the scoring cannot see.
    """
    material, texture = _stem(material_name), _stem(texture_name)
    if not material or not texture or len(texture) >= len(material):
        return False
    return material.startswith(texture) and material[len(texture)] == "_"


def _walk(pkg, index, export, depth, seen, reject=None, material_name=None):
    """The most diffuse-looking texture reachable from `export`.

    Returns (owner, texture, sample_expression) so the caller can also ask the
    sample which UV channel it reads.

    Taking the *first* texture the graph reaches is not good enough. A UE3
    diffuse chain routinely multiplies a reflection or detail term in before it
    adds the base colour, so the first sample down the "A" input is often a
    cubemap or a specular map: DM-Deck's floor master material reaches
    T_UN_CubeMaps_Robot_Paint01 two levels before the
    T_LT_Floors_BSP_Organic11_D it actually paints with. Every reachable sample
    is collected instead and the best-named one wins.
    """
    found = []
    _collect(pkg, index, export, depth, seen, found)
    if not found:
        return None, None, None
    # What the material is named after wins outright, but only among the
    # candidates the caller has not rejected -- the pixel test comes first, so a
    # relief bake cannot claim the material's name. See names_the_texture.
    if material_name:
        usable = [e for e in found if reject is None or not reject(e[0], e[1])]
        for entry in usable or found:
            if names_the_texture(material_name, entry[1].name):
                return entry
    best = min(score_texture_name(entry[1].name) for entry in found)
    tied = [e for e in found if score_texture_name(e[1].name) == best]
    # Names alone cannot separate a base colour from an overlay that is also
    # called _D: WAR-PowerSurge's cliffs multiply a tiling rock texture by a
    # per-mesh relief bake, and the bake is the one nearer the output. Where
    # the caller can tell them apart -- by looking at the pixels -- the first
    # candidate it does not reject wins; graph order decides the rest.
    if reject is not None and len(tied) > 1:
        for entry in tied:
            if not reject(entry[0], entry[1]):
                return entry
    return tied[0]


def _subobject_textures(pkg, index, material_export):
    """Every texture sampled by expressions owned by this material."""
    out = []
    for e in pkg.exports:
        if e.outer != material_export.index:
            continue
        owner, tex = _texture_of(pkg, index, e)
        if tex is not None:
            out.append((owner, tex))
    return out


# resolve_diffuse's return type is fixed by its callers, so the expression it
# landed on is left here for material_uv_channel to pick up.
_LAST_SAMPLE = [None]

MASKED_BLEND = "BLEND_Masked"
TRANSLUCENT_BLENDS = ("BLEND_Translucent", "BLEND_Additive", "BLEND_Modulate")


def material_blend_mode(pkg, index, ref, depth=6):
    """'opaque', 'masked' or 'translucent' for a UE3 material.

    The distinction matters on import: UT2004 wants MASKED=1 for a hard cutout
    (foliage, chainlink) and ALPHA=1 for genuine blending (glass). Using ALPHA
    for a cutout gives soft, badly-sorted edges instead of crisp holes.
    """
    if depth <= 0:
        return "opaque"
    owner, export = index.resolve(pkg, ref)
    if export is None:
        return "opaque"
    props, start, _end = read_object_properties(owner, export)
    if start is None:
        return "opaque"
    blend = str(props.get("BlendMode", "BLEND_Opaque"))
    if blend == MASKED_BLEND:
        return "masked"
    if blend in TRANSLUCENT_BLENDS:
        return "translucent"
    for key in ("Opacity", "OpacityMask"):
        expr = _expression_ref(props.get(key))
        if expr is not None and not expr.is_null:
            return "masked" if key == "OpacityMask" else "translucent"
    parent = props.get("Parent")
    if parent is not None and not parent.is_null:
        return material_blend_mode(owner, index, parent, depth - 1)
    return "opaque"


def material_uses_opacity(pkg, index, ref, depth=6):
    """Does this material actually use transparency?

    UE3 routinely packs specular/gloss/detail masks into a diffuse texture's
    alpha channel, so "the texture has an alpha channel" says nothing about
    whether the surface is meant to be see-through. Only the material knows:
    it is transparent if something drives Opacity/OpacityMask, or if its
    BlendMode is not opaque. Flagging a UT2004 texture ALPHA=1 on the strength
    of a DXT5 format alone renders solid walls half-transparent.
    """
    if depth <= 0:
        return False
    owner, export = index.resolve(pkg, ref)
    if export is None:
        return False
    props, start, _end = read_object_properties(owner, export)
    if start is None:
        return False
    for key in ("Opacity", "OpacityMask"):
        value = props.get(key)
        expr = _expression_ref(value)
        if expr is not None and not expr.is_null:
            return True
    blend = props.get("BlendMode")
    if blend is not None and str(blend) not in ("BLEND_Opaque", "0"):
        return True
    parent = props.get("Parent")
    if parent is not None and not parent.is_null:
        return material_uses_opacity(owner, index, parent, depth - 1)
    return False


def resolve_opacity(pkg, index, ref, depth=12):
    """The texture driving Opacity/OpacityMask: (Package, export, channel).

    (None, None, 0) when nothing does. UE3 graphs are free to sample opacity
    from a texture entirely separate from the diffuse -- `..._M` next to
    `..._D` is the house style, and 10 of DM-HeatRay's 17 masked materials do
    it. UE2 has no such indirection, so the caller has to fold this texture's
    channel into the diffuse's alpha before export or the cutout never happens.

    `channel` is the byte offset within a BGRA pixel: the input's own component
    mask picks it, and a lone MaskR means the red channel.
    """
    if depth <= 0:
        return None, None, 0
    owner, export = index.resolve(pkg, ref)
    if export is None:
        return None, None, 0
    props, start, _end = read_object_properties(owner, export)
    if start is None:
        return None, None, 0

    for key in ("OpacityMask", "Opacity"):
        value = props.get(key)
        expr = _expression_ref(value)
        if expr is None or expr.is_null:
            continue
        expr_owner, expr_export = index.resolve(owner, expr)
        if expr_export is None:
            continue
        found_owner, found, _sample = _walk(expr_owner, index, expr_export, depth, set())
        if found is None:
            continue
        channel = 0
        inner = getattr(value, "value", None)
        if hasattr(inner, "get") and inner.get("Mask"):
            # BGRA on disk, so red is the third byte.
            if inner.get("MaskR") and not inner.get("MaskG") and not inner.get("MaskB"):
                channel = 2
            elif inner.get("MaskG") and not inner.get("MaskR"):
                channel = 1
        return found_owner, found, channel

    parent = props.get("Parent")
    if parent is not None and not parent.is_null:
        return resolve_opacity(owner, index, parent, depth - 1)
    return None, None, 0


# The threshold `convert/textures.py` refuses a texture at, repeated here so a
# resolution decision can apply it without importing the converter.
NOT_DIFFUSE = 100

# Parameter names that state outright which slot they fill.
DIFFUSE_SLOTS = ("diffuse", "albedo", "basecolor", "base_color")


def names_diffuse_slot(parameter):
    """Does this parameter name say it *is* the diffuse, rather than merely
    scoring well?

    Narrower than score_texture_name on purpose. That function ranks candidates
    against each other, where a weak hint is better than nothing; this one
    decides whether an instance's override outranks its parent outright, which
    only an explicit slot name earns.
    """
    low = (parameter or "").lower()
    return any(token in low for token in DIFFUSE_SLOTS)


def resolve_diffuse(pkg, index, ref, depth=12, reject=None):
    """Resolve a material reference to (Package, Texture2D export), or (None, None)."""
    owner, export = index.resolve(pkg, ref)
    if export is None:
        return None, None
    cls = owner.class_name_of(export)

    if cls == "Texture2D":
        return owner, export

    props, start, _end = read_object_properties(owner, export)
    if start is None:
        return None, None

    if cls in ("MaterialInstanceConstant", "MaterialInstanceTimeVarying"):
        best = None
        params = props.get("TextureParameterValues")
        if params is not None and len(params):
            for entry in params.as_props():
                value = entry.get("ParameterValue")
                if value is None or value.is_null:
                    continue
                tex_owner, tex = index.resolve(owner, value)
                if tex is None or tex_owner.class_name_of(tex) != "Texture2D":
                    continue
                name = str(entry.get("ParameterName", tex.name))
                score = score_texture_name(name) + score_texture_name(tex.name)
                if best is None or score < best[0]:
                    best = (score, tex_owner, tex, names_diffuse_slot(name))
        # An instance that overrides only some parameters must not short-circuit
        # its parent. CTF-FacingWorlds' cliffs are the case: the instance
        # overrides Normal alone, so the only texture it names is a normal map,
        # while the DiffuseTexture the surface is actually painted with sits one
        # level up. Taking the instance's word for it renders the backdrop in
        # iridescent blue and magenta.
        parent = props.get("Parent")
        inherited = (None, None)
        if parent is not None and not parent.is_null:
            inherited = resolve_diffuse(owner, index, parent, depth - 1, reject)
        if best is not None and inherited[1] is not None:
            # The pixel test outranks the names. An instance often overrides
            # nothing but maps that are not colour at all: WAR-Serenity's cliffs
            # wear M_UN_Rock_SM_Cliffs01_MI_SideA_05, whose only non-normal
            # override is a "ShadeMap" pointing at the per-mesh relief bake,
            # while the parent names T_UN_Terrain_FloorStone_Rock01 under a
            # parameter called DiffuseTexture. Both score -20, so the names
            # cannot separate them and the bake wins on graph position -- and
            # the cliff renders as a pale, flat lightmap.
            if reject is not None:
                if reject(best[1], best[2]) and not reject(*inherited):
                    return inherited
            # An override of the diffuse slot *by name* settles it. The instance
            # is stating what this material is painted with, where the parent
            # only carries the default its author left in the slot, and those
            # defaults are engine placeholders: DM-HeatRay's rubble inherits
            # M_Shader_Simple, whose Diffuse parameter defaults to
            # Engine_MI_Shaders.T_Diffuse -- a 32x32 flat grey. Worse, the name
            # is why it wins: "T_Diffuse" scores -20 for saying diffuse while
            # the real T_HU_Deco_SM_RubbleA_D02 scores 0, so the placeholder
            # beats the texture it stands in for.
            if best[3] and score_texture_name(best[2].name) < NOT_DIFFUSE:
                return best[1], best[2]
            # Otherwise compare on the texture alone: the parameter name helped
            # choose among this instance's own overrides, but what gets drawn is
            # the texture, and that is what the two candidates differ in.
            if score_texture_name(inherited[1].name) < score_texture_name(best[2].name):
                return inherited
        if best is not None:
            return best[1], best[2]
        return inherited

    # A plain Material: follow the diffuse input through the expression graph.
    for key in DIFFUSE_INPUTS:
        value = props.get(key)
        ref_expr = _expression_ref(value)
        if ref_expr is None or ref_expr.is_null:
            continue
        expr_owner, expr = index.resolve(owner, ref_expr)
        if expr is None:
            continue
        found_owner, found, sample = _walk(expr_owner, index, expr, depth, set(),
                                            reject, export.name)
        if found is not None:
            if sample is not None:
                _LAST_SAMPLE[0] = (found_owner, sample)
            return found_owner, found

    # Fall back to whichever of the material's own textures looks most diffuse.
    candidates = _subobject_textures(owner, index, export)
    if candidates:
        return min(candidates, key=lambda ot: score_texture_name(ot[1].name))
    return None, None


def material_uv_channel(pkg, index, ref, depth=12):
    """(uv_channel, u_tiling, v_tiling) for a material's diffuse texture.

    Resolves the diffuse the same way `resolve_diffuse` does, then asks the
    sample it landed on which coordinates it reads.
    """
    _LAST_SAMPLE[0] = None
    _owner, tex = resolve_diffuse(pkg, index, ref, depth)
    if tex is None or _LAST_SAMPLE[0] is None:
        return 0, 1.0, 1.0
    sample_owner, sample = _LAST_SAMPLE[0]
    return sample_coordinates(sample_owner, index, sample)
