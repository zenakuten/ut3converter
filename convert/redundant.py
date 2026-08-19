"""Drop additive brushes that contribute nothing to the CSG result.

UT3 authors geometry as a union of solid blocks, so it costs nothing there to
leave a block sitting entirely inside another, or to duplicate one outright.
DM-HeatRay does both -- `Brush_470` is a geometrically identical copy of
`Brush_125`.

UE2 does not take it so calmly. A face that lands exactly on another brush's
face is cospatial, and `AddBrushToWorldFunc` (Editor/Src/UnBsp.cpp:1085) keeps
the poly for `F_COSPATIAL_FACING_OUT` but drops it for `F_COSPATIAL_FACING_IN`,
so a redundant brush contributes no nodes while still taking part in every
subsequent split. Measured against a built map, both halves of that duplicate
pair came out only partly solid.

Dropping them is safe rather than a guess: if B lies wholly inside A then
A union B is A, so B changes nothing -- unless a subtract lands between the two
in CSG order, in which case B is refilling what the subtract carved and has to
stay.

Volume is not the whole story though. Two brushes can occupy the same space and
carry different *surfaces*: DM-HeatRay's `Brush_470` is geometrically identical
to `Brush_125`, but 125's faces are all `EngineMaterials.DefaultMaterial` while
470 is the one holding the real floor texture. Dropping 470 leaves the floor
grey. So a brush is only redundant when it brings no material the keeper lacks.
"""

import random

# Points are tested against a brush's own planes with this slack, so a brush
# resting exactly on another's face still counts as inside it.
SURFACE_SLACK = 0.05

# How many interior samples decide containment. These are convex solids, so a
# handful is plenty; the bounding-box test has already done the coarse work.
SAMPLES = 40
MIN_SAMPLES = 10


def _planes_of(brush, origin):
    out = []
    for poly in brush.polygons:
        verts = [tuple(v[i] + origin[i] for i in range(3)) for v in poly.vertices]
        if len(verts) < 3:
            continue
        nx = ny = nz = 0.0
        for i in range(len(verts)):
            a, b = verts[i], verts[(i + 1) % len(verts)]
            nx += (a[1] - b[1]) * (a[2] + b[2])
            ny += (a[2] - b[2]) * (a[0] + b[0])
            nz += (a[0] - b[0]) * (a[1] + b[1])
        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        if length < 1e-12:
            continue
        n = (nx / length, ny / length, nz / length)
        out.append((n, sum(n[i] * verts[0][i] for i in range(3))))
    return out


def _origin_of(brush):
    for key, value in brush.properties:
        if key == "Location":
            nums = value.strip("()").replace("X=", "").replace("Y=", "").replace("Z=", "")
            try:
                return [float(c) for c in nums.split(",")]
            except ValueError:
                return [0.0, 0.0, 0.0]
    return [0.0, 0.0, 0.0]


def _inside(point, planes):
    return all(sum(n[i] * point[i] for i in range(3)) - d <= SURFACE_SLACK
               for n, d in planes)


def _materials(brush):
    """The set of textures a brush puts on its faces."""
    return {poly.texture for poly in brush.polygons if poly.texture}


def find_redundant(brushes, seed=5):
    """Names of additive brushes wholly inside an earlier one, safely droppable."""
    prepared = []
    for brush in brushes:
        origin = _origin_of(brush)
        planes = _planes_of(brush, origin)
        if not planes:
            prepared.append(None)
            continue
        verts = [tuple(v[i] + origin[i] for i in range(3))
                 for poly in brush.polygons for v in poly.vertices]
        lo = [min(v[i] for v in verts) for i in range(3)]
        hi = [max(v[i] for v in verts) for i in range(3)]
        prepared.append((brush, planes, lo, hi))

    rng = random.Random(seed)
    redundant = []
    for i, inner in enumerate(prepared):
        if inner is None or inner[0].csg != "CSG_Add":
            continue
        for j, outer in enumerate(prepared[:i]):
            if outer is None or outer[0].csg != "CSG_Add":
                continue
            if any(inner[2][k] < outer[2][k] - SURFACE_SLACK
                   or inner[3][k] > outer[3][k] + SURFACE_SLACK for k in range(3)):
                continue
            points = []
            for _ in range(300):
                p = tuple(rng.uniform(inner[2][k], inner[3][k]) for k in range(3))
                if _inside(p, inner[1]):
                    points.append(p)
                if len(points) >= SAMPLES:
                    break
            if len(points) < MIN_SAMPLES:
                continue
            if not all(_inside(p, outer[1]) for p in points):
                continue
            # A subtract landing between the two means the inner brush is
            # refilling what was carved, so it is doing real work.
            carved = False
            for between in prepared[j + 1:i]:
                if between is None or between[0].csg != "CSG_Subtract":
                    continue
                if all(inner[2][k] < between[3][k] and inner[3][k] > between[2][k]
                       for k in range(3)):
                    carved = True
                    break
            if carved:
                continue
            # The inner brush may be the one carrying the real surfaces.
            if _materials(inner[0]) - _materials(outer[0]):
                continue
            redundant.append((inner[0].name, outer[0].name))
            break
    return redundant
