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

**These cannot be converted from a `ucc make` build.** UT2004 would need a
Shader or FinalBlend, and nothing in a script build can create one: the texture
import exec only makes Texture objects, and no editor factory has bCreateNew.
Nor can a generated class smuggle one in -- a `Begin Object` block in
defaultproperties creates an *inline* subobject on the class default object,
never a package object, and UT2004 references are `Class'Package.Group.Object'`
with an optional group and no sub-object level, so no path could name it.

What a plain Texture can express is limited to bMasked, bAlphaTexture and
bTwoSided (Engine/Texture.uc:37-39): alpha-blended translucency is reachable,
additive is not.

So these actors are skipped by default. UE2 would render them as overlapping
alpha-blended quads, which costs fill rate for something that would not look
right regardless. Pass --keep-effect-meshes to convert them anyway (they will
appear as opaque surfaces), or build a .utx of FinalBlends by hand in UnrealEd
if a faithful result is wanted.

Note the deliberately narrow test. It requires *all three* of unlit shading, a
non-opaque blend mode, and no DiffuseColor input at all:

* BLEND_Masked foliage (ferns, grass, ivy) is lit and keeps its diffuse -- real
  geometry, converts normally.
* `M_LT_Base_BSP_Glass_01` is unlit and translucent but *does* have a
  DiffuseColor, so windows stay as geometry rather than vanishing.
* Only the purely procedural glows -- beams and fog sheets, whose colour is a
  VectorParameter and whose alpha is DepthBiasedAlpha -- are skipped.
"""

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
        found = _substitute_for(material.name, MATERIAL_SUBSTITUTES)
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
