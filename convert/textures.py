"""Texture conversion: UE3 materials -> UT2004 texture package.

Produces a directory laid out the way UT2004 mods expect:

    <TexPackage>/Classes/<TexPackage>.uc     generated #exec TEXTURE IMPORT lines
    <TexPackage>/Textures/*.dds|*.tga        the extracted surfaces

`ucc make` then builds it, and the t3d's texture references resolve against it.
"""

import os
import re
import zlib

import struct

from ut2.images import write_placeholder, write_texture
from ut2.materials import MaterialSet

# ut2.images.write_placeholder's own default; recorded so the fallback texture
# scales its surfaces like any other.
PLACEHOLDER_SIZE = 128.0
from ut3.objects.material import (diffuse_blend, material_albedo, material_blend_mode,
                                  resolve_diffuse, resolve_opacity, score_texture_name)
from ut3.objects.texture import read_texture

_SANITIZE = re.compile(r"[^A-Za-z0-9_]")

# The largest mip exported. Raised from 1024 after measuring what the source
# content actually holds: a third of every texture the 12 TOXIKK maps use is
# larger than 1024, 84 distinct ones, and the top mip is genuinely present in
# every case rather than streamed out the way UT3's cooked packages leave it. At
# 1024 all of those shipped a mip level down.
#
# The cost is package size and video memory, both roughly doubling for a map
# built mostly of 2048s -- BL-Dekk's went from 82MB to 160MB. UT2004 itself has
# no trouble with either, and `--max-texture-size` is there for a map or a
# machine that does.
#
# It also moves the .t3d: the BSP writer derives each surface's UV scale from the
# size the texture actually ships at, so raising the cap doubles every TextureU
# and TextureV that gains a mip. A .t3d and its package are a matched pair --
# loading one against a package built at a different cap tiles the BSP wrong.
DEFAULT_MAX_SIZE = 4096

# UE3 normalises BSP surface UVs by a fixed constant instead of by the texture
# size, which is why its |TextureU| values cluster on 1, 1/2, 1/3, 1/4 whatever
# texture the surface wears -- swap a 512 for a 2048 in UE3 and the tiling does
# not budge. UE2 divides by the texture size instead, so converting a surface
# means restating its scale in terms of the size actually exported:
#
#     |TextureU_UE2| = |TextureU_UE3| * exported_size / UE3_BSP_UV_SCALE
#
# 128 is derived, not guessed: T_LT_Floors_BSP_Organic05b_D is 512 declared and
# 512 exported (so no size reduction to confuse things), UT3 gives it
# |TextureU| 0.5, and 2.0 was measured in the editor as the value that matches.
# 0.5 * 512 / 2.0 = 128. Every other surface then lands on UT3's own repeat
# distance to the unit.
UE3_BSP_UV_SCALE = 128.0

# A name scoring this badly is not a colour map at all -- a normal map or a
# cubemap. Drawing one as diffuse is worse than drawing nothing: a normal map
# renders as iridescent blue and magenta, which is what CTF-FacingWorlds'
# cliffs and ocean looked like. The neutral placeholder is the better answer.
NOT_DIFFUSE = 100

# What counts as a relief bake rather than a base colour, on a 0..1 scale: how
# bright the mean pixel is, and how far apart its channels spread. See
# TextureSet._is_relief_bake.
BAKE_BRIGHTNESS = 0.70
BAKE_SATURATION = 0.02


def sanitize(name):
    out = _SANITIZE.sub("_", name or "")
    if out and out[0].isdigit():
        out = "_" + out
    return out or "Texture"


def package_tag(package_name):
    """A short per-package suffix that keeps texture *object names* unique.

    The ASE importer does not resolve `*BITMAP` by path. It walks every
    UMaterial in memory and compares `It->GetName()` -- the leaf name, with no
    package -- taking the first match (Editor/Src/UnStaticMesh.cpp:680). A
    fully-qualified BITMAP would therefore never match at all, and two packages
    holding the same texture name are decided by object-table order, which is
    build order.

    That is not hypothetical: `ucc make` builds every EditPackages entry in one
    process, so each converted map's textures stay resident while the next map's
    meshes import. Four converted maps here share 47 texture names, because they
    draw on the same UT3 libraries -- WAR-PowerSurge's rocks bound to
    CTFFacingWorldsTex's copy of T_UN_Rock2_BSP_Rock03 and wore the wrong
    artwork. Only meshes are affected; every other reference the converter emits
    is a full path and resolves properly.

    A hash rather than the package name because FName caps at 64 characters and
    UT3's texture names already run to 39.
    """
    return "%04x" % (zlib.crc32((package_name or "").encode("utf-8")) & 0xFFFF)


def _material_key(ref):
    """Cache key for a material reference: the package, then its index.

    `ObjRef` carries the package its index was read from, so this needs nothing
    from the caller -- see the note in add_material for why the package has to
    be part of it.
    """
    return (ref.pkg.path, ref.is_import, ref.index)


class TextureSet:
    """Resolved materials, de-duplicated by the texture they land on."""

    BASE_FALLBACK_NAME = "DefaultBSP"

    def __init__(self, package_name, fallback=True, group="BSP", materials=True,
                 all_textures=False):
        self.package_name = package_name
        # Every texture this package defines carries it, so no two converted
        # maps can hand the ASE importer the same name -- see package_tag.
        self.tag = package_tag(package_name)
        self.FALLBACK_NAME = "%s_%s" % (self.BASE_FALLBACK_NAME, self.tag)
        self._bake_cache = {}
        # Textures import into a group, so references must be fully qualified:
        # Class'Package.Group.Object'. Actor properties resolve by exact path --
        # only the t3d polygon importer has an ANY_PACKAGE name-search fallback
        # (Editor/Src/UnEdFact.cpp:1602), which is why BSP surfaces resolved
        # while TerrainMap silently came back None.
        self.group = group
        # UnrealEd reports a poly with no material as a null material reference
        # on every build, so unresolved materials get a placeholder instead.
        self.fallback = fallback
        # Ship every texture each material refers to, not only the one drawn.
        # A UT3 material samples a diffuse, a normal, a specular, a mask and
        # often a cubemap; the conversion picks one and the rest never reach
        # UT2004, so a mapper opening the package has nothing to rebuild a
        # material out of. See ut3.objects.material.all_material_textures.
        self.all_textures = all_textures
        # names registered only because of that, for reporting
        self.extra = []
        self.by_material = {}   # (is_import, index) -> texture name
        self.textures = {}      # texture name -> (Package, export)
        self.unresolved = 0
        self.failed = []
        # texture name -> True when some material using it really is transparent
        self.needs_alpha = {}
        # texture name -> 'masked' when some material cuts it out
        self.masked = {}
        # texture name -> the (width, height) actually written, which is what
        # decides a BSP surface's UV scale
        self.exported_size = {}
        # texture name -> (Package, export, channel) when the material keeps its
        # opacity in a separate texture, which UE2 cannot follow -- it has to be
        # composited into this texture's alpha before export.
        self.opacity = {}
        # names whose separate mask was successfully folded in
        self.composited = []
        # names a material wanted cut out but that arrived with no alpha channel
        self.no_alpha_channel = []
        # textures refused as diffuse because they are plainly not colour maps
        self.refused = []
        # texture name -> (channel, tint) when the material states its albedo as
        # one channel of a packed mask rather than shipping a colour map. The
        # pair is part of a texture's identity, not a property of it: one mask
        # tinted two ways is two UT2004 textures.
        self.albedo = {}
        # names whose albedo channel was decoded and tinted on the way out
        self.tinted = []
        # UE2 material objects built for surfaces a flat Texture cannot express
        # -- translucent, additive, modulated or unlit. Empty when --no-materials
        # is given, in which case every surface falls back to its texture, which
        # is what the converter did before Phase 14.
        self.materials = MaterialSet(package_name, self.tag) if materials else None
        # material key -> the MaterialSet name built for it, when there is one
        self.ue2_material = {}
        # material key -> (package, ref, texture name) for every non-opaque
        # material, settled into real objects by build_materials once the
        # textures are written. It has to wait: whether a UE3 translucent
        # surface becomes FB_AlphaBlend or FB_Translucent turns on whether the
        # texture ends up with an alpha channel at all, and only the export
        # knows that.
        self.pending = {}
        # texture name -> True when what was written can be blended by its alpha
        self.alpha_channel = {}
        # what got built, for reporting
        self.built = []
        # materials UT3 itself draws at zero opacity
        self.invisible = []
        # texture names exported as a glow: alpha baked from their own luminance
        self.glow = set()
        # glow name -> how much brighter than its texture the material draws it,
        # already capped against what the texture can take. See bake_self_alpha.
        self.gain = {}
        # names that really were brightened, for reporting
        self.boosted = []
        # texture name -> (channel, colour one, colour two) for a two-tone
        # material, recoloured on the way out
        self.blend = {}
        # names that really were recoloured, for reporting
        self.blended = []

    def name_for(self, ref):
        """UT2004 texture reference for a material; the placeholder if unresolved."""
        name = None
        if ref is not None and not ref.is_null:
            name = self.by_material.get(_material_key(ref))
        if not name:
            if not self.fallback:
                return None
            name = self.FALLBACK_NAME
        return self.path(name)

    def path(self, name):
        """Fully qualified object path for a texture in this package."""
        if not name:
            return None
        if self.group:
            return "%s.%s.%s" % (self.package_name, self.group, name)
        return "%s.%s" % (self.package_name, name)

    def scale_for(self, ref):
        """What to multiply a UE3 surface's TextureU/V by, per axis.

        See UE3_BSP_UV_SCALE: the whole conversion is the exported texture size
        over that constant. A texture the cooker reduced to a smaller mip falls
        out of the same rule rather than needing a correction of its own.
        """
        name = None
        if ref is not None and not ref.is_null:
            name = self.by_material.get(_material_key(ref))
        width, height = self.exported_size.get(name or self.FALLBACK_NAME,
                                               (UE3_BSP_UV_SCALE, UE3_BSP_UV_SCALE))
        return width / UE3_BSP_UV_SCALE, height / UE3_BSP_UV_SCALE

    def drop(self, name, why):
        """Forget a texture that could not be written, so nothing references it."""
        self.textures.pop(name, None)
        self.albedo.pop(name, None)
        for key, value in self.by_material.items():
            if value == name:
                self.by_material[key] = None
        self.failed.append((name, why))

    # An FName is 64 characters (Core/Inc/UnName.h:16). Past that the name is
    # truncated on import while the t3d keeps the full one, so the reference
    # stops matching. UT3 has textures named after the material that flattened
    # them: `M_UN_Volumetrics_Lightbeam_Cheap_02_FloodlightsCold_Flattened` is
    # 61 before the tag is added.
    MAX_NAME = 64

    def _unique(self, base):
        name = self._fit(sanitize(base), "")
        if name not in self.textures:
            return name
        n = 2
        while True:
            candidate = self._fit(sanitize(base), "_%d" % n)
            if candidate not in self.textures:
                return candidate
            n += 1

    def _fit(self, base, counter):
        room = self.MAX_NAME - len(self.tag) - len(counter) - 1
        return "%s_%s%s" % (base[:max(1, room)], self.tag, counter)

    def add_material(self, pkg, index, ref):
        # Keyed by the package too: a PackageIndex only means anything relative
        # to the table that holds it, so index -880 in one content package and
        # -880 in another are unrelated objects. A cooked UT3 map resolves
        # everything through its own table and the index alone was unique; a UDK
        # map draws on dozens of .upk files, and without the package the first
        # material to claim an index answered for every other package's.
        key = _material_key(ref)
        if key in self.by_material:
            return self.by_material[key]
        if self.all_textures:
            self._add_every_texture(pkg, index, ref)
        owner, tex_export, channel, tint = material_albedo(
            pkg, index, ref, reject=self._is_relief_bake)
        # A texture the material itself points at as its albedo channel is not
        # up for refusal on its name: these are called `..._M` precisely because
        # they are masks, and the material has already said which channel of it
        # to draw.
        if channel is None and tex_export is not None \
                and (score_texture_name(tex_export.name) >= NOT_DIFFUSE
                     or self._is_normal_map(owner, tex_export)):
            self.refused.append(tex_export.name)
            tex_export = None
        if tex_export is None:
            self.unresolved += 1
            self.by_material[key] = None
            return None
        # Two materials often share one texture; keep a single copy.
        mode = material_blend_mode(pkg, index, ref)
        transparent = mode == "translucent"
        cutout = mode == "masked"
        albedo = (channel, tint) if channel is not None else None
        blend = diffuse_blend(pkg, index, ref) if albedo is None else None
        name, existed = self.add_texture(owner, tex_export, albedo, blend=blend)
        if existed:
            self.needs_alpha[name] = self.needs_alpha.get(name, False) or transparent
            self.masked[name] = self.masked.get(name, False) or cutout
        else:
            self.needs_alpha[name] = transparent
            self.masked[name] = cutout
        self.by_material[key] = name
        self._note_opacity(name, pkg, index, ref, owner, tex_export)
        self._note_material(key, name, pkg, index, ref)
        return name

    def add_texture(self, owner, export, albedo=None, glow=False, blend=None,
                    gain=1.0):
        """Register one Texture2D; returns (name, was already registered).

        Two materials often share one texture, and a glow shares one with
        whatever else draws it -- so this is keyed on the texture itself rather
        than on the material that asked, and the albedo channel is part of the
        key because one packed mask drawn at two tints is two UT2004 textures.

        `glow` is part of the key for the same reason: a glow copy carries an
        alpha channel baked from its own luminance (see bake_self_alpha), and
        the same texture drawn as an ordinary diffuse must not. So is `gain`:
        UT3's stop lights draw one emissive at 250 times its texture and its
        signs at 8, and two brightnesses of one glow are two UT2004 textures.
        """
        for name, (existing_pkg, existing) in self.textures.items():
            if existing_pkg is owner and existing.index == export.index \
                    and self.albedo.get(name) == albedo \
                    and self.blend.get(name) == blend \
                    and abs(self.gain.get(name, 1.0) - gain) < 1e-6 \
                    and (name in self.glow) == glow:
                return name, True
        suffix = "_Glow" if glow else ("_2T" if blend else "")
        name = self._unique(export.name + suffix)
        self.textures[name] = (owner, export)
        if albedo is not None:
            self.albedo[name] = albedo
        self.needs_alpha.setdefault(name, False)
        self.masked.setdefault(name, False)
        if glow:
            self.glow.add(name)
            self.needs_alpha[name] = True
            if gain > 1.0:
                self.gain[name] = gain
        if blend is not None:
            self.blend[name] = blend
        return name, False

    def _add_every_texture(self, pkg, index, ref):
        """Register everything this material refers to, drawn or not.

        Plain copies: no albedo channel, no tint, no glow bake, so a texture
        that is *also* drawn keeps its own dressed-up entry and this adds the
        undressed one beside it only if the two differ. Nothing here is
        referenced by a material, so the cost is package size and nothing else.
        """
        from ut3.objects.material import all_material_textures

        try:
            every = all_material_textures(pkg, index, ref)
        except (ValueError, IndexError, KeyError, struct.error):
            return
        for owner, export in every:
            name, existed = self.add_texture(owner, export)
            if not existed:
                self.extra.append(name)

    def _note_material(self, key, texture_name, pkg, index, ref):
        """Remember a surface UE2 can say more about than a Texture can.

        Three kinds qualify. A non-opaque one, which needs a FinalBlend to say
        how it meets the framebuffer. An *unlit* opaque one, which needs a
        Shader to stop the lighting pass touching it. And a lit opaque one that
        glows a second texture on top of the one it is painted with, which is
        the pair of slots a Shader exists for.

        The last two used to be skipped, on the grounds that UT3 marks a great
        deal of ordinary geometry unlit and self-illuminating all of it would
        flatten the lighting. Measured rather than assumed, that is not so: 42
        of 487 opaque materials across four maps are unlit, and every one is a
        city sign, an ad board, a holo screen or a sky. 128 more carry a
        separate emissive texture -- signs, street lights, lit windows.
        """
        if self.materials is None or key in self.pending:
            return
        from convert.shaders import surface_style
        from ut3.objects.material import (constant_colour, diffuse_scale,
                                          diffuse_tint, opacity_scale,
                                          resolve_emissive)

        blend, unlit, _two_sided = surface_style(pkg, index, ref)
        glow = None
        if blend == "BLEND_Opaque":
            glow_owner, glow_export = resolve_emissive(pkg, index, ref,
                                                       reject=self._unusable_glow)
            if glow_export is not None:
                glow, _existed = self.add_texture(
                    glow_owner, glow_export, glow=True,
                    gain=self._glow_gain(pkg, index, ref, glow_owner, glow_export))
            elif not unlit:
                # An opaque, lit surface still has something to say if it
                # multiplies its texture by a constant -- 14 of BL-Dekk's 31
                # material instances do, and two of them by a long way. A plain
                # scalar brightness counts as much as a coloured tint: HeatRay's
                # sign boards state theirs that way and nothing else.
                if not (texture_name and constant_colour(pkg, index, ref) is None
                        and (diffuse_tint(pkg, index, ref)
                             or diffuse_scale(pkg, index, ref))):
                    return
        # A material UT3 draws at zero opacity draws nothing, and the only
        # honest conversion is nothing. DM-HeatRay's
        # `M_EFX_Particles_Distortion01` states `Opacity = Constant 0`: it is
        # pure screen distortion, which UE2 cannot do at all, and what UT3
        # shows through it is the scene behind. Building a material for it
        # would cost a draw call for an invisible quad -- but worse, *not*
        # recording it here is what keeps the actor out of the map, since
        # `will_build` is what decides whether an effect mesh is drawn. Left in,
        # the actor is kept, gets no Skins, and wears its flat texture solid.
        if blend != "BLEND_Opaque" and opacity_scale(pkg, index, ref) <= 0.0:
            self.invisible.append(str(ref))
            return
        self.pending[key] = (pkg, ref, texture_name, glow)

    def will_build(self, pkg, index, ref):
        """Would a UE2 material be built for this UE3 material?

        Asked before anything is exported, so it can only answer from what
        add_material already recorded -- which is enough, because the two
        things that decide it (a non-opaque blend mode and something to draw)
        are both known by then.
        """
        if self.materials is None or ref is None or ref.is_null:
            return False
        self.add_material(pkg, index, ref)
        return _material_key(ref) in self.pending

    def build_materials(self, index):
        """Turn every pending non-opaque surface into UE2 material objects."""
        if self.materials is None:
            return 0
        from convert.shaders import build_material

        for key in sorted(self.pending, key=lambda k: (str(k[0]), k[1], k[2])):
            pkg, ref, texture_name, glow = self.pending[key]
            if texture_name is not None and texture_name not in self.textures:
                continue        # the texture failed to export; nothing to dress
            if glow is not None and (glow not in self.textures
                                     or glow not in self.glow):
                # Either the texture failed to export, or its luminance could
                # not be measured and baked. The glow is drawn by an additive
                # specular pass, which does not read that alpha (see
                # build_material), so the bake is no longer what makes the glow
                # work -- but it is still the test that the texture *has*
                # measurable luminance, and `_unusable_glow` is written against
                # exactly the set the baker can handle. A texture that failed it
                # is one nothing should reference, so the surface keeps its flat
                # texture.
                glow = None
            built, description = build_material(
                self.materials, self, pkg, index, ref,
                self.path(texture_name), texture_name, self.path(glow))
            if built is None:
                continue
            self.ue2_material[key] = built
            self.built.append(description)
        return len(self.materials)

    def material_for(self, ref):
        """What a surface wearing this UE3 material should reference.

        The UE2 material object where one was built, otherwise the plain
        texture path `name_for` returns -- so every caller gets an answer and
        the ones that need no material never notice this exists.
        """
        if ref is not None and not ref.is_null:
            built = self.ue2_material.get(_material_key(ref))
            if built is not None:
                return self.materials.bare_path(built)
        return self.name_for(ref)

    def material_class_for(self, ref):
        """`Class'Package.Name'` for a surface, which an actor property wants.

        A t3d polygon names no class and takes `material_for`; `Skins(n)=` and
        every other actor property resolves by exact path and needs this.
        """
        if ref is not None and not ref.is_null:
            built = self.ue2_material.get(_material_key(ref))
            if built is not None:
                return self.materials.path(built)
        name = self.name_for(ref)
        return "Texture'%s'" % name if name else None


    @classmethod
    def _glow_gain(cls, pkg, index, ref, owner, export):
        """How far to brighten a glow texture, capped at what it can take.

        The material's own factor comes from `emissive_gain` and is often large:
        HeatRay's signs state 4, 8, 10 and 15, and UT3's stop lights 250. Applied
        whole it is right for a sparse emissive and wrong for a dense one, which
        simply goes white -- and the texture selection is not always perfect, so
        a 400 landing on a cloud texture would paint the sky.

        The cap is the factor that brings the texture to full brightness *on
        average*. Past that UT3 is relying on a bloom pass that has no UE2
        counterpart, and all a larger number can do here is erase detail. It
        costs nothing in practice: the four HeatRay signs want 4, 8, 10 and 15
        against caps of 272, 37.8, 13.1 and 16.0, so every one is applied whole.
        """
        from ut3.objects.material import emissive_gain

        gain = emissive_gain(pkg, index, ref, owner, export)
        if gain <= 1.0:
            return 1.0
        channels = cls._decode_smallest(owner, export)
        if channels is None:
            return 1.0
        n = min(len(c) for c in channels)
        if not n:
            return 1.0
        mean = sum(0.299 * channels[0][i] + 0.587 * channels[1][i]
                   + 0.114 * channels[2][i] for i in range(n)) / n
        if mean <= 0.0:
            return 1.0
        return min(gain, 255.0 / mean)

    def _unusable_glow(self, owner, export):
        """Can this texture not serve as a glow?

        Several ways. Its name may disqualify it outright -- a normal map, a
        cubemap or a heightmap is not light, and a heightmap drawn as one is
        the rainbow sheen reported on BL-Dekk. Its pixels may say the same. It
        may be featureless -- the engine's placeholder emissive is
        exactly that, `UN_Shaders.T_Diffuse` being 32x32 at mean 128 with a
        spread of zero -- and a flat emissive is an unfilled slot rather than a
        glow, which drawn as one washes the whole surface out. Or its format may
        be one whose luminance cannot be measured, which is also the set
        `bake_self_alpha` cannot bake, so the two stay in step by construction:
        refusing here keeps a texture nothing will reference from being
        exported at all.
        """
        key = ("glow", id(owner), export.index)
        if key not in self._bake_cache:
            name = (export.name or "").lower()
            bad = (score_texture_name(export.name) >= NOT_DIFFUSE
                   # A heightmap is geometry, not light. `score_texture_name`
                   # has no opinion on the word because a heightmap never turns
                   # up as a diffuse candidate; as a glow it does.
                   or "height" in name
                   or self._is_normal_map(owner, export))
            if not bad:
                spread = self._measure_spread(owner, export)
                bad = spread is None or spread <= 1.0
            if not bad:
                bad = self._flat_bright_channel(owner, export)
            self._bake_cache[key] = bad
        return self._bake_cache[key]

    @classmethod
    def _flat_bright_channel(cls, owner, export):
        """Is one channel pinned high across the whole texture?

        Then it is packed data, not light. `T_LT_Floors_SM_Walkpipe01_F` is the
        case reported on WAR-PowerSurge: red averages 72, green 1.8 and blue is
        *255 at every pixel*. A channel that never varies carries no image, and
        added over a surface it is a flat wash of that colour -- the cables, the
        pipes and the whole power node came out violet.

        `_measure_spread` cannot see it: it averages the three channels together,
        so variation in red hides a constant blue. And `_is_normal_map` does not
        either, this being nothing like a tangent-space normal (green is 1.8,
        not 128).

        A channel pinned at *zero* is left alone deliberately -- that is an
        ordinary coloured glow, a red lamp being (200, 10, 10), and it adds
        nothing where it is dark. Only a bright constant does damage. A texture
        flat in every channel is already refused by the spread test above.
        """
        channels = cls._decode_smallest(owner, export)
        if channels is None:
            return False
        for channel in channels:
            if not channel:
                continue
            if max(channel) - min(channel) <= 8 and min(channel) >= 192:
                return True
        return False

    def _is_normal_map(self, owner, export):
        """Does this texture read as a tangent-space normal map?

        Phase 7c refuses normal maps as colour because one drawn as diffuse
        renders in iridescent blue and magenta, and did so on CTF-FacingWorlds'
        cliffs. That refusal is by name, and names run out: TOXIKK's
        `SF_T_TilingBubbles_N_H` ends `_N_H` rather than `_n`, scores a clean 0,
        and paints BL-Dekk's pools with a normal map.

        The pixels are unmistakable where the name is not. A tangent-space
        normal is a unit vector packed around (0, 0, 1), so a flat one is
        (128, 128, 255) and any real one stays near it -- blue pinned high,
        red and green either side of the midpoint. Checked over three maps it
        flags two textures: the one the name already catches, and this one.
        """
        key = ("normal", id(owner), export.index)
        if key not in self._bake_cache:
            means = self._measure_means(owner, export)
            self._bake_cache[key] = bool(
                means and means[2] >= 200
                and 100 <= means[0] <= 160 and 100 <= means[1] <= 160)
        return self._bake_cache[key]

    @classmethod
    def _measure_means(cls, owner, export):
        """Mean R, G, B over the smallest usable mip, or None."""
        channels = cls._decode_smallest(owner, export)
        if channels is None:
            return None
        n = min(len(c) for c in channels)
        if not n:
            return None
        return [sum(c[:n]) / float(n) for c in channels]

    @classmethod
    def _measure_spread(cls, owner, export):
        """max minus min mean-brightness over the smallest mip, or None."""
        channels = cls._decode_smallest(owner, export)
        if channels is None:
            return None
        n = min(len(c) for c in channels)
        if not n:
            return None
        means = [sum(c[i] for c in channels) / 3.0 for i in range(n)]
        return max(means) - min(means)

    @staticmethod
    def _decode_smallest(owner, export):
        """[R, G, B] byte lists for the smallest usable mip, or None."""
        from ut2 import dxt
        from ut3.objects.texture import read_texture

        try:
            texture = read_texture(owner, export)
        except (ValueError, IndexError, KeyError, struct.error):
            return None
        if texture is None:
            return None
        usable = [m for m in texture.mips if m.present and m.width >= 4]
        if not usable:
            return None
        mip = min(usable, key=lambda m: m.width * m.height)
        channels = []
        for channel in range(3):
            try:
                if texture.format == "PF_DXT1":
                    values = dxt.decode_dxt1_channel(mip.data, mip.width, mip.height, channel)
                elif texture.format in ("PF_DXT3", "PF_DXT5"):
                    values = dxt.decode_dxt5_channel(mip.data, mip.width, mip.height, channel)
                elif texture.format == "PF_A8R8G8B8":
                    values = dxt.decode_raw_channel(mip.data, mip.width, mip.height, 4,
                                                    2 - channel)
                elif texture.format == "PF_G8":
                    values = dxt.decode_raw_channel(mip.data, mip.width, mip.height, 1, 0)
                else:
                    return None
            except (ValueError, IndexError, struct.error):
                return None
            if not values:
                return None
            channels.append(values)
        return channels

    def _is_relief_bake(self, owner, export):
        """Is this a per-mesh relief bake rather than a base colour?

        UE3 multiplies some meshes by a light/relief map baked for that mesh
        alone, and it is named _D like any diffuse, so only the pixels tell
        them apart: a bake is near-white and carries almost no colour, because
        everything it does is shade what it is drawn over. Measured on the
        smallest mip, so this costs nothing -- and only asked at all when two
        candidates are equally diffuse-looking by name.

        WAR-PowerSurge's cliffs are the case that needs it: mean 0.81 and
        saturation 0.005 against the tiling rock it should have used. The
        thresholds sit well clear of real grey stone -- the greyest genuine
        diffuse in that map measures 0.58 and 0.028.
        """
        key = (id(owner), export.index)
        if key not in self._bake_cache:
            self._bake_cache[key] = self._measure_bake(owner, export)
        return self._bake_cache[key]

    @staticmethod
    def _measure_bake(owner, export):
        from ut2 import dxt
        from ut3.objects.texture import read_texture

        try:
            texture = read_texture(owner, export)
        except (ValueError, IndexError, KeyError, struct.error):
            return False
        if texture is None:
            return False
        usable = [m for m in texture.mips if m.present and m.width >= 4]
        if not usable:
            return False
        mip = min(usable, key=lambda m: m.width * m.height)
        channels = []
        for channel in range(3):
            try:
                if texture.format == "PF_DXT1":
                    values = dxt.decode_dxt1_channel(mip.data, mip.width, mip.height, channel)
                elif texture.format == "PF_A8R8G8B8":
                    values = dxt.decode_raw_channel(mip.data, mip.width, mip.height, 4,
                                                    2 - channel)
                elif texture.format == "PF_G8":
                    values = dxt.decode_raw_channel(mip.data, mip.width, mip.height, 1, 0)
                else:
                    return False
            except (ValueError, IndexError, struct.error):
                return False
            if not values:
                return False
            channels.append(values)
        n = min(len(c) for c in channels)
        if not n:
            return False
        brightness = saturation = 0.0
        for i in range(n):
            pixel = [c[i] for c in channels]
            brightness += sum(pixel) / 3.0
            saturation += max(pixel) - min(pixel)
        brightness /= n * 255.0
        saturation /= n * 255.0
        return brightness >= BAKE_BRIGHTNESS and saturation <= BAKE_SATURATION

    def _note_opacity(self, name, pkg, index, ref, diffuse_owner, diffuse_export):
        """Record a mask that lives in its own texture, so export can bake it in."""
        if name in self.opacity:
            return
        mask_owner, mask_export, channel = resolve_opacity(pkg, index, ref)
        if mask_export is None:
            return
        if mask_owner is diffuse_owner and mask_export.index == diffuse_export.index:
            return  # already in this texture's own alpha; nothing to do
        self.opacity[name] = (mask_owner, mask_export, channel)


def collect_brush_materials(pkg, index, texture_set):
    """Resolve every material referenced by the map's brush polygons."""
    from ut3.objects.level import is_builder_brush, ordered_exports
    from ut3.objects.model import find_polys, read_polys
    from ut3.props import read_object_properties

    for export in ordered_exports(pkg, {"Brush"}):
        props, start, _end = read_object_properties(pkg, export)
        if start is None or is_builder_brush(pkg, export, props):
            continue
        model = props.get("Brush")
        if model is None or not model.is_export:
            continue
        polys_export = find_polys(pkg, model.export)
        if polys_export is None:
            continue
        for poly in read_polys(pkg, polys_export):
            if not poly.material.is_null:
                texture_set.add_material(pkg, index, poly.material)
    return texture_set


def _albedo_channel(fmt, data, width, height, channel):
    """One RGB channel of a surface as bytes, or None if the format is unhandled.

    `channel` is 0/1/2 for red/green/blue -- the order the material states in
    its parameter name. Note this is not the order `bake_opacity` uses, which
    takes its channel from the opacity resolution in the opposite convention.
    """
    from ut2 import dxt

    if fmt == "PF_DXT1":
        return dxt.decode_dxt1_channel(data, width, height, channel)
    if fmt == "PF_G8":
        return dxt.decode_raw_channel(data, width, height, 1, 0)
    if fmt == "PF_A8R8G8B8":
        # Stored BGRA, so red is the third byte.
        return dxt.decode_raw_channel(data, width, height, 4, 2 - channel)
    return None


def bake_tint(fmt, data, width, height, albedo):
    """Draw one channel of a packed mask as colour. Returns (fmt, data, done).

    A material that states its albedo as `Mask 1 (R=Diffuse, ...)` has no colour
    map to export -- what UE2 must be handed is that channel multiplied by the
    instance's Base Color, as an ordinary texture.
    """
    from ut2 import dxt

    channel, tint = albedo
    values = _albedo_channel(fmt, data, width, height, channel)
    if values is None:
        return fmt, data, False
    return "PF_DXT1", dxt.encode_dxt1_tinted(values, width, height,
                                             tint or (1.0, 1.0, 1.0)), True


def bake_blend(fmt, data, width, height, blend):
    """Recolour a texture with a two-tone material's pair. (fmt, data, done).

    `result = rgb * lerp(colour one, colour two, mask channel)` -- see
    `ut3.objects.material.diffuse_blend`. The output is always DXT1: what comes
    out is full colour rather than one hue at varying brightness, so
    `encode_dxt1_tinted` cannot express it and `encode_dxt1_rgb` finds each
    block's line instead. Alpha is not carried across, which is right here --
    every material doing this is opaque, and on the pipes the alpha *is* the
    mask, so keeping it would leave the shape of the trim in a channel nothing
    reads.
    """
    from ut2 import dxt

    channel, first, second = blend
    if fmt == "PF_DXT1":
        rgb = [dxt.decode_dxt1_channel(data, width, height, c) for c in range(3)]
        mask = rgb[channel] if channel < 3 else None
    elif fmt in ("PF_DXT3", "PF_DXT5"):
        rgb = [dxt.decode_dxt5_channel(data, width, height, c) for c in range(3)]
        mask = (dxt.decode_dxt5_alpha(data, width, height) if channel == 3
                else rgb[channel])
    elif fmt == "PF_A8R8G8B8":
        rgb = [dxt.decode_raw_channel(data, width, height, 4, 2 - c) for c in range(3)]
        mask = (dxt.decode_raw_channel(data, width, height, 4, 3) if channel == 3
                else rgb[channel])
    else:
        return fmt, data, False
    if mask is None or not rgb[0]:
        return fmt, data, False

    n = min(len(mask), min(len(c) for c in rgb))
    pixels = []
    for i in range(n):
        t = mask[i] / 255.0
        out = []
        for c in range(3):
            # Linear space, which is where UE3 does the blend, then the one
            # conversion at the end. Blending the displayed bytes instead
            # would darken every midpoint.
            tint = first[c] + (second[c] - first[c]) * t
            out.append(max(0, min(255, int(round(
                _srgb(_linear(rgb[c][i] / 255.0) * tint) * 255.0)))))
        pixels.append(tuple(out))
    return "PF_DXT1", dxt.encode_dxt1_rgb(pixels, width, height), True


def _linear(value):
    """sRGB byte fraction to linear."""
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def _srgb(value):
    from ut3.objects.graph import linear_to_srgb

    return linear_to_srgb(value)


def bake_self_alpha(fmt, data, width, height, gain=1.0):
    """Put a texture's own luminance in its alpha. Returns (fmt, data, done).

    `gain` brightens the colour first -- see `TextureSet._glow_gain`. UE3 states
    an emissive's strength as a constant beside the texture and a ColorModifier
    cannot brighten, so this is the one place a boost can be carried. The
    multiply is in linear space, which is where UE3 does it; clamping per pixel
    is what UT3's framebuffer does too, minus the bloom.

    Written for `Shader.SelfIllumination` + `SelfIlluminationMask`, which is how
    the glow used to be drawn: the engine reads the mask's alpha and *lerps*
    between the lit diffuse and the glow (D3D9MaterialState.cpp:1082), so the
    alpha had to say where the glow is -- and for a UE3 emissive that is exactly
    how bright it is.

    The glow is an additive specular pass now, which does not read this alpha at
    all, because a lerp is not what UE3 does and on a dark sign it read as
    nothing happening. The bake stays for two reasons that outlived it: it is
    the measurement behind `_unusable_glow`'s featureless test, and it is what
    decides which formats can serve as a glow at all, so the two remain in step
    by construction.
    """
    from ut2 import dxt

    if fmt == "PF_DXT1":
        channels = [dxt.decode_dxt1_channel(data, width, height, c) for c in range(3)]
    elif fmt in ("PF_DXT3", "PF_DXT5"):
        # Its existing alpha is overwritten, and that is the right call: half of
        # UT3's emissive textures are DXT5, and what their alpha holds is not a
        # glow mask -- the material never samples it, because a UE3 emissive is
        # read as colour and its brightness *is* the mask.
        channels = [dxt.decode_dxt5_channel(data, width, height, c) for c in range(3)]
    elif fmt == "PF_A8R8G8B8":
        channels = [dxt.decode_raw_channel(data, width, height, 4, 2 - c) for c in range(3)]
    elif fmt == "PF_G8":
        grey = dxt.decode_raw_channel(data, width, height, 1, 0)
        channels = [grey, grey, grey]
    else:
        return fmt, data, False
    if not channels or not channels[0]:
        return fmt, data, False
    n = min(len(c) for c in channels)
    if gain > 1.0:
        channels = [[max(0, min(255, int(round(
            _srgb(_linear(channels[c][i] / 255.0) * gain) * 255.0))))
            for i in range(n)] for c in range(3)]
    # Rec. 601 luma: a glow reads by brightness, and green carries most of it.
    values = [min(255, int(0.299 * channels[0][i] + 0.587 * channels[1][i]
                           + 0.114 * channels[2][i])) for i in range(n)]
    if gain > 1.0 and fmt != "PF_A8R8G8B8":
        # The colour has changed, so the blocks have to be built again rather
        # than copied through. DXT5's colour half is DXT1's layout exactly, so
        # one encode serves every compressed source format.
        pixels = [(channels[0][i], channels[1][i], channels[2][i])
                  for i in range(n)]
        colour = dxt.encode_dxt1_rgb(pixels, width, height)
        return "PF_DXT5", dxt.dxt1_with_alpha(colour, values, width, height), True
    if fmt == "PF_DXT1":
        return "PF_DXT5", dxt.dxt1_with_alpha(data, values, width, height), True
    if fmt in ("PF_DXT3", "PF_DXT5"):
        return "PF_DXT5", dxt.dxt5_with_alpha(data, values, width, height), True
    out = bytearray(data)
    if fmt == "PF_A8R8G8B8":
        for i, value in enumerate(values):
            at = i * 4 + 3
            if at < len(out):
                out[at] = value
                if gain > 1.0:
                    for c in range(3):
                        out[i * 4 + 2 - c] = channels[c][i]
        return fmt, bytes(out), True
    return fmt, data, False


def bake_opacity(fmt, data, width, height, mask, index, max_size=DEFAULT_MAX_SIZE):
    """Fold a separate mask texture into `data`'s alpha. Returns (fmt, data, done).

    UE2 masks a surface by the alpha of the texture it draws and nothing else,
    so a UE3 material sampling its opacity from a second texture has to have it
    baked in here. DXT1 has no alpha to bake into, so those are repacked as DXT5
    -- colour blocks copied through untouched, since DXT5's colour half is
    DXT1's layout exactly.
    """
    from ut2 import dxt
    from ut3.objects.texture import read_texture

    mask_owner, mask_export, channel = mask
    mask_texture = read_texture(mask_owner, mask_export, index)
    if mask_texture is None:
        return fmt, data, False
    # Prefer the mip that already matches; the mask is often authored smaller.
    candidates = [m for m in mask_texture.mips
                  if m.present and m.width <= max_size and m.height <= max_size]
    if not candidates:
        return fmt, data, False
    exact = [m for m in candidates if m.width == width and m.height == height]
    mip = exact[0] if exact else max(candidates, key=lambda m: m.width * m.height)

    if mask_texture.format == "PF_DXT1":
        values = dxt.decode_dxt1_channel(mip.data, mip.width, mip.height,
                                         {2: 0, 1: 1, 0: 2}.get(channel, 0))
    elif mask_texture.format in ("PF_DXT3", "PF_DXT5"):
        values = dxt.decode_dxt5_alpha(mip.data, mip.width, mip.height)
    elif mask_texture.format == "PF_G8":
        values = dxt.decode_raw_channel(mip.data, mip.width, mip.height, 1, 0)
    elif mask_texture.format == "PF_A8R8G8B8":
        values = dxt.decode_raw_channel(mip.data, mip.width, mip.height, 4, channel)
    else:
        return fmt, data, False
    values = dxt.resample(values, mip.width, mip.height, width, height)

    if fmt == "PF_DXT1":
        return "PF_DXT5", dxt.dxt1_with_alpha(data, values, width, height), True
    if fmt in ("PF_DXT3", "PF_DXT5"):
        return "PF_DXT5", dxt.dxt5_with_alpha(data, values, width, height), True
    if fmt == "PF_A8R8G8B8":
        out = bytearray(data)
        for i, value in enumerate(values):
            at = i * 4 + 3
            if at < len(out):
                out[at] = value
        return fmt, bytes(out), True
    return fmt, data, False


def export_textures(texture_set, out_dir, index, max_size=DEFAULT_MAX_SIZE, group=None,
                    extra_exec=None):
    """Write the surfaces and the generated .uc. Returns (written, skipped)."""
    group = group or texture_set.group
    package = texture_set.package_name
    classes_dir = os.path.join(out_dir, package, "Classes")
    textures_dir = os.path.join(out_dir, package, "Textures")
    os.makedirs(classes_dir, exist_ok=True)
    os.makedirs(textures_dir, exist_ok=True)

    exec_lines = []
    written = 0
    if texture_set.fallback:
        path, options = write_placeholder(os.path.join(textures_dir, texture_set.FALLBACK_NAME))
        texture_set.exported_size[texture_set.FALLBACK_NAME] = (PLACEHOLDER_SIZE,
                                                                PLACEHOLDER_SIZE)
        opts = " ".join("%s=%s" % (k, v) for k, v in sorted(options.items()))
        exec_lines.append(
            "#exec TEXTURE IMPORT NAME=%s GROUP=%s FILE=Textures\\%s %s"
            % (texture_set.FALLBACK_NAME, group, os.path.basename(path), opts)
        )
        written += 1
    for name in sorted(list(texture_set.textures)):  # list(): drop() mutates the dict
        owner, export = texture_set.textures[name]
        texture = read_texture(owner, export, index)
        if texture is None:
            texture_set.drop(name, "unreadable")
            continue
        mip = texture.largest
        # Step down mips rather than rescaling: UE3 already generated them.
        candidates = [m for m in texture.mips
                      if m.present and m.width <= max_size and m.height <= max_size]
        if candidates:
            mip = max(candidates, key=lambda m: m.width * m.height)
        if mip is None:
            texture_set.drop(name, "no mip data")
            continue
        fmt, data = texture.format, mip.data
        # Everything below the chosen mip, in order, so the .dds can carry a
        # real chain. UT2004 will not build one for a DXT texture -- see
        # ut2/images.py -- and a mip-less world texture aliases to noise the
        # moment it is seen at an angle or a distance.
        smaller = [(m.width, m.height, m.data) for m in texture.mips
                   if m.present and m.width < mip.width]
        smaller.sort(key=lambda m: -m[0] * m[1])
        chain = [(mip.width, mip.height, data)] + smaller
        albedo = texture_set.albedo.get(name)
        if albedo is not None:
            tinted_chain = []
            tinted = False
            for level_width, level_height, level in chain:
                level_fmt, level, done = bake_tint(
                    texture.format, level, level_width, level_height, albedo)
                tinted = tinted or done
                tinted_chain.append((level_width, level_height, level))
                fmt = level_fmt
            if tinted:
                texture_set.tinted.append(name)
                chain = tinted_chain
                data = chain[0][2]

        recolour = texture_set.blend.get(name)
        if recolour is not None:
            blend_chain = []
            done = False
            source_fmt = fmt
            for level_width, level_height, level in chain:
                level_fmt, level, ok = bake_blend(
                    source_fmt, level, level_width, level_height, recolour)
                done = done or ok
                blend_chain.append((level_width, level_height, level))
                fmt = level_fmt
            if done:
                texture_set.blended.append(name)
                chain = blend_chain
                data = chain[0][2]

        if name in texture_set.glow:
            glow_chain = []
            baked = False
            source_fmt = fmt
            # One gain for the whole chain, measured on the texture rather than
            # on each mip: a per-level factor would make the glow change
            # brightness with distance.
            gain = texture_set.gain.get(name, 1.0)
            for level_width, level_height, level in chain:
                level_fmt, level, done = bake_self_alpha(
                    source_fmt, level, level_width, level_height, gain)
                baked = baked or done
                glow_chain.append((level_width, level_height, level))
                fmt = level_fmt
            if baked:
                chain = glow_chain
                data = chain[0][2]
                if gain > 1.0:
                    texture_set.boosted.append(name)
            else:
                texture_set.glow.discard(name)

        mask = texture_set.opacity.get(name)
        if mask is not None:
            baked_chain = []
            baked = False
            # What the chain holds now, which the tint bake above may have
            # changed. Re-reading `fmt` inside the loop would feed level 2 the
            # format level 1 was converted *to*.
            source_fmt = fmt
            for level_width, level_height, level in chain:
                # The mask is resampled to each level in turn, so the cutout
                # survives all the way down instead of only in the top mip.
                level_fmt, level, done = bake_opacity(
                    source_fmt, level, level_width, level_height, mask,
                    index, max_size)
                baked = baked or done
                baked_chain.append((level_width, level_height, level))
                fmt = level_fmt
            if baked:
                texture_set.composited.append(name)
                chain = baked_chain
                data = chain[0][2]
        path, options = write_texture(
            os.path.join(textures_dir, name), mip.width, mip.height, fmt, data,
            mips=chain
        )
        # Only the material can say what the alpha channel means: a hard cutout
        # (MASKED) or real blending (ALPHA). Cutouts win when both apply -- but
        # neither means anything without an alpha channel to test, and DXT1 has
        # none. Claiming MASKED there is worse than leaving it opaque: DXT1's
        # three-colour blocks decode their fourth index as transparent black,
        # which UE3 never samples but UE2 would, punching holes at random.
        options.pop("ALPHA", None)
        if fmt == "PF_DXT1":
            if texture_set.masked.get(name) or texture_set.needs_alpha.get(name):
                texture_set.no_alpha_channel.append(name)
        elif texture_set.masked.get(name):
            options["MASKED"] = 1
            texture_set.alpha_channel[name] = True
        else:
            options["ALPHA"] = 1 if texture_set.needs_alpha.get(name) else 0
            texture_set.alpha_channel[name] = bool(texture_set.needs_alpha.get(name))
        # The size written, not the size UT3 declares: BSP surface UVs are
        # restated against it (see UE3_BSP_UV_SCALE).
        texture_set.exported_size[name] = (float(mip.width), float(mip.height))
        if path is None:
            texture_set.drop(name, "unsupported format %s" % texture.format)
            continue
        opts = " ".join("%s=%s" % (k, v) for k, v in sorted(options.items()))
        exec_lines.append(
            "#exec TEXTURE IMPORT NAME=%s GROUP=%s FILE=Textures\\%s %s"
            % (name, group, os.path.basename(path), opts)
        )
        written += 1

    # Only now is it settled which textures survived and which of them carry a
    # usable alpha channel, and a generated material needs both facts.
    texture_set.build_materials(index)
    materials = texture_set.materials
    material_lines = materials.emit() if materials else []

    uc_path = os.path.join(classes_dir, "%s.uc" % package)
    with open(uc_path, "wb") as f:
        body = [
            "// Generated by ut3conv -- textures converted from UT3.",
            "// Build with: ucc make",
            "class %s extends Object" % package,
            "    abstract;",
            "",
        ]
        body.extend(exec_lines)
        # Meshes must come after the textures: the ASE importer resolves
        # *BITMAP names against textures that are already loaded.
        if extra_exec:
            body.append("")
            body.extend(extra_exec)
        # `abstract` is deliberate and unrelated: nothing ever instances this
        # class, it exists to carry the #exec lines and now the materials too.
        if material_lines:
            body.extend(["", materials.declaration()])
        body.extend(["", "defaultproperties", "{"])
        body.extend(material_lines)
        body.extend(["}", ""])
        f.write("\r\n".join(body).encode("latin-1", "replace"))
    return written, uc_path
