"""Detecting UE3 effect materials -- and why they are skipped rather than converted.

A UT3 light beam or fog sheet is not a textured surface. `M_EV_Lightbeam_Master_01`
is BLEND_Translucent + MLM_Unlit with no DiffuseColor, and its inputs are
procedural:

    EmissiveColor : VectorParameter "Color"              -- a flat colour
    Opacity       : StaticSwitch "UseDepthBiasedAlpha"
                      -> DepthBiasedAlpha                -- soft depth fade
                      -> Multiply(ScalarParameter "Opacity", Clamp(...),
                                  StaticSwitch "Fade with Distance")

There is no texture anywhere in the visible path; the texture a naive resolver
lands on (`T_EV_DustPanner_01`) comes from a disabled `UseTextureOverlay`
branch. Assigning it produces a solid grey slab where UT3 draws a soft glow.

(Phase 14d fixed that half: `material.live_branch` follows only the side of a
static switch UE3 actually compiles, so these now resolve
`T_EV_LightBeam_Falloff_02` -- the texture the material really samples.)

**A `ucc make` build CAN create a Shader or FinalBlend.** This file used to
say the opposite; the claim was wrong on both halves, and `ShaderLab/` at the
install root is the probe that settled it.

A `Begin Object` block in defaultproperties is constructed with
`Outer = InParent` and `RF_Public`, and during `ImportPropertiesScripts`
InParent is `Class->GetOuter()` -- the class's own *package*
(Editor/Src/UnEditor.cpp:824, and `GEditor->Bootstrapping` is zero there, being
raised only around `#exec`). So the object lands in the package root exactly
like an `#exec TEXTURE IMPORT`ed Texture, and `UObject::ResolveName`
(Core/Src/UnObj.cpp:3648) walks '.' to arbitrary depth, so `Package.Object`
names it in a t3d, an actor property or another package's import table.
`editinlinenew` has nothing to do with it -- ImportProperties never reads the
flag, and `ColorModifier` (`noteditinlinenew`) builds just as happily.

Two conditions, both learned the hard way:

* **Something must reference the material or it is not saved.** SavePackage
  writes only tagged objects. The first probe defined a TexPanner, a TexScaler,
  a Combiner, a Shader and a FinalBlend, referenced none of them, and produced
  a package containing none of them -- silently, with no error. A
  `var array<Material> KeepAlive;` on the generated class, filled in
  defaultproperties, is what holds them.
* **Order matters within one `ucc make`.** A package is only resolvable by a
  later one if it appears earlier in EditPackages.

What a plain Texture can express, by contrast, is limited to bMasked,
bAlphaTexture and bTwoSided (Engine/Texture.uc:37-39): alpha-blended
translucency is reachable, additive is not. That limit is what drove every
substitution below, and it no longer binds.

So the skipping below is now the *fallback*, not the rule. `effect_is_drawable`
keeps an effect actor whenever a UE2 material can be built for it -- which needs
a non-opaque blend mode and a real texture -- and only what fails both goes on
being dropped. `--no-materials` restores the old behaviour exactly, and
`--keep-effect-meshes` still forces every effect through as an opaque surface.

The remaining cost is fill rate: UE2 draws these as overlapping alpha-blended
quads, and a map with a lot of haze pays for it in overdraw rather than in
triangles.

What converts is more than the blend mode. `ut3/objects/graph.py` folds a UE3
expression graph to a constant where it can, which is what recovers the colour
of a material with no texture in its colour path at all -- and that is most of
UT3's volumetric effects, including the fog sheet above: `M_EV_FogSheet_Master_01`
computes EmissiveColor as `Color.rgb * Color.a` and every goo pit and light
shaft in the game is one instance overriding that parameter. `build_material`
below turns the result into a ColorModifier, a Panner into a TexPanner, and the
blend mode into a FinalBlend.

Note the deliberately narrow test. It requires *all three* of unlit shading, a
non-opaque blend mode, and no DiffuseColor input at all:

* BLEND_Masked foliage (ferns, grass, ivy) is lit and keeps its diffuse -- real
  geometry, converts normally.
* `M_LT_Base_BSP_Glass_01` is unlit and translucent but *does* have a
  DiffuseColor, so windows stay as geometry rather than vanishing.
* Only the purely procedural glows -- beams and fog sheets, whose colour is a
  VectorParameter and whose alpha is DepthBiasedAlpha -- are skipped.
"""

from ut3.objects.material import (MASKED_BLEND, constant_colour, diffuse_scale,
                                  diffuse_tint, emissive_tint, material_panner,
                                  opacity_scale)
from ut3.props import read_object_properties

# Blend modes that, combined with unlit shading, mark a volumetric effect.
EFFECT_BLEND_MODES = ("BLEND_Translucent", "BLEND_Additive", "BLEND_Modulate")
EFFECT_LIGHTING = "MLM_Unlit"


def material_is_effect(pkg, index, ref):
    """Is this material an unlit translucent effect with no textured equivalent?"""
    if ref is None or ref.is_null:
        return False
    owner, export = index.resolve(pkg, ref)
    if export is None:
        return False
    props, start, _end = read_object_properties(owner, export)
    if start is None:
        return False
    if str(props.get("BlendMode", "BLEND_Opaque")) not in EFFECT_BLEND_MODES:
        return False
    if str(props.get("LightingModel", "")) != EFFECT_LIGHTING:
        return False
    # A DiffuseColor means there is real surface colour to convert (glass, for
    # instance). Only materials with nothing textured driving them are effects.
    return props.get("DiffuseColor") is None


# UT2004 cannot *build* a Shader or FinalBlend from a ucc make, for all the
# reasons above -- but it ships plenty of them. Where a UT3 effect material is
# recognisably one UT2004 already has, the stock material is referenced by name
# and the actor keeps its geometry instead of being dropped. `Actor.Skins`
# overrides a static mesh's materials per actor (Engine/Actor.uc:316), so
# nothing has to be authored and nothing has to be imported.
#
# DM-Deck's goo pits are the case that matters: three S_EV_FogSheet_01 sheets
# wearing M_HU_Deck_Goo_Translucent and M_UN_Volumetrics_TexturedFogSheet_01_Goo.
# Skipped as effects, the pit that kills you is invisible.
#
# Still worth it after Phase 14, and checked rather than assumed. The goo
# surface takes its colour from a cubemap reflection tinted (2.0, 4.0, 0.4);
# cubemaps are refused as colour maps and this pipeline exports none, so a
# generated material would fall back to T_EV_DustPanner_01 -- near-uniform grey
# (mean 126 over 106..164). A flat grey slab against an animated goop. The
# vertical haze around the pit is a different material and does convert.

EFFECT_SUBSTITUTES = (
    ("goo", "FinalBlend'XEffectMat.goop.GoopFB'"),
    ("slime", "FinalBlend'XEffectMat.goop.GoopFB'"),
)

# The same idea for a surface that is *not* an effect by the test above -- it is
# lit, or it has a DiffuseColor -- but whose colour still is not in any texture.
# UT3 draws water with a Phong translucent shader whose tint, refraction and
# fresnel are all parameters; the only texture in the graph is a detail normal
# map, which is refused as a colour map (correctly -- drawn as diffuse it is
# iridescent blue and magenta), leaving the surface flat grey.
#
# So it gets a stock UT2004 material instead, exactly as the goo pits do.
# Matched on the *material* name rather than the mesh's, since UT3 dresses one
# generic plane as whatever a map needs.
#
# glass06_finalblend is a translucent blue-green pane out of UCGeneric, which
# ships with the game and needs no ServerPackages entry -- the map's import
# table pulls it in the same way DM-Deck17 pulls XEffectMat for its portals.
# It reads as water rather than as an effect, which the shield shells do not.
MATERIAL_SUBSTITUTES = (
    ("liquid", "FinalBlend'UCGeneric.Glass.glass06_finalblend'"),
    ("water", "FinalBlend'UCGeneric.Glass.glass06_finalblend'"),
)


# How flat a mesh has to be, relative to its own width, to count as a surface
# sheet rather than a volume of haze.
FLATNESS = 0.05
# And how close to level, once the actor's rotation is applied.
LEVEL = 0.85


def sheet_is_horizontal(pkg, index, mesh_ref, rotation, cache=None):
    """Is this actor a flat, level sheet -- a liquid surface rather than haze?

    UT3 draws a goo pit as one horizontal sheet for the surface plus several
    vertical ones filling the shaft, all the same `S_EV_FogSheet_01` plane.
    The vertical ones are volumetric: UT3 fades them with depth-biased alpha so
    they read as haze, and they genuinely span far above the goo -- one of
    DM-Deck's runs 1172uu over the surface. Given a stock UT2004 FinalBlend
    they arrive as solid green slabs stabbing up through the floor instead, so
    only the surface sheet is worth substituting; the haze goes on being
    skipped for exactly the reasons at the top of this file.
    """
    from convert.movers import _rotate, _rotation_matrix
    from ut3.objects.staticmesh import read_static_mesh

    key = ("flat", mesh_ref.is_import, mesh_ref.index)
    normal = cache.get(key) if cache is not None else None
    if normal is None:
        owner, export = index.resolve(pkg, mesh_ref)
        if export is None or owner.class_name_of(export) != "StaticMesh":
            return False
        mesh = read_static_mesh(owner, export)
        if mesh is None or mesh.lod0 is None or not mesh.lod0.positions:
            return False
        pos = mesh.lod0.positions
        extent = [max(v[i] for v in pos) - min(v[i] for v in pos) for i in range(3)]
        widest = max(extent)
        if widest <= 0:
            return False
        thin = min(range(3), key=lambda i: extent[i])
        # A sheet is flat only if one axis is negligible against the others.
        if extent[thin] > widest * FLATNESS:
            normal = None
        else:
            normal = tuple(1.0 if i == thin else 0.0 for i in range(3))
        if cache is not None:
            cache[key] = normal or False
    if not normal:
        return False
    return abs(_rotate(normal, _rotation_matrix(rotation))[2]) >= LEVEL


def _substitute_for(name, table=EFFECT_SUBSTITUTES):
    low = (name or "").lower()
    for token, replacement in table:
        if token in low:
            return replacement
    return None


def effect_substitute(pkg, index, mesh_ref, overrides=(), cache=None,
                      table=EFFECT_SUBSTITUTES):
    # (table is a parameter only so water_substitute can share the traversal.)
    """A stock UT2004 material stand-in for this actor's effect material, or None.

    The component's material overrides are checked first and they are what
    matters in practice: DM-Deck dresses one generic `S_EV_FogSheet_01` as goo
    per actor, so the mesh's own elements say nothing about it.
    """
    for ref in overrides:
        if ref is None or ref.is_null:
            continue
        _owner, material = index.resolve(pkg, ref)
        if material is None:
            continue
        found = _substitute_for(material.name, table)
        if found:
            return found

    key = ("sub", table is EFFECT_SUBSTITUTES, mesh_ref.is_import, mesh_ref.index)
    if cache is not None and key in cache:
        return cache[key]
    found = None
    owner, export = index.resolve(pkg, mesh_ref)
    if export is not None and owner.class_name_of(export) == "StaticMesh":
        from ut3.objects.staticmesh import read_static_mesh

        mesh = read_static_mesh(owner, export)
        if mesh is not None and mesh.lod0 is not None:
            for element in mesh.lod0.elements:
                _material_owner, material = index.resolve(owner, element.material)
                if material is None:
                    continue
                found = _substitute_for(material.name, table)
                if found:
                    break
    if cache is not None:
        cache[key] = found
    return found


# Textures that carry a control channel rather than colour, judged by name.
# Deliberately not a general rule in score_texture_name: a mask drawn as an
# alpha-blended diffuse is usually better than the grey placeholder, and 52
# rain-puddle decals across four maps plus every TM_UN_Glass_BasicPane_01_Mask
# window pane rely on exactly that. It is only inside the water rule -- where a
# stock material is standing by -- that a mask counts as no colour at all.
NOT_COLOUR = ("mask",)


def _has_token(name, tokens):
    low = (name or "").lower()
    return any(token in low for token in tokens)


def _material_candidates(pkg, index, mesh_ref, overrides, cache=None):
    """Every material this actor could be wearing: overrides first, then elements.

    Yields (owner package, reference) so a caller can resolve the material
    itself -- an element's material belongs to the mesh's package, not the
    map's, and resolving it against the wrong one finds nothing.
    """
    for ref in overrides:
        if ref is not None and not ref.is_null:
            yield pkg, ref

    key = ("mats", mesh_ref.is_import, mesh_ref.index)
    elements = cache.get(key) if cache is not None else None
    if elements is None:
        elements = []
        owner, export = index.resolve(pkg, mesh_ref)
        if export is not None and owner.class_name_of(export) == "StaticMesh":
            from ut3.objects.staticmesh import read_static_mesh

            mesh = read_static_mesh(owner, export)
            if mesh is not None and mesh.lod0 is not None:
                elements = [(owner, element.material) for element in mesh.lod0.elements
                            if element.material is not None and not element.material.is_null]
        if cache is not None:
            cache[key] = elements
    for owner, ref in elements:
        yield owner, ref


def _chain_substitute(pkg, index, ref, depth=8):
    """`_substitute_for` applied up the instance chain, not just at the leaf.

    A leaf's name is often not the material's identity. Matinee builds one per
    animated parameter and calls it `MaterialInstanceConstant_835`, which says
    nothing -- but BL-Dekk's landing pool runs

        MaterialInstanceConstant_835 <- ... <- M_LiquidEden_VertexOffset_INST
                                            <- M_LiquidEden_INST

    and "Liquid" is right there once the walk goes past the leaf. The pool was
    coming out as the checkerboard placeholder, its albedo resolving to
    `SF_T_TilingBubbles_N_H` and being refused as a normal map -- correctly, and
    with nothing left to draw.
    """
    while depth > 0 and ref is not None and not ref.is_null:
        owner, export = index.resolve(pkg, ref)
        if export is None:
            return None
        found = _substitute_for(export.name, MATERIAL_SUBSTITUTES)
        if found:
            return found
        props, start, _end = read_object_properties(owner, export)
        if start is None:
            return None
        parent = props.get("Parent")
        if parent is None or parent.is_null:
            return None
        pkg, ref, depth = owner, parent, depth - 1
    return None


def water_substitute(pkg, index, mesh_ref, overrides, rotation, texture_set, cache=None):
    """A stock UT2004 water material for a surface that would otherwise be grey.

    Three conditions, and every one of them earns its place against a real map:

    * The material has to name a liquid. Necessary but nowhere near sufficient
      -- CTF-Coret hangs `M_LT_Base_BSP_Glass_Water_01` on 56 window frames.
    * The mesh has to be a flat, level sheet, which is what separates a water
      *surface* from everything else the token catches: DM-Turbine's cables wear
      `M_LT_Mech_SM_Techcylinder02_Water_01`, DM-KBarge has a `Snowy_WaterTank`
      barrel, DM-OceanRelic a `M_Energy_Forcefield_Waterwall_01` bubble. All
      solid geometry, none of it flat.
    * And the material has to resolve to no colour map at all -- or to nothing
      but a mask, which is a control channel and not colour either. This is the
      one that makes the rule safe: a substitution can then only ever replace
      something with no colour in it, never a texture UT3 actually authored.
      Coret's window frames resolve theirs and are untouched even where one is
      level.

    The mask clause is what CTF-Nanoblack needs. Its pools resolve
    `T_NEC_Nanoblack_WaterMask`, which is not water: solid red (255,0,8) at a
    mean alpha of 14/255, an opacity channel UT3 feeds to a shader parameter.
    Drawn as an alpha-blended diffuse it is a 95% transparent red film, so the
    sheet reads as missing entirely.
    """
    if not sheet_is_horizontal(pkg, index, mesh_ref, rotation, cache):
        return None
    for owner, ref in _material_candidates(pkg, index, mesh_ref, overrides, cache):
        _material_owner, material = index.resolve(owner, ref)
        if material is None:
            continue
        found = _chain_substitute(owner, index, ref)
        if not found:
            continue
        # add_material caches by material, so asking here costs nothing that
        # registering the mesh would not have spent anyway.
        resolved = texture_set.add_material(owner, index, ref)
        if resolved is None or _has_token(resolved, NOT_COLOUR):
            return found
    return None


# Meshes that are volumetric effects whatever their material says. The material
# test below cannot reach these: `S_UN_Volumetrics_FogVolume_Mesh_01` carries an
# element whose material does not resolve at all, so there is no BlendMode or
# LightingModel to judge and it converts as a solid cone of geometry -- a light
# shaft rendered as a wall. UE2 has nothing that draws a fog volume, so the name
# is the only signal left and it is a reliable one: UT3 keeps these in a package
# called UN_Volumetrics and names them for what they are.
#
# Narrower than it looks. `S_EV_FogSheet_01` is deliberately not matched -- that
# is DM-Deck's goo surface, which does resolve, and which EFFECT_SUBSTITUTES
# turns into a stock UT2004 FinalBlend rather than dropping.
EFFECT_MESH_NAMES = ("FogVolume",)


def mesh_name_is_effect(name):
    """Is this mesh a volumetric effect by name alone?"""
    low = (name or "").lower()
    return any(token.lower() in low for token in EFFECT_MESH_NAMES)


def mesh_is_effect(pkg, index, mesh_ref, cache=None):
    """Is this static mesh *nothing but* effect materials?

    Every element has to be one. A beam or a fog sheet is a single quad wearing
    a single procedural material, which is what this is meant to catch; asking
    whether *any* element is an effect throws away real geometry that happens to
    have one glowing part. WAR-PowerSurge's processing plant is eight elements
    of beams, pipes and doors plus one translucent shield panel, and vanished
    entirely on the strength of that one.
    """
    key = (mesh_ref.is_import, mesh_ref.index)
    if cache is not None and key in cache:
        return cache[key]
    result = False
    owner, export = index.resolve(pkg, mesh_ref)
    if export is not None and owner.class_name_of(export) == "StaticMesh":
        from ut3.objects.staticmesh import read_static_mesh

        if mesh_name_is_effect(export.name):
            result = True
        else:
            mesh = read_static_mesh(owner, export)
            if mesh is not None and mesh.lod0 is not None and mesh.lod0.elements:
                result = all(material_is_effect(owner, index, element.material)
                             for element in mesh.lod0.elements)
    if cache is not None:
        cache[key] = result
    return result


# ---------------------------------------------------------------------------
# Building a UE2 material rather than settling for a flat texture.
# ---------------------------------------------------------------------------
#
# The mapping is Epic's own, read out of XEffectMat.utx rather than invented:
# an additive glow is a FinalBlend with FB_Brighten, ZWrite off and TwoSided on
# over a plain Texture (`Link.LinkBeamBlueFB`); an alpha-blended effect is
# FB_Translucent over a Shader (`goop.GoopFB` over `goop.GoopShader`); and
# "unlit" is expressed by giving a Shader the same material for Diffuse and
# SelfIllumination, which is exactly what GoopShader does.
#
# EFrameBufferBlending, from Engine/FinalBlend.uc.
# Read out of D3D9MaterialState.cpp:299 rather than chosen by feel:
#   FB_Brighten    SRCALPHA / ONE            additive, scaled by source alpha
#   FB_AlphaBlend  SRCALPHA / INVSRCALPHA    what BLEND_Translucent means
#   FB_Translucent ONE / INVSRCCOLOR         keyed on brightness, not alpha
#   FB_Modulate    DESTCOLOR / SRCCOLOR
# BLEND_Translucent maps to FB_AlphaBlend, which is exact -- but only where the
# texture has an alpha channel to blend on. build_material falls back to
# FB_Translucent where it does not; see there.
#
# BLEND_Additive maps to FB_Translucent rather than to the FB_Brighten its name
# promises, and this is the one entry that is a judgement rather than a
# translation. FB_Brighten is SRCALPHA/ONE: `dst + src`, a true add. UE3 means a
# true add too -- but it accumulates in a float buffer and tonemaps, so two
# overlapping light cones compress toward white. UE2 adds into eight bits and
# clips at the first crossing, so the same two cones are simply white, and a
# third adds nothing at all. Reported on WAR-PowerSurge, whose cones are drawn
# two-sided and so stack against themselves before any other cone is involved.
#
# FB_Translucent is ONE/INVSRCCOLOR: `src + dst * (1 - src)`, the screen
# operator. It is the LDR blend with the same shape as add-then-tonemap --
# monotonic, saturating, and *identical* to a true add for one layer over black,
# which is what an effect over an unlit background is. Where it differs is
# exactly where the artefact was: 0.35 over 0.35 gives 0.58 rather than 0.70,
# and a third layer 0.73 rather than a clamped 1.0. Black stays transparent
# under both, an additive black adding nothing and a brightness-keyed black
# being clear, so nothing about what is drawn changes -- only how the layers
# meet. 495 materials across the map set are additive.
FRAME_BUFFER_BLENDING = {
    "BLEND_Additive": "FB_Translucent",
    "BLEND_Translucent": "FB_AlphaBlend",
    "BLEND_Modulate": "FB_Modulate",
}


def _base_material_props(pkg, index, ref, depth=6):
    """The properties of the Material a reference ultimately rests on.

    A `MaterialInstanceConstant` states almost nothing itself -- BlendMode,
    LightingModel and TwoSided live on the parent Material at the end of the
    chain, which is why every caller here has to walk it rather than read the
    instance.
    """
    while depth > 0:
        if ref is None or ref.is_null:
            return None, None
        owner, export = index.resolve(pkg, ref)
        if export is None:
            return None, None
        props, start, _end = read_object_properties(owner, export)
        if start is None:
            return None, None
        if props.get("BlendMode") is not None or props.get("LightingModel") is not None:
            return owner, props
        parent = props.get("Parent")
        if parent is None or parent.is_null:
            return owner, props
        pkg, ref, depth = owner, parent, depth - 1
    return None, None


def surface_style(pkg, index, ref):
    """(blend mode, unlit, two-sided) for a UE3 material reference.

    Blend is UE3's own `BLEND_*` name, so the caller decides what UE2 makes of
    it; `BLEND_Opaque` when nothing says otherwise.
    """
    _owner, props = _base_material_props(pkg, index, ref)
    if props is None:
        return "BLEND_Opaque", False, False
    blend = str(props.get("BlendMode", "BLEND_Opaque"))
    unlit = str(props.get("LightingModel", "MLM_Phong")) == "MLM_Unlit"
    return blend, unlit, props.get("TwoSided") is True


def build_material(material_set, texture_set, pkg, index, ref,
                   texture_path, base_name, glow_path=None):
    """Build UE2 material objects for one non-opaque UT3 material.

    Returns (outermost object name, (name, blend, unlit, colour, panner)) --
    the second half is only for reporting -- or (None, None) when a plain
    Texture already says everything UE2 can say.

    The shape is Epic's, read out of `XEffectMat.utx`: a texture, optionally
    under a `TexPanner`, optionally under a `Shader` that self-illuminates it,
    optionally under a `ColorModifier` that tints it, and a `FinalBlend` on the
    outside deciding how it meets the framebuffer. Every one of those is a
    `UModifier` except the Shader, and the render interface accumulates the
    whole chain into one state (D3D9RenderInterface.h:395), so they compose.
    """
    blend, unlit, two_sided = surface_style(pkg, index, ref)
    framebuffer = FRAME_BUFFER_BLENDING.get(blend)
    colour = constant_colour(pkg, index, ref)
    if colour is None and texture_path is not None:
        # A material that draws a texture and multiplies it by a constant. The
        # two are mutually exclusive by construction and have to stay that way:
        # `constant_colour` only answers when the colour input folds whole,
        # which needs no texture in it, and on DM-Deck every material with a
        # tint also folds -- applying both would square the colour.
        colour = diffuse_tint(pkg, index, ref)
    if framebuffer is None and texture_path is not None:
        # And a plain scalar brightness on top of whatever tint there was. Only
        # for an opaque surface: everything with a framebuffer blend already
        # takes its level from `opacity_scale` below, and the two would compound.
        # HeatRay's StaticMeshActor_2017 is the case this exists for -- a sign
        # backed with the wall concrete at Constant(0.1), drawn at 1.0 as a white
        # slab. See ut3.objects.material.diffuse_scale.
        tone = diffuse_scale(pkg, index, ref)
        if tone is not None:
            from ut3.objects.graph import to_color

            # Through sRGB, the same way diffuse_tint converts a parameter: UE3
            # multiplies in linear space and UE2's ColorModifier in display
            # space, so 0.1 is a factor of 89/255, not 26/255.
            factor = to_color((tone, tone, tone))[0] / 255.0
            base = colour or (255, 255, 255)
            dimmed = tuple(max(0, min(255, int(round(c * factor)))) for c in base)
            if dimmed != (0, 0, 0):
                colour = dimmed
    panner = material_panner(pkg, index, ref)
    scale = opacity_scale(pkg, index, ref) if framebuffer else 1.0

    if texture_path is None and colour is None:
        return None, None
    if framebuffer is None and not unlit and glow_path is None:
        # A masked surface is already fully expressed by the texture's own
        # MASKED=1 and the opacity bake in convert/textures.py.
        if panner is None and colour is None:
            return None, None

    if texture_path is None:
        # No texture in the graph at all: the material *is* a colour. UT3 draws
        # a good many flares and falloff spheres this way.
        inner = material_set.add("ConstantColor", base_name,
                                 [("Color", _color(colour))])
        colour = None
    else:
        inner = None
        kind = "Texture"
        current = texture_path
        if panner is not None:
            yaw, rate = panner
            inner = material_set.add("TexPanner", base_name, [
                ("Material", "%s'%s'" % (kind, current)),
                ("PanDirection", "(Yaw=%d)" % yaw),
                ("PanRate", "%f" % rate),
            ])
            kind, current = "TexPanner", material_set.bare_path(inner)
        if (framebuffer is None and glow_path is not None
                and colour is not None and colour != (255, 255, 255)):
            # The tint belongs to the diffuse alone, so it has to go *under* the
            # Shader rather than around it. A ColorModifier wrapping the whole
            # Shader reaches the specular stage too -- the base modifier is
            # copied into `SpecularModifierInfo` and applied there
            # (D3D9MaterialState.cpp:1171, :1188) -- and dimming the glow by the
            # same factor as the board it sits on is the opposite of what UT3
            # does: `M_HU_Deco_SM_CitySign03b` states 0.1 on its DiffuseColor
            # and 1.0 on its emissive. Only the opaque glowing case moves; where
            # there is a framebuffer blend the ColorModifier's *alpha* is what
            # drives it, and that only works from outside.
            inner = material_set.add("ColorModifier", base_name, [
                ("Material", "%s'%s'" % (kind, current)),
                ("Color", _color(colour)),
                ("AlphaBlend", "False"),
                ("RenderTwoSided", "False"),
            ])
            kind, current = "ColorModifier", material_set.bare_path(inner)
            colour = None
        if unlit or glow_path is not None:
            # Two different things, one object, and they compose. Unlit: UE3
            # says the lighting pass must not touch this, and UE2 has no such
            # flag on a Texture, so it takes the Shader that XEffectMat's goop
            # uses -- the same material in Diffuse and SelfIllumination, with no
            # SelfIlluminationMask, leaving nothing for lighting to modulate.
            # Glowing: the surface is drawn normally and a *second* texture is
            # added on top. HeatRay's city signs are the case -- painted
            # `..._D`, glowing `..._E`.
            shader = [("Diffuse", "%s'%s'" % (kind, current))]
            if glow_path:
                # `Specular`, not `SelfIllumination`, and the slot's name is the
                # only thing about it that does not fit. UE3's emissive is
                # added: `Final = Diffuse * Light + Emissive`. UE2 has exactly
                # one operation that does that, and it is the specular pass with
                # no SpecularityMask -- D3DTOP_ADD in one pass
                # (D3D9MaterialState.cpp:544, HandleSpecular_SP with
                # UseSpecularity 0), ONE/ONE in two (:1500), and GL_ADD /
                # GL_ONE in the OpenGL driver (OpenGLMaterialState.cpp:589,
                # :1667). `ModulateSpecular2X` defaults False, which is what
                # keeps it an add rather than a modulate.
                #
                # SelfIllumination cannot express it. Without a mask it
                # *replaces* the diffuse and unlits the surface
                # (D3D9MaterialState.cpp:972); with one it lerps --
                # D3DTOP_BLENDCURRENTALPHA, `glow * a + lit * (1 - a)` (:1082,
                # :520). A lerp is not an add, and on a sign it reads as
                # nothing happening: `M_HU_Deco_SM_CitySign01b` paints a
                # near-black panel (mean luminance 34 of 255) and glows a bright
                # one (max 237), and at the mask's mid-tones the lerp drew about
                # 40 where UT3 draws 100. Reported as StaticMeshActor_641 being
                # "ok but not glowing".
                glow_kind, glow_current = "Texture", glow_path
                glow_colour = emissive_tint(pkg, index, ref)
                if glow_colour is not None:
                    # The glow's own colour, which is not the diffuse's: a lamp
                    # states DiffuseColor for the housing it lights and
                    # LightColor for what it emits. It goes on the glow alone,
                    # so it has to wrap the texture here rather than the Shader.
                    inner_glow = material_set.add("ColorModifier", base_name, [
                        ("Material", "%s'%s'" % (glow_kind, glow_current)),
                        ("Color", _color(glow_colour)),
                        ("AlphaBlend", "False"),
                        ("RenderTwoSided", "False"),
                    ])
                    glow_kind = "ColorModifier"
                    glow_current = material_set.bare_path(inner_glow)
                shader.append(("Specular", "%s'%s'" % (glow_kind, glow_current)))
            if unlit:
                shader.append(("SelfIllumination", "%s'%s'" % (kind, current)))
            if framebuffer is None and blend == MASKED_BLEND:
                # No FinalBlend will wrap this one, so the Shader's own
                # OutputBlending is what decides. OB_Normal with no Opacity
                # turns AlphaTest off outright (D3D9MaterialState.cpp:1521),
                # which would draw a cutout solid and lose the mask the
                # texture's own MASKED=1 was imported for.
                shader.append(("OutputBlending", "OB_Masked"))
            if two_sided and framebuffer is None:
                shader.append(("TwoSided", "True"))
            inner = material_set.add("Shader", base_name, shader)

    kind = material_set.definitions[inner][0] if inner else "Texture"
    current = material_set.bare_path(inner) if inner else texture_path

    if framebuffer == "FB_AlphaBlend" and not _has_alpha(texture_set, base_name):
        # BLEND_Translucent means SRCALPHA/INVSRCALPHA in both engines, but a
        # texture with no alpha channel reads as alpha 1 and draws solid. UE2's
        # own FB_Translucent is ONE/INVSRCCOLOR (D3D9MaterialState.cpp:325) --
        # keyed on brightness rather than alpha, so black is transparent. For a
        # beam or a fog sheet, whose UE3 opacity comes from a depth fade no
        # build can evaluate, that is the closest thing available and it is
        # what the texture was drawn for.
        framebuffer = "FB_Translucent"

    # Where the material scales its own opacity, UE2 has two places to put it
    # and which one depends on the blend. FB_AlphaBlend and FB_Brighten are
    # both driven by source alpha, so it goes in the ColorModifier's alpha;
    # FB_Translucent is ONE/INVSRCCOLOR and ignores alpha entirely, so there
    # the level *is* the brightness and it scales the colour instead. Additive
    # surfaces take the second path now that they blend as FB_Translucent, which
    # is the right one for them: their level was never about alpha either.
    alpha = 255
    if scale < 1.0:
        if framebuffer in ("FB_AlphaBlend", "FB_Brighten"):
            alpha = max(0, min(255, int(round(255.0 * scale))))
        elif framebuffer == "FB_Translucent":
            base = colour or (255, 255, 255)
            colour = tuple(max(0, min(255, int(round(c * scale)))) for c in base)

    # White at full alpha multiplies by one: the object would be created,
    # serialize to nothing (UCC drops a property equal to the class default)
    # and still cost a texture stage at render time -- and stages run out,
    # "No stages left for constant color modifier" being a real failure path
    # (D3D9MaterialState.cpp:1751).
    #
    # *Near* white is refused for the second of those reasons alone. A material
    # may state a tint that does nothing anybody can see: WAR-PowerSurge's
    # organic supports default their DiffuseColor to (239,240,241), a 6% dim
    # with no hue in it, and reading parameter defaults put a ColorModifier on
    # 642 actors for it. Under 8% off white and under 8 apart is not a tint.
    if alpha == 255 and colour is not None \
            and min(colour) >= 235 and max(colour) - min(colour) <= 8:
        colour = None
    if colour is not None or alpha != 255:
        # ColorModifier multiplies the material under it by a constant, colour
        # and alpha alike (HandleTFactor_SP, D3D9MaterialState.cpp:223). That
        # is exactly what a UT3 glow is: one greyscale falloff texture worn by
        # a dozen instances differing only in a colour and an opacity. Without
        # it DM-Deck's seven light beams are one white beam seven times, at
        # twenty times the strength UT3 draws them.
        inner = material_set.add("ColorModifier", base_name, [
            ("Material", "%s'%s'" % (kind, current)),
            ("Color", _color(colour or (255, 255, 255), alpha)),
            # Both default True, and both have to be turned off. `AlphaBlend`
            # rewrites a surface still at ONE/ZERO into SRCALPHA/INVSRCALPHA
            # (D3D9MaterialState.cpp:1735), so a *tint* on an opaque wall makes
            # it translucent -- which is why BL-Dekk's tinted floors and pipes
            # came out see-through and popping in and out of each other as the
            # camera moved, translucent surfaces being sorted per actor.
            # `RenderTwoSided` is ORed in the same way and would force
            # two-sidedness on geometry UT3 draws one-sided. Where a surface
            # really is blended or two-sided the FinalBlend above says so.
            ("AlphaBlend", "False"),
            ("RenderTwoSided", "False"),
        ])
        kind, current = "ColorModifier", material_set.bare_path(inner)

    if framebuffer is None:
        # A masked surface: the texture's own MASKED=1 does the cutout and
        # there is no framebuffer blend to state. Whatever was built above --
        # a panner, a self-illuminating Shader, a tint -- is the whole answer.
        return inner, (inner, blend, unlit, colour, panner)

    properties = [
        ("Material", "%s'%s'" % (kind, current)),
        ("FrameBufferBlending", framebuffer),
        # An effect that writes depth sorts against itself and punches holes in
        # whatever is drawn after it. Epic turns it off on every one of
        # XEffectMat's blended materials.
        ("ZWrite", "False"),
    ]
    if two_sided:
        properties.append(("TwoSided", "True"))
    built = material_set.add("FinalBlend", base_name, properties)
    return built, (built, blend, unlit, colour, panner)


def _color(rgb, alpha=255):
    return "(R=%d,G=%d,B=%d,A=%d)" % (rgb[0], rgb[1], rgb[2], alpha)


def _has_alpha(texture_set, name):
    """Did the exported texture end up with an alpha channel worth blending on?"""
    return bool(texture_set is not None and texture_set.alpha_channel.get(name))


def effect_is_drawable(pkg, index, mesh_ref, overrides, texture_set, cache=None):
    """Can this effect mesh be drawn properly now that materials can be built?

    Everything at the top of this file about beams and fog sheets was written
    when the only available answer was a flat opaque Texture, and it holds for
    that answer: `M_EV_Lightbeam_Master_01` resolves to `T_EV_DustPanner_01`,
    off a disabled `UseTextureOverlay` branch, and drawn opaque that is a grey
    slab where UT3 has a soft glow.

    Drawn *additively* it is not. Black contributes nothing under FB_Brighten,
    so a dark gradient texture over an additive blend is a glow -- the objection
    was to the blend mode, not to the texture. So an effect mesh is kept when a
    UE2 material can actually be built for it, which needs a non-opaque blend
    mode and then either a real texture or a colour its graph folds to. One or
    the other is not negotiable: a material with neither would take the grey
    placeholder, and a grey placeholder drawn additively is a bright haze over
    everything behind it.
    """
    if texture_set is None or texture_set.materials is None:
        return False
    for owner, ref in _material_candidates(pkg, index, mesh_ref, overrides, cache):
        blend, _unlit, _two_sided = surface_style(owner, index, ref)
        if FRAME_BUFFER_BLENDING.get(blend) is None:
            continue
        if texture_set.add_material(owner, index, ref) \
                or constant_colour(owner, index, ref):
            return True
    return False
