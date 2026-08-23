#!/usr/bin/env python3
"""Regression tests for reading UDK (package version 868) content.

    python3 tests/test_udk.py [path/to/TOXIKK/UDKGame/Content]

TOXIKK is the only UDK game to hand, so these read its shipped packages rather
than a fixture. They skip cleanly when it is not installed.

The mesh figures are not invented: they come from UDK's own OBJ export
(`UDK.com BatchExport te_Lab.upk StaticMesh OBJ`), which is the ground truth
the reader was built against.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.textures import TextureSet
from ut2 import dxt
from ut3.objects.material import (_LAST_SAMPLE, declared_diffuse_channel,
                                  material_albedo, material_panner,
                                  resolve_diffuse)
from ut3.objects.staticmesh import read_static_mesh, validate
from ut3.objects.texture import read_texture
from ut3.resolve import PackageIndex

DEFAULT_CONTENT = os.path.expanduser(
    "~/.steam/steam/steamapps/common/TOXIKK/UDKGame/Content"
)

# name -> (vertices, triangles), read out of UDK's own OBJ export.
OBJ_TRUTH = {
    "MF_SM_Mainframe_01": (494, 368),
    "MF_SM_BentFloor_01": (444, 380),
    "MF_SM_Light_01": (81, 118),
    "MF_SM_Glass_01": (52, 34),
}

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


def main(content):
    index = PackageIndex([content])
    pkg = index.package("te_Lab")
    if pkg is None:
        print("te_Lab.upk not found under %s -- nothing to test" % content)
        return 0

    print("package")
    check("version", pkg.version, 868)

    print("static meshes, against UDK's own OBJ export")
    meshes = {e.name: e for e in pkg.exports_of_class("StaticMesh")}
    check("every mesh in the package is accounted for",
          sorted(meshes), sorted(OBJ_TRUTH))
    for name, (vertices, triangles) in sorted(OBJ_TRUTH.items()):
        mesh = read_static_mesh(pkg, meshes[name])
        check_that("%s parses" % name, mesh is not None)
        if mesh is None:
            continue
        lod = mesh.lod0
        check("%s vertices" % name, len(lod.positions), vertices)
        check("%s triangles" % name, len(lod.triangles), triangles)
        check_that("%s validates" % name, validate(mesh)[0], validate(mesh)[1])
        # Not every element names one -- BentFloor leaves two of its three to
        # the engine default -- but the ones that do must resolve.
        named = [e for e in lod.elements if not e.material.is_null]
        check_that("%s resolves the materials it names" % name,
                   all(index.resolve(pkg, e.material)[1] is not None for e in named),
                   "%d of %d elements" % (len(named), len(lod.elements)))

    # The UV block sits four bytes earlier than UT3's. Reading it at UT3's
    # offset does not fail on a two-channel mesh, it silently returns the
    # lightmap set -- so check the values, not just that there are some.
    print("UV channel")
    light = read_static_mesh(pkg, meshes["MF_SM_Light_01"])
    check_that("a one-channel mesh still yields UVs", len(light.lod0.uvs) == 81,
               "%d" % len(light.lod0.uvs))
    u, v = light.lod0.uvs[0]
    check_that("and they match the OBJ's first vt (0.117981, 0.959991), V flipped",
               abs(u - 0.117981) < 0.001 and abs((1.0 - v) - 0.959991) < 0.001,
               "(%.4f, %.4f)" % (u, v))

    print("textures")
    textures = pkg.exports_of_class("Texture2D")
    read = [read_texture(pkg, e, index) for e in textures]
    check("Texture2D exports", len(textures), 29)
    check_that("every one reads", all(t is not None for t in read))
    check_that("every one yields pixel data",
               all(t.largest is not None for t in read if t is not None))
    wall = [t for t in read if t is not None and t.name == "MF_T_Wall_01_M"][0]
    check("the biggest mip is the full 2048", (wall.largest.width, wall.largest.height),
          (2048, 2048))
    check("and it is a complete chain", len(wall.mips), 12)

    print("materials that declare their own channels")
    check("R=Diffuse names the red channel",
          declared_diffuse_channel("Mask 1 (R=Diffuse, G= Specular, B=Gloss)"), 0)
    check("an ordinary parameter declares nothing",
          declared_diffuse_channel("DiffuseTexture"), None)
    instances = {e.name: e for e in pkg.exports_of_class("MaterialInstanceConstant")}
    for name, texture, tint in (
        ("MF_M_Wall_01_Dark_INST", "MF_T_Wall_01_M", 0.146),
        ("MF_M_Wall_01_INST", "MF_T_Wall_01_M", 0.798),
        ("MF_M_Light_01_INST", "MF_T_Light_01_M", 0.831),
    ):
        owner, tex, channel, colour = material_albedo(
            pkg, index, pkg.ref(instances[name].index))
        check("%s draws" % name, tex.name if tex else None, texture)
        check("%s uses the red channel" % name, channel, 0)
        check_that("%s tints it" % name, colour is not None and abs(colour[0] - tint) < 0.001,
                   "%.3f" % colour[0] if colour else "none")

    # The mask is one texture; the tint is not a property of it but part of its
    # identity, so two instances over one mask must not collapse into one.
    print("tint variants")
    texture_set = TextureSet("LabTex", group="Meshes")
    names = [texture_set.add_material(pkg, index, pkg.ref(instances[n].index))
             for n in ("MF_M_Wall_01_Dark_INST", "MF_M_Wall_01_INST")]
    check_that("one mask at two tints is two textures", names[0] != names[1],
               "%s vs %s" % tuple(names))
    check("and both are held", len(texture_set.textures), 2)

    print("the tinted DXT1 encoder")
    values = [(x * 255) // 7 for _ in range(8) for x in range(8)]
    encoded = dxt.encode_dxt1_tinted(values, 8, 8, (1.0, 1.0, 1.0))
    check("a 8x8 surface is four blocks", len(encoded), 32)
    back = dxt.decode_dxt1_channel(encoded, 8, 8, 0)
    check_that("an untinted ramp survives the round trip",
               max(abs(a - b) for a, b in zip(values, back)) <= 8,
               "max error %d" % max(abs(a - b) for a, b in zip(values, back)))
    # Every texel in a block is one colour at a different brightness, so the
    # ramp is collinear and DXT1 keeps the hue exactly -- give or take the
    # 5 bits it stores blue in.
    coloured = dxt.encode_dxt1_tinted(values, 8, 8, (1.0, 0.4, 0.2))
    red = dxt.decode_dxt1_channel(coloured, 8, 8, 0)
    green = dxt.decode_dxt1_channel(coloured, 8, 8, 1)
    check_that("a coloured tint keeps its hue",
               abs(green[63] / red[63] - 0.4) < 0.05,
               "G/R = %.3f at full brightness" % (green[63] / red[63]))
    check_that("and scales brightness", red[7] > red[3] > red[0],
               "%d > %d > %d across one row" % (red[7], red[3], red[0]))

    print()
    # A sample and the texture it reads are not in the same package as often as
    # a cooked map makes it look. BL-Dekk's waterfalls are the case: the
    # material reference is in the map, the TextureSample is in fo_Water.upk and
    # the texture in a third package again. The sample used to be recorded
    # against the *texture's* package, so material_panner read whatever export
    # happened to sit at that index -- a SeqAct_Interp, in the hologram case --
    # and every one of these materials came out still.
    dekk = index.package("BL-Dekk")
    if dekk is not None:
        print("a sample read across package boundaries")
        found = dekk.find("SF_M_Water_Fall_Dekk_INST")
        if found:
            ref = dekk.ref(found[0].index)
            _LAST_SAMPLE[0] = None
            _owner, tex = resolve_diffuse(dekk, index, ref)
            sample = _LAST_SAMPLE[0]
            check("the waterfall draws its tiling texture",
                  tex.name if tex else None, "T_ASC_VisCP_SM_WaterFall01Tile_D")
            check_that("and the sample is recorded against its own package",
                       sample is not None
                       and os.path.basename(sample[0].path) == "fo_Water.upk",
                       os.path.basename(sample[0].path) if sample else "none")
            panner = material_panner(dekk, index, ref)
            check_that("so the waterfall still scrolls",
                       panner is not None and abs(panner[1] - 0.0608) < 0.001,
                       str(panner))

    if _failures:
        print("%d check(s) failed: %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    content = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONTENT
    if not os.path.isdir(content):
        print("TOXIKK content not found at %s -- skipping" % content)
        sys.exit(0)
    sys.exit(main(content))
