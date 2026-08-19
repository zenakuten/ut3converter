#!/usr/bin/env python3
"""Regression tests for texture conversion (Phase 1c).

    python3 tests/test_textures.py [path/to/DM-HeatRay.ut3]
"""

import glob
import os
import struct
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.geometry import convert_brushes
from convert.textures import TextureSet, collect_brush_materials, export_textures
from ut3.objects.texture import expected_size, read_texture
from ut3.package import Package
from ut3.resolve import PackageIndex

DEFAULT_MAP = (
    "/home/josh/.steam/steam/steamapps/common/Unreal Tournament 3/"
    "UTGame/CookedPC/Maps/DM-HeatRay.ut3"
)

_failures = []


def check(label, got, want):
    if got == want:
        print("  ok    %s = %r" % (label, got))
    else:
        print("  FAIL  %s = %r (expected %r)" % (label, got, want))
        _failures.append(label)


def check_that(label, cond, detail=""):
    if cond:
        print("  ok    %s %s" % (label, detail))
    else:
        print("  FAIL  %s %s" % (label, detail))
        _failures.append(label)


def main(path):
    p = Package(path)
    index = PackageIndex.for_map(path)

    print("package index")
    check_that("finds the CookedPC tree", len(index.paths) > 500, "%d packages" % len(index.paths))
    check_that("locates ASC_Base.upk", index.path_for("ASC_Base") is not None)

    print("texture reading")
    export = p.find("T_ASC_Base_BSP_Concrete01_N")[0]
    texture = read_texture(p, export, index)
    check("format", texture.format, "PF_DXT1")
    check("declared size", (texture.width, texture.height), (2048, 2048))
    biggest = texture.largest
    # The cooker strips the top mip of 2048s; 1024 is what ships.
    check("largest available mip", (biggest.width, biggest.height), (1024, 1024))
    check("mip payload size", len(biggest.data),
          expected_size(texture.format, biggest.width, biggest.height))
    check_that("every mip with a payload is correctly sized",
               all(len(m.data) == expected_size(texture.format, m.width, m.height)
                   for m in texture.mips if m.present))

    total = readable = 0
    for e in p.exports_of_class("Texture2D"):
        total += 1
        t = read_texture(p, e, index)
        if t is not None and t.largest is not None:
            readable += 1
    check("every Texture2D yields pixel data", readable, total)

    print("material resolution")
    texture_set = TextureSet("TestTex")
    collect_brush_materials(p, index, texture_set)
    resolved = sum(1 for v in texture_set.by_material.values() if v)
    check_that("most BSP materials resolve to a texture",
               resolved >= len(texture_set.by_material) - 3,
               "%d of %d" % (resolved, len(texture_set.by_material)))
    diffuse_like = [n for n in texture_set.textures if not re.search(r"_N$|Normal", n)]
    check_that("resolved textures look like diffuse maps, not normal maps",
               len(diffuse_like) == len(texture_set.textures),
               "%d of %d" % (len(diffuse_like), len(texture_set.textures)))

    print("export")
    out_dir = tempfile.mkdtemp(prefix="ut3conv-tex-")
    try:
        written, uc_path = export_textures(texture_set, out_dir, index, max_size=1024)
        check_that("textures written", written > 20, str(written))
        files = glob.glob(os.path.join(out_dir, "TestTex", "Textures", "*"))
        # +1 for the placeholder that unresolved materials fall back to.
        check("a file per surviving texture, plus the placeholder",
              len(files), len(texture_set.textures) + 1)
        check_that("the placeholder was written",
                   any(os.path.basename(f).startswith(texture_set.FALLBACK_NAME) for f in files))

        with open(uc_path, "rb") as fh:
            uc_bytes = fh.read()
        uc = uc_bytes.decode("latin-1")
        declared = set(re.findall(r"NAME=(\S+)", uc))
        on_disk = {os.path.splitext(os.path.basename(f))[0] for f in files}
        check("every declared texture has a file", sorted(declared - on_disk), [])
        check("every file is declared", sorted(on_disk - declared), [])
        # UCC needs CRLF Latin-1 and chokes on backslashes inside comments.
        check_that("uc is CRLF and free of high bytes",
                   b"\r\n" in uc_bytes and all(b < 0x80 for b in uc_bytes))
        comment_lines = [ln for ln in uc.splitlines() if ln.strip().startswith("//")]
        check_that("no backslashes in generated comments",
                   all("\\" not in ln for ln in comment_lines))

        for f in files:
            if f.endswith(".dds"):
                with open(f, "rb") as fh:
                    head = fh.read(128)
                check_that("dds header for %s" % os.path.basename(f),
                           head[:4] == b"DDS " and len(head) == 128
                           and head[84:88] in (b"DXT1", b"DXT3", b"DXT5"))
                break

        print("alpha and UV scale")
        # UE3 packs spec/gloss masks into diffuse alpha, so DXT5 alone must not
        # imply transparency -- only the material's Opacity input does.
        alpha = [n for n, v in texture_set.needs_alpha.items() if v]
        check_that("almost nothing is flagged transparent",
                   len(alpha) <= 2, "%d of %d: %s" % (len(alpha), len(texture_set.textures), alpha))
        check_that("the DXT5 floor texture is NOT transparent",
                   not texture_set.needs_alpha.get("T_LT_Floors_BSP_Organic05b_D"))
        check_that("a fence texture is not left opaque",
                   any("Fence" in n for n in alpha) or any("Fence" in n for n in texture_set.masked),
                   str(alpha))
        # BLEND_Masked wants a hard cutout, not blending: MASKED=1, not ALPHA=1.
        masked = [n for n, v in texture_set.masked.items() if v]
        check_that("cutout materials are masked, not alpha-blended",
                   masked and not (set(masked) & set(alpha)),
                   "%d masked, %d alpha" % (len(masked), len(alpha)))
        # UE3 states BSP surface UVs against a fixed 128 whatever texture the
        # surface wears; UE2 states them against the texture size. So a
        # surface's scale is restated in terms of the size actually exported,
        # and a texture the cooker reduced falls out of the same rule rather
        # than needing a correction of its own.
        from convert.textures import UE3_BSP_UV_SCALE

        check("the fallback placeholder is the reference size",
              texture_set.exported_size[texture_set.FALLBACK_NAME],
              (UE3_BSP_UV_SCALE, UE3_BSP_UV_SCALE))
        check_that("so it leaves its surfaces alone",
                   texture_set.scale_for(None) == (1.0, 1.0))
        sizes = {n: v[0] for n, v in texture_set.exported_size.items()}
        check_that("nothing was exported above the cap",
                   max(sizes.values()) <= 1024, "largest %d" % max(sizes.values()))
        # The case that made this visible: a 2048 brick reduced to 1024. UT3
        # gives it |TextureU| 0.25 for a 512uu repeat; 1024/128 = 8 restates
        # that as 2.0, which is 1024/2.0 = the same 512uu.
        brick = "T_HU_Walls_BSP_BrickA01_blue_D"
        if brick in texture_set.exported_size:
            factor = texture_set.exported_size[brick][0] / UE3_BSP_UV_SCALE
            check("a 1024 export scales its surfaces by 8", factor, 8.0)
            check("so UT3's 0.25 becomes 2.0", 0.25 * factor, 2.0)
            check("both meaning a 512uu repeat",
                  texture_set.exported_size[brick][0] / (0.25 * factor),
                  UE3_BSP_UV_SCALE / 0.25)

        # Textures live in a group, so references must be Package.Group.Object.
        # Actor properties resolve by exact path; only the polygon importer has
        # an ANY_PACKAGE fallback, which masked this for BSP but left TerrainMap
        # silently None.
        ref = texture_set.name_for(None)
        check_that("references are fully qualified with the group",
                   ref and ref.count(".") == 2, str(ref))
        check("group path", texture_set.path("Foo"), "TestTex.BSP.Foo")

        print("t3d consistency")
        brushes, _stats = convert_brushes(p, texture_set=texture_set)
        refs = set()
        for brush in brushes:
            for poly in brush.polygons:
                if poly.texture:
                    refs.add(poly.texture.rsplit(".", 1)[1])
        check("no t3d reference lacks a texture file", sorted(refs - on_disk), [])
        # UnrealEd flags a poly with no material as a null material reference on
        # every build, so every polygon must carry a texture.
        untextured = [q for b in brushes for q in b.polygons if not q.texture]
        check("no polygon is left without a material", len(untextured), 0)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    print("separate opacity masks")
    # UE3 graphs sample opacity from whatever texture they like: 12 of
    # DM-HeatRay's masked materials keep it in a "_M" beside the "_D". UE2 masks
    # by the drawn texture's own alpha and nothing else, so it has to be baked
    # in or the cutout never happens.
    from ut2 import dxt
    from ut3.objects.material import resolve_opacity

    foliage = index.package("UN_Foliage")
    plant = [e for e in foliage.exports
             if e.name == "M_UN_Foliage_SM_PlantMix01"][0]
    mask_owner, mask_export, channel = resolve_opacity(foliage, index,
                                                       foliage.ref(plant.index))
    check("the ivy mask is its own texture", mask_export.name,
          "T_UN_Foliage_SM_PlantMix01_M")
    check("read from the red channel (BGRA byte 2)", channel, 2)

    # A DXT5 block is an 8-byte alpha block then a colour block in exactly
    # DXT1's layout, so an opaque DXT1 block survives the repack byte for byte.
    opaque = struct.pack("<HHI", 0xF800, 0x001F, 0x1B1B1B1B)
    alpha = bytes([255] * 16)
    packed = dxt.dxt1_with_alpha(opaque, alpha, 4, 4)
    check("a repacked block is DXT5-sized", len(packed), 16)
    check("its colour half is untouched", packed[8:16], opaque)
    check("and it decodes as fully opaque",
          set(dxt.decode_dxt5_alpha(packed, 4, 4)), {255})

    # DXT1's three-colour mode decodes its fourth index as transparent *black*,
    # which UE3 never samples but a straight reinterpretation as DXT5 would.
    punch = struct.pack("<HHI", 0x001F, 0xF800, 0xFFFFFFFF)
    fixed = dxt.dxt1_with_alpha(punch, alpha, 4, 4)
    c0, c1 = struct.unpack_from("<HH", fixed, 8)
    check_that("a punchthrough block is forced back to four colours", c0 > c1,
               "0x%04X > 0x%04X" % (c0, c1))
    check_that("and its texels are no longer black",
               any(dxt.decode_dxt1_channel(fixed[8:16], 4, 4, 0)))

    cut = dxt.dxt1_with_alpha(opaque, bytes([0] * 16), 4, 4)
    check("an all-masked block decodes transparent",
          set(dxt.decode_dxt5_alpha(cut, 4, 4)), {0})
    check("resampling is nearest-neighbour",
          bytes(dxt.resample(bytes([0, 255]), 2, 1, 4, 1)), bytes([0, 0, 255, 255]))

    print("picking the diffuse out of a material graph")
    from ut3.objects.material import resolve_diffuse, score_texture_name

    # A cubemap is a reflection probe and never the diffuse. It used to win
    # anyway: the old scoring matched "_c" anywhere in a name, and
    # "T_UN_CubeMaps_Robot_Paint01" contains one, so it read as a colour map.
    check_that("a cubemap scores worse than a real diffuse",
               score_texture_name("T_UN_CubeMaps_Robot_Paint01")
               > score_texture_name("T_LT_Floors_BSP_Organic11_D"))
    check_that("and worse than a specular map",
               score_texture_name("T_UN_CubeMaps_Robot_Paint01")
               > score_texture_name("T_LT_Floors_BSP_Organic11_S"))
    check_that("a normal map is still the worst kind of colour map",
               score_texture_name("UN_DetailTex_Crackle2_N") > 0)
    check("a plain diffuse suffix wins", score_texture_name("Foo_D"), -20)

    # The graph reaches the cubemap two levels before the texture the surface is
    # actually painted with, so first-found is not good enough -- every
    # reachable sample is collected and the best-named one wins.
    floors = index.package("LT_Floors")
    if floors is None:
        print("  --    LT_Floors not installed, skipping the graph check")
    else:
        master = [e for e in floors.exports
                  if e.name == "M_LT_Floors_BSP_Master"][0]
        _owner, diffuse = resolve_diffuse(floors, index, floors.ref(master.index))
        check("DM-Deck's floor master resolves past the cubemap",
              diffuse.name if diffuse else None, "T_LT_Floors_BSP_Organic11_D")

    print("relief bakes")
    # UE3 multiplies some meshes by a light/relief map baked for that mesh
    # alone. It is named _D like any diffuse, so only the pixels separate them:
    # near-white with almost no colour. WAR-PowerSurge's cliffs picked the bake
    # over the tiling rock they are actually painted with.
    from convert.textures import BAKE_BRIGHTNESS, BAKE_SATURATION
    from ut3.objects.material import resolve_diffuse

    bakes = [e for e in p.exports
             if p.class_name_of(e) == "Material"
             and e.name == "M_UN_Cave_SM_Rocky_Mesh_Clustera"]
    if bakes:
        ref = p.ref(bakes[0].index)
        plain = resolve_diffuse(p, index, ref)[1]
        picked = resolve_diffuse(p, index, ref, reject=texture_set._is_relief_bake)[1]
        check_that("the relief bake is what names alone would pick",
                   getattr(plain, "name", None) == "T_UN_Rock_SM_Cliffs01_D",
                   getattr(plain, "name", None))
        check_that("but the pixels send it to the tiling rock instead",
                   getattr(picked, "name", None) == "T_UN_Cave_Rock_Wall_Chunks_D",
                   getattr(picked, "name", None))
    check_that("the thresholds stay clear of real grey stone",
               BAKE_BRIGHTNESS > 0.58 and BAKE_SATURATION < 0.028,
               "%.2f / %.3f vs T_UN_Cave_Rock_Floor_D at 0.58 / 0.028"
               % (BAKE_BRIGHTNESS, BAKE_SATURATION))

    print("texture names are unique across packages")
    # The ASE importer does not resolve *BITMAP by path: it walks every
    # UMaterial in memory comparing GetName() and takes the first match
    # (UnStaticMesh.cpp:680), so two converted maps defining one texture name
    # are decided by build order. Four maps converted here shared 47 names and
    # WAR-PowerSurge's rocks bound to CTFFacingWorldsTex's copy of
    # T_UN_Rock2_BSP_Rock03.
    from convert.textures import package_tag

    a, b = TextureSet("WARPowerSurgeTex"), TextureSet("CTFFacingWorldsTex")
    check_that("the same source texture lands on different names",
               a._unique("T_UN_Rock2_BSP_Rock03") != b._unique("T_UN_Rock2_BSP_Rock03"),
               "%s vs %s" % (a._unique("T_UN_Rock2_BSP_Rock03"),
                             b._unique("T_UN_Rock2_BSP_Rock03")))
    check_that("the placeholder is per-package too",
               a.FALLBACK_NAME != b.FALLBACK_NAME,
               "%s vs %s" % (a.FALLBACK_NAME, b.FALLBACK_NAME))
    check_that("the tag is stable across runs",
               package_tag("WARPowerSurgeTex") == a.tag and len(a.tag) == 4, a.tag)
    check_that("two TextureSets on one package agree",
               TextureSet("DMDeckTex").tag == TextureSet("DMDeckTex").tag)
    # FName caps at 64 and UT3's own names already reach 39.
    longest = max((len(n) for n in texture_set.textures), default=0)
    check_that("tagged names stay well inside FName's 64", longest <= 55,
               "longest %d" % longest)

    print("names that are never a diffuse")
    # A normal map or a cubemap is not a base colour whatever else the name
    # says, and a bonus must not be able to buy it back under the threshold.
    # CTF-LostCause's water is why: T_Base_Tile_DetailNormal scores +100 for
    # "normal" and -20 for "base" -- part of the asset name, not a claim -- and
    # 80 slipped under NOT_DIFFUSE, painting the sheet in iridescent blue and
    # magenta where UT3 draws water.
    from convert.textures import NOT_DIFFUSE
    from ut3.objects.material import DISQUALIFIED, score_texture_name

    for name in ("T_Base_Tile_DetailNormal", "T_UN_Rock_SM_Cliffs01_N",
                 "T_Foo_Normalmap", "T_UN_CubeMaps_Robot_Paint01",
                 "T_Base_Color_CubeMap"):
        check("%s is refused outright" % name, score_texture_name(name), DISQUALIFIED)
    check_that("and it is well past the threshold that refuses one",
               DISQUALIFIED >= NOT_DIFFUSE)
    # Real colour maps must keep scoring as before, bonuses and all.
    check("a plain diffuse is unaffected", score_texture_name("T_HU_Walls_BSP_Block03_D"), -20)
    check("an unsuffixed base colour too", score_texture_name("T_UN_Foliage_SM_Bark01"), 0)
    check_that("a specular map is still merely discouraged, not disqualified",
               0 < score_texture_name("T_Foo_S") < DISQUALIFIED)

    print("a material named after its texture")
    # UE3 names assets in pairs, and that is the one signal strong enough to beat
    # the name scoring when a base colour carries no _D suffix at all.
    # M_UN_Foliage_SM_Bark01_Fresnel reaches its own unsuffixed bark (score 0)
    # and T_UN_Foliage_SM_Tree_TilingBark_02_D, a tiling overlay that wins on the
    # suffix alone (-20) and paints the tree in the wrong bark.
    from ut3.objects.material import names_the_texture

    check_that("a material is named after its own texture",
               names_the_texture("M_UN_Foliage_SM_Bark01_Fresnel",
                                 "T_UN_Foliage_SM_Bark01"))
    check_that("an unrelated overlay is not",
               not names_the_texture("M_UN_Foliage_SM_Bark01_Fresnel",
                                     "T_UN_Foliage_SM_Tree_TilingBark_02_D"))
    # An equal name is not a claim. M_UN_Sky_SM_Invasion2 samples both its
    # namesake and T_UN_Sky_SM_CloudsSun, and the dome is painted with CloudsSun
    # through UV channel 1 -- the reason the sky does not pinch at its apex.
    check_that("an equal name is left to the scoring",
               not names_the_texture("M_UN_Sky_SM_Invasion2", "T_UN_Sky_SM_Invasion2"))
    # The boundary has to be a separator, or Bark0 would claim Bark01.
    check_that("a partial word is not a match",
               not names_the_texture("M_UN_Foliage_SM_Bark01_Fresnel",
                                     "T_UN_Foliage_SM_Bark0"))
    # The rule must not undo the relief-bake rejection: PowerSurge's cliffs are
    # painted by a tiling rock, not by the per-mesh bake the material is
    # nearly named after. Stripping the texture's _D first would match it.
    check_that("a _D texture cannot claim a _Master material's name",
               not names_the_texture("M_UN_Rock_SM_Cliffs01_Master",
                                     "T_UN_Rock_SM_Cliffs01_D"))
    check_that("nor can an empty name match anything",
               not names_the_texture("", "T_Foo") and not names_the_texture("M_Foo", ""))

    print("an instance that overrides only a shade map")
    # A MaterialInstanceConstant often overrides nothing that is colour at all.
    # WAR-Serenity's cliffs wear M_UN_Rock_SM_Cliffs01_MI_SideA_05, whose only
    # non-normal override is a "ShadeMap" naming the per-mesh relief bake, while
    # its parent names T_UN_Terrain_FloorStone_Rock01 under a parameter called
    # DiffuseTexture. Both score -20, so the names cannot separate them and the
    # bake won on graph position -- the cliff rendered as a pale flat lightmap.
    # The pixel test has to outrank the names here, as it already does for a
    # plain Material's expression graph.
    from ut3.objects.material import resolve_diffuse, score_texture_name

    check("the two candidates are indistinguishable by name",
          score_texture_name("T_UN_Rock_SM_Cliffs01_D"),
          score_texture_name("T_UN_Terrain_FloorStone_Rock01_D"))
    rock = index.path_for("UN_Rock")
    if rock:
        rock_pkg = Package(rock)
        found = [x for x in rock_pkg.find("M_UN_Rock_SM_Cliffs01_MI_SideA_05")
                 if "Material" in rock_pkg.class_name_of(x)]
        if found:
            probe = TextureSet("Probe")
            _o, picked = resolve_diffuse(rock_pkg, index, rock_pkg.ref(found[0].index),
                                         reject=probe._is_relief_bake)
            check("the parent's diffuse wins over the instance's shade map",
                  getattr(picked, "name", None), "T_UN_Terrain_FloorStone_Rock01")
            check_that("and what it picked is not a bake",
                       not probe._is_relief_bake(_o, picked))

    print("an instance that overrides the diffuse slot by name")
    # The mirror image of the case above. DM-HeatRay's rubble wears
    # M_HU_Deco_SM_RubbleA_02, which sets a parameter called "Diffuse" to its
    # own texture and inherits Engine_MI_Shaders.M_Shader_Simple, whose Diffuse
    # parameter defaults to a 32x32 flat grey. The names decide it exactly
    # backwards: the placeholder is called "T_Diffuse".
    from ut3.objects.material import names_diffuse_slot

    check_that("the engine placeholder outscores the texture it stands in for",
               score_texture_name("T_Diffuse")
               < score_texture_name("T_HU_Deco_SM_RubbleA_D02"))
    # So an explicit override of the diffuse slot settles it instead. Narrow on
    # purpose: only a name that says which slot it fills counts, which is what
    # keeps the shade-map case above resolving to its parent.
    for name in ("Diffuse", "DiffuseTexture", "BaseColor", "Albedo", "diffuse_2"):
        check_that("%s names the diffuse slot" % name, names_diffuse_slot(name))
    for name in ("Spec", "Normal", "NormalDetail", "ShadeMap", "Mask", ""):
        check_that("%s does not" % name, not names_diffuse_slot(name))

    rubble = [x for x in p.find("M_HU_Deco_SM_RubbleA_02")
              if "Material" in p.class_name_of(x)]
    if rubble:
        probe = TextureSet("Probe")
        _o, picked = resolve_diffuse(p, index, p.ref(rubble[0].index),
                                     reject=probe._is_relief_bake)
        check("the rubble keeps its own texture", getattr(picked, "name", None),
              "T_HU_Deco_SM_RubbleA_D02")

    print("dds mip chains")
    # UT2004 builds no mips for a DXT texture -- CreateMips returns at
    # UnTex.cpp:492 and Compress bails unless the source is RGBA8 -- so the .dds
    # has to carry the chain or the texture has exactly one level. The importer
    # check()s each level's size (UnEdFact.cpp:2610), so a wrong one is an
    # editor assertion, not a bad-looking texture.
    from ut2.images import mip_chain, mip_size, write_dds

    check("a 4x4 DXT1 level is 8 bytes", mip_size("PF_DXT1", 4, 4), 8)
    check("levels below 4x4 are padded up to it, not packed",
          mip_size("PF_DXT1", 1, 1), 8)
    check("DXT5 is twice DXT1", mip_size("PF_DXT5", 8, 8), 64)

    def levels(fmt, w, h):
        out = []
        while True:
            out.append((w, h, b"\0" * mip_size(fmt, w, h)))
            if w <= 1 and h <= 1:
                return out
            w, h = (w + 1) // 2, (h + 1) // 2

    full = levels("PF_DXT1", 1024, 1024)
    check("a 1024 chain is 11 levels", len(full), 11)
    check("and all of it is accepted", len(mip_chain("PF_DXT1", 1024, 1024, full)), 11)
    # 4/3 of the top level is the whole point: it is the difference between our
    # 1024x1024 DXT5 at 1,048,629 bytes and the stock one at 1,398,370.
    total = sum(len(d) for _w, _h, d in full)
    check_that("the chain adds about a third",
               1.32 < total / float(len(full[0][2])) < 1.34,
               "%.3f" % (total / float(len(full[0][2]))))
    short = mip_chain("PF_DXT1", 1024, 1024, full[:3])
    check("a truncated chain is kept, not rejected", len(short), 3)
    bad = full[:2] + [(256, 256, b"\0" * 999)] + full[3:]
    check("a mis-sized level ends the chain rather than asserting later",
          len(mip_chain("PF_DXT1", 1024, 1024, bad)), 2)
    gap = full[:2] + full[3:]
    check("so does a missing level", len(mip_chain("PF_DXT1", 1024, 1024, gap)), 2)
    # UT3 records its 2x2 and 1x1 levels as 4x4, which is the size the importer
    # computes for them anyway; comparing raw dimensions would drop the two
    # levels a surface is drawn with when it is furthest away.
    padded = full[:8] + [(4, 4, b"\0" * 8), (4, 4, b"\0" * 8), (4, 4, b"\0" * 8)]
    check("levels stored at their padded size are still taken",
          len(mip_chain("PF_DXT1", 1024, 1024, padded)), 11)

    tmp = tempfile.mkdtemp(prefix="ut3conv-dds-")
    try:
        path = os.path.join(tmp, "chain.dds")
        write_dds(path, 1024, 1024, "PF_DXT1", full[0][2], full)
        blob = open(path, "rb").read()
        check("the file holds every level", len(blob), 128 + total)
        check("dwMipMapCount states them", struct.unpack_from("<I", blob, 28)[0], 11)
        check_that("and the mipmap flags are set",
                   struct.unpack_from("<I", blob, 8)[0] & 0x20000
                   and struct.unpack_from("<I", blob, 108)[0] & 0x400008)
        # One level still has to import, and dwMipMapCount 0 is how DDS says so.
        path = os.path.join(tmp, "single.dds")
        write_dds(path, 4, 4, "PF_DXT1", full[-1][2])
        blob = open(path, "rb").read()
        check("a single-level dds says zero mips",
              struct.unpack_from("<I", blob, 28)[0], 0)
        check("and carries just the one", len(blob), 128 + 8)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
