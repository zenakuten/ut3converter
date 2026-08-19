#!/usr/bin/env python3
"""Regression tests for ReverbVolume conversion.

    python3 tests/test_reverb.py [path/to/WAR-PowerSurge.ut3]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.geometry import VOLUME_CLASSES, VOLUME_PROPERTIES, convert_brushes
from convert.reverb import (EFFECT_CLASS, PRESETS, effect_object, preset_name,
                            settings_of, wet_room)
from ut3.package import Package

DEFAULT_MAP = (
    "/home/josh/.local/share/Steam/steamapps/common/Unreal Tournament 3/"
    "UTGame/CookedPC/Private/Maps/WAR-PowerSurge.ut3"
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
    print("preset table")
    # Straight from I3DL2_ENVIRONMENT_PRESET_* in DirectX9/Include/dsound.h, in
    # DSFXI3DL2Reverb's declared order (dsound.h:1628). Spot-checked against the
    # header so a transcription slip cannot pass silently.
    check("HALLWAY is the header's row", PRESETS["HALLWAY"],
          (-1000, -300, 0.0, 1.49, 0.59, -1219, 0.007, 441, 0.011, 100.0, 100.0, 5000.0))
    check("so is CAVE", PRESETS["CAVE"],
          (-1000, 0, 0.0, 2.91, 1.30, -602, 0.015, -302, 0.022, 100.0, 100.0, 5000.0))
    check_that("every preset carries all twelve parameters",
               all(len(v) == 12 for v in PRESETS.values()))

    # UE3's ReverbPreset enum is the I3DL2 preset list under another name, so
    # every value it can hold has to land on a row here -- including the six
    # (SmallRoom..Plate) that EAX's own 26 "original environments" leave out.
    ue3_presets = [
        "REVERB_Default", "REVERB_Bathroom", "REVERB_StoneRoom", "REVERB_Auditorium",
        "REVERB_ConcertHall", "REVERB_Cave", "REVERB_Hallway", "REVERB_StoneCorridor",
        "REVERB_Alley", "REVERB_Forest", "REVERB_City", "REVERB_Mountains",
        "REVERB_Quarry", "REVERB_Plain", "REVERB_ParkingLot", "REVERB_SewerPipe",
        "REVERB_Underwater", "REVERB_SmallRoom", "REVERB_MediumRoom",
        "REVERB_LargeRoom", "REVERB_MediumHall", "REVERB_LargeHall", "REVERB_Plate",
    ]
    missing = [n for n in ue3_presets if preset_name(n) not in PRESETS]
    check("every UT3 ReverbPreset has an I3DL2 row", missing, [])
    check("the enum prefix is stripped", preset_name("REVERB_StoneCorridor"),
          "STONECORRIDOR")

    print("wet level")
    # I3DL2 has no master gain, but lRoom is the room effect level in millibels,
    # so UE3's separate 0..1 wet level composes onto it.
    check("full wet leaves the preset alone", wet_room(-1000, 1.0), -1000)
    check("and so does an unstated one", wet_room(-1000, None), -1000)
    check("a quarter wet is 2000*log10(0.25) quieter", wet_room(-1000, 0.25), -2204)
    check("silence floors at the field's minimum", wet_room(-1000, 0.0), -10000)
    check_that("nothing can exceed 0mB", wet_room(0, 1.0) <= 0)

    print("volume wiring")
    check("a ReverbVolume becomes a PhysicsVolume, which is where VolumeEffect lives",
          VOLUME_CLASSES["ReverbVolume"], "PhysicsVolume")
    # It is a PhysicsVolume only to carry sound, so it must lose the overlap
    # election to any volume that means its physics -- all of which sit at 0.
    check("and loses priority to real physics volumes",
          dict(VOLUME_PROPERTIES["ReverbVolume"])["Priority"], "-1")
    check_that("the effect uses the one concrete I3DL2Listener that ships",
               EFFECT_CLASS == "EFFECT_WaterVolume")

    print("against the map")
    if not os.path.exists(path):
        print("  (map not found, skipping: %s)" % path)
    else:
        from ut3.objects.level import ordered_exports
        from ut3.props import read_object_properties

        p = Package(path)
        seen = {}
        for export in ordered_exports(p, {"ReverbVolume"}):
            props, start, _end = read_object_properties(p, export)
            if start is None:
                continue
            key, volume = settings_of(props)
            seen[key] = seen.get(key, 0) + 1
            effect = effect_object("V", props)
            if key is not None:
                check_that("%s resolves to an effect" % export.name, effect is not None)
        check_that("the map's presets are all known",
                   all(k is None or k in PRESETS for k in seen), str(sorted(seen)))
        check_that("more than one preset is in use", len(seen) > 1, str(seen))

        brushes, stats = convert_brushes(p, texture_package="T", include_volumes=True)
        reverb = [b for b in brushes if b.name.startswith("ReverbVolume")]
        check_that("every reverb volume carries exactly one inline object",
                   reverb and all(len(b.objects) == 1 for b in reverb),
                   "%d volumes" % len(reverb))
        check_that("and points VolumeEffect at it",
                   all(b.ref_property == "VolumeEffect" for b in reverb))
        text = "\n".join(reverb[0].lines())
        check_that("the t3d writes it as a Begin Object block",
                   "Begin Object Class=EFFECT_WaterVolume" in text
                   and "VolumeEffect=EFFECT_WaterVolume'MyLevel." in text, text[:80])
        check_that("the object is level-owned, as UT2004's own exports are",
                   "'MyLevel." in text)

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
