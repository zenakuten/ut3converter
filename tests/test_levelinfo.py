#!/usr/bin/env python3
"""Regression tests for the level's LevelInfo and its game type.

    python3 tests/test_levelinfo.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.levelinfo import CTF, DEATHMATCH, ONSLAUGHT, game_type, make_level_info

_failures = []


def check(label, got, want):
    if got == want:
        print("  ok    %s = %r" % (label, got))
    else:
        print("  FAIL  %s = %r (expected %r)" % (label, got, want))
        _failures.append(label)


class Onslaught:
    cores = 2


class NoCores:
    cores = 0


class Objectives:
    def __init__(self, by_class):
        self.by_class = by_class


def check_that(label, cond, detail=""):
    if cond:
        print("  ok    %s %s" % (label, detail))
    else:
        print("  FAIL  %s %s" % (label, detail))
        _failures.append(label)


def main():
    print("game type, from what the conversion produced")
    # LevelInfo defaults PreCacheGame to deathmatch (Engine/LevelInfo.uc:522),
    # so a converted map precaches the wrong content unless this is set.
    check("no objectives at all is deathmatch", game_type(None, None), DEATHMATCH)
    check("power cores mean Onslaught", game_type(Onslaught(), None), ONSLAUGHT)
    check("flag bases mean CTF",
          game_type(NoCores(), Objectives({"xRedFlagBase": 1, "xBlueFlagBase": 1})),
          CTF)
    # A Warfare map has flag bases too -- UT3's orbs -- so cores must win.
    check("a Warfare map with flag bases is still Onslaught",
          game_type(Onslaught(), Objectives({"xRedFlagBase": 1})), ONSLAUGHT)
    check("objectives that are not flag bases stay deathmatch",
          game_type(NoCores(), Objectives({"Triggers": 3})), DEATHMATCH)

    print("the actor")
    actor = make_level_info(ONSLAUGHT)
    # The importer moves a LevelInfo from the t3d to Actors(0) and drops the one
    # the level came with (Editor/Src/UnEdFact.cpp:637).
    check("class", actor.cls, "LevelInfo")
    # DefaultGameType is what the map actually launches as -- ONS-Torlan sets
    # only that and leaves PreCacheGame at its deathmatch default -- and the
    # editor cannot be relied on to fill it in, because its
    # TObjectIterator<ALevelInfo> pass fixes the LevelInfo this actor orphaned
    # and returns (Engine/Src/UnLevel.cpp:1203).
    check("names the game type as both properties", actor.properties,
          [("DefaultGameType", '"Onslaught.ONSOnslaughtGame"'),
           ("PreCacheGame", '"Onslaught.ONSOnslaughtGame"')])

    print("vehicle CTF")
    # Flags and vehicles together is VCTF, which UT2004 has as stock. The
    # vehicles need no help: outside Onslaught an ONSVehicleFactory activates
    # itself for the team of the nearest GameObjective (ONSVehicleFactory.uc:41)
    # and a flag base is one -- which is why VCTF-Containment converts without
    # any vehicle-specific work.
    from convert.levelinfo import VEHICLE_CTF

    class Vehicles:
        cores = 0
        vehicles = 16

    class NoVehicles:
        cores = 0
        vehicles = 0

    check("flags plus vehicles is VCTF", game_type(Vehicles(), Objectives({"xRedFlagBase": 1})), VEHICLE_CTF)
    check("flags alone stay plain CTF", game_type(NoVehicles(), Objectives({"xRedFlagBase": 1})), CTF)
    # Cores win regardless: a Warfare map has vehicles too.
    check("cores still mean Onslaught", game_type(Onslaught(), Objectives({"xRedFlagBase": 1})), ONSLAUGHT)
    check("and vehicles without flags are not CTF at all",
          game_type(Vehicles(), None), DEATHMATCH)

    print("the Onslaught radar range")
    # The radar sizes itself from the terrain unless told otherwise:
    # RadarRange = |PrimaryTerrain.TerrainScale.X * TerrainMap.USize| / 2
    # (ONSHUDOnslaught.uc:83), and LevelInfo defaults bUseTerrainForRadarRange
    # to true. WAR-Torlan's second terrain covers distant scenery at 1032uu
    # quads, which sizes the radar at 132,096uu for a 54,372uu play area and
    # draws the whole level into the middle fifth of the minimap.
    from convert.levelinfo import (MAX_RADAR_RANGE, MIN_RADAR_RANGE,
                                   radar_range)

    # Centred on the world origin -- MapCenter = vect(0,0,0) at :270 -- so what
    # counts is the furthest reach from there, not the width.
    check("the range is measured from the origin, not the centre",
          radar_range(((-5000.0, -1000.0, 0.0), (25000.0, 1000.0, 0.0))), 25000.0)
    check("and takes whichever axis reaches further",
          radar_range(((-1000.0, -30000.0, 0.0), (1000.0, 2000.0, 0.0))), 30000.0)
    check("clamped the way the HUD clamps it", radar_range(((0.0, 0.0, 0.0),) * 2),
          MIN_RADAR_RANGE)
    check("at the top too", radar_range(((-1e9, 0.0, 0.0), (0.0, 0.0, 0.0))),
          MAX_RADAR_RANGE)

    bounds = ((-27186.0, -25379.0, 0.0), (27186.0, 25379.0, 0.0))
    ons = dict(make_level_info(ONSLAUGHT, bounds=bounds).properties)
    check("an Onslaught map stops sizing its radar from the terrain",
          ons.get("bUseTerrainForRadarRange"), "False")
    check("and states the range it needs", ons.get("CustomRadarRange"), "27186.000000")
    # Nothing else reads these, so a deathmatch map is left alone.
    dm = dict(make_level_info(DEATHMATCH, bounds=bounds).properties)
    check_that("a non-Onslaught map is not given a radar range",
               "CustomRadarRange" not in dm and "bUseTerrainForRadarRange" not in dm)
    check_that("nor is one whose bounds are unknown",
               "CustomRadarRange" not in dict(make_level_info(ONSLAUGHT).properties))

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
