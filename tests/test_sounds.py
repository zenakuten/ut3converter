#!/usr/bin/env python3
"""Regression tests for ambient sound conversion (Phase 5c).

    python3 tests/test_sounds.py [path/to/DM-HeatRay.ut3]
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.sounds import (MAX_RADIUS_RATIO, SoundSet, convert_ambient_sounds,
                            sound_radius)
from ut3.objects.sound import distribution_value, ogg_channels, read_sound_wave
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


def main(path):
    p = Package(path)
    index = PackageIndex.for_map(path)

    print("wave payloads")
    waves = p.exports_of_class("SoundNodeWave")
    check("SoundNodeWave exports", len(waves), 107)
    read = [read_sound_wave(p, e, index) for e in waves]
    check_that("every wave carries an inline Ogg",
               all(w is not None and w.ogg[:4] == b"OggS" for w in read),
               "%d of %d" % (sum(1 for w in read if w), len(read)))
    wind = [w for w in read if w and w.name == "stereo_wind04"][0]
    check("stereo_wind04 sample rate", wind.sample_rate, 11025)
    check("stereo_wind04 duration", round(wind.duration, 2), 7.95)
    # NumChannels is elided when it matches the archetype, so the property says
    # mono for a wave that plainly is not. ALAudio refuses to build a buffer for
    # a stereo USound at all, so this is what decides the downmix.
    check("the Ogg header knows it is stereo", ogg_channels(wind.ogg), 2)

    print("distributions through the archetype chain")
    # Through the actor, not by node name: every ambient actor owns a node
    # called SoundNodeAmbient_0 or _2, distinguished only by its outer.
    actor = [e for e in p.exports if e.name == "AmbientSoundSimple_13"][0]
    actor_props, _start, _end = read_object_properties(p, actor)
    owner, node = index.resolve(p, actor_props.get("AmbientProperties"))
    props, _start, _end = read_object_properties(owner, node)
    # Min/Max are only written when the mapper changed them. Reading the
    # instance alone gives 0 -- a silent pitch shift of two octaves down.
    check("an untouched pitch reads as unity",
          distribution_value(p, index, props.get("PitchModulation"), 1.0), 1.0)
    check("an edited radius still reads as edited",
          distribution_value(p, index, props.get("MaxRadius"), 5000.0), 900.0)

    print("radius mapping")
    # Half volume at the geometric mean in UE3, at 2*SoundRadius in UE2.
    check("a 200..900 ambient", round(sound_radius(200.0, 900.0), 1), 212.1)
    check_that("the min:max ratio is capped",
               sound_radius(0.0, 1000.0) == sound_radius(1000.0 / MAX_RADIUS_RATIO, 1000.0),
               "MinRadius 0 would otherwise collapse the mean to zero")
    check("no radius without a max", sound_radius(400.0, 0.0), None)

    print("actors")
    sound_set = SoundSet("TestTex")
    actors, stats = convert_ambient_sounds(p, index, sound_set)
    # 59 simple + 5 non-loop + 5 toggleables that set bAutoPlay. The Cicada
    # engine and the alarm horn are switched on by Kismet, which does not
    # convert, and start silent in UT3 too.
    check("ambient actors", stats.actors, 69)
    check("toggleables left out", stats.skipped_silent, 2)
    check("waves referenced", len(sound_set.waves), 36)
    check_that("every actor is an AmbientSound",
               all(a.cls == "AmbientSound" for a in actors))
    check_that("every actor has a location",
               all(prop_of(a, "Location") for a in actors))
    check_that("every actor names at least one sound",
               all(a.sound_names for a in actors))
    check_that("names are legal t3d identifiers",
               all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", a.name) for a in actors))
    check_that("names are unique", len({a.name for a in actors}) == len(actors))

    loops = [a for a in actors if prop_of(a, "AmbientSound")]
    check("looping ambients", len(loops), 64)
    check_that("every loop is fully qualified",
               all(str(prop_of(a, "AmbientSound")).startswith("Sound'TestTex.Ambient.")
                   for a in loops))
    check_that("every ambient sets a volume in range",
               all(1 <= int(prop_of(a, "SoundVolume")) <= 255 for a in actors))
    # UnAudio.cpp:196 clamps SoundPitch/64 to 0.5..2.0, so anything outside
    # 32..128 is silently ignored rather than applied.
    check_that("every pitch survives the engine clamp",
               all(32 <= int(prop_of(a, "SoundPitch")) <= 128 for a in actors))

    wind_actor = [a for a in actors if a.sound_names == ["stereo_wind04"]][0]
    check("the map-wide wind bed keeps its reach",
          round(float(prop_of(wind_actor, "SoundRadius"))), 46669)
    # UE3's 1.0 lands on 13 -- see UT2_VOLUME_UNITY, where the reasoning from 255
    # down to 100 is the engine's and the rest is calibration by ear against
    # DM-Dekk. What the check pins is that the mapping stays proportional to
    # UT3's own number rather than flattening every ambient to one level.
    check("its volume follows UT3's 0.68", int(prop_of(wind_actor, "SoundVolume")), 9)

    print("one-shot emitters")
    # UE3 draws one of 13 traffic flybys every 1..5 seconds. UT2004 runs each
    # emitter on its own clock, so the interval is stretched by the slot count
    # to keep the same number of sounds per second.
    non_loop = [a for a in actors if not prop_of(a, "AmbientSound")]
    check("non-loop actors", len(non_loop), 5)
    check("emitter entries", stats.emitters, 65)
    check("slots per actor", len(non_loop[0].sound_names), 13)
    check("interval is 13 slots x a 3s mean", prop_of(non_loop[0], "SoundEmitters(0)"),
          "(EmitInterval=39.000000,EmitVariance=26.000000,"
          "EmitSound=Sound'TestTex.Ambient.TrafficFlyby01')")
    check_that("the variance never outruns the interval",
               all(float(re.search(r"EmitVariance=([\d.]+)", v).group(1))
                   < float(re.search(r"EmitInterval=([\d.]+)", v).group(1))
                   for a in non_loop for k, v in a.properties
                   if k.startswith("SoundEmitters")))

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
