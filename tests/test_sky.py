#!/usr/bin/env python3
"""Regression tests for sky conversion.

    python3 tests/test_sky.py [path/to/DM-HeatRay.ut3]

The default mode keeps UT3's model: the dome is ordinary level geometry, not a
UT2004 skybox. What has to hold is that the dome still encloses everything even
though it gets scaled down to fit UE2's world.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.skybox import (FAR_CLIPPING_PLANE, HALF_WORLD_MAX, VIEW_SAFETY,
                            WORLD_SAFETY, fit_inline_dome, looks_like_sky)

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


def encloses(bounds, centre, radius):
    lo, hi = bounds
    return all(lo[i] <= centre[i] - radius and centre[i] + radius <= hi[i]
               for i in range(3))


def main():
    print("dome detection")
    check_that("UT3's dome is recognised", looks_like_sky("S_UN_Sky_SM_Dome01"))
    check_that("ordinary meshes are not", not looks_like_sky("S_LT_Floor_SM_Panel"))

    print("what the sky room does not inherit")
    # A backdrop copy is seen from the skybox camera, not from where the mesh
    # stood in the level, so UT3's draw distance means nothing there -- carried
    # over it would hide the geometry outright. Everything else survives the
    # move, DrawScale being rescaled with the position.
    from convert.skybox_move import move_to_skybox
    from ut2.t3d import Actor

    moved = move_to_skybox(
        [Actor("StaticMeshActor", "M", [
            ("StaticMesh", "StaticMesh'P.S_Dome'"),
            ("Location", "(X=100.000000,Y=0.000000,Z=0.000000)"),
            ("DrawScale", "2.000000"),
            ("CullDistance", "14000.000000"),
        ])],
        map_center=(0.0, 0.0, 0.0), sky_center=(0.0, 0.0, 100000.0), scale=0.25)
    keys = [k for k, _v in moved[0].properties]
    check_that("a backdrop copy drops its cull distance", "CullDistance" not in keys,
               str(keys))
    check("and keeps its mesh", dict(moved[0].properties)["StaticMesh"],
          "StaticMesh'P.S_Dome'")
    check("with DrawScale rescaled by the move",
          dict(moved[0].properties)["DrawScale"], "0.500000")
    check_that("and is marked unlit", dict(moved[0].properties).get("bUnlit") == "True")

    print("inline fit keeps UT3's authored size")
    # The dome is UT3's, not a shrink-wrap of the level: a sky scaled to just
    # clear the geometry sits far too close to read as a horizon.
    bounds = ((-1000.0, -1000.0, -500.0), (1000.0, 1000.0, 500.0))
    scale, radius, where, fitted, clamped = fit_inline_dome(
        bounds, [500.0, 0.0, 0.0], 100.0, [], margin=1.25, native_scale=300.0)
    check("UT3's DrawScale is kept", scale, 300.0)
    check("radius", radius, 30000.0)
    check("nothing limited it", clamped, "")
    check("dome stays where UT3 put it", list(where), [500.0, 0.0, 0.0])
    check_that("dome encloses the level", encloses(fitted, where, radius))

    print("the geometry margin is a floor, not a target")
    # A dome too small to cover the level is grown; it is never shrunk to fit.
    scale, radius, _w, _f, _c = fit_inline_dome(bounds, [0.0, 0.0, 0.0], 100.0, [],
                                                margin=1.25, native_scale=2.0)
    check("grown to clear the geometry", radius, 1250.0)

    print("a dome that fits keeps UT3's offset, scaled with it")
    # A dome at offset d with radius R subtends exactly the same angles from the
    # play area as one at offset s*d with radius s*R, so scaling the two
    # together is what preserves the view when only the world limit bites.
    # The far plane is the tighter limit for any map near the world origin, so
    # this needs a level parked out near the edge for HALF_WORLD_MAX to bind.
    edge = ((229000.0, -1000.0, -1000.0), (231000.0, 1000.0, 1000.0))
    off = [232000.0, 0.0, 0.0]
    _s, radius, where, _f, why = fit_inline_dome(
        edge, off, 1078.4, [], margin=1.25, native_scale=30.0, world_margin=1000.0)
    check("limited by the world, not the far plane", why, "UE2's 262144uu world")
    shrink = radius / (1078.4 * 30.0)
    centre = [(edge[0][i] + edge[1][i]) / 2.0 for i in range(3)]
    check_that("the offset shrank by the same factor",
               abs((where[0] - centre[0]) - (off[0] - centre[0]) * shrink) < 1e-6,
               "shrink %.3f, dome now at X=%.0f" % (shrink, where[0]))

    print("inline fit, dome parked off to one side (DM-HeatRay's case)")
    # UT3 puts DM-HeatRay's dome 50,728uu from the map centre and covers the
    # level with sheer size. Here the far plane binds, so the offset is spent on
    # radius instead: a centred dome is the largest one that can be drawn at all.
    dome = [-454.0, 52736.0, -23090.0]
    bounds = ((-5000.0, -4000.0, -6000.0), (7000.0, 8000.0, 3000.0))
    far = [[-39604.0, 12000.0, 4000.0], [30000.0, -20000.0, 1000.0]]
    scale, radius, where, fitted, _clamped = fit_inline_dome(
        bounds, dome, 1078.4, far, margin=1.25, native_scale=300.0, world_margin=1500.0)
    centre = [(bounds[0][i] + bounds[1][i]) / 2.0 for i in range(3)]
    check_that("dome is centred on the play area", 
               all(abs(where[i] - centre[i]) < 1e-6 for i in range(3)),
               "%s" % ([round(v) for v in where],))
    check_that("dome encloses every backdrop actor",
               all(max(abs(loc[i] - where[i]) for i in range(3)) < radius
                   for loc in far))
    check_that("dome encloses the level bounds",
               all(max(abs(c[i] - where[i]) for i in range(3)) < radius
                   for c in bounds))
    check_that("world brush encloses the dome", encloses(fitted, where, radius),
               "%s" % ([round(v) for v in fitted[1]],))
    reach = max(max(abs(v) for v in fitted[0]), max(abs(v) for v in fitted[1]))
    check_that("everything stays inside UE2's world", reach < HALF_WORLD_MAX,
               "%.0f < %.0f" % (reach, HALF_WORLD_MAX))
    # UT3's dome is ~4.6x the extent it covers; the far plane allows ~1.3x here.
    # That gap is the whole reason --sky-mode skybox exists, and it cannot be
    # closed by scaling: the sky is simply not allowed to be that far away.
    span = max(max(abs(loc[i] - where[i]) for i in range(3)) for loc in far)
    check_that("dome still covers the backdrop, if not by UT3's margin",
               radius / span > 1.0, "%.1f : 1 (UT3 manages 4.6 : 1)" % (radius / span))

    print("inline fit is capped by the far plane")
    # UT3's own DrawScale 300 is a 323,520uu radius. HALF_WORLD_MAX rules that
    # out on its own, but the far plane rules it out five times over: the whole
    # dome has to sit within 65536uu of anywhere a player can stand, or the
    # projection matrix (Engine/Src/UnRender.cpp:1510) depth-clips it.
    offset = [0.0, 40000.0, 0.0]
    scale, radius, _where, fitted, clamped = fit_inline_dome(
        ((-5000.0,) * 3, (5000.0,) * 3), offset, 1078.4, [], margin=1.25,
        native_scale=300.0, world_margin=1500.0)
    check("the binding limit is named", clamped, "the 65536uu far plane")
    check_that("radius is under UT3's", radius < 1078.4 * 300.0, "%.0f" % radius)
    reach = max(max(abs(v) for v in fitted[0]), max(abs(v) for v in fitted[1])) + 1500.0
    check_that("world brush corner stays inside HALF_WORLD_MAX",
               reach <= HALF_WORLD_MAX * WORLD_SAFETY + 1e-6,
               "%.0f <= %.0f" % (reach, HALF_WORLD_MAX * WORLD_SAFETY))
    check_that("the offset is spent on radius instead when the far plane binds",
               max(abs(_where[i] - 0.0) for i in range(3)) < 1e-6,
               "dome centred on the play area")

    print("the whole dome stays inside the far plane")
    # This is the check that matters in the editor: a dome that pokes past the
    # far plane is not merely clipped at the edges, it visibly stops partway up.
    play = ((-4096.0, -3072.0, -3200.0), (6656.0, 7344.0, 378.0))
    _s, radius, where, _f, why = fit_inline_dome(
        play, [-454.0, 52736.0, -23090.0], 1078.4, [[-39604.0, 0.0, 0.0]],
        margin=1.25, native_scale=300.0, world_margin=1024.0)
    worst = 0.0
    for corner in [(x, y, z) for x in play[0][:1] + play[1][:1]
                   for y in (play[0][1], play[1][1]) for z in (play[0][2], play[1][2])]:
        worst = max(worst, sum((corner[i] - where[i]) ** 2 for i in range(3)) ** 0.5)
    check_that("furthest dome surface is inside the far plane",
               worst + radius <= FAR_CLIPPING_PLANE,
               "%.0f + %.0f = %.0f <= %.0f" % (worst, radius, worst + radius,
                                               FAR_CLIPPING_PLANE))
    check_that("and it uses most of what is available",
               worst + radius > FAR_CLIPPING_PLANE * VIEW_SAFETY * 0.9,
               "%.0f" % (worst + radius))
    check("limited by the far plane", why, "the 65536uu far plane")

    print("skybox mode still builds a room")
    from convert.skybox import make_skybox

    actors = make_skybox(((-1000.0,) * 3, (1000.0,) * 3), "Pkg", "Dome", 1078.4)
    check("room, zone and dome", [a.cls for a in actors],
          ["Brush", "SkyZoneInfo", "StaticMeshActor"])

    print("the far clipping plane")
    # Fitting inside UE2's world is not enough to be drawn. The far plane of the
    # projection matrix is a hard 65536 (Core.cpp:197, used at UnRender.cpp:1510)
    # and is never reassigned; the zone's DistanceFogEnd only moves the frustum
    # *culling* plane (UnRender.cpp:1066). WAR-Serenity parks 33 meshes up to
    # 139,245uu from a player start, and the sky showed through them.
    from convert.skybox_move import furthest_from, is_outside

    box = ((-1000.0, -1000.0, -1000.0), (1000.0, 1000.0, 1000.0))
    # Worst case, not nearest: the far corner of the play area is where a player
    # can stand and still have to see the thing.
    check("distance is taken from the furthest corner a player can reach",
          round(furthest_from(box, [1000.0, 0.0, 0.0])), round((2000.0 ** 2 + 1000.0 ** 2 + 1000.0 ** 2) ** 0.5))
    check_that("a mesh inside the play area is still measured to the far corner",
               furthest_from(box, [0.0, 0.0, 0.0]) > 1000.0)
    check_that("and a distant backdrop exceeds the far plane",
               furthest_from(box, [100000.0, 0.0, 0.0]) > FAR_CLIPPING_PLANE)
    check("the plane is the engine's constant, not a tunable", FAR_CLIPPING_PLANE, 65536.0)
    # The trigger must look at the backdrop alone. Measured over every mesh, a
    # large level exceeds the plane on its own diagonal: WAR-PowerSurge reads
    # 67,149uu that way while having no backdrop actors at all.
    check_that("an actor inside the world brush is not backdrop",
               not is_outside([500.0, 500.0, 500.0], box))
    check_that("one outside it is",
               is_outside([100000.0, 0.0, 0.0], box))

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
