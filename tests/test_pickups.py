#!/usr/bin/env python3
"""Regression tests for pickup and path conversion (Phase 4).

    python3 tests/test_pickups.py [path/to/DM-HeatRay.ut3]
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.pickups import (PICKUP_CLASSES, WEAPON_CLASSES, convert_paths,
                             convert_pickups)
from ut3.package import Package
from ut3.props import read_object_properties

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

    print("mapping tables")
    # Every weapon must land on a class that exists in UT2004, and weapons go on
    # a base rather than being placed bare -- that is what stock maps do.
    # XWeapons holds the deathmatch arsenal; the AVRiL is UT2004's too, but it
    # ships with Onslaught, which is where stock ONS maps take it from.
    check_that("every weapon maps into XWeapons or Onslaught",
               all(v.split(".")[0] in ("XWeapons", "Onslaught")
                   for v in WEAPON_CLASSES.values()),
               str(sorted({v.split(".")[0] for v in WEAPON_CLASSES.values()})))
    check_that("every item maps into XGame or XPickups",
               all(v.split(".")[0] in ("XGame", "XPickups")
                   for v, _note in PICKUP_CLASSES.values()))
    # UT2004's Lightning Gun *is* class SniperRifle (see its ItemName), so this
    # is the right target and not a leftover from the UT2003 rifle.
    check("UT3's sniper becomes the Lightning Gun",
          WEAPON_CLASSES["UTWeap_SniperRifle"], "XWeapons.SniperRifle")

    print("pickups")
    taken = set()
    items, stats = convert_pickups(p, taken=taken)
    check("weapon bases", stats.weapons, 10)
    # 27, not 29: UT3 cooks class default objects into the map
    # (Default__UTArmorPickup_ShieldBelt), and converting one would drop a
    # phantom pickup at the world origin.
    check("items", stats.items, 27)
    check_that("nothing was left unmapped", not stats.unmapped, str(stats.unmapped))
    classes = {}
    for actor in items:
        classes[actor.cls] = classes.get(actor.cls, 0) + 1
    check("weapons are placed on xWeaponBase", classes.get("xWeaponBase"), 10)
    check("health vials", classes.get("MiniHealthPack"), 17)
    check("medium health", classes.get("HealthCharger"), 6)
    check("shield belts", classes.get("SuperShieldCharger"), 1)
    check_that("class defaults are not converted",
               all("Default__" not in a.name for a in items))
    check_that("every pickup has a location",
               all(prop_of(a, "Location") for a in items))
    bases = [a for a in items if a.cls == "xWeaponBase"]
    check_that("every weapon base names a weapon class",
               all(str(prop_of(a, "WeaponType")).startswith("Class'XWeapons.")
                   for a in bases))
    weapons = sorted(str(prop_of(a, "WeaponType")) for a in bases)
    check("DM-HeatRay's arsenal", weapons.count("Class'XWeapons.ShockRifle'"), 2)
    # The substitutions are deliberate and must stay visible in the report.
    check_that("substitutions are reported", bool(stats.substitutions),
               str(stats.substitutions))

    print("resting on the floor")
    # Both engines put Location at the centre of the collision cylinder, and
    # they disagree on the heights, so a straight copy leaves everything
    # floating -- which UnrealEd reports for every base
    # (AxPickUpBase::CheckForErrors traces just 8uu down, UnErrorChecking.cpp:204).
    from convert.pickups import UT2_BASE_HEIGHT, ground_offset

    check("weapon base drops by UT3 44 minus UT2004 3",
          ground_offset("UTWeaponPickupFactory", "xWeaponBase"), 41.0)
    check("charger drops onto the floor",
          ground_offset("UTPickupFactory_MediumHealth", "HealthCharger"),
          44.0 - UT2_BASE_HEIGHT)
    # The vial is the one that moves up: UT3 gives it a 20uu cylinder against
    # MiniHealthPack's 23.
    check("health vial rises slightly",
          ground_offset("UTPickupFactory_HealthVial", "MiniHealthPack"), -3.0)
    check("path nodes drop by 50 - 43",
          ground_offset("PathNode", "PathNode"), 7.0)
    check_that("every base ends up within the 8uu the map check allows",
               all(ground_offset(k, v.split(".")[-1]) >= 44.0 - 8.0
                   for k, (v, _n) in PICKUP_CLASSES.items()
                   if v.split(".")[-1].endswith("Charger")))

    print("paths")
    paths, stats = convert_paths(p, stats=stats, taken=taken)
    check("path nodes", stats.path_nodes, 144)
    check("jump pads", stats.jump_pads, 4)
    # A jump pad with no forced path is a map-check error in UnrealEd and gets a
    # straight-up 3*TESTJUMPZ velocity instead of one aimed at the destination
    # (Engine/Src/UnNavigationPoint.cpp:1305), so every link must survive.
    check("every jump pad kept its destination", stats.jump_pads_linked, 4)

    emitted = {a.name for a in paths}
    pads = [a for a in paths if a.cls == "UTJumpPad"]
    for pad in pads:
        target = prop_of(pad, "ForcedPaths(0)")
        check_that("%s points at a path node that exists" % pad.name,
                   target in emitted, "-> %s" % target)
    check_that("path node names are unique",
               len(emitted) == len(paths), "%d names for %d actors"
               % (len(emitted), len(paths)))
    check_that("names are legal t3d identifiers",
               all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", a.name) for a in paths))
    check_that("pickups and paths never share a name",
               len({a.name for a in items} & emitted) == 0)

    print("weapon lockers")
    # Both engines hold the same array of (weapon class, spare ammo) on the same
    # 50x80 cylinder, so only the ammo is invented -- UT3 never states it.
    from convert.pickups import (LOCKER_AMMO, UT2_LOCKER_RISE, UT3_LOCKER_FLOOR,
                                 convert_weapon_lockers)

    lockers, locker_stats = convert_weapon_lockers(p)
    ut3_lockers = [e for e in p.exports
                   if p.class_name_of(e) in ("UTWeaponLocker_Content", "UTWeaponLocker")
                   and not e.name.startswith("Default__")]
    if ut3_lockers:
        check("a locker per UT3 locker", len(lockers), len(ut3_lockers))
        check("counted", locker_stats.lockers, len(lockers))
        text = "\n".join(lockers[0].lines())
        check_that("its weapons are UT2004 classes",
                   "WeaponClass=Class'XWeapons." in text,
                   [l.strip() for l in text.splitlines() if "Weapons(0)" in l][0])
        check_that("with the ammo stock ONS maps give them",
                   "ExtraAmmo=%d" % LOCKER_AMMO["ShockRifle"] in text
                   or "ExtraAmmo=%d" % LOCKER_AMMO["LinkGun"] in text)
        # UT3 draws its locker 50 below the actor; UT2004's PrePivot is applied
        # before DrawScale (Engine/Inc/AActor.h:65), putting its mesh 52.5 down.
        src, _s, _e = read_object_properties(p, ut3_lockers[0])
        src_z = src.get("Location").value[2]
        got = float(re.search(r"Z=(\S+?)\)", [v for k, v in lockers[0].properties
                                               if k == "Location"][0]).group(1))
        check("and it rises by the difference in pivots", round(got - src_z, 1),
              round(UT2_LOCKER_RISE - UT3_LOCKER_FLOOR, 1))
    # UT2004 has the AVRiL under its own name, so it should not be skipped.
    check("the AVRiL maps to Onslaught's own",
          WEAPON_CLASSES.get("UTWeap_Avril_Content"), "Onslaught.ONSAVRiL")

    print("jump pad markers")
    # UT2004's JumpPad has no DrawType and no mesh (Engine/JumpPad.uc), so a
    # converted pad is an invisible trigger with nothing to show where it is.
    # UT3 keeps its marker on the class -- Default__UTJumpPad's components hold
    # a static mesh translated down 47 -- so no placed pad states it, and only
    # that offset is taken: the plate and effect themselves are stock UT2004.
    from convert.pickups import (JUMP_PAD_EFFECT, JUMP_PAD_MESH,
                                 JUMP_PAD_PLATE_RISE, jump_pad_markers)
    from ut3.resolve import PackageIndex

    index = PackageIndex.for_map(path)
    pads = [e for e in p.exports
            if p.class_name_of(e) == "UTJumpPad" and not e.name.startswith("Default__")]
    markers = jump_pad_markers(p, index, scale=1.0)
    if pads:
        plates = [a for a in markers if a.cls == "StaticMeshActor"]
        effects = [a for a in markers if a.cls == "Emitter"]
        check("a plate per jump pad", len(plates), len(pads))
        check("an effect per jump pad", len(effects), len(pads))
        text = "\n".join(plates[0].lines())
        check_that("the plate is the stock mesh", JUMP_PAD_MESH in text,
                   text.splitlines()[1].strip())
        # The pad's own cylinder launches the player; a solid mesh over it would
        # be another lip to walk up, as the power node pads were.
        check_that("and it does not collide", "bCollideActors=False" in text)
        pad_props, _s, _e = read_object_properties(p, pads[0])
        pad_z = pad_props.get("Location").value[2]
        plate_z = float(re.search(r"Z=(\S+?)\)", [v for k, v in plates[0].properties
                                                   if k == "Location"][0]).group(1))
        # 47 down to the floor UT3 draws its own plate on, then half the stock
        # plate's height back up, since that one is pivoted at its centre.
        check("the plate rests on the floor", round(pad_z - plate_z),
              round(47 - JUMP_PAD_PLATE_RISE))
        effect = "\n".join(effects[0].lines())
        check("the effect has both emitters", effect.count("Begin Object"),
              len(JUMP_PAD_EFFECT))
        check_that("referred to the way the editor writes them",
                   "Emitters(0)=SpriteEmitter'MyLevel." in effect)
        # The emitters belong to the level, so a name reused across pads would
        # be renamed on import and every later pad would refer to the wrong one.
        names = re.findall(r"Begin Object Class=SpriteEmitter Name=(\S+)",
                           "\n".join(l for a in effects for l in a.lines()))
        check("every emitter object name is unique", len(names), len(set(names)))

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
