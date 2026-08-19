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

# ut2.images.write_placeholder's own default; recorded so the fallback texture
# scales its surfaces like any other.
PLACEHOLDER_SIZE = 128.0
from ut3.objects.material import (material_blend_mode, resolve_diffuse,
                                  resolve_opacity, score_texture_name)
from ut3.objects.texture import read_texture

_SANITIZE = re.compile(r"[^A-Za-z0-9_]")

# UT2004 handles 2048 but 1024 is the practical ceiling for a UE2 world texture.
DEFAULT_MAX_SIZE = 1024

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


class TextureSet:
    """Resolved materials, de-duplicated by the texture they land on."""

    BASE_FALLBACK_NAME = "DefaultBSP"

    def __init__(self, package_name, fallback=True, group="BSP"):
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

    def name_for(self, ref):
        """UT2004 texture reference for a material; the placeholder if unresolved."""
        name = None
        if ref is not None and not ref.is_null:
            name = self.by_material.get((ref.is_import, ref.index))
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
            name = self.by_material.get((ref.is_import, ref.index))
        width, height = self.exported_size.get(name or self.FALLBACK_NAME,
                                               (UE3_BSP_UV_SCALE, UE3_BSP_UV_SCALE))
        return width / UE3_BSP_UV_SCALE, height / UE3_BSP_UV_SCALE

    def drop(self, name, why):
        """Forget a texture that could not be written, so nothing references it."""
        self.textures.pop(name, None)
        for key, value in self.by_material.items():
            if value == name:
                self.by_material[key] = None
        self.failed.append((name, why))

    def _unique(self, base):
        name = "%s_%s" % (sanitize(base), self.tag)
        if name not in self.textures:
            return name
        n = 2
        while "%s_%d" % (name, n) in self.textures:
            n += 1
        return "%s_%d" % (name, n)

    def add_material(self, pkg, index, ref):
        key = (ref.is_import, ref.index)
        if key in self.by_material:
            return self.by_material[key]
        owner, tex_export = resolve_diffuse(pkg, index, ref, reject=self._is_relief_bake)
        if tex_export is not None and score_texture_name(tex_export.name) >= NOT_DIFFUSE:
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
        for name, (existing_pkg, existing) in self.textures.items():
            if existing_pkg is owner and existing.index == tex_export.index:
                self.by_material[key] = name
                self.needs_alpha[name] = self.needs_alpha.get(name, False) or transparent
                self.masked[name] = self.masked.get(name, False) or cutout
                self._note_opacity(name, pkg, index, ref, owner, tex_export)
                return name
        name = self._unique(tex_export.name)
        self.textures[name] = (owner, tex_export)
        self.needs_alpha[name] = transparent
        self.masked[name] = cutout
        self.by_material[key] = name
        self._note_opacity(name, pkg, index, ref, owner, tex_export)
        return name


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
        mask = texture_set.opacity.get(name)
        if mask is not None:
            baked_chain = []
            baked = False
            for level_width, level_height, level in chain:
                # The mask is resampled to each level in turn, so the cutout
                # survives all the way down instead of only in the top mip.
                level_fmt, level, done = bake_opacity(
                    texture.format, level, level_width, level_height, mask,
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
        else:
            options["ALPHA"] = 1 if texture_set.needs_alpha.get(name) else 0
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
        body.extend(["", "defaultproperties", "{", "}", ""])
        f.write("\r\n".join(body).encode("latin-1", "replace"))
    return written, uc_path
