#!/usr/bin/env python3
"""Regression tests for CTF conversion, on CTF-FacingWorlds (Phase 7).

The third map converted end to end, and the first team map. It is also the one
that puts its scenery past UE2's world limit.

    python3 tests/test_ctf.py [path/to/CTF-FacingWorlds.ut3]
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.actors import convert_player_starts
from convert.objectives import OBJECTIVE_CLASSES, UT3_NAV_HEIGHT, convert_objectives
from ut2 import dxt
from ut2.t3d import HALF_WORLD_MAX, T3DMap
from ut2.t3d import Actor as T3DActor
from ut3.package import Package
from ut3.props import read_object_properties

DEFAULT_MAP = (
    "/home/josh/.steam/steam/steamapps/common/Unreal Tournament 3/"
    "UTGame/CookedPC/Maps/CTF-FacingWorlds.ut3"
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

    print("flag bases")
    objectives, stats = convert_objectives(p)
    check("objectives converted", stats.objectives, 2)
    check("one base per team", stats.by_class,
          {"xRedFlagBase": 1, "xBlueFlagBase": 1})
    # UT3 cooks its class defaults into the map. Converting one would drop a
    # phantom flag base at the world origin, and CTF would score on it.
    check_that("class defaults are not converted",
               all("Default__" not in a.name for a in objectives))
    check_that("both are placed", all(prop_of(a, "Location") for a in objectives))

    # Both engines centre the actor on its collision cylinder, and the cylinders
    # differ: UT3's navigation points are 50 high, xRealCTFBase is 80
    # (XGame/xRealCTFBase.uc:36). Copied straight over, the stand sinks 30uu.
    red = [e for e in p.exports if e.name == "UTCTFRedFlagBase_1"][0]
    props, _start, _end = read_object_properties(p, red)
    ut3_z = props.get("Location").value[2]
    emitted = [a for a in objectives if a.name == "UTCTFRedFlagBase_1"][0]
    z = float(re.search(r"Z=(\S+?)\)", prop_of(emitted, "Location")).group(1))
    check("the base rises by the cylinder difference", round(z - ut3_z), 30)
    check("which is UT2004's 80 against UT3's 50",
          OBJECTIVE_CLASSES["UTCTFRedFlagBase"][1] - UT3_NAV_HEIGHT, 30.0)

    print("team player starts")
    starts, start_stats = convert_player_starts(p)
    check("player starts", start_stats.player_starts, 20)
    check("of which team-assigned", start_stats.team_starts, 10)
    teams = {}
    for actor in starts:
        team = prop_of(actor, "TeamNumber")
        teams[team] = teams.get(team, 0) + 1
    # Ten a side. UT3 elides TeamIndex 0, which is red in both engines, so only
    # the blue half carries the property -- the rest take UT2004's own default.
    check("half are explicitly blue", teams.get("1"), 10)
    check_that("and the rest default to red", teams.get(None) == 10, str(teams))

    print("scenery past UE2's world limit")
    # FacingWorlds hangs its backdrop 336,707uu out. UE2 clamps every coordinate
    # to +/-262144, so a void big enough to enclose that does not exist and the
    # backdrop has to go into the skybox instead -- not a preference, the only
    # arrangement that works.
    far = 0
    for export in p.exports:
        if p.class_name_of(export) not in ("StaticMeshActor", "InterpActor"):
            continue
        props, start, _end = read_object_properties(p, export)
        if start is None:
            continue
        location = props.get("Location")
        if location is None or not location.value:
            continue
        if max(abs(c) for c in location.value) > HALF_WORLD_MAX:
            far += 1
    check_that("the map really does reach past it", far > 0, "%d actors beyond" % far)

    print("DXT5 alpha, which this map is the first to need")
    # A mask stored as DXT5 sent the baker down the alpha decoder for the first
    # time, and its interpolation weights were wrong: the pairs have to sum to
    # the divisor, 6:1..1:6 over 7 and 4:1..1:4 over 5. Writing (7-i) and (5-i)
    # produces values past 255 and the conversion dies outright.
    for a0, a1 in ((255, 255), (255, 0), (0, 255), (17, 200), (200, 17), (0, 0)):
        ramp = dxt.alpha_ramp(a0, a1)
        check_that("alpha_ramp(%d,%d) stays a byte" % (a0, a1),
                   all(0 <= v <= 255 for v in ramp), str(ramp))
        check("alpha_ramp(%d,%d) has 8 entries" % (a0, a1), len(ramp), 8)
    # The encoder's table and the decoder must agree or a baked mask reads back
    # as something else; both come from alpha_ramp now.
    bits = sum(i << (3 * i) for i in range(8))
    block = bytes((255, 0)) + bits.to_bytes(6, "little") + bytes(8)
    check("encoder and decoder agree",
          tuple(dxt.decode_dxt5_alpha(block, 4, 4)[:8]), dxt._ALPHA_CODES)

    print("the skybox has to fit inside the world too")
    # The room is placed clear of whatever *stays* in the level. Deciding that
    # after choosing which scenery to move puts the room clear of the very
    # meshes about to go into it -- which pushed FacingWorlds' room to x=-335058
    # and made the whole sky disappear, with no error anywhere.
    check("the writer knows UE2's limit", HALF_WORLD_MAX, 262144.0)
    guard = T3DMap()
    guard.add(T3DActor("Light", "Inside", [("Location", "(X=1000.000000,Y=0.000000,Z=0.000000)")]))
    stranded = T3DActor("Light", "Outside",
                        [("Location", "(X=299312.000000,Y=66702.000000,Z=-15455.000000)")])
    guard.add(stranded)
    found = guard.out_of_world()
    check("a stranded actor is caught", [a.name for a in found], ["Outside"])
    guard.drop(found)
    check("and dropped", [a.name for a in guard.actors], ["Inside"])

    print("material instances must not short-circuit their parent")
    # The cliffs' instance overrides Normal and nothing else, so the only
    # texture it names is a normal map, while the DiffuseTexture the surface is
    # painted with sits one level up its parent chain. Taking the instance's
    # word for it rendered the whole backdrop in iridescent blue and magenta.
    from ut3.objects.material import resolve_diffuse, score_texture_name
    from ut3.resolve import PackageIndex

    index = PackageIndex.for_map(path)
    faces = index.package("ASC_Face")
    if faces is None:
        print("  --    ASC_Face not installed, skipping")
    else:
        cliff = [e for e in faces.exports
                 if e.name == "M_UN_Rock_SM_Cliffs01_CaveWall_01_INST"][0]
        _owner, tex = resolve_diffuse(faces, index, faces.ref(cliff.index))
        check("the cliff instance reaches its parent's diffuse",
              tex.name if tex else None, "T_UN_Cave_Rock_Big_Wall_D")

    # And where no parent has one either, a normal map is refused outright: the
    # neutral placeholder beats iridescent purple.
    from convert.textures import NOT_DIFFUSE

    check_that("a normal map is never accepted as diffuse",
               score_texture_name("T_UN_Liquid_ChasmOcean_WaveRipple_01_N") >= NOT_DIFFUSE)
    check_that("nor a cubemap",
               score_texture_name("T_UN_CubeMaps_Robot_Paint01") >= NOT_DIFFUSE)
    # But an ordinary diffuse must still pass, and so must the borderline ones:
    # refusing too much trades purple for grey everywhere.
    for name in ("T_UN_Cave_Rock_Big_Wall_D", "T_HU_Floors_BSP_Asphalt01_Spec",
                 "T_Web", "T_UN_Sky_SM_Onyx"):
        check_that("%s is still usable" % name, score_texture_name(name) < NOT_DIFFUSE)

    print("geometry UT3 never draws")
    # The brown slabs in the sky were not translucent meshes rendered wrong --
    # they were never rendered at all. `HiddenGame` on a PrimitiveComponent
    # means UE3 does not draw it in play, and this map keeps 21 such meshes as
    # shadow casters in a group the author called "necris cloud shadowcasters".
    # Because nothing draws them their material is arbitrary: one wears a stone
    # floor texture, which is exactly what appeared in the sky.
    from convert.meshes import MeshSet, _hidden_in_game, convert_actors
    from convert.textures import TextureSet

    nebula = [e for e in p.exports if e.name == "StaticMeshActor_841"][0]
    props, _start, _end = read_object_properties(p, nebula)
    comp_ref = props.get("StaticMeshComponent")
    comp, _s, _e = read_object_properties(p, comp_ref.export)
    check_that("the sky nebula really is hidden in UT3", _hidden_in_game(comp))
    check("and its group says why", str(props.get("Group")),
          "necris cloud shadowcasters")

    mesh_actors, mesh_stats = convert_actors(p, index, MeshSet("TestTex"),
                                             TextureSet("TestTex"))
    check("hidden meshes skipped", mesh_stats.skipped_hidden, 21)
    check_that("so none of them reaches the map",
               not [a for a in mesh_actors
                    if "Nebula_01" in str(prop_of(a, "StaticMesh"))])

    print("level-wide settings")
    # UT3 keeps KillZ on its WorldInfo. UT2004's ZoneInfo default is -10000, so
    # without carrying it across a player who walks off the edge falls far past
    # where UT3 would have killed them and lands on the bottom of the world.
    from convert.lights import convert_lights
    from convert.terrain import make_zone_info
    from ut3.objects.level import kill_z

    check("UT3's KillZ for this map", kill_z(p), -3000.0)
    # Two SkyLights, and they add: UT3 lights the scene with both, so taking
    # the larger and dropping the other loses real light. Checked at an explicit
    # gain, because the summing is what is under test here and the shipped gain
    # is small enough to round both contributions together.
    _lights, summed = convert_lights(p, ambient_gain=128.0)
    check("SkyLight contributions", summed.ambient_parts, [12, 20])
    check("summed into the zone's ambient", summed.ambient[0], 32)
    check_that("and the hue follows the larger of them, not an average",
               summed.ambient[1:] != (0, 255))

    lights, light_stats = convert_lights(p)
    # Two dim SkyLights sum to 4 here, under the floor a map with any SkyLight
    # gets -- see MIN_SKYLIGHT_AMBIENT. The stock maps sit lower still (median
    # 4), but they light themselves with hand-placed lights this pipeline only
    # approximates, and 4 plays too dark.
    from convert.lights import MIN_SKYLIGHT_AMBIENT

    check("the floor applies to a map whose SkyLights are dim",
          light_stats.ambient[0], MIN_SKYLIGHT_AMBIENT)
    check("and it records what the gain alone would have given",
          light_stats.ambient_floored, 4)

    zone = make_zone_info(((0, 0, 0), (100, 100, 100)), 1024.0,
                          ambient=light_stats.ambient, kill_z=kill_z(p))
    check_that("the zone carries KillZ", prop_of(zone, "KillZ") == "-3000.000000")
    check("and the ambient", prop_of(zone, "AmbientBrightness"),
          str(light_stats.ambient[0]))
    # bTerrainZone is only right when there is terrain; this map has none.
    check_that("and is not claimed as a terrain zone",
               prop_of(zone, "bTerrainZone") is None)

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
