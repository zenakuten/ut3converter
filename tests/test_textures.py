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
        # A surface may now name a generated material object rather than a
        # texture -- an unlit or glowing one gets a Shader, a translucent one a
        # FinalBlend -- so both are legitimate targets. What must never happen
        # is a reference to neither.
        built = set(texture_set.materials.definitions) if texture_set.materials else set()
        check("no t3d reference lacks a texture file or a material",
              sorted(refs - on_disk - built), [])
        check_that("and some surfaces do name a generated material",
                   bool(refs & built), "%d of %d" % (len(refs & built), len(refs)))
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

    print("folding a UE3 material graph to a constant")
    from ut3.objects import graph as G
    from ut3.objects.material import constant_colour, material_panner

    # sRGB, not linear: UE3 authors colour parameters linearly and gamma-
    # corrects on output, UE2 stores what it displays. 0.5 linear is 188, not
    # 128, and getting this wrong makes every tint muddy.
    check("mid grey converts through sRGB", G.to_color((0.5, 0.5, 0.5, 1.0))[:3],
          (188, 188, 188))
    check("black and white are fixed points",
          (G.to_color((0.0,) * 4)[:3], G.to_color((1.0,) * 4)[:3]),
          ((0, 0, 0), (255, 255, 255)))
    check("alpha is passed in, not taken from the colour input",
          G.to_color((1.0, 1.0, 1.0, 0.0), 1.0)[3], 255)

    # M_EV_FogSheet_Master_01 states its EmissiveColor as Color.rgb * Color.a
    # and nothing else, so an instance overriding Color is the entire
    # appearance of a fog sheet. This is the case the whole folder exists for.
    sheet = p.find("M_EV_FogSheet_Master_01_INST")
    check_that("a fog sheet instance folds to its own colour",
               sheet and constant_colour(p, index, p.ref(sheet[0].index))
               == (144, 173, 189),
               str(constant_colour(p, index, p.ref(sheet[0].index)) if sheet else None))
    master = p.find("M_EV_FogSheet_Master_01")
    check("the master it inherits from folds to its own default",
          constant_colour(p, index, p.ref(master[0].index)), (255, 255, 255))

    # An opacity chain must NOT fold: UT3 drives it from PixelDepth through a
    # DotProduct and a Divide, which is a per-pixel depth fade with no UE2
    # counterpart. Folding it anyway produced 0.0002.
    from ut3.objects.material import _expression_ref, base_material
    owner, _base, mprops = base_material(p, index, p.ref(master[0].index))
    opacity = _expression_ref(mprops.get("Opacity"))
    check("an opacity chain declines rather than guessing",
          G.fold(owner, index, opacity,
                 G.collect_parameters(p, index, p.ref(master[0].index))), None)

    # An opacity chain does not fold, but the *level* it is scaled to usually
    # is -- one scalar parameter multiplied in at the top. That scalar is the
    # difference between a light beam and a searchlight: DM-Deck's beams run
    # from 0.035 to 0.25, and drawing them all at full strength is what made
    # StaticMeshActor_4611 look wrong.
    from ut3.objects.material import opacity_scale

    # This one puts its Opacity scalar one level down inside the product, under
    # a DepthBiasedAlpha. Stopping at the first Multiply whose sides both fail
    # to fold missed it and drew the beam ten times too strong.
    beam = p.find("M_EV_Lightbeam_Master_01_INST")
    check("a scalar nested inside the product is still found",
          beam and round(opacity_scale(p, index, p.ref(beam[0].index)), 3), 0.1)
    sheet_inst = p.find("M_EV_FogSheet_Master_01_INST")
    check("a material that scales nothing reads 1.0",
          sheet_inst and opacity_scale(p, index, p.ref(sheet_inst[0].index)), 1.0)
    # A Clamp is transparent on the spine but not inside the product: this
    # map's beams clamp `PixelDepth * 0.0025` deep in theirs, and taking that
    # 0.0025 as a level gives 0.000125 and an invisible beam.
    beam1 = p.find("LightBeam1Sided")
    check("a clamp inside the product is not mistaken for a level",
          beam1 and round(opacity_scale(p, index, p.ref(beam1[0].index)), 3), 0.25)

    # And one taken from under a chain of texture samples this map's
    # window glass multiplies together.
    glass = p.find("M_LT_Base_BSP_Glass_01")
    check("a scalar multiplied into an opacity chain is taken",
          glass and round(opacity_scale(p, index, p.ref(glass[0].index)), 3), 0.4)
    # Zero means UT3 draws nothing: M_EFX_Particles_Distortion01 is pure
    # screen distortion, states Opacity = Constant 0, and must not become an
    # opaque quad.
    distort = p.find("M_EFX_Particles_Distortion01")
    check("a material UT3 draws at zero opacity reads zero",
          distort and opacity_scale(p, index, p.ref(distort[0].index)), 0.0)
    refused = TextureSet("Pkg")
    refused.add_material(p, index, p.ref(distort[0].index))
    check("and is not built at all", len(refused.pending), 0)
    check_that("but is reported", len(refused.invisible) == 1)

    # A Panner on the coordinates is one of the few nodes with an exact UE2
    # counterpart. UE2 states it as a rotator plus a rate, and the rate is the
    # magnitude of UE3's (SpeedX, SpeedY).
    panner = material_panner(p, index, p.ref(master[0].index))
    check_that("a panner converts to a direction and a rate",
               panner is not None and abs(panner[1] - 0.0335) < 0.001, str(panner))
    # And the direction is the angle, *unnegated*. The ASE writes `1.0 - v` and
    # the importer computes `1.0 - ST.Y` back (UnStaticMesh.cpp:1048), so the
    # flips cancel and a converted mesh carries UT3's own UVs; the BSP writer
    # never flips at all. Negating SpeedY for a flip that does not survive to
    # the data reversed every panning material along V.
    import math
    speeds = {"M_EV_FogSheet_Master_01": (0.03, -0.015),
              "M_EV_Lightbeam_Master_01": (0.01, 0.04)}
    for name, (sx, sy) in speeds.items():
        found = p.find(name)
        if not found:
            continue
        got = material_panner(p, index, p.ref(found[0].index))
        want = int(round(math.atan2(sy, sx) / (2 * math.pi) * 65536)) & 0xFFFF
        check("%s pans the way UT3 states it" % name, got and got[0], want)
    # A panner along U alone is the control: it has no V component to get
    # backwards, so it must be yaw 0 under either reading.
    check("a pure-U panner is yaw 0",
          int(round(math.atan2(0.0, 0.75) / (2 * math.pi) * 65536)) & 0xFFFF, 0)

    # A Panner animates the sample it is wired to and no other. Where the drawn
    # sample is known, its Coordinates are the authority *including when they
    # say no* -- HeatRay's city sign has a scrolling LED underlay and static
    # artwork over it, and reading the material at large put the underlay's
    # Panner on the artwork and set the whole sign sliding.
    sign = p.find("M_HU_Deco_SM_CitySignStores")
    check("a sample with no Panner of its own does not pan",
          sign and material_panner(p, index, p.ref(sign[0].index)), None)
    # But where the texture came from the last-resort scan there is no sample
    # to ask, and the material's own Panner is the only information there is.
    # The fog sheets and light beams live here: no texture in the colour path.
    sheet = p.find("M_EV_FogSheet_Master_01_INST")
    check_that("a material with no drawn sample still uses its own Panner",
               sheet and material_panner(p, index, p.ref(sheet[0].index)) is not None)

    print("generated UE2 materials (Phase 14)")
    from convert.shaders import FRAME_BUFFER_BLENDING
    from ut2.materials import MaterialSet

    # Read out of D3D9MaterialState.cpp:299 rather than chosen: FB_Brighten is
    # SRCALPHA/ONE, FB_AlphaBlend is SRCALPHA/INVSRCALPHA, FB_Translucent is
    # ONE/INVSRCCOLOR -- keyed on brightness, not alpha.
    check("additive maps to FB_Brighten",
          FRAME_BUFFER_BLENDING["BLEND_Additive"], "FB_Brighten")
    check("translucent maps to FB_AlphaBlend, alpha permitting",
          FRAME_BUFFER_BLENDING["BLEND_Translucent"], "FB_AlphaBlend")
    check("modulate maps to FB_Modulate",
          FRAME_BUFFER_BLENDING["BLEND_Modulate"], "FB_Modulate")
    check("masked builds nothing on its own",
          FRAME_BUFFER_BLENDING.get("BLEND_Masked"), None)

    # The real thing, end to end, against the map: HeatRay's light beams and
    # fog sheets are the materials a flat texture cannot express.
    live = TextureSet("HeatRayTex")
    for name in ("M_EV_FogSheet_Master_01_INST", "M_EV_Lightbeam_Master_01_INST"):
        found = p.find(name)
        if found:
            live.add_material(p, index, p.ref(found[0].index))
    check_that("non-opaque materials are held for later", len(live.pending) > 0)
    check("and nothing is built until the textures are settled",
          len(live.materials), 0)
    # build_materials wants exported textures; fake the two facts it reads.
    for texture_name in list(live.textures):
        live.alpha_channel[texture_name] = False
    live.build_materials(index)
    check_that("then real objects appear", len(live.materials) > 0,
               "%d objects" % len(live.materials))
    kinds = set(kind for kind, _props in live.materials.definitions.values())
    check_that("a Shader for the unlit part", "Shader" in kinds, str(sorted(kinds)))
    check_that("a FinalBlend on the outside", "FinalBlend" in kinds)
    check_that("a TexPanner, since both materials scroll", "TexPanner" in kinds)
    blends = [dict(props).get("FrameBufferBlending")
              for kind, props in live.materials.definitions.values()
              if kind == "FinalBlend"]
    # No alpha channel above, so BLEND_Translucent has to fall back to UE2's
    # brightness-keyed blend rather than draw solid.
    check("with no alpha to blend on, translucent falls back to FB_Translucent",
          set(blends), {"FB_Translucent"})

    ms = MaterialSet("Pkg", "abcd")
    a = ms.add("Shader", "Beam_abcd", [("Diffuse", "Texture'Pkg.BSP.Beam_abcd'")])
    check("the tag is not doubled onto a name that already carries it",
          a, "Beam_abcdSH")
    b = ms.add("FinalBlend", "Beam_abcd", [("Material", ms.path(a))])
    before = len(ms)
    check("an identical definition is shared, not duplicated",
          ms.add("Shader", "Beam_abcd", [("Diffuse", "Texture'Pkg.BSP.Beam_abcd'")]), a)
    check("so the set does not grow", len(ms), before)

    text = "\n".join(ms.emit())
    check_that("every object is declared", text.count("Begin Object") == len(ms))
    # SavePackage writes only what something references: without KeepAlive the
    # whole lot is built and silently dropped.
    check("and every one is kept alive", text.count("GeneratedMaterials("), len(ms))
    check_that("a dependency is written before what refers to it",
               text.index("Name=%s" % a) < text.index("Name=%s" % b))

    # A normal map drawn as diffuse renders in iridescent blue and magenta
    # (Phase 7c). The name rules catch most; the pixels catch the rest, a
    # tangent-space normal being a unit vector packed around (128, 128, 255).
    def measured(means):
        probe = TextureSet("Pkg")
        probe._measure_means = lambda o, e: means
        return probe._is_normal_map(None, type("E", (), {"index": 1})())
    check("a flat tangent normal is refused", measured([128, 128, 255]), True)
    check("BL-Dekk's SF_T_TilingBubbles_N_H is refused", measured([128, 126, 254]), True)
    check("blue sky is not a normal map", measured([90, 140, 220]), False)
    check("grey stone is not a normal map", measured([120, 118, 115]), False)
    check("a bright cyan diffuse is not a normal map", measured([60, 200, 230]), False)

    # A two-tone material blends two colours per pixel by one channel of its
    # own map, which no tint can express -- the pipes are a red body with pale
    # trim, and multiplying the whole texture by red loses the trim.
    from ut2.dxt import encode_dxt1_rgb, decode_dxt1_channel

    block = [(255, 0, 0)] * 8 + [(255, 255, 255)] * 8
    encoded = encode_dxt1_rgb(block, 4, 4)
    check("a DXT1 block is eight bytes", len(encoded), 8)
    got = [tuple(decode_dxt1_channel(encoded, 4, 4, c)[i] for c in range(3))
           for i in (0, 15)]
    check("and keeps both colours of a two-tone block", got, [(255, 0, 0), (255, 255, 255)])

    print("materials follow their texture")
    check_that("a set with materials on has a MaterialSet",
               TextureSet("Pkg").materials is not None)
    check_that("--no-materials gives none",
               TextureSet("Pkg", materials=False).materials is None)
    # A material over a texture that failed to export would name an object the
    # package never imports, so build_materials has to skip it.
    orphan = TextureSet("Pkg")
    orphan.pending[("pkg", False, 7)] = (p, None, "GoneAway_%s" % orphan.tag, None)
    orphan.build_materials(index)
    check("a dropped texture builds no material", len(orphan.materials), 0)

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
