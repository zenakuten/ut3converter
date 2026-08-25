"""Find the diffuse texture behind a UE3 material.

UE3 materials are expression graphs with no UE2 equivalent, so this walks the
graph from the diffuse input until it reaches a texture sample. Where that
fails, it falls back to the material's own texture expressions, preferring names
that do not look like normal/specular maps.
"""

import re

from ..props import Struct, read_object_properties

DIFFUSE_INPUTS = ("DiffuseColor", "DiffusePower", "EmissiveColor", "BaseColor")

# Where an effect keeps the texture it is actually drawn with. A UT3 light beam
# or fog sheet puts a flat colour in EmissiveColor and the whole of its visible
# shape in Opacity -- M_EV_Lightbeam_Master_01's emissive is one white
# VectorParameter, while its cone lives on a TextureSampleParameter2D called
# "FalloffTexture" six nodes down the opacity chain. Searched only after the
# colour inputs have found nothing, so a material that states its diffuse the
# ordinary way is untouched.
OPACITY_INPUTS = ("Opacity", "OpacityMask")
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


_DECLARED_CHANNEL = re.compile(r"([rgba])\s*=\s*diffuse", re.I)
_CHANNEL_INDEX = {"r": 0, "g": 1, "b": 2, "a": 3}
_BASE_COLOR = re.compile(r"base\s*colou?r", re.I)

# Set by resolve_diffuse when the texture it returned was chosen because a
# parameter name declared which channel holds the albedo. Read it through
# material_albedo, which clears it first, the way material_uv_channel reads
# _LAST_SAMPLE.
_LAST_ALBEDO = [None]


def declared_diffuse_channel(name):
    """The channel a parameter name declares as diffuse, or None.

    TOXIKK's materials carry no diffuse map at all. They pack albedo, specular
    and gloss into the channels of one texture and say so in the parameter
    name -- "Mask 1 (R=Diffuse, G= Specular, B=Gloss)" -- then tint the result
    with a Base Color vector parameter. A name stating its own channel layout
    is far better evidence than any heuristic, and the scoring table would
    otherwise throw that texture away: "mask", "spec" and "gloss" all count
    against the one texture in the material that actually holds the colour,
    and the search then falls through to the parent and lands on an unrelated
    default.
    """
    found = _DECLARED_CHANNEL.search(name or "")
    if found is None:
        return None
    return _CHANNEL_INDEX.get(found.group(1).lower())


def resolve_base_color(pkg, index, ref, depth=12):
    """The Base Color an instance tints its albedo with, as (r, g, b), or None.

    Walked from the instance outwards, so a leaf that restates the colour wins
    over the parent it inherits the texture from -- which is the usual shape:
    MF_M_Wall_01_Dark_INST carries only a darker Base Color and takes its mask
    from MF_M_Wall_01_INST one level up.
    """
    if depth <= 0:
        return None
    owner, export = index.resolve(pkg, ref)
    if export is None:
        return None
    if owner.class_name_of(export) not in ("MaterialInstanceConstant",
                                           "MaterialInstanceTimeVarying"):
        return None
    props, start, _end = read_object_properties(owner, export)
    if start is None:
        return None
    params = props.get("VectorParameterValues")
    if params is not None and len(params):
        for entry in params.as_props():
            if _BASE_COLOR.search(str(entry.get("ParameterName", ""))) is None:
                continue
            value = entry.get("ParameterValue")
            if isinstance(value, Struct) and value.value is not None:
                return tuple(value.value[:3])
    parent = props.get("Parent")
    if parent is not None and not parent.is_null:
        return resolve_base_color(owner, index, parent, depth - 1)
    return None


def material_albedo(pkg, index, ref, depth=12, reject=None):
    """(owner, texture, channel, tint) for what a material is painted with.

    `channel` is None for an ordinary diffuse map, and a channel index when the
    material declares its albedo as one channel of a packed mask; `tint` is the
    Base Color to multiply that channel by, or None.
    """
    _LAST_ALBEDO[0] = None
    owner, tex = resolve_diffuse(pkg, index, ref, depth, reject)
    if tex is None:
        return None, None, None, None
    return owner, tex, _LAST_ALBEDO[0], resolve_base_color(pkg, index, ref, depth)


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


def live_branch(pkg, index, export, props, params):
    """For a StaticSwitchParameter, the input UE3 actually compiles: "A" or "B".

    A static switch is not a runtime branch. UE3 compiles one side into the
    shader and discards the other, so a texture on the dead side is never
    sampled -- and following it anyway is how `M_EV_Lightbeam_Master_01` came
    to be painted with `T_EV_DustPanner_01`, which `convert/shaders.py` has
    described as "a disabled UseTextureOverlay branch" since Phase 1.

    DM-Deck's goo sheets are the case that finally forced it. Their material is
    called `M_UN_Volumetrics_TexturedFogSheet_01_Goo`, but it overrides no
    static parameters at all and the master defaults UseTextureOverlay off --
    so the cloud overlay it is named after is not drawn. UT3 draws a plain
    falloff, tinted green. We drew the cloud.
    """
    name = str(props.get("ParameterName", ""))
    on = params.switches.get(name) if params is not None else None
    if on is None:
        on = props.get("DefaultValue") is True
    return "A" if on else "B"


def _collect(pkg, index, export, depth, seen, found, params=None, follow=None):
    """Every texture sample reachable from `export`, in graph order.

    Reachable means through the branches this material actually compiles: see
    live_branch. Without `params` only the switches' own defaults are known,
    which is still better than following both sides.
    """
    if depth <= 0 or export.index in seen:
        return
    seen.add(export.index)
    owner, tex = _texture_of(pkg, index, export)
    if tex is not None:
        # The sample carries its own package. It is not `owner`: that is where
        # the *texture* lives, and a cooked material routinely samples a texture
        # from another package -- TOXIKK's holograms sample MF_T_TDXHolo_01_D
        # out of UA_Decos_02 from a material in the map. Pairing the expression
        # with the texture's package reads the wrong export at that index, and
        # material_panner then asks a SeqAct_Interp for its Coordinates.
        found.append((owner, tex, (pkg, export)))
        return
    props, start, _end = read_object_properties(pkg, export)
    if start is None:
        return
    keys = follow or FOLLOW_INPUTS
    if pkg.class_name_of(export) == "MaterialExpressionStaticSwitchParameter":
        keys = (live_branch(pkg, index, export, props, params),)
    for key in keys:
        for value in props.get_all(key):
            ref = _expression_ref(value)
            if ref is None and hasattr(value, "is_null"):
                ref = value
            if ref is None or ref.is_null:
                continue
            sub_owner, sub_export = index.resolve(pkg, ref)
            if sub_export is None:
                continue
            _collect(sub_owner, index, sub_export, depth - 1, seen, found,
                     params, follow)


# What the texture an *effect* is drawn with is called, where it differs from
# the diffuse rule. "falloff" and "mask" are penalised in score_texture_name as
# evidence that a texture is not the diffuse, which is right for a wall and
# wrong for a light cone: an effect surface has no albedo at all, and the
# falloff is the entire shape of the thing.
#
# Merely lifting the penalty is not enough, because it leaves a falloff level
# with anything unremarkable and graph order then decides -- which is how
# HeatRay's cones came to be drawn with their dust grain. "falloff" scores as a
# bonus instead, at the same 20 the diffuse words carry: on an unlit additive
# surface, naming yourself the falloff is exactly as strong a claim to being
# what gets drawn as naming yourself the diffuse.
#
# "mask" splits on which input the texture was reached through, and the two
# answers are opposite. Down a *colour* input a mask is a cutout applied to
# something else, and that something else is what gets drawn: KBarge's icicles
# are unlit additive and reach T_UN_BSP_Snow_IcicleMask alongside
# T_UN_Terrain_Snow_02, and neutralising the penalty there tied them so graph
# order drew the icicles as their own silhouette instead of as snow. Coret's
# bubble tube did the same and came out painted with a Coca-Cola sign's mask.
# Down *Opacity* a mask is the opacity, by definition -- UN_Glass's panes reach
# TM_UN_Glass_BasicPane_01_Mask that way, against T_LT_Base_02_S, a specular
# map from an unrelated asset that scores 20 only because "base" is in
# _WORD_BONUS and happens to be in its package name.
_EFFECT_WORDS = {"falloff": -20}
_OPACITY_WORDS = {"falloff": -20, "mask": 0}

# An effect surface: no lighting pass touches it and it is not drawn solid. A
# masked cutout is deliberately not one -- there the mask is the cutout and the
# colour is a separate texture, which is the ordinary diffuse case.
EFFECT_BLENDS = ("BLEND_Additive", "BLEND_Translucent", "BLEND_Modulate")


def is_effect_material(props):
    """Does this Material's own state say it is an effect? See _EFFECT_EXEMPT."""
    return (str(props.get("LightingModel", "MLM_Phong")) == "MLM_Unlit"
            and str(props.get("BlendMode", "BLEND_Opaque")) in EFFECT_BLENDS)


def _rescored(name, table, suffixes=None):
    """score_texture_name with some of its penalties restated for a context."""
    score = score_texture_name(name)
    if score >= DISQUALIFIED:
        return score
    low = name.lower()
    for token, penalty in _SUFFIX_PENALTY:
        if suffixes and token in suffixes and low.endswith(token):
            score += suffixes[token] - penalty
    for token, penalty in _WORD_PENALTY:
        if token in low and token in table:
            score += table[token] - penalty
    return score


def score_effect_name(name):
    """Lower is better: how likely this name is the map an effect is shaped by."""
    return _rescored(name, _EFFECT_WORDS)


def score_opacity_name(name):
    """score_effect_name, for a texture reached down an opacity input."""
    return _rescored(name, _OPACITY_WORDS)


# `_e` and "emis" are marked down by `score_texture_name` as evidence a texture
# is not the diffuse. That is right, and it is also the strongest possible
# evidence that it *is* the emissive, which is what resolve_emissive is looking
# for. Left as penalties they lose to any unremarkable name in the same graph:
# HeatRay's `M_HU_Deco_SM_CitySign03b` reaches `T_HU_Deco_SM_CitySign03_E` (20)
# and `T_HU_Deco_SM_CitySign03_Phase` (0), and the sign was drawn glowing with
# the Phase texture -- which is not art at all. It feeds `Sine(Time + Phase)`
# and `Floor(Phase)`, the per-region blink control.
_EMISSIVE_WORDS = {"emis": -20}
_EMISSIVE_SUFFIXES = {"_e": -20}


def score_emissive_name(name):
    """Lower is better: how likely this name is what a material glows with."""
    return _rescored(name, _EMISSIVE_WORDS, _EMISSIVE_SUFFIXES)


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


def _walk(pkg, index, export, depth, seen, reject=None, material_name=None,
          params=None, follow=None, scorer=None):
    """The most diffuse-looking texture reachable from `export`.

    Returns (owner, texture, sample) so the caller can also ask the sample which
    UV channel it reads. `sample` is a (package, export) pair, since it need not
    live in the same package as either the texture or the material.

    Taking the *first* texture the graph reaches is not good enough. A UE3
    diffuse chain routinely multiplies a reflection or detail term in before it
    adds the base colour, so the first sample down the "A" input is often a
    cubemap or a specular map: DM-Deck's floor master material reaches
    T_UN_CubeMaps_Robot_Paint01 two levels before the
    T_LT_Floors_BSP_Organic11_D it actually paints with. Every reachable sample
    is collected instead and the best-named one wins.
    """
    found = []
    _collect(pkg, index, export, depth, seen, found, params, follow)
    if not found:
        return None, None, None
    score = scorer or score_texture_name
    # What the material is named after wins outright, but only among the
    # candidates the caller has not rejected -- the pixel test comes first, so a
    # relief bake cannot claim the material's name. See names_the_texture.
    if material_name:
        usable = [e for e in found if reject is None or not reject(e[0], e[1])]
        for entry in usable or found:
            if names_the_texture(material_name, entry[1].name):
                return entry
    best = min(score(entry[1].name) for entry in found)
    tied = [e for e in found if score(e[1].name) == best]
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


def _subobject_textures(pkg, index, material_export, params=None):
    """Every texture this material samples, preferring the ones it really draws.

    The unfiltered list is every texture expression the material owns, reachable
    or not, which is the last resort when the graph walk found nothing. That is
    exactly where a dead static-switch branch does its damage: DM-Deck's goo
    sheets own a cloud overlay they never sample, and it wins the name contest
    against the falloff they do sample -- `score_texture_name` penalises
    "falloff" by 60 as a rule aimed at cubemap falloffs.

    So the reachable set is computed first and used when it is not empty.
    Falling back to the whole list when nothing is reachable keeps a material
    whose inputs this reader cannot follow from losing its texture entirely.
    """
    owned = []
    for e in pkg.exports:
        if e.outer != material_export.index:
            continue
        owner, tex = _texture_of(pkg, index, e)
        if tex is not None:
            owned.append((owner, tex))
    if not owned:
        return owned
    live = _reachable_textures(pkg, index, material_export, params)
    if live:
        narrowed = [entry for entry in owned
                    if (entry[0].path, entry[1].index) in live]
        if narrowed:
            return narrowed
    return owned


# Every input a material can draw something through. Wider than DIFFUSE_INPUTS:
# reachability is being asked, not "where is the colour", and a fog sheet's
# only texture is in its Opacity.
_MATERIAL_INPUTS = ("DiffuseColor", "DiffusePower", "EmissiveColor", "BaseColor",
                    "Opacity", "OpacityMask", "SpecularColor")


def _reachable_textures(pkg, index, material_export, params=None):
    """{(package path, export index)} for the textures this material samples."""
    return _reachable(pkg, index, material_export, params)[0]


def reachable_parameters(pkg, index, material_export, params=None):
    """Names of the VectorParameters this material's graph actually reads."""
    return _reachable(pkg, index, material_export, params)[1]


def _reachable(pkg, index, material_export, params=None):
    """(texture keys, vector parameter names) reachable through live branches.

    Its own traversal rather than `_collect`'s. That one follows a fixed list of
    inputs chosen for hunting a diffuse, and a fog sheet's only texture hangs
    off a `DepthBiasedAlpha.Alpha` -- not on the list, so the walk stopped one
    node short of everything that mattered. Widening the diffuse walk instead
    would change which texture every material in every map resolves to, to
    answer a question about reachability. So this follows *every* property that
    resolves to a MaterialExpression, and answers only that question.
    """
    props, start, _end = read_object_properties(pkg, material_export)
    if start is None:
        return set(), set()
    found = set()
    vectors = set()
    seen = set()

    def visit(owner, export, depth):
        if depth <= 0 or export.index in seen:
            return
        seen.add(export.index)
        tex_owner, tex = _texture_of(owner, index, export)
        if tex is not None:
            found.add((tex_owner.path, tex.index))
            return
        node, node_start, _e = read_object_properties(owner, export)
        if node_start is None:
            return
        if owner.class_name_of(export) == "MaterialExpressionVectorParameter":
            name = str(node.get("ParameterName", ""))
            if name:
                vectors.add(name)
        keys = None
        if owner.class_name_of(export) == "MaterialExpressionStaticSwitchParameter":
            keys = (live_branch(owner, index, export, node, params),)
        for key, _i, _t, value in node:
            if keys is not None and key not in keys:
                continue
            ref = _expression_ref(value)
            if ref is None and hasattr(value, "is_null") and not value.is_null:
                ref = value
            if ref is None or ref.is_null:
                continue
            sub_owner, sub = index.resolve(owner, ref)
            if sub is None or "MaterialExpression" not in sub_owner.class_name_of(sub):
                continue
            visit(sub_owner, sub, depth - 1)

    for key in _MATERIAL_INPUTS:
        ref = _expression_ref(props.get(key))
        if ref is None or ref.is_null:
            continue
        owner, export = index.resolve(pkg, ref)
        if export is not None:
            visit(owner, export, 24)
    return found, vectors


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


def resolve_diffuse(pkg, index, ref, depth=12, reject=None, params=None):
    """Resolve a material reference to (Package, Texture2D export), or (None, None).

    `params` carries the instance chain's parameter overrides so that static
    switches can be evaluated on the way down -- see live_branch. It is
    collected once from the *outermost* reference and passed unchanged into the
    recursion, because that is where the overrides live: by the time the walk
    reaches the base Material, the leaf that set them is several levels behind.
    """
    if params is None:
        from . import graph as G

        params = G.collect_parameters(pkg, index, ref)
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
        declared = None
        # Not to be confused with `params`, the instance chain's scalar/vector/
        # switch overrides: these are the texture slots this instance replaces.
        overrides = props.get("TextureParameterValues")
        if overrides is not None and len(overrides):
            for entry in overrides.as_props():
                value = entry.get("ParameterValue")
                if value is None or value.is_null:
                    continue
                tex_owner, tex = index.resolve(owner, value)
                if tex is None or tex_owner.class_name_of(tex) != "Texture2D":
                    continue
                name = str(entry.get("ParameterName", tex.name))
                channel = declared_diffuse_channel(name)
                if channel is not None and declared is None:
                    declared = (tex_owner, tex, channel)
                score = score_texture_name(name) + score_texture_name(tex.name)
                if best is None or score < best[0]:
                    best = (score, tex_owner, tex, names_diffuse_slot(name))
        # A declared channel settles it: the material has said outright where
        # its colour lives, so neither the scoring nor the parent gets a vote.
        if declared is not None:
            _LAST_ALBEDO[0] = declared[2]
            return declared[0], declared[1]
        # An instance that overrides only some parameters must not short-circuit
        # its parent. CTF-FacingWorlds' cliffs are the case: the instance
        # overrides Normal alone, so the only texture it names is a normal map,
        # while the DiffuseTexture the surface is actually painted with sits one
        # level up. Taking the instance's word for it renders the backdrop in
        # iridescent blue and magenta.
        parent = props.get("Parent")
        inherited = (None, None)
        if parent is not None and not parent.is_null:
            inherited = resolve_diffuse(owner, index, parent, depth - 1, reject,
                                        params)
        if best is not None and inherited[1] is not None:
            # The pixel test outranks the names. An instance often overrides
            # nothing but maps that are not colour at all: WAR-Serenity's cliffs
            # wear M_UN_Rock_SM_Cliffs01_MI_SideA_05, whose only non-normal
            # override is a "ShadeMap" pointing at the per-mesh relief bake,
            # while the parent names T_UN_Terrain_FloorStone_Rock01 under a
            # parameter called DiffuseTexture. Both score -20, so the names
            # cannot separate them and the bake wins on graph position -- and
            # the cliff renders as a pale, flat lightmap.
            # An override that *names* the diffuse slot settles it before any
            # guessing. The relief-bake test below reads pixels, and its own
            # docstring says it is only for candidates that names cannot
            # separate -- but a bright, desaturated tiling texture looks exactly
            # like a bake to it. TOXIKK's panels are the case:
            # T_HighTechPanels_D measures 0.739 brightness at 0.008 saturation,
            # further into the bake region than WAR-PowerSurge's genuine bake,
            # while its parameter is called "DiffuseMap" outright. Guessing over
            # a material's own statement painted 111 of BL-Dekk's meshes with
            # the mud texture its parent happened to carry.
            if best[3] and score_texture_name(best[2].name) < NOT_DIFFUSE:
                return best[1], best[2]
            if reject is not None:
                if reject(best[1], best[2]) and not reject(*inherited):
                    return inherited
            # (The explicit-slot check that used to sit here now runs above,
            # ahead of the relief-bake test. It is what keeps DM-HeatRay's
            # rubble off Engine_MI_Shaders.T_Diffuse, the 32x32 flat grey its
            # parent leaves in the slot -- a placeholder that wins the name
            # contest precisely because it is called "T_Diffuse".)
            # Otherwise compare on the texture alone: the parameter name helped
            # choose among this instance's own overrides, but what gets drawn is
            # the texture, and that is what the two candidates differ in.
            if score_texture_name(inherited[1].name) < score_texture_name(best[2].name):
                return inherited
        if best is not None:
            return best[1], best[2]
        return inherited

    # A plain Material: follow the diffuse input through the expression graph.
    #
    # An effect scores its candidates by a different rule -- see
    # score_effect_name. HeatRay's light cones are the case the exemption exists
    # for: M_LT_Light_SM_Lightcone01 computes
    # (Dust02_panned + Dust02_panned) * LightColor * Falloff01 * Falloff02, and
    # the two falloffs are the cone while the dust is animated grain added over
    # it. Under the diffuse rule the falloffs take the 60-point "falloff"
    # penalty and T_LT_Light_SM_LightCone_Dust02 -- flat, shapeless noise --
    # scores 0 and wins, so the cone rendered as a grey static panel.
    scorer = score_effect_name if is_effect_material(props) else None
    for key in DIFFUSE_INPUTS:
        value = props.get(key)
        ref_expr = _expression_ref(value)
        if ref_expr is None or ref_expr.is_null:
            continue
        expr_owner, expr = index.resolve(owner, ref_expr)
        if expr is None:
            continue
        found_owner, found, sample = _walk(expr_owner, index, expr, depth, set(),
                                            reject, export.name, params,
                                            scorer=scorer)
        if found is not None:
            if sample is not None:
                _LAST_SAMPLE[0] = sample
            return found_owner, found

    # Still nothing: the colour path holds no texture, so the texture this
    # material draws -- if it draws one -- is in its opacity. See OPACITY_INPUTS.
    #
    # This runs ahead of the _subobject_textures fallback below and is strictly
    # better informed than it: both end up choosing among the textures the
    # material actually samples, but the walk knows graph order and *which
    # sample* it landed on, which the flat "every texture this material owns"
    # scan cannot. That matters twice over. M_EV_Lightbeam_Master_01 samples two
    # falloffs and multiplies them together; they score identically, so the scan
    # picked whichever came first in the export table -- T_EV_LightBeam_Falloff_02,
    # a round blob -- over the cone the beam is shaped by. And leaving
    # _LAST_SAMPLE unset sends material_panner to the material at large, where it
    # found the Panner on T_EV_DustPanner_01: a texture on the dead side of the
    # UseTextureOverlay static switch, which UT3 does not draw at all. Every
    # light beam in the map was scrolling to it.
    #
    # "Alpha" is followed here and not in the diffuse walk because that is where
    # a depth fade hangs its input: the fog sheets reach their falloff only
    # through MaterialExpressionDepthBiasedAlpha.Alpha.
    for key in OPACITY_INPUTS:
        ref_expr = _expression_ref(props.get(key))
        if ref_expr is None or ref_expr.is_null:
            continue
        expr_owner, expr = index.resolve(owner, ref_expr)
        if expr is None:
            continue
        found_owner, found, sample = _walk(
            expr_owner, index, expr, depth, set(), reject, export.name, params,
            follow=FOLLOW_INPUTS + ("Alpha",), scorer=score_opacity_name)
        if found is not None:
            if sample is not None:
                _LAST_SAMPLE[0] = sample
            return found_owner, found

    # Fall back to whichever of the material's own textures looks most diffuse.
    candidates = _subobject_textures(owner, index, export, params)
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


# The inputs a colour can come out of, in the order a UE2 surface wants them.
# Diffuse first: a lit material's colour is its diffuse, and its emissive is an
# extra glow on top. For an unlit one there is no diffuse at all and emissive
# is the whole of it.
COLOUR_INPUTS = ("DiffuseColor", "EmissiveColor")


def base_material(pkg, index, ref, depth=8):
    """The Material at the end of an instance chain: (package, export, props).

    A `MaterialInstanceConstant` states parameter overrides and nothing else --
    BlendMode, LightingModel, TwoSided and the expression graph all live on the
    Material it eventually rests on.
    """
    while depth > 0 and ref is not None and not ref.is_null:
        owner, export = index.resolve(pkg, ref)
        if export is None:
            return None, None, None
        props, start, _end = read_object_properties(owner, export)
        if start is None:
            return None, None, None
        parent = props.get("Parent")
        if parent is None or parent.is_null:
            return owner, export, props
        pkg, ref, depth = owner, parent, depth - 1
    return None, None, None


def constant_colour(pkg, index, ref):
    """The flat colour a material's graph computes, as (R, G, B) bytes, or None.

    Only when the material's colour input folds to a constant -- see
    `ut3/objects/graph.py`. A material with a texture in its colour path
    returns None here and is drawn with the texture instead, which is the right
    answer: the texture already carries the colour.

    This is what a UT3 glow actually is. `M_EV_FogSheet_Master_01` computes its
    EmissiveColor as `Color.rgb * Color.a` and nothing else, and every goo pit,
    steam vent and light shaft in the game is one instance overriding that
    parameter. Resolved as "a texture" it comes out white.
    """
    from . import graph as G

    owner, export, props = base_material(pkg, index, ref)
    if props is None:
        return None
    params = G.collect_parameters(pkg, index, ref)
    for key in COLOUR_INPUTS:
        expr = _expression_ref(props.get(key))
        if expr is None or expr.is_null:
            continue
        # The *first connected* colour input is the one that defines the
        # surface, and if it does not fold then the material's colour is in a
        # texture and there is no constant tint. Falling through to the next
        # input instead is how TOXIKK's `SF_M_SnowBarrier` came to be tinted
        # black: its DiffuseColor is a texture, so it declined, and its unused
        # EmissiveColor folded to zero -- which as a ColorModifier multiplies
        # the barrier away entirely.
        folded = G.fold(owner, index, expr, params)
        if folded is None:
            return None
        colour = G.to_color(folded)[:3]
        # Black is not a tint anyone writes. It is an input left at zero, and
        # multiplying by it erases the surface, so the texture is drawn as it
        # is instead.
        return None if colour == (0, 0, 0) else colour
    return None


def material_panner(pkg, index, ref):
    """(PanDirection yaw, PanRate) for a material that scrolls, or None.

    UT3 scrolls with a `Panner` node on the texture coordinates and UT2004 has
    `TexPanner`, which states the same thing as a rotator and a rate. The two
    conventions line up exactly, which took checking rather than assuming:

    * Both offset the texture *coordinates* by speed times time. UE3's Panner
      is `UV + Time * (SpeedX, SpeedY)`; `UTexPanner::GetMatrix`
      (Engine/Src/UnMaterial.cpp:500) builds a translation of
      `PanRate * PanDirection.Vector()` and UE2 applies it the same way round.
    * **V is not flipped, despite appearances.** `ut2/ase.py` writes `1.0 - v`
      and the ASE importer computes `1.0 - ST.Y` back
      (Editor/Src/UnStaticMesh.cpp:1048), so the two cancel and a converted
      mesh carries UT3's own UVs; the BSP surface writer never flips at all.
      This function used to negate SpeedY for a flip that does not survive to
      the data, which reversed every panning material along V -- reported as
      DM-HeatRay's light cones scrolling the wrong way.

    So `PanRate * PanDirection.Vector() == (SpeedX, SpeedY)`, and the pair is
    just a magnitude and an angle. UE2 pans along one axis only, which is all
    any case in the stock maps needs.

    A Panner animates the sample it is wired to and no other. So where the
    drawn sample is known, its own Coordinates chain is the authority --
    *including when it says there is no Panner*. HeatRay's
    `M_HU_Deco_SM_CitySignStores` is the case: its LED underlay scrolls, the
    sign artwork over it does not, and reading the material at large put the
    underlay's Panner on the artwork and set the whole sign sliding.

    `resolve_diffuse` leaves the sample it landed on in `_LAST_SAMPLE`, but it
    only lands on one when a graph walk reached a texture. Where the texture
    came instead from the last-resort "every texture this material owns" scan
    there is no sample to ask, and the material's own Panner is the only
    information there is.

    That fallback used to catch the fog sheets and the light beams as well,
    since their colour path holds no texture at all. It gave them the wrong
    answer: the Panner those materials own is wired to `T_EV_DustPanner_01`, on
    the dead side of the `UseTextureOverlay` static switch, and the drawn
    sample -- the FalloffTexture parameter the opacity walk now finds -- has
    camera-relative arithmetic on its coordinates and does not pan at all. 341
    materials across the map set were scrolling to a texture UT3 never draws.
    """
    from . import graph as G
    import math

    owner, export, props = base_material(pkg, index, ref)
    if props is None:
        return None

    _LAST_SAMPLE[0] = None
    resolve_diffuse(pkg, index, ref)
    if _LAST_SAMPLE[0] is not None:
        sample_owner, sample = _LAST_SAMPLE[0]
        sample_props, start, _end = read_object_properties(sample_owner, sample)
        if start is None:
            return None
        coords = _expression_ref(sample_props.get("Coordinates"))
        if coords is None or coords.is_null:
            return None                 # untransformed UV: this does not pan
        found = G.find_panner(sample_owner, index, coords)
    else:
        found = None
        for key in COLOUR_INPUTS + ("Opacity", "OpacityMask"):
            expr = _expression_ref(props.get(key))
            if expr is None or expr.is_null:
                continue
            found = G.find_panner(owner, index, expr)
            if found is not None:
                break
    if found is None:
        return None

    speed_x, speed_y = found
    rate = math.hypot(speed_x, speed_y)
    if rate <= 0.0:
        return None
    yaw = int(round(math.atan2(speed_y, speed_x) / (2 * math.pi) * 65536)) & 0xFFFF
    return yaw, rate


def opacity_scale(pkg, index, ref):
    """How far a material scales its own opacity, as 0..1.

    1.0 when it does not, which is also the answer for an opaque material.
    See `graph.constant_scale` for what is and is not taken.
    """
    from . import graph as G

    owner, _export, props = base_material(pkg, index, ref)
    if props is None:
        return 1.0
    params = G.collect_parameters(pkg, index, ref)
    for key in ("Opacity", "OpacityMask"):
        expr = _expression_ref(props.get(key))
        if expr is None or expr.is_null:
            continue
        scale = G.constant_scale(owner, index, expr, params)
        if scale is None:
            return 1.0
        return max(0.0, min(1.0, scale))
    return 1.0


def resolve_emissive(pkg, index, ref, reject=None):
    """The texture a material *glows* with: (Package, Texture2D export).

    (None, None) when it does not glow, or when what it glows with is the
    texture it is already painted with -- UE3 routinely feeds one texture to
    both DiffuseColor and EmissiveColor, and UE2 gets that from a Shader whose
    Diffuse and SelfIllumination are the same material.

    This is the half of a UT3 sign that a flat texture cannot carry. HeatRay's
    `M_HU_Deco_SM_CitySignsTexts` paints `T_HU_Deco_SM_CitySign01b_D` and glows
    `T_HU_Deco_SM_CitySignsTexts_E` on top of it at fifteen times brightness;
    drawn as the diffuse alone the sign is there but dead. UE2's Shader has
    exactly the two slots.

    `reject` is the caller's pixel test. The engine's placeholder emissive is a
    32x32 flat image (`UN_Shaders.T_Diffuse` measures mean 128 with a spread of
    zero), and a flat emissive is not a glow: it is either a slot nobody filled
    or a uniform brightening, and either way it would wash the surface out.
    """
    from . import graph as G

    owner, export, props = base_material(pkg, index, ref)
    if props is None:
        return None, None
    params = G.collect_parameters(pkg, index, ref)

    # A material that says outright it does not glow, does not glow. This is a
    # statement rather than a heuristic and it outranks the graph walk, which
    # can reach a texture through a path the switch does not gate: TOXIKK's
    # `M_HighTechPanel_EdenParticles_INST` sets bUseEmissive False and still
    # leads to `SF_T_GroundHeightmaps`, and a heightmap drawn as light is the
    # rainbow sheen reported on BL-Dekk's wall panels.
    for name, value in params.switches.items():
        if name.lower().replace(" ", "") == "buseemissive" and not value:
            return None, None

    expr = _expression_ref(props.get("EmissiveColor"))
    if expr is None or expr.is_null:
        return None, None
    expr_owner, expr_export = index.resolve(owner, expr)
    if expr_export is None:
        return None, None
    # Collected rather than walked, so that `reject` can be applied *before* the
    # best is chosen instead of after. `_walk` returns one candidate and the
    # caller could only throw it away, which cost two materials their glow the
    # moment score_emissive_name started preferring `_E` names: CTF-Strident's
    # `M_LT_Mech_SM_Megawalls01` reaches a featureless `..._E` and a usable
    # `..._EPan`, and rejecting the winner returned nothing at all rather than
    # falling through to the one behind it.
    candidates = []
    _collect(expr_owner, index, expr_export, 12, set(), candidates, params)
    usable = [entry for entry in candidates
              if reject is None or not reject(entry[0], entry[1])]
    if not usable:
        return None, None
    found_owner, found, _sample = min(
        usable, key=lambda entry: score_emissive_name(entry[1].name))

    diffuse_owner, diffuse = resolve_diffuse(pkg, index, ref)
    if diffuse is not None and diffuse_owner is found_owner \
            and diffuse.index == found.index:
        return None, None
    return found_owner, found


def emissive_gain(pkg, index, ref, glow_owner, glow_export):
    """How much brighter than its texture a material's glow is drawn. >= 1.0.

    A ColorModifier multiplies by a byte and cannot brighten, so a boost has
    nowhere to live in the material -- but the glow copy of the texture is
    generated for this purpose alone and re-encoded on the way out, so it can
    carry it. See `graph.sample_gain` for why the walk goes to the sample rather
    than reading the input as a whole, and `convert/textures.py:bake_self_alpha`
    for where the factor is applied.

    1.0 when the material does not scale its emissive, and never below: a glow
    dimmer than its texture is expressible as a ColorModifier and is not this
    function's business.
    """
    from . import graph as G

    owner, _base, props = base_material(pkg, index, ref)
    if props is None:
        return 1.0
    expr = _expression_ref(props.get("EmissiveColor"))
    if expr is None or expr.is_null:
        return 1.0

    def is_target(sample_pkg, sample_export):
        tex_owner, tex = _texture_of(sample_pkg, index, sample_export)
        return (tex is not None and tex_owner is glow_owner
                and tex.index == glow_export.index)

    gain = G.sample_gain(owner, index, expr, is_target,
                         G.collect_parameters(pkg, index, ref))
    if gain is None or gain <= 1.0:
        return 1.0
    return gain


# Vector parameters that tint the diffuse map rather than replacing it. Matched
# only when the material's graph actually reads the parameter -- see
# diffuse_tint -- so the name narrows a candidate rather than deciding one.
# "lightcolor" is here for the volumetrics: a light cone's instances differ in
# nothing else. M_LT_Light_SM_Lightcone01 multiplies LightColor straight into
# its emissive chain, and it is the only vector parameter the graph reads, so
# without it HeatRay's eight cones -- colorA, colorC, colorE, colorG, Blue,
# Red -- all render as the same full-brightness white on an additive blend.
_DIFFUSE_TINTS = ("diffusecolor", "basecolor", "diffusetint", "color", "tint",
                  "lightcolor")


def diffuse_tint(pkg, index, ref):
    """The constant colour a material multiplies its diffuse map by, or None.

    UE3 routinely paints one texture a dozen ways with a vector parameter.
    TOXIKK does it heavily: 14 of BL-Dekk's 31 material instances carry a
    non-white `DiffuseColor`, and some are not subtle -- its landing pools are
    (0.04, 0.073, 0.243), a deep blue, drawn from an untinted grey texture.
    UT2004 expresses exactly this with a `ColorModifier`.

    Distinct from `constant_colour`, and the two cannot both fire: that one
    answers materials whose colour input folds to a constant *because there is
    no texture in it*, which is a flat glow. This one answers a material that
    does draw a texture and multiplies it.

    Two conditions. The parameter has to be named like a tint, and it has to be
    one the graph actually reads -- a chain of instances accumulates parameters
    that later revisions stopped using, and `reachable_parameters` walks only
    the branches the material compiles. White is refused as a no-op, and so is
    a value above 1, which is a brightness boost rather than a tint and would
    clip to white in a byte.
    """
    from . import graph as G

    owner, base, props = base_material(pkg, index, ref)
    if props is None:
        return None
    params = G.collect_parameters(pkg, index, ref)
    live = reachable_parameters(owner, index, base, params)
    if not live:
        return None

    best = None
    pkg_at, ref_at, depth = pkg, ref, 8
    while depth > 0 and ref_at is not None and not ref_at.is_null:
        owner_at, export_at = index.resolve(pkg_at, ref_at)
        if export_at is None:
            break
        at_props, start, _end = read_object_properties(owner_at, export_at)
        if start is None:
            break
        array = at_props.get("VectorParameterValues")
        if array is not None and len(array):
            for entry in array.as_props():
                name = str(entry.get("ParameterName", ""))
                if name not in live:
                    continue
                if name.lower().replace(" ", "") not in _DIFFUSE_TINTS:
                    continue
                value = entry.get("ParameterValue")
                if isinstance(value, Struct) and value.value is not None:
                    best = tuple(float(v) for v in value.value[:3])
                    break
        if best is not None:
            break
        parent = at_props.get("Parent")
        if parent is None or parent.is_null:
            break
        pkg_at, ref_at, depth = owner_at, parent, depth - 1

    if best is None:
        # Nothing in the instance chain overrides it -- which does not mean the
        # parameter has no value. A VectorParameter carries its own
        # DefaultValue, and a *bare Material* placed with no instance at all
        # gets exactly that. WAR-PowerSurge's 80 light cones are the case: they
        # use `M_LT_Light_SM_Lightcone01` directly, whose LightColor defaults to
        # (0.1, 0.1, 0.05), and drawn at 1.0 on an additive blend they came out
        # as solid white triangles. HeatRay's cones hid it, every one of them
        # being an instance that states a colour.
        best = _parameter_default(owner, index, base, live)

    if best is None or any(v > 1.0 for v in best):
        return None
    # A two-tone material has both of its colours baked into the texture
    # already, so tinting on top would apply the body colour twice.
    if diffuse_blend(pkg, index, ref) is not None:
        return None
    colour = G.to_color(best)[:3]
    # White multiplies by one, so it is a no-op that costs a texture stage.
    # Black is refused for the same reason `constant_colour` refuses it: it
    # erases the surface, and a UE3 material that tints something black
    # generally does so through a mask this cannot follow rather than over the
    # whole texture.
    if colour in ((255, 255, 255), (0, 0, 0)):
        return None
    return colour


# Where a colour that gets drawn can be stated. The first one present wins, the
# way resolve_diffuse walks DIFFUSE_INPUTS: a material with no DiffuseColor at
# all draws whatever its EmissiveColor computes.
_SCALE_INPUTS = ("DiffuseColor", "EmissiveColor", "BaseColor")


def diffuse_scale(pkg, index, ref):
    """The constant brightness a material multiplies its colour map by, or None.

    A sibling of `diffuse_tint`: that one reads a vector parameter an instance
    overrides, this one a plain scalar the material multiplies in. UT3 uses it
    to reuse one texture at two brightnesses without authoring a second --
    HeatRay's `M_HU_Deco_SM_CitySign03b` and `..._05b` back their signs with
    `T_HU_Base_BSP_Concrete01`, the same concrete the walls use, at
    `Constant(0.1)`. Drawn at 1.0 the boards came out as white slabs, which is
    what StaticMeshActor_2017 and StaticMeshActor_462 were reported as.

    See `graph.product_factor` for how narrow the shape it accepts is, and why.
    Only a dimming factor converts: UE2's ColorModifier multiplies by a byte, so
    a value above 1 is a brightness boost it cannot express -- `..._CitySignStores`
    wants its glow at 5.0 and does not get it. Zero is refused for the same
    reason `diffuse_tint` refuses black: DM-Deck's teleporter fingers fold to it
    through a mask this cannot follow, and drawn black the surface disappears.
    """
    from . import graph as G

    owner, _base, props = base_material(pkg, index, ref)
    if props is None:
        return None
    params = G.collect_parameters(pkg, index, ref)
    for key in _SCALE_INPUTS:
        expr = _expression_ref(props.get(key))
        if expr is None or expr.is_null:
            continue
        level = G.product_factor(owner, index, expr, params)
        if level is None or not 0.0 < level < 1.0:
            return None
        return level
    return None


def _parameter_default(pkg, index, material, live):
    """The DefaultValue of the material's own tint parameter, or None.

    Only a parameter that is both named like a tint and one the graph actually
    reads -- the same two conditions the instance walk applies, for the same
    reasons.
    """
    for i, export in enumerate(pkg.exports):
        if export.outer != material.index:
            continue
        if pkg.class_name_of(export) != "MaterialExpressionVectorParameter":
            continue
        props, start, _end = read_object_properties(pkg, export)
        if start is None:
            continue
        name = str(props.get("ParameterName", ""))
        if name not in live or name.lower().replace(" ", "") not in _DIFFUSE_TINTS:
            continue
        value = props.get("DefaultValue")
        if isinstance(value, Struct) and value.value is not None:
            return tuple(float(v) for v in value.value[:3])
    return None


def _live_vectors(pkg, index, ref, live, depth=8):
    """{name: (r, g, b)} for live vector parameters, leaf first."""
    out = {}
    while depth > 0 and ref is not None and not ref.is_null:
        owner, export = index.resolve(pkg, ref)
        if export is None:
            break
        props, start, _end = read_object_properties(owner, export)
        if start is None:
            break
        array = props.get("VectorParameterValues")
        if array is not None and len(array):
            for entry in array.as_props():
                name = str(entry.get("ParameterName", ""))
                if name not in live:
                    continue
                value = entry.get("ParameterValue")
                if isinstance(value, Struct) and value.value is not None:
                    out.setdefault(name, tuple(float(v) for v in value.value[:3]))
        parent = props.get("Parent")
        if parent is None or parent.is_null:
            break
        pkg, ref, depth = owner, parent, depth - 1
    return out


def diffuse_blend(pkg, index, ref):
    """(channel, colour one, colour two) where a material is two-tone, or None.

    TOXIKK paints a panel with two colours chosen per pixel by one channel of
    the diffuse map: `DiffuseColor` for the body, `DiffuseColor2` for the trim,
    `DiffuseColorMaskChannel` saying which channel picks between them, and
    `bUseDiffuseColor2` saying whether any of it applies. **32 of BL-Dekk's
    materials do this** -- a third of the map -- and none of DM-Deck's, so it is
    an asset-authoring style rather than something UE3 does generally.

    Applying only the first colour is what made a pipe "appear as a flat
    texture": its body is red and its trim near-white, and multiplying the
    whole texture by red loses the trim entirely. The floor panels are worse,
    blending warm white against near-black.

    Colours come back linear, for the caller to multiply in that space before
    converting once at the end.
    """
    from . import graph as G

    owner, base, props = base_material(pkg, index, ref)
    if props is None:
        return None
    params = G.collect_parameters(pkg, index, ref)
    if not params.switches.get("bUseDiffuseColor2"):
        return None
    channel = params.masks.get("DiffuseColorMaskChannel")
    if channel is None:
        return None
    live = reachable_parameters(owner, index, base, params)
    if not live:
        return None
    values = _live_vectors(pkg, index, ref, live)
    first, second = values.get("DiffuseColor"), values.get("DiffuseColor2")
    if first is None or second is None or first == second:
        return None
    if any(v > 1.0 for v in first + second):
        return None
    return channel, first, second
