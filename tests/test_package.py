#!/usr/bin/env python3
"""Regression tests for the UE3 package reader, against a stock UT3 map.

    python3 tests/test_package.py [path/to/DM-HeatRay.ut3]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ut3.package import Package
from ut3.props import Array, Struct, read_object_properties

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


def main(path):
    p = Package(path)

    print("header")
    check("version", p.version, 512)
    check("cooker", p.cooker_version, 57)
    check("names", p.name_count, 4785)
    check("imports", p.import_count, 1168)
    check("exports", p.export_count, 28400)
    check("chunks", len(p.chunks), 70)

    print("tables")
    # The export table must land exactly on the depends table.
    hist = p.class_histogram()
    check("Polys", hist.get("Polys"), 611)
    check("Model", hist.get("Model"), 611)
    check("Brush", hist.get("Brush"), 313)
    check("StaticMeshActor", hist.get("StaticMeshActor"), 2403)
    check("Texture2D", hist.get("Texture2D"), 507)
    check("PointLight", hist.get("PointLight"), 381)
    check("SpotLight", hist.get("SpotLight"), 79)
    check("PlayerStart", hist.get("PlayerStart"), 17)

    print("properties: PlayerStart_0")
    e = p.find("PlayerStart_0")[0]
    props, start, end = read_object_properties(p, e)
    check("property list consumes the export", end, e.size)
    loc = props.get("Location")
    check_that("Location is a Vector", isinstance(loc, Struct) and loc.type == "Vector", str(loc))
    check("Location.X", round(loc.value[0], 3), 1259.473)
    check("Rotation.Yaw", props.get("Rotation").value[1], -18848)
    check("bPreferredVehiclePath", props.get("bPreferredVehiclePath"), True)
    check("bPrimaryStart", props.get("bPrimaryStart"), False)
    check("DrawScale", props.get("DrawScale"), 2.0)
    paths = props.get("PathList")
    check_that("PathList is an Array", isinstance(paths, Array) and len(paths) == 7, repr(paths))

    print("properties: PointLight_0")
    e = p.find("PointLight_0")[0]
    props, start, end = read_object_properties(p, e)
    check("property list consumes the export", end, e.size)
    check("Location", tuple(props.get("Location").value), (-1125.0, 1402.0, -862.0))
    check("LightComponent", props.get("LightComponent").name, "PointLightComponent_439")
    # ComponentMap values are 0-based export indices; +1 makes them PackageIndex.
    comp = p.ref(e.components["PointLightComponent0"])
    check("ComponentMap resolves", comp.name, "PointLightComponent_439")

    print("properties: whole package")
    parsed = 0
    for e in p.exports:
        if e.size == 0:
            continue
        _props, start, _end = read_object_properties(p, e)
        if start is not None:
            parsed += 1
    check_that(
        "at least 98%% of exports parse", parsed >= p.export_count * 0.98,
        "%d/%d" % (parsed, p.export_count),
    )

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
