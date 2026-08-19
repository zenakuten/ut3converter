#!/usr/bin/env python3
"""Regression tests for lift conversion, on DM-Deck (Phase 6).

DM-Deck is the second map the converter has been run against end to end, and
the first with real lifts. It is also what settled the move-frame rule that
DM-HeatRay could only half answer.

    python3 tests/test_lifts.py [path/to/DM-Deck.ut3]
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.curve import played_range
from convert.meshes import MeshSet
from convert.movers import (LIFT_STATE, convert_movers, find_lifts,
                            find_move_tracks, find_touch_movers, make_keys)
from convert.textures import TextureSet
from ut3.package import Package
from ut3.props import read_object_properties
from ut3.resolve import PackageIndex

DEFAULT_MAP = (
    "/home/josh/.steam/steam/steamapps/common/Unreal Tournament 3/"
    "UTGame/CookedPC/Maps/DM-Deck.ut3"
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

    print("finding the lifts")
    lifts = find_lifts(p, index)
    named = {p.exports[i - 1].name for i in lifts}
    # LiftCenter.MyLift names the lift outright -- far better than guessing
    # from the Matinee, and it is what UT3's own bot paths use.
    check("lifts named by a LiftCenter", named, {"InterpActor_16", "InterpActor_19"})
    # UE3's "a pawn used this mover" event agrees with LiftCenter here.
    check("SeqEvent_Mover reports on the same two",
          {p.exports[i - 1].name for i in find_touch_movers(p)}, named)

    print("keys")
    tracks = find_move_tracks(p, index)
    for name in sorted(named):
        export = [e for e in p.exports if e.name == name][0]
        props, _start, _end = read_object_properties(p, export)
        rotation = tuple(props.get("Rotation").value) if props.get("Rotation") else (0, 0, 0)
        location = tuple(props.get("Location").value)
        key_pos, key_rot, move_time = make_keys(tracks[export.index], rotation, location)
        # Both of Deck's lifts are the same 608uu rise, which is what the
        # LiftExits at the top (z=-604) against the bottom (z=-1228) require.
        check("%s keys" % name, len(key_pos), 2)
        check("%s starts where it is placed" % name,
              tuple(round(c) for c in key_pos[0]), (0, 0, 0))
        check("%s rises 608uu, straight up" % name,
              tuple(round(c) for c in key_pos[1]), (0, 0, 608))
        check("%s takes 1.5s" % name, round(move_time, 3), 1.5)
        check("%s does not turn" % name, key_rot, [(0, 0, 0), (0, 0, 0)])

    print("the window Matinee actually plays")
    # InterpActor_16's track carries a key at t=-2.196 describing the descent it
    # makes before the sequence starts. Sampling the curve's own extent instead
    # of [0, InterpLength] converts the lift as dropping 605uu through the floor.
    lift16 = tracks[[e for e in p.exports if e.name == "InterpActor_16"][0].index]
    check("its track really does start before zero", round(lift16.pos[0].t, 3), -2.196)
    check("InterpLength", round(lift16.length, 3), 1.502)
    # Clipped to both: the sequence starts at 0 and the last key is at 1.5, so
    # nothing past that would add anything.
    check("so the played window is [0, last key]",
          tuple(round(v, 3) for v in played_range(lift16.pos, lift16.length)),
          (0.0, 1.5))

    print("actors")
    movers, moved, stats = convert_movers(p, index, MeshSet("TestTex"),
                                          TextureSet("TestTex"))
    check("lifts converted", stats.lifts, 2)
    check("lift nav points", stats.lift_nav, 10)
    lift_movers = [a for a in movers
                   if a.cls == "Mover" and prop_of(a, "InitialState") == '"%s"' % LIFT_STATE]
    check("movers in the lift state", len(lift_movers), 2)
    # LiftCenter.SpecialHandling tests for this state by name
    # (Engine/LiftCenter.uc:38), so it is not interchangeable with the others.
    check_that("every lift is tagged", all(prop_of(a, "Tag") for a in lift_movers))
    # ALiftCenter::FindBase scans for actors whose Tag matches its LiftTag and
    # errors "Lift has same tag as another lift" on the second Mover it finds,
    # leaving MyLift unset so bots never use the lift. So a lift's parts cannot
    # be bSlave movers sharing its tag, the way the bullet train's are.
    all_movers = [a for a in movers if a.cls == "Mover"]
    for lift in lift_movers:
        shared = [a for a in all_movers if prop_of(a, "Tag") == prop_of(lift, "Tag")]
        check("%s is the only Mover with its tag" % lift.name, len(shared), 1)
    # The parts get their own tag and their own copy of the path, and the lift
    # triggers them as it starts moving.
    for lift in lift_movers:
        part_tag = prop_of(lift, "Tag")[:-1] + 'Parts"'
        parts = [a for a in all_movers if prop_of(a, "Tag") == part_tag]
        check_that("%s drives %d part(s) by event" % (lift.name, len(parts)),
                   parts and prop_of(lift, "OpeningEvent") == part_tag)
        check_that("which run the same path", all(
            prop_of(a, "KeyPos(1)") == prop_of(lift, "KeyPos(1)") for a in parts))
        check_that("in a state that answers a trigger", all(
            prop_of(a, "InitialState") == '"TriggerOpenTimed"' for a in parts))
        # Both ends time their own open/wait/close, so the waits must agree.
        check_that("waiting as long as the lift does", all(
            prop_of(a, "StayOpenTime") == prop_of(lift, "StayOpenTime") for a in parts))
    # A lift has to carry players, so it keeps the engine's own encroach
    # behaviour rather than the ignore-everything one background scenery gets.
    check_that("a lift is not set to ignore what it hits",
               all(prop_of(a, "MoverEncroachType") is None for a in lift_movers))

    centres = [a for a in movers if a.cls == "LiftCenter"]
    exits = [a for a in movers if a.cls == "LiftExit"]
    check("LiftCenters", len(centres), 2)
    check("LiftExits", len(exits), 8)
    tags = {prop_of(a, "Tag") for a in lift_movers}
    check_that("every nav point binds to a lift that exists",
               all(prop_of(a, "LiftTag") in tags for a in centres + exits),
               str(sorted(tags)))
    # UT3 leaves MyLift unset on exits, so they are matched to the nearest
    # centre. Deck's two lifts sit 832uu apart, which separates cleanly.
    by_tag = {}
    for actor in exits:
        by_tag.setdefault(prop_of(actor, "LiftTag"), []).append(actor.name)
    check("exits split evenly between the two lifts",
          sorted(len(v) for v in by_tag.values()), [4, 4])
    # UTJumpLiftExit is a lift-jump spot: UT2004 says the same thing with a flag.
    jumps = [a for a in exits if prop_of(a, "bLiftJumpExit") == "True"]
    check("lift-jump exits", len(jumps), 2)
    check_that("and they are the ones UT3 marked",
               {a.name for a in jumps} == {"UTJumpLiftExit_5", "UTJumpLiftExit_6"})
    check_that("nav names are legal t3d identifiers",
               all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", a.name)
                   for a in centres + exits))

    print("hazard volumes")
    # UT3's green goo. UT2004 has no slime volume class, so it is built from a
    # PhysicsVolume the way XGame.LavaVolume is, with the bio rifle's damage
    # type -- what UT2004 uses for anything green that dissolves you.
    from convert.geometry import VOLUME_CLASSES, VOLUME_PROPERTIES, convert_brushes

    check_that("UTSlimeVolume is converted at all", "UTSlimeVolume" in VOLUME_CLASSES)
    slime = dict(VOLUME_PROPERTIES["UTSlimeVolume"])
    check("it causes pain", slime["bPainCausing"], "True")
    check("with the bio damage type", slime["DamageType"],
          "Class'XWeapons.DamTypeBioGlob'")
    # UT3's own UTSlimeVolume defaults, not numbers picked here.
    check("at UT3's damage rate", slime["DamagePerSec"], "7.000000")
    check("and UT3's drag", slime["FluidFriction"], "5.000000")
    check_that("it is a fluid you sink in", slime.get("bWaterVolume") == "True")
    # UT2004 ships exact counterparts for these two, so they are used directly
    # rather than a PhysicsVolume dressed up to look like one.
    check("UTLavaVolume uses the stock lava volume",
          VOLUME_CLASSES.get("UTLavaVolume"), "LavaVolume")
    check("UTWaterVolume uses the stock water volume",
          VOLUME_CLASSES.get("UTWaterVolume"), "WaterVolume")

    volumes, brush_stats = convert_brushes(p, include_volumes=True)
    slimes = [b for b in volumes if b.name.startswith("UTSlimeVolume")]
    check("DM-Deck's goo pits", len(slimes), 3)
    check_that("each is a PhysicsVolume",
               all(getattr(b, "cls", None) == "PhysicsVolume" for b in slimes))
    check_that("each carries the damage settings",
               all(("bPainCausing", "True") in b.properties for b in slimes))

    print("the goo you can see")
    # The pit is drawn by fog-sheet meshes wearing M_HU_Deck_Goo_Translucent,
    # which are unlit translucent and so skipped as effects -- leaving a pit
    # that kills you but is invisible. UT2004 cannot build a FinalBlend from a
    # ucc make, but it ships one that says the same thing, and Actor.Skins
    # overrides a mesh's material per actor.
    from convert.shaders import EFFECT_SUBSTITUTES

    check_that("goo has a stock stand-in",
               any(t == "goo" for t, _m in EFFECT_SUBSTITUTES))
    check("which is the bio rifle's own material",
          dict(EFFECT_SUBSTITUTES)["goo"], "FinalBlend'XEffectMat.goop.GoopFB'")

    # Only the liquid *surface* gets it. UT3 builds a goo pit from one
    # horizontal sheet plus several vertical ones filling the shaft, all the
    # same plane mesh; the vertical ones are haze faded by depth-biased alpha
    # and genuinely reach far above the goo (one of Deck's runs 1172uu over the
    # surface), so a solid FinalBlend turns them into slabs through the floor.
    from convert.meshes import MeshSet as _MeshSet, convert_actors
    from convert.shaders import sheet_is_horizontal
    from convert.textures import TextureSet as _TextureSet

    mesh_actors, mesh_stats = convert_actors(p, index, _MeshSet("TestTex"),
                                             _TextureSet("TestTex"))
    goo = [a for a in mesh_actors if prop_of(a, "Skins(0)")]
    check("sheets given the goo material", len(goo), 2)
    check_that("and every one of them is level",
               all(prop_of(a, "Rotation") in (None, "(Pitch=0,Yaw=32769,Roll=0)")
                   for a in goo), str([prop_of(a, "Rotation") for a in goo]))
    # Both sit on a goo surface: -1288 against -1292, and -888 against -892.
    heights = sorted(round(float(re.search(r"Z=(\S+?)\)", prop_of(a, "Location")).group(1)))
                     for a in goo)
    check("at the two goo surfaces", heights, [-1288, -888])
    # And you have to be able to fall through one. UT2004's StaticMeshActor is
    # solid by default on all three counts, so a sheet drawn over a pit would
    # be a floor.
    check_that("the goo surface is walk-through",
               all(prop_of(a, "bCollideActors") == "False"
                   and prop_of(a, "bBlockActors") == "False" for a in goo))
    # UT3 states it on the actor or on the mesh component, and either counts.
    check_that("and so is everything else UT3 marks that way",
               mesh_stats.non_colliding > 700, "%d actors" % mesh_stats.non_colliding)

    print("teleporters")
    # Both engines pair a teleporter the same way: the sender names its
    # destination in URL, the destination answers to that name in Tag. UT3's
    # DM-Deck uses "RedeemME"; DM-Deck17 uses "upstairsred" for the same pair.
    from convert.teleporters import (BASE_MESH, PORTAL_MESH, PORTAL_SKIN,
                                     convert_teleporters)

    tele, tele_stats = convert_teleporters(p)
    check("teleporters converted", tele_stats.teleporters, 2)
    check("one sends", tele_stats.senders, 1)
    check("one receives", tele_stats.destinations, 1)
    check_that("and it is a matched pair, not a dangling URL",
               not tele_stats.unpaired, str(tele_stats.unpaired))
    ports = [a for a in tele if a.cls == "Teleporter"]
    urls = [prop_of(a, "URL") for a in ports if prop_of(a, "URL")]
    tags = [prop_of(a, "Tag") for a in ports if prop_of(a, "Tag")]
    check("the URL names the destination's tag", urls, tags)
    # UE3's default Tag is the class name and means nothing; carrying it over
    # would invent a destination that does not exist.
    check_that("the class-name tag is not carried over",
               '"UTTeleporter"' not in tags, str(tags))

    # UT2004's Teleporter draws nothing at all, so a converted one is invisible
    # without help. DM-Deck17 dresses its own with two stock meshes.
    portals = [a for a in tele if PORTAL_MESH in str(prop_of(a, "StaticMesh"))]
    bases = [a for a in tele if BASE_MESH in str(prop_of(a, "StaticMesh"))]
    # Only what you walk into. A destination is somewhere you arrive, and UT3
    # says so itself by giving it a Tag and no URL.
    check("only the sender is drawn", len(portals), 1)
    check("with a base under it", len(bases), 1)
    check_that("and the destination is left bare",
               len([a for a in tele if a.cls == "StaticMeshActor"]) == 2)

    # Placed from the floor, not from the teleporter: DM-Deck17 stands its
    # teleporter 56.25 above the floor and a UT3 one stands 34, and both meshes
    # hang below their pivot (TelePorterbase -81..-56, teleporter-proc
    # -59..+71), so measuring from the actor buries the base underground.
    from convert.teleporters import (BASE_ABOVE_FLOOR, PORTAL_ABOVE_FLOOR,
                                     UT3_FLOOR_DROP)

    sender = [a for a in ports if prop_of(a, "URL")][0]
    sender_z = float(re.search(r"Z=(\S+?)\)", prop_of(sender, "Location")).group(1))
    floor = sender_z - UT3_FLOOR_DROP
    for actor, above, extent in ((portals[0], PORTAL_ABOVE_FLOOR, (-59.0, 71.0)),
                                 (bases[0], BASE_ABOVE_FLOOR, (-81.0, -56.0))):
        z = float(re.search(r"Z=(\S+?)\)", prop_of(actor, "Location")).group(1))
        check("%s sits %g above the floor" % (actor.name, above), round(z - floor, 2), above)
    # Which puts the base straddling the floor and the portal standing on it,
    # exactly as DM-Deck17 has them.
    base_z = float(re.search(r"Z=(\S+?)\)", prop_of(bases[0], "Location")).group(1))
    check("the base's top clears the floor by 21",
          round(base_z - 56.0 - floor), 21)
    check_that("and its bottom is only just below it",
               -8 < base_z - 81.0 - floor < 0, "%.0f" % (base_z - 81.0 - floor))
    check("wearing the shield shell, as DM-Deck17 does",
          prop_of(portals[0], "Skins(0)"), PORTAL_SKIN)
    # You walk through the portal to use the teleporter, so it must not block.
    check_that("neither mesh can block the teleporter or snag a player",
               all(prop_of(a, "bBlockActors") == "False"
                   and prop_of(a, "bCollideActors") == "False"
                   for a in portals + bases))
    check_that("names are unique",
               len({a.name for a in tele}) == len(tele))

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
