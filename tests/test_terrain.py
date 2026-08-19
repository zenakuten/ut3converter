#!/usr/bin/env python3
"""Regression tests for terrain conversion (Phase 3).

    python3 tests/test_terrain.py [path/to/DM-HeatRay.ut3]
"""

import os
import re
import shutil
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.terrain import UE3_HEIGHT_UNITS, convert_terrain
from convert.textures import TextureSet
from ut2.bmp import write_bmp16
from ut3.objects.terrain import read_terrain
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


def prop_of(actor, key):
    for k, v in actor.properties:
        if k == key:
            return v
    return None


def main(path):
    p = Package(path)
    index = PackageIndex.for_map(path)

    print("terrain reading")
    terrain = read_terrain(p, p.find("Terrain_1")[0])
    check_that("terrain parsed", terrain is not None)
    check("grid", (terrain.width, terrain.height), (129, 129))
    check("heights", len(terrain.heights), 129 * 129)
    check("layers", len(terrain.layers), 3)
    check("alpha maps", [len(a) for a in terrain.alpha_maps], [129 * 129] * 3)
    check_that("heights sit around the 32768 zero point",
               32000 < min(terrain.heights) and max(terrain.heights) < 33500,
               "%d..%d" % (min(terrain.heights), max(terrain.heights)))

    print("bmp writer")
    tmp = tempfile.mkdtemp(prefix="ut3conv-bmp-")
    try:
        values = [(y * 4 + x) * 1000 for y in range(4) for x in range(4)]
        bmp = write_bmp16(os.path.join(tmp, "t.bmp"), 4, 4, values)
        d = open(bmp, "rb").read()
        sig, _size, _r1, _r2, off = struct.unpack_from("<2sIHHI", d, 0)
        _bs, w, h, planes, bits, comp = struct.unpack_from("<IiiHHI", d, 14)
        check("bmp signature", sig, b"BM")
        # biBitCount 16 is what selects the TEXF_G16 path (UnEdFact.cpp:2191).
        check("16-bit, uncompressed, one plane", (planes, bits, comp), (1, 16, 0))
        check("dimensions", (w, h), (4, 4))
        raw = list(struct.unpack_from("<16H", d, off))
        # The importer maps file row y to texture row (h-1-y), so rows are
        # written bottom-up and must come back in the original order.
        restored = []
        for y in range(h - 1, -1, -1):
            restored.extend(raw[y * w : (y + 1) * w])
        check("rows survive the importer's flip", restored, values)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("conversion")
    out = tempfile.mkdtemp(prefix="ut3conv-terrain-")
    try:
        texture_set = TextureSet("TestTex")
        actors, exec_lines, stats = convert_terrain(p, index, out, "TestTex", texture_set)
        check("terrains converted", stats.terrains, 1)
        check("layers converted", stats.layers, 3)
        check("actors emitted", len(actors), 1)
        # Road's UE3 alpha map is all zeros -- it is the implicit base layer --
        # so it gets a generated opaque map instead of the empty one.
        check("base layers made opaque", stats.base_layers, 1)
        check("exec lines (heightmap + 2 alpha maps + opaque base)", len(exec_lines), 4)

        files = os.listdir(os.path.join(out, "TestTex", "Terrain"))
        check("files written", len(files), 4)
        check_that("an opaque base map exists",
                   any(f.endswith("_AlphaBase.tga") for f in files))
        check_that("heightmap is a bmp", any(f.endswith("_Height.bmp") for f in files))
        check_that("alpha maps are tgas",
                   sum(1 for f in files if f.endswith(".tga")) == 3)

        actor = actors[0]
        check("actor class", actor.cls, "TerrainInfo")
        # UE3 Z = Loc.Z + (h-32768)/128 * DrawScale * DrawScale3D.Z
        # UE2 Z = Loc.Z + h * TerrainScale.Z/256   (UnTerrain.cpp:1464)
        z_unit = terrain.draw_scale * terrain.draw_scale_3d[2] / UE3_HEIGHT_UNITS
        # The grid is resampled onto a square power of two (UT2004 asserts alpha
        # maps are square, UnTerrain.cpp:175), so a vertex step spans
        # (width-1)/(size-1) UE3 patches rather than exactly one.
        size_ = 128
        step = (terrain.width - 1) / float(size_ - 1)
        psx = terrain.draw_scale * terrain.draw_scale_3d[0] * step
        check("TerrainScale", prop_of(actor, "TerrainScale"),
              "(X=%f,Y=%f,Z=%f)" % (psx, psx, z_unit * 256.0))
        # UE2's terrain mapping is CENTRED: CalcCoords re-centres ToWorld with
        # /= (HeightmapX/2, HeightmapY/2, 32767) at UnTerrain.cpp:1466, so
        # Location is the heightmap centre at height 32767 and every vertex is
        #   World = Location + (x - HeightmapX/2)*Scale, (h - 32767)*Scale.Z/256
        loc = (terrain.location[0] + (size_ / 2.0) * psx,
               terrain.location[1] + (size_ / 2.0) * psx,
               terrain.location[2] - z_unit)
        emitted = prop_of(actor, "Location")
        check("Location is the heightmap centre at 32767", emitted,
              "(X=%f,Y=%f,Z=%f)" % loc)
        # A UT2004 vertex sits where UE3's grid is sampled: source coordinate
        # (x*step, y*step), which is the source vertex itself at the corners.
        unit = terrain.draw_scale * terrain.draw_scale_3d[0]
        worst = 0.0
        for (x, y) in ((0, 0), (64, 64), (127, 127), (10, 90), (127, 0)):
            ue3 = (terrain.location[0] + x * step * unit,
                   terrain.location[1] + y * step * unit)
            ue2 = (loc[0] + (x - size_ / 2.0) * psx,
                   loc[1] + (y - size_ / 2.0) * psx)
            worst = max(worst, max(abs(ue3[i] - ue2[i]) for i in range(2)))
        check_that("vertex world positions match UT3 exactly", worst < 1e-6, "%.9f uu" % worst)
        check_that("the resampled grid spans the same ground as UT3",
                   abs((size_ - 1) * psx - (terrain.width - 1) * unit) < 1e-6,
                   "%.1f uu across" % ((size_ - 1) * psx))
        # The corners are pinned, so their heights survive the resample intact.
        corner_z = loc[2] + (terrain.heights[0] - 32767) * z_unit
        check_that("corner height is unchanged by the resample",
                   abs(corner_z - (terrain.location[2]
                                   + (terrain.heights[0] - 32768) * z_unit)) < 1e-6,
                   "%.3f uu" % corner_z)
        # Heights must stay centred on ~32768: UE2 treats 32767 as zero, which is
        # why working UT2004 heightmaps are mid-grey rather than dark.
        import struct as _s
        hm = open(os.path.join(out, "TestTex", "Terrain",
                               [f for f in files if f.endswith("_Height.bmp")][0]), "rb").read()
        off = _s.unpack_from("<I", hm, 10)[0]
        vals = _s.unpack_from("<%dH" % (size_ * size_), hm, off)
        check_that("heights stay centred on 32768, not biased to zero",
                   30000 < min(vals) and max(vals) < 35000,
                   "%d..%d" % (min(vals), max(vals)))
        # Every terrain texture must be square and power-of-two: UT2004 asserts
        # AlphaMap->USize == AlphaMap->VSize outright (UnTerrain.cpp:175) and
        # takes down the editor on import when it does not hold.
        sizes = {}
        for f in files:
            data = open(os.path.join(out, "TestTex", "Terrain", f), "rb").read()
            if f.endswith(".bmp"):
                sizes[f] = _s.unpack_from("<ii", data, 18)
            else:
                sizes[f] = _s.unpack_from("<HH", data, 12)
        check_that("terrain textures are square",
                   all(w == h for w, h in sizes.values()),
                   ", ".join("%s %dx%d" % (f, w, h) for f, (w, h) in sizes.items()))
        check_that("terrain textures are power-of-two",
                   all(w & (w - 1) == 0 for w, _h in sizes.values()))
        check_that("alpha maps match the heightmap 1:1 (the fast path at "
                   "UnTerrain.cpp:178)",
                   len({w for w, _h in sizes.values()}) == 1)
        # The actor must sit ON the terrain, inside the level's subtracted space:
        # a TerrainInfo in solid space has no zone and renders nothing at all.



        check_that("every layer names a texture",
                   all("Texture=Texture'" in prop_of(actor, "Layers(%d)" % i)
                       for i in range(3)))
        # Tagged with the package hash like every other generated texture: UE3
        # names the first terrain in every map Terrain_0, so the names would
        # otherwise be identical across converted maps.
        from convert.textures import package_tag

        check_that("terrain map reference is fully qualified",
                   prop_of(actor, "TerrainMap")
                   == "Texture'TestTex.Terrain.Terrain_1_%s_Height'" % package_tag("TestTex"),
                   prop_of(actor, "TerrainMap"))
        check_that("every layer names an alpha map",
                   all("AlphaMap=Texture'" in prop_of(actor, "Layers(%d)" % i)
                       for i in range(3)))
        # Layer 0 must be the fully opaque base whatever alpha map UE3 gave it.
        # UE3 hands the bottom layer the weight the others leave over, so the
        # stored maps never sum to full coverage; without an opaque base those
        # texels draw nothing and the terrain renders black, then vanishes
        # altogether once a whole sector is empty.
        check_that("the first layer is the opaque base",
                   "_AlphaBase'" in prop_of(actor, "Layers(0)"),
                   prop_of(actor, "Layers(0)"))
        check_that("and only the first layer",
                   not any("_AlphaBase'" in prop_of(actor, "Layers(%d)" % i)
                           for i in (1, 2)))
        # A layer repeats every UScale *quads* (UnTerrain.cpp:1874), and UE3
        # says the same thing in its own quads as MappingScale, so the two are
        # one division by the resample step apart. Getting this wrong is not
        # subtle: a fixed 16 tiled WAR-PowerSurge's ground every 1799uu across
        # and 3213uu along where UT3 asks for 512 both ways, and the terrain
        # rendered as a flat grey smear with horizontal streaks.
        from convert.terrain import DEFAULT_MAPPING_SCALE, _layer_scale

        u, v = _layer_scale(2.0, 0.4392, 0.7843)
        check_that("a layer's tiling comes from UT3's MappingScale",
                   abs(u - 4.554) < 0.01 and abs(v - 2.550) < 0.01,
                   "UScale %.3f VScale %.3f" % (u, v))
        check_that("the two axes scale independently, since the quads are not square",
                   abs(u - v) > 1.0)
        check_that("in world units both axes land on the same repeat distance",
                   abs(u * (256 * 0.4392) - v * (256 * 0.7843)) < 1.0,
                   "%.0fuu vs %.0fuu" % (u * 256 * 0.4392, v * 256 * 0.7843))
        check("an explicit --terrain-layer-scale still overrides both",
              _layer_scale(2.0, 0.4392, 0.7843, 16.0), (16.0, 16.0))
        # MappingScale is elided at its class default, and that default is not 1.
        check("UT3's default MappingScale", DEFAULT_MAPPING_SCALE, 4.0)
        # UE3 cuts holes in a terrain where meshes take over as the floor. The
        # bitmap must be exactly HeightmapX*HeightmapY/32 words or PostLoad
        # discards it and marks everything visible again (UnTerrain.cpp:1670),
        # which puts ground back inside the buildings.
        bitmap = [int(v) for k, v in actor.properties
                  if k.startswith("QuadVisibilityBitmap(")]
        check("visibility bitmap is one bit per quad", len(bitmap), size_ * size_ // 32)
        check_that("some quads are cut away", stats.hidden_quads > 0,
                   "%d of %d" % (stats.hidden_quads, size_ * size_))
        check_that("but most of the terrain survives",
                   sum(bin(w & 0xFFFFFFFF).count("1") for w in bitmap) > size_ * size_ // 4)
        base_tga = os.path.join(out, "TestTex", "Terrain",
                                [f for f in files if f.endswith("_AlphaBase.tga")][0])
        pixels = open(base_tga, "rb").read()[18:]
        check_that("the base map is opaque in every texel",
                   all(a == 255 for a in pixels[3::4]),
                   "%d texels" % (len(pixels) // 4))
    finally:
        shutil.rmtree(out, ignore_errors=True)

    print("decoration layers")
    # UT3 hangs a layer's ground cover off its *material*, as FoliageMeshes, not
    # off the layer -- TM_Serenity_Floormix_01 carries S_UN_Foliage_SM_Weed01
    # and S_UN_Foliage_SM_Grass03. Converting the layers without it leaves the
    # ground bare. UT2004's counterpart is TerrainInfo.DecoLayers, which
    # scatters a mesh per quad against a density texture (UnTerrain.cpp:3825).
    from convert.terrain import DECO_FADE_START, _density_map, effective_weights

    # AlphaMapThreshold is where UT3 says the cover starts; above it the weight
    # is rescaled rather than flattened, so a layer that fades out takes its
    # ground cover with it instead of ending on a hard line.
    density = _density_map([0, 100, 178, 217, 255], 0.7)
    check("below the threshold nothing is scattered", list(density[:3]), [0, 0, 0])
    check_that("just above it, sparsely", 0 < density[3] < 255, str(density[3]))
    check("at full weight, fully", density[4], 255)
    check("a zero threshold keeps the weight as it is",
          list(_density_map([0, 128, 255], 0.0)), [0, 128, 255])

    class _T:
        # texel 0: the other layer owns it outright; texel 1: it owns a quarter,
        # so three quarters fall to the base; texel 2: nobody else claims it.
        alpha_maps = [[0, 0, 255], [255, 64, 0]]

    # The bottom layer's stored map is only what the artist painted *extra*:
    # UE3 gives it whatever the layers above leave over. Serenity's FloorMix
    # reads 4.6% coverage from its stored map and 62.6% once that is counted.
    check("a layer that is not the base keeps its stored map",
          effective_weights(_T(), 0, False), [0, 0, 255])
    check("the base layer is handed the leftover",
          effective_weights(_T(), 0, True), [0, 191, 255])
    check_that("so ground the other layers do not claim is fully covered",
               effective_weights(_T(), 0, True)[2] == 255)
    check_that("the fade starts inside UT3's draw radius", 0.0 < DECO_FADE_START < 1.0)

    print("the radar background")
    # ONSHUDOnslaught draws Level.RadarMapImage behind the node graph (:276) and
    # returns early when it is None, which is why converted maps had no minimap.
    # The HUD treats it as a plan at a known scale: with MapCenter at the origin
    # (:270) the draw reduces to the whole texture, so the image must span
    # exactly [-RadarRange, +RadarRange] with U along world X and V along world Y.
    from convert.minimap import render, sample

    def flat(size_, unit, height, origin=(0.0, 0.0, 0.0), visible=None):
        return {"size": size_, "unit_x": unit, "unit_y": unit, "z_unit": 1.0,
                "origin": origin, "visible": visible,
                "heights": [32768 + height] * (size_ * size_)}

    # A 4x4 grid of 100uu quads anchored at the origin covers 0..300 on both axes.
    grid = flat(4, 100.0, 500)
    check_that("a point on the terrain samples it", sample([grid], 10.0, 10.0) is not None)
    check("and reads its height", round(sample([grid], 10.0, 10.0)[0]), 500)
    check_that("a point off it samples nothing", sample([grid], -50.0, 0.0) is None)

    # Where two terrains overlap the finer one wins: WAR-Torlan lays a 193uu
    # playfield over a 1032uu landscape, and the radar has to show the playfield.
    coarse = flat(4, 1000.0, 100)
    fine = flat(4, 100.0, 900)
    check("the finer terrain decides the pixel",
          round(sample([coarse, fine], 50.0, 50.0)[0]), 900)

    image = render([grid], 300.0, 8)
    check_that("an image is produced", image is not None)
    check("four bytes per pixel, BGRA", len(image), 8 * 8 * 4)
    # The terrain covers 0..300 and the image spans -300..+300, so the lower-left
    # quadrant is off it and must stay transparent rather than painted.
    def alpha(i, j):
        return image[(j * 8 + i) * 4 + 3]
    check("off the terrain is transparent", alpha(1, 1), 0)
    check_that("and on it is not", alpha(6, 6) == 255)
    # A cut-away quad is a hole in the ground and reads as one.
    hidden = flat(4, 100.0, 500, visible=[0] * (4 * 4 // 32 + 1))
    check("quads UT3 cut away stay transparent", render([hidden], 300.0, 8), None)
    check("no terrain at all means no image", render([], 300.0, 8), None)

    print("rotation")
    # UT2004 terrain is axis-aligned: CalcCoords builds ToWorld from Location
    # and TerrainScale alone (UnTerrain.cpp:1464), with Rotation nowhere in it.
    # WAR-Serenity's terrain is turned 90 degrees, and ignoring that put a
    # 23040 x 46592 terrain at X 23040..46080 while the map sat at X -20000.
    from convert.terrain import quarter_turns, rotate_grid, rotated_placement

    class _Rot:
        def __init__(self, value):
            self.value = value

    check("no rotation is no turns", quarter_turns(None), 0)
    check("a quarter turn reads as one", quarter_turns(_Rot((0, 16384, 0))), 1)
    check("a half turn as two", quarter_turns(_Rot((0, 32768, 0))), 2)
    check("negative yaw wraps", quarter_turns(_Rot((0, -16384, 0))), 3)
    check_that("an off-axis yaw is refused rather than approximated",
               quarter_turns(_Rot((0, 8192, 0))) is None)
    # UT3 maps do not land exactly on the grid. Both of VCTF-Containment's
    # terrains are one rotator unit out -- 0.0055 degrees, authoring noise --
    # and an exact test left them lying where UE3 put them.
    check("a yaw a single unit off a quarter turn still counts",
          quarter_turns(_Rot((0, 131073, 0))), 0)
    check("and one a unit off a half turn", quarter_turns(_Rot((0, 163841, 0))), 2)
    check_that("but the tolerance is nowhere near a real angle",
               quarter_turns(_Rot((0, 16300, 0))) is None)
    check_that("and so is a pitched terrain",
               quarter_turns(_Rot((4096, 0, 0))) is None)

    # 3 wide, 2 tall: 0 1 2 / 3 4 5. A quarter turn counter-clockwise sends the
    # local +X axis to +Y, so the right-hand column becomes the top row.
    grid, w, h = rotate_grid([0, 1, 2, 3, 4, 5], 3, 2, 1)
    check("a quarter turn swaps the grid's axes", (w, h), (2, 3))
    check("and reindexes it", grid, [3, 0, 4, 1, 5, 2])
    check("four turns are the identity",
          rotate_grid([0, 1, 2, 3, 4, 5], 3, 2, 4), ([0, 1, 2, 3, 4, 5], 3, 2))

    # Serenity's own numbers: anchored at X 23040, 181 x 365 vertices, 128 apart.
    corner, units, dims = rotated_placement((23040.0, -13616.0), 181, 365,
                                            128.0, 128.0, 1)
    check("the anchor moves to the corner that is now first",
          corner, (23040.0 - 364 * 128.0, -13616.0))
    check("the spacings swap with the axes", units, (128.0, 128.0))
    check("as do the dimensions", dims, (365, 181))
    check_that("so the rotated terrain covers the ground the map is on",
               corner[0] < -20000 and corner[0] + (dims[0] - 1) * units[0] > 20000,
               "X %.0f..%.0f" % (corner[0], corner[0] + (dims[0] - 1) * units[0]))
    check("an unrotated terrain is left exactly as it was",
          rotated_placement((10.0, 20.0), 4, 8, 2.0, 3.0, 0),
          ((10.0, 20.0), (2.0, 3.0), (4, 8)))

    print("zone info")
    from convert.terrain import make_zone_info
    # UT2004 empties the LevelInfo's terrain list (UnLevel.cpp:845), so a
    # terrain whose zone is the LevelInfo -- i.e. any map with no ZoneInfo --
    # never renders. The converted map must supply one.
    bounds = ((-100.0, -200.0, -300.0), (100.0, 200.0, 300.0))
    zone = make_zone_info(bounds, 1024.0, ambient=(32, 145, 210), terrain=True)
    check("zone class", zone.cls, "ZoneInfo")
    check("zone carries the SkyLight ambient", prop_of(zone, "AmbientBrightness"), "32")
    # Nothing in the engine sets bTerrainZone; without it rendering, collision
    # and traces all skip the terrain (UnRenderVisibility.cpp:2008).
    check("zone is flagged as a terrain zone", prop_of(zone, "bTerrainZone"), "True")
    # Every map gets a ZoneInfo now, so the flag has to be conditional -- a map
    # with no terrain must not claim to be a terrain zone.
    bare = make_zone_info(bounds, 1024.0, ambient=(32, 145, 210))
    check_that("but only when there is terrain",
               prop_of(bare, "bTerrainZone") is None)
    z = float(re.search(r"Z=([-\d.]+)", prop_of(zone, "Location")).group(1))
    check_that("zone sits above the geometry but inside the world brush",
               300.0 < z < 300.0 + 1024.0, "%.1f" % z)

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
