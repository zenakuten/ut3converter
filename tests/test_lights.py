#!/usr/bin/env python3
"""Regression tests for light and PlayerStart conversion (Phase 1d/1e).

    python3 tests/test_lights.py [path/to/DM-HeatRay.ut3]
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.actors import convert_player_starts
from convert.lights import (
    DEFAULT_GAIN,
    cone_angle_to_ue2,
    colour_to_ue2,
    convert_lights,
    light_radius_to_ue2,
)
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
    print("unit conversions")
    # WorldLightRadius() = 25 * (LightRadius + 1), so 700uu -> 27.
    check("radius 700uu", light_radius_to_ue2(700.0), 27.0)
    check("radius 1024uu", round(light_radius_to_ue2(1024.0), 3), 39.96)
    check_that("radius is clamped into byte range",
               light_radius_to_ue2(1e9) == 255.0 and light_radius_to_ue2(0.0) == 1.0)
    # half-angle = acos(1 - LightCone/256), so 60 degrees -> 128.
    check("cone 60 degrees", cone_angle_to_ue2(60.0), 128)
    check("cone 0 degrees", cone_angle_to_ue2(0.0), 0)
    check("cone 90 degrees", cone_angle_to_ue2(90.0), 255)
    # UE2 LightSaturation is inverted: 255 is white.
    check("white", colour_to_ue2((255, 255, 255))[:2], (0, 255))
    check("pure red is fully saturated", colour_to_ue2((255, 0, 0))[:2], (0, 0))
    check("pure green hue", colour_to_ue2((0, 255, 0))[0], 85)
    check("value is returned separately", colour_to_ue2((128, 128, 128))[2], 128 / 255.0)

    p = Package(path)

    print("light conversion")
    lights, stats = convert_lights(p)
    check("lights converted", stats.converted, 461)
    check("class distribution", dict(stats.by_class), {"Light": 380, "Spotlight": 79, "Sunlight": 2})
    check("disabled lights skipped", stats.skipped_disabled, 2)
    check("zero-brightness lights skipped", stats.skipped_dark, 5)
    check_that("SkyLight reported as unsupported", stats.unsupported.get("SkyLight") == 1)
    # UT2004 has no SkyLight actor, so it becomes a zone ambient recommendation.
    check_that("SkyLight yields an ambient recommendation", stats.ambient is not None,
               str(stats.ambient))
    # Grounded in what UT2004 maps actually do, not in taste: 250 of this
    # install's 311 stock maps never set zone ambient at all, and across the 367
    # zones that do the median is 4 and the 90th percentile is 12. The light in
    # a UT2004 map comes from placed lights. An earlier gain put this map at 32
    # and WAR-PowerSurge at 118 -- ten times that 90th percentile, and visibly
    # brighter than the UT3 original.
    # The gain sets the relationship between maps and the floor sets the bottom
    # of it. One gain cannot do both: WAR-PowerSurge stacks two SkyLights into
    # 15, which reads well, while DM-HeatRay and WAR-Torlan have one dim one and
    # land on 4 and 3 -- too dark to play. Raising the gain until those looked
    # right put PowerSurge back near the 118 that was visibly wrong.
    from convert.lights import MIN_SKYLIGHT_AMBIENT

    check("ambient brightness", stats.ambient[0], MIN_SKYLIGHT_AMBIENT)
    check("this map's own SkyLight is dimmer than the floor", stats.ambient_floored, 4)
    check_that("so it was raised, not scaled",
               stats.ambient[0] == MIN_SKYLIGHT_AMBIENT
               and stats.ambient_floored < MIN_SKYLIGHT_AMBIENT)
    # A map that already clears the floor is left exactly where the gain put it.
    from convert.lights import convert_lights as _cl

    _lights, bright = _cl(p, ambient_gain=64.0)
    check_that("a map above the floor keeps its own value",
               bright.ambient[0] > MIN_SKYLIGHT_AMBIENT
               and bright.ambient_floored is None, str(bright.ambient[0]))
    # And asking for no ambient at all still means none.
    _lights, none = _cl(p, ambient_gain=0.0)
    check("--ambient-gain 0 is still zero", none.ambient[0], 0)

    by_name = {a.name: a for a in lights}
    pl0 = by_name["PointLight_0"]
    # PointLight_0: UE3 Brightness 0.35, Radius 700, no LightColor (white).
    check("PointLight_0 brightness", prop_of(pl0, "LightBrightness"), "%.6f" % (0.35 * DEFAULT_GAIN))
    check("PointLight_0 radius", prop_of(pl0, "LightRadius"), "27.000000")
    check("PointLight_0 saturation is white", prop_of(pl0, "LightSaturation"), "255")
    check("PointLight_0 location", prop_of(pl0, "Location"),
          "(X=-1125.000000,Y=1402.000000,Z=-862.000000)")

    check_that("every spotlight has a cone",
               all(prop_of(a, "LightCone") is not None for a in lights if a.cls == "Spotlight"))
    check_that("no sunlight carries a radius",
               all(prop_of(a, "LightRadius") is None for a in lights if a.cls == "Sunlight"))
    check_that("brightness stays within 0..255",
               all(0.0 <= float(prop_of(a, "LightBrightness")) <= 255.0 for a in lights))
    check("light names are unique", len(by_name), len(lights))

    print("radius scaling")
    wide, _ = convert_lights(p, radius_scale=2.0)
    wide_by_name = {a.name: a for a in wide}
    check("radius doubles with --light-radius-scale 2",
          prop_of(wide_by_name["PointLight_0"], "LightRadius"), "55.000000")

    print("player starts")
    starts, actor_stats = convert_player_starts(p)
    check("player starts converted", actor_stats.player_starts, 17)
    source = p.find("PlayerStart_0")[0]
    props, _s, _e = read_object_properties(p, source)
    ps0 = {a.name: a for a in starts}["PlayerStart_0"]
    loc = props.get("Location").value
    # Not a straight copy: Location is the centre of the collision cylinder and
    # UT3's PlayerStart is 80uu tall against UT2004's inherited 43, so a start
    # copied verbatim hangs 37uu above the floor.
    from convert.actors import GROUND_OFFSET

    check("PlayerStart_0 x/y match source, z drops onto the floor",
          prop_of(ps0, "Location"),
          "(X=%f,Y=%f,Z=%f)" % (loc[0], loc[1], loc[2] - GROUND_OFFSET))
    check("the drop is UT3's 80 against UT2004's 43", GROUND_OFFSET, 37.0)
    check("PlayerStart_0 rotation matches source", prop_of(ps0, "Rotation"),
          "(Pitch=%d,Yaw=%d,Roll=%d)" % tuple(props.get("Rotation").value))
    check_that("all starts are UT2004 PlayerStarts",
               all(a.cls == "PlayerStart" for a in starts))
    check("start names are unique", len({a.name for a in starts}), len(starts))

    print("emitted text")
    for actor in lights[:50] + starts:
        text = "\n".join(actor.lines(0))
        if "nan" in text.lower() or "inf" in text.lower():
            check_that("no NaN/Inf in %s" % actor.name, False)
            break
    else:
        check_that("no NaN or Inf in emitted actors", True)

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
