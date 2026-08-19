#!/usr/bin/env python3
"""Regression tests for Matinee -> Mover conversion (Phase 5d).

    python3 tests/test_movers.py [path/to/DM-HeatRay.ut3]
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.curve import CurvePoint, eval_curve, sample
from convert.meshes import MeshSet
from convert.movers import (MAX_KEYS, _rotation_matrix, _rotate, convert_movers,
                            find_move_tracks, key_extent, make_keys)
from convert.textures import TextureSet
from ut3.package import Package
from ut3.props import read_object_properties
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


def _find_map(reference, name):
    """Locate another UT3 map relative to the one under test.

    UT3 keeps maps in three directories -- Maps, Private/Maps and UT3G/Maps
    (the Titan Pack) -- so search from CookedPC rather than assuming a sibling.
    """
    root = os.path.dirname(os.path.abspath(reference))
    while root != os.path.dirname(root) and os.path.basename(root) != "CookedPC":
        root = os.path.dirname(root)
    for dirpath, _dirs, files in os.walk(root):
        if name + ".ut3" in files:
            return os.path.join(dirpath, name + ".ut3")
    return None


def main(path):
    p = Package(path)
    index = PackageIndex.for_map(path)

    print("curve evaluation")
    pts = [CurvePoint(0.0, (0.0, 0.0, 0.0), (0,) * 3, (0,) * 3, "CIM_Linear"),
           CurvePoint(2.0, (10.0, 0.0, 0.0), (0,) * 3, (0,) * 3, "CIM_Linear")]
    check("linear midpoint", eval_curve(pts, 1.0)[0], 5.0)
    check("clamped past the end", eval_curve(pts, 99.0)[0], 10.0)
    check("clamped before the start", eval_curve(pts, -1.0)[0], 0.0)
    check("five even samples", [round(v[0], 3) for v in sample(pts, 5)],
          [0.0, 2.5, 5.0, 7.5, 10.0])
    hold = [CurvePoint(0.0, (0.0,) * 3, (0,) * 3, (0,) * 3, "CIM_Constant"),
            CurvePoint(2.0, (10.0, 0.0, 0.0), (0,) * 3, (0,) * 3, "CIM_Linear")]
    check("a constant segment holds", eval_curve(hold, 1.9)[0], 0.0)

    print("attachment offsets rotate by the parent")
    # Not the move frame -- this is UE3 hard attachment, and it is how the
    # carriage spacing is stated. The train's lead car sits at 180 degrees of
    # yaw and its followers' RelativeLocation of +6788 X lands at -6788 X.
    half_turn = _rotation_matrix((0, 32768, 0))
    turned = _rotate((6788.0, 0.0, 0.0), half_turn)
    check("half a turn of yaw flips X", round(turned[0]), -6788)
    check("and leaves Z alone", round(turned[2]), 0)
    check("no rotation is identity",
          tuple(round(c) for c in _rotate((1.0, 2.0, 3.0), _rotation_matrix((0, 0, 0)))),
          (1, 2, 3))

    print("move tracks")
    tracks = find_move_tracks(p, index)
    by_name = {p.exports[i - 1].name: t for i, t in tracks.items()}
    check("actors with a move track", len(tracks), 6)
    check_that("the bullet train is one of them", "InterpActor_7" in by_name)
    train = by_name["InterpActor_7"]
    check("the train's track is relative to its initial transform", train.relative, True)
    # LevelBeginning -> Delay -> Play, and Completed -> Delay again: a loop with
    # a pause in it, which is how UT3 spaces out repeating scenery.
    check("and it repeats forever", train.looping, True)
    # The cinematic ship fires 120s after a scripted death and plays once.
    check("the cinematic ship does not", by_name["InterpActor_0"].looping, False)

    print("keys")
    placed = (-12384.1015625, 3283.88720703125, 435.9999694824218)
    key_pos, key_rot, move_time = make_keys(train, (0, 32768, 0), placed)
    check("a two-point track stays two keys", len(key_pos), 2)
    # UnrealEd draws a mover at BasePos + KeyPos[KeyNum], so a non-zero key 0
    # would show the train off its rails in the editor as well as in game.
    check("key 0 is the placed transform", tuple(round(c) for c in key_pos[0]), (0, 0, 0))
    start = tuple(round(placed[i] + key_pos[0][i]) for i in range(3))
    end = tuple(round(placed[i] + key_pos[1][i]) for i in range(3))
    check("it starts parked on the viaduct", start, (-12384, 3284, 436))
    # The deck runs along y=3284 at z=436 and its pillars carry it through the
    # play area (PlayerStarts span x -1798..3876), so the train has to stay on
    # that line and run +X to cross the level.
    check("and runs 70021uu along it, through the play area", end, (57637, 3284, 436))
    check("never leaving the deck", (end[1], end[2]), (3284, 436))
    # InterpLength, not the last key's time: the track's final key sits at
    # t=5.9913 but Matinee stops the sequence at 5.986.
    check("MoveTime is how long Matinee plays it", round(move_time, 3), 5.986)
    check("its rotation never changes", key_rot, [(0, 0, 0), (0, 0, 0)])

    print("the frame a relative track is stated in")
    # HeatRay's is the exception: flagged relative but holding world
    # coordinates, so the delta between its keys is already a world offset and
    # turning it by the placed 180 degrees would run the train backwards off the
    # end of the line. The check above is what pins that down.
    check_that("this track does not start at the origin",
               any(abs(c) > 1.0 for c in train.pos[0].out),
               str(train.pos[0].out))
    # Everywhere else a relative track is stated in the actor's own frame and
    # has to be turned. Two maps prove it on their own, because the actor says
    # in its name which way it has to go and the unturned delta does not.
    for map_name, actor, want_local, want_world in (
            # 992uu of lift, placed upside down: unturned it descends.
            ("CTF-Vertebrae", "InterpActor_3", (0, 0, -992), (0, 0, 992)),
            # And this one's unturned delta is not even vertical.
            ("DM-RisingSun", "InterpActor_2", (0, 380, 0), (0, 0, 380))):
        lift_map = _find_map(path, map_name)
        if lift_map is None:
            print("  skip   %s not found" % map_name)
            continue
        lp = Package(lift_map)
        lift_tracks = find_move_tracks(lp, PackageIndex.for_map(lift_map))
        export = [e for e in lp.exports if e.name == actor][0]
        track = lift_tracks[export.index]
        props, _s, _e = read_object_properties(lp, export)
        rotation = tuple(props.get("Rotation").value)
        far = max(track.pos, key=lambda pt: sum(c * c for c in pt.out))
        check("%s %s states its motion locally" % (map_name, actor),
              tuple(round(c) for c in far.out), want_local)
        keys, _rot, _time = make_keys(track, rotation, (0.0, 0.0, 0.0))
        moved = max(keys, key=lambda k: sum(c * c for c in k))
        check("and it is turned into place", tuple(round(c) for c in moved), want_world)
        check_that("which is the only way a lift goes up",
                   moved[2] > 0 and abs(moved[0]) < 1 and abs(moved[1]) < 1)

    wobble = by_name["InterpActor_1"]
    pos, rot, _time = make_keys(wobble, (0, 0, 0), (0.0, 0.0, 0.0))
    check("a curved track is resampled to the key limit", len(pos), MAX_KEYS)
    check_that("and it is a rotation, not a translation",
               all(abs(c) < 1.0 for k in pos for c in k) and any(any(k) for k in rot))

    print("actors")
    mesh_set = MeshSet("TestTex")
    movers, moved, stats = convert_movers(p, index, mesh_set, TextureSet("TestTex"))
    # Five bullet-train carriages and the three-piece cinematic ship. The four
    # light beams are unlit translucent effect meshes, skipped here for the same
    # reason the static pass skips them.
    check("movers emitted", stats.movers, 8)
    check("of which attached followers", stats.followers, 6)
    # Counted per group, not per actor: the train loops, the ship waits.
    check("looping groups", stats.looping, 1)
    check("dormant groups, awaiting a trigger", stats.dormant, 1)
    check("no lifts in this map", stats.lifts, 0)
    check("effect meshes left out", stats.skipped_no_mesh, 4)
    check_that("every mover is claimed so the static pass skips it",
               len(moved) == stats.movers + stats.skipped_no_mesh,
               "%d claimed" % len(moved))
    check_that("all are class Mover", all(a.cls == "Mover" for a in movers))
    check_that("every mover draws a static mesh",
               all(prop_of(a, "DrawType") == "DT_StaticMesh"
                   and prop_of(a, "StaticMesh") for a in movers))
    check_that("names are legal t3d identifiers",
               all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", a.name) for a in movers))
    leaders = [a for a in movers if prop_of(a, "bSlave") is None]
    slaves = [a for a in movers if prop_of(a, "bSlave") == "True"]
    check("leaders", len(leaders), 2)
    check("slaves", len(slaves), 6)
    # A non-slave Mover attaches every bSlave Mover sharing its Tag to itself
    # (Engine/Mover.uc:454), so a follower needs a matching Tag and no path.
    check_that("every slave shares its leader's tag",
               {prop_of(a, "Tag") for a in slaves}
               <= {prop_of(a, "Tag") for a in leaders})
    check_that("and carries no keys of its own",
               all(not any(k.startswith("Key") for k, _v in a.properties)
                   for a in slaves))
    check_that("every leader is tagged", all(prop_of(a, "Tag") for a in leaders))
    # UT2004's default is ME_ReturnWhenEncroach, and a 70,000uu train shoving
    # players around is not what UT3 did. Lifts and doors keep the default,
    # because they are supposed to carry people.
    check_that("background movers cannot crush or shove",
               all(prop_of(a, "MoverEncroachType") == "ME_IgnoreWhenEncroach"
                   for a in leaders))
    check_that("NumKeys matches the keys actually written",
               all(int(prop_of(a, "NumKeys"))
                   == sum(1 for k, _v in a.properties if k.startswith("KeyPos("))
                   for a in leaders))
    check_that("NumKeys never exceeds Mover.KeyPos",
               all(int(prop_of(a, "NumKeys")) <= MAX_KEYS for a in leaders))

    train_movers = [a for a in movers if "BulletTrain" in str(prop_of(a, "StaticMesh"))]
    check("the whole train is emitted, not just the lead car", len(train_movers), 5)
    check_that("every carriage shares one tag",
               len({prop_of(a, "Tag") for a in train_movers}) == 1)
    check("exactly one carriage carries the path",
          sum(1 for a in train_movers if prop_of(a, "NumKeys")), 1)
    lead = [a for a in train_movers if prop_of(a, "NumKeys")][0]
    check("and it loops without a trigger", prop_of(lead, "InitialState"),
          '"ConstantLoop"')

    print("world reach")
    low, high = key_extent(lead)
    # The parked position is one end of the extent and the run is the other, so
    # the void has to cover both -- an actor outside it renders nowhere.
    check("the extent covers the run", (round(low[0]), round(high[0])), (-12384, 57637))
    check_that("starting from where it is parked", round(low[0]) == -12384)
    # Slaves travel too, so the void has to reach their path as well.
    slave = [a for a in train_movers if prop_of(a, "bSlave")][0]
    slo, shi = key_extent(slave)
    check_that("a slave's extent covers the leader's path too",
               round(shi[0] - slo[0]) == 70021)

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
