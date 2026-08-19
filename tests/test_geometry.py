#!/usr/bin/env python3
"""Regression tests for BSP brush conversion (Phase 1b).

    python3 tests/test_geometry.py [path/to/DM-HeatRay.ut3]
"""

import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.geometry import (MAX_KARMA_PLANES, MAX_POLY_VERTICES, convert_brushes,
                              make_builder_brush, make_world_brush, split_polygon)
from ut2.t3d import SHARED_POLY_FLAGS, Polygon, T3DMap
from ut3.objects.model import find_polys, read_bounds, read_polys
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


def sub(a, b):
    return tuple(a[i] - b[i] for i in range(3))


def dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def is_closed_solid_polys(brush):
    """Every edge of a closed hull is shared by exactly two faces."""
    edges = {}
    for poly in brush.polygons:
        verts = poly.vertices
        if len(verts) < 3:
            return False
        for i in range(len(verts)):
            a = tuple(round(c, 2) for c in verts[i])
            b = tuple(round(c, 2) for c in verts[(i + 1) % len(verts)])
            edges[(a, b)] = edges.get((a, b), 0) + 1
    if len(brush.polygons) < 4:
        return False
    return all(c == 1 and edges.get((b, a), 0) == 1 for (a, b), c in edges.items())


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

    print("source geometry")
    brush_actors = [e for e in p.exports if p.class_name_of(e) == "Brush"]
    check("brush actors", len(brush_actors), 313)

    total = planar = wound = unit = 0
    worst_plane = 0.0
    for e in brush_actors:
        props, _s, _e = read_object_properties(p, e)
        model = props.get("Brush")
        polys_export = find_polys(p, model.export)
        for q in read_polys(p, polys_export):
            total += 1
            v0 = q.vertices[0]
            dev = max(abs(dot(q.normal, sub(v, v0))) for v in q.vertices)
            worst_plane = max(worst_plane, dev)
            if dev < 0.01:
                planar += 1
            n = cross(sub(q.vertices[1], v0), sub(q.vertices[2], v0))
            length = math.sqrt(dot(n, n))
            if length > 1e-6 and dot(n, q.normal) / length > 0.99:
                wound += 1
            if abs(math.sqrt(dot(q.normal, q.normal)) - 1.0) < 1e-3:
                unit += 1
    check("polygons read", total, 1918)
    check("polygons whose vertices are planar", planar, total)
    check_that("worst vertex plane deviation < 0.01uu", worst_plane < 0.01, "%.6f" % worst_plane)
    check("polygons with unit normals", unit, total)
    check("polygons wound to match their normal", wound, total)

    print("conversion")
    brushes, stats = convert_brushes(p, texture_package="TestTex")
    # 307, not 312: five of DM-HeatRay's brushes do not enclose a volume -- four
    # are a single flat face and one is an open 18-face hull -- and UE2 turns an
    # open brush added with CSG_Add into a solid half-space. In game that is an
    # invisible plane that kills whatever crosses it, since the pawn lands in
    # zone 0 and the engine calls FellOutOfWorld (Engine/Src/UnPhysic.cpp:336).
    # 304: three more are wholly inside another additive brush with no subtract
    # in between, so they add nothing but cospatial faces for UE2's CSG to trip
    # over (Brush_470 is an exact duplicate of Brush_125).
    # 306 rather than 305: one more brush survives now that a T-junction no
    # longer reads as an open hull -- see the closedness note below.
    check("brushes emitted", stats.brushes, 306)
    # 2, not 3: Brush_470 is geometrically identical to Brush_125 but is the one
    # carrying the real floor texture, so dropping it left the floor grey. A
    # brush only counts as redundant when it brings no material the keeper lacks.
    check("redundant brushes dropped", len(stats.redundant), 2)
    check_that("a duplicate holding the only real texture is kept",
               "Brush_470" not in stats.redundant, str(stats.redundant))
    check("polygons emitted", stats.polygons, 1896)
    check("open brushes rejected", len(stats.skipped_open), 4)
    # Checked by the area residue rather than by matching edges, because the
    # edge test is what the conversion itself uses and would only be restating
    # it -- and because edges are the half of this that gets it wrong. A face
    # spanning what two neighbours split leaves edges unmatched on a perfectly
    # closed brush: CTF-Coret's Brush_143 and Brush_144 are CSG_Subtract shapes
    # with six such edges each, and dropping them left the recess they carve as
    # solid rock wearing the placeholder texture.
    from ut3.objects.model import surface_closes

    def residue_closed(brush):
        return surface_closes(brush.polygons)

    check_that("every emitted brush encloses a volume",
               all(residue_closed(b) for b in brushes),
               "%d brushes" % len(brushes))
    # And the rejected ones really are open, by the same measure.
    check_that("and the rejected ones do not", stats.skipped_open,
               "%d rejected" % len(stats.skipped_open))
    check("subtractive brushes", stats.subtractive, 145)
    check("the builder brush is skipped", stats.builder_brushes, 1)
    # CSG is order-dependent; the level's actor order is authoritative, not the
    # name-sorted export table.
    check("brushes are emitted in level order, not export order",
          [b.name for b in brushes[:3]], ["Brush_259", "Brush_440", "Brush_441"])

    # Base is the texture origin only -- UE2 planes come from Vertex[0] -- so it
    # must be passed through exactly as UT3 has it, off-plane or not.
    src = {}
    for e in [x for x in p.exports if p.class_name_of(x) == "Brush"]:
        pr, s0, _e = read_object_properties(p, e)
        if s0 is None or pr.get("Brush") is None or not pr.get("Brush").is_export:
            continue
        pe = find_polys(p, pr.get("Brush").export)
        if pe is None:
            continue
        piv = pr.get("PrePivot").value if pr.get("PrePivot") else (0.0, 0.0, 0.0)
        src[e.name] = [tuple(round(q.base[i] - piv[i], 3) for i in range(3))
                       for q in read_polys(p, pe)]
    off = 0
    for brush in brushes:
        want = src.get(brush.name)
        if not want:
            continue
        got = [tuple(round(c, 3) for c in q.origin) for q in brush.polygons]
        if got != want:
            off += 1
    check("every Origin matches the UT3 source", off, 0)

    leaked = [q.flags for b in brushes for q in b.polygons if q.flags & ~SHARED_POLY_FLAGS]
    check("no UE3-only poly flag bits leak through", leaked, [])
    # UE3 hides a BSP face by applying EngineMaterials.RemoveSurfaceMaterial;
    # UE2 needs that as PF_Invisible or the face renders as a blank wall.
    # PF_Invisible must never be emitted: UE2 turns it into NF_NotVisBlocking
    # and zone assignment then treats the open space beyond the face as inside,
    # i.e. solid, which kills anything that enters (Editor/Src/UnVisi.cpp:1170).
    # Measured on DM-HeatRay: 10.51% of the play volume wrongly solid with the
    # flag, 0.00% without.
    from ut2.t3d import PF_INVISIBLE
    hidden = [q for b in brushes for q in b.polygons if q.flags & PF_INVISIBLE]
    check("PF_Invisible is never emitted on a CSG brush", len(hidden), 0)
    check("faces UT3 hid are still counted", stats.invisible_polys, 393)

    # PrePivot is baked into the vertices, so no brush may emit one: 282 of 313
    # brushes share a single Location and were placed entirely by PrePivot.
    check_that("no brush relies on PrePivot",
               all(not any(k == "PrePivot" for k, _v in b.properties) for b in brushes))
    # PAN is applied to Base and then overwritten by ORIGIN (UnEdFact.cpp:1606),
    # so emitting it is dead weight -- and after ORIGIN it would push Base back
    # off its plane and miscut CSG.
    check_that("no polygon emits a Pan line",
               all(not q.pan_u and not q.pan_v for b in brushes for q in b.polygons))

    print("world brush (subtractive UT2004 vs additive UT3)")
    world = make_world_brush(stats.world_bounds, margin=1024.0)
    check("world brush is subtractive", world.csg, "CSG_Subtract")
    check("world brush is a box", len(world.polygons), 6)
    wound_ok = 0
    for q in world.polygons:
        v0 = q.vertices[0]
        n = cross(sub(q.vertices[1], v0), sub(q.vertices[2], v0))
        length = math.sqrt(dot(n, n))
        if length > 1e-6 and dot(n, q.normal) / length > 0.99:
            wound_ok += 1
    check("world brush faces wound outward", wound_ok, 6)
    lo, hi = stats.world_bounds
    center = [(lo[i] + hi[i]) / 2 for i in range(3)]
    corners = [v for q in world.polygons for v in q.vertices]
    encloses = all(
        min(c[i] for c in corners) + center[i] <= lo[i]
        and max(c[i] for c in corners) + center[i] >= hi[i]
        for i in range(3)
    )
    check_that("world brush encloses all geometry", encloses)

    # Surfaces using a half-size texture must carry half-size UV vectors.
    from convert.textures import TextureSet as _TS
    print("skybox")
    from convert.skybox import looks_like_sky, make_skybox
    from ut2.t3d import PF_FAKE_BACKDROP
    check_that("sky meshes are recognised by name",
               looks_like_sky("S_UN_Sky_SM_Dome01") and not looks_like_sky("S_HU_Walls_SM_Brick"))
    sky = make_skybox(stats.world_bounds, "TestTex", "S_UN_Sky_SM_Dome01", 1078.4)
    check("skybox emits room + zone + dome", [a.cls for a in sky],
          ["Brush", "SkyZoneInfo", "StaticMeshActor"])
    check("sky room is subtractive", sky[0].csg, "CSG_Subtract")
    # All three sit at the same point so the dome surrounds the sky viewpoint.
    locs = {v for a in sky for k, v in a.properties if k == "Location"}
    check_that("room, zone and dome share an origin", len(locs) == 1, str(locs))
    # With a skybox the world brush shows sky instead of a flat grey texture.
    lit = make_world_brush(stats.world_bounds, margin=1024.0, fake_backdrop=True)
    check_that("world brush faces are FakeBackdrop",
               all(q.flags & PF_FAKE_BACKDROP for q in lit.polygons))
    plain = make_world_brush(stats.world_bounds, margin=1024.0)
    check_that("and are not, without a skybox",
               not any(q.flags & PF_FAKE_BACKDROP for q in plain.polygons))

    print("builder brush slot")
    # UT2004 eats the first Class=Brush actor as the builder brush
    # (UnEdFact.cpp:647), so a throwaway must come before any real geometry.
    builder = make_builder_brush()
    check("builder brush is CSG_Active", builder.csg, "CSG_Active")
    check("builder brush is named Brush", builder.name, "Brush")
    check("builder brush is a cube", len(builder.polygons), 6)

    print("UT3's own builder brush")
    # It must never be converted: built as solid it is a slab of BSP in mid-air
    # wearing the placeholder texture, and there is no brush to select for it in
    # the editor because UT2004's own builder has taken that slot.
    from ut3.objects.level import is_builder_brush

    templates = [e for e in p.exports if p.class_name_of(e) == "Brush"
                 and is_builder_brush(p, e)]
    check("this map has exactly one", len(templates), 1)
    # An absent CsgOper is what identifies it: ABrush defaults the property to
    # CSG_Active, the builder's own value, so a real brush always serializes an
    # explicit CSG_Add or CSG_Subtract and the builder serializes nothing.
    silent = []
    for export in p.exports:
        if p.class_name_of(export) != "Brush":
            continue
        props, start, _end = read_object_properties(p, export)
        if start is not None and props.get("CsgOper") is None:
            silent.append(export.name)
    check("and it is the only brush without a CsgOper",
          silent, [e.name for e in templates])

    # DM-Deimos is why the model name alone will not do: its builder brush is
    # named Model_4 like any other, and it converted into a checkerboard wall
    # standing beside PathNode_76.
    deimos_path = _find_map(path, "DM-Deimos")
    if deimos_path is None:
        print("  skip   DM-Deimos not found")
    else:
        deimos = Package(deimos_path)
        stray = [e for e in deimos.exports if e.name == "Brush_0"][0]
        props, _s, _e = read_object_properties(deimos, stray)
        check("DM-Deimos Brush_0 has no CsgOper", props.get("CsgOper"), None)
        check_that("its model is named like a real brush's",
                   str(props.get("Brush").name).startswith("Model_"))
        check_that("but it is recognised as the builder anyway",
                   is_builder_brush(deimos, stray))
        converted, deimos_stats = convert_brushes(deimos, "DMDeimosTex")
        check("and it is not converted", deimos_stats.builder_brushes, 1)
        check_that("so no brush actor carries its name",
                   not any(a.name == "Brush_0" for a in converted))

    print("t3d output")
    t3d = T3DMap()
    t3d.add(builder)
    t3d.add(world)
    for brush in brushes:
        t3d.add(brush)
    text = t3d.text()
    check("Begin/End Actor balance", text.count("Begin Actor"), text.count("End Actor"))
    check("Begin/End Polygon balance", text.count("Begin Polygon"), text.count("End Polygon"))
    check("Begin/End Brush balance", text.count("Begin Brush"), text.count("End Brush"))
    check("Begin/End PolyList balance", text.count("Begin PolyList"), text.count("End PolyList"))
    check_that("no NaN or Inf in output", not re.search(r"(?i)\b(nan|inf)\b", text))
    names = re.findall(r"Begin Actor Class=\S+ Name=(\S+)", text)
    check("actor names are unique", len(names), len(set(names)))
    check_that("map is wrapped in Begin/End Map",
               text.startswith("Begin Map") and text.rstrip().endswith("End Map"))
    # Karma's hull decomposition clips a quad against every plane on the BSP
    # path into an FPoly whose Vertex array is a flat 16 with no bounds check,
    # so a BlockingVolume brush past MAX_KARMA_PLANES crashes the game on load
    # (see the bBlockKarma note in convert/geometry.py). Every such volume must
    # have Karma switched off.
    over_budget = []
    for cls, _name, body in re.findall(
            r"\n   Begin Actor Class=(\w+) Name=(\w+)(.*?)\n   End Actor", text, re.S):
        if cls != "BlockingVolume":
            continue
        if body.count("Begin Polygon") > MAX_KARMA_PLANES \
                and "bBlockKarma=False" not in body:
            over_budget.append(_name)
    check_that("no BlockingVolume exceeds Karma's plane budget with Karma on",
               not over_budget, "%d over budget: %s" % (len(over_budget),
                                                       ", ".join(over_budget[:4])))
    check_that("the plane budget matches FPoly::Vertex[16] minus the starting quad",
               MAX_KARMA_PLANES == 16 - 4 + 1, MAX_KARMA_PLANES)

    # The importer reads vertices into FPoly::Vertex[16] and silently drops the
    # rest (Editor/Src/UnEdFact.cpp:1624), so an n-gon over the limit arrives as
    # a wedge. WAR-PowerSurge's core pits are 64-vertex cylinder caps and those
    # caps are the floor of the core room -- truncated, the floor is a hole.
    big = [q for b in brushes for q in b.polygons if len(q.vertices) > MAX_POLY_VERTICES]
    check("no face exceeds the importer's vertex limit", len(big), 0)

    import math as _math

    def _area(vs):
        a = 0.0
        for i in range(len(vs)):
            a += vs[i][0] * vs[(i + 1) % len(vs)][1] - vs[(i + 1) % len(vs)][0] * vs[i][1]
        return abs(a) / 2

    ring = [(_math.cos(i * 2 * _math.pi / 64) * 780,
             _math.sin(i * 2 * _math.pi / 64) * 780, -168.0) for i in range(64)]
    cap = Polygon(origin=(0, 0, -168), normal=(0, 0, -1), texture_u=(1, 0, 0),
                  texture_v=(0, 1, 0), vertices=ring, texture="T.X", flags=5, link=3)
    pieces = split_polygon(cap)
    check("a 64-vertex cap splits into fan pieces", [len(q.vertices) for q in pieces],
          [16, 16, 16, 16, 8])
    check_that("every piece fits the limit",
               all(len(q.vertices) <= MAX_POLY_VERTICES for q in pieces))
    # The pieces must tile the original exactly, or the floor gains a seam.
    check_that("and they tile the original with no gap",
               abs(_area(ring) - sum(_area(q.vertices) for q in pieces)) < 1e-6,
               "%.9f" % abs(_area(ring) - sum(_area(q.vertices) for q in pieces)))
    # CSG and texturing must see exactly what UE3 described.
    check_that("carrying the plane and texturing across unchanged",
               all(q.normal == cap.normal and q.origin == cap.origin
                   and q.texture_u == cap.texture_u and q.texture == cap.texture
                   and q.flags == cap.flags and q.link == cap.link for q in pieces))

    first = re.search(r"Begin Actor Class=Brush Name=(\S+)", text).group(1)
    check("the first brush in the file is the throwaway builder", first, "Brush")
    second = re.findall(r"Begin Actor Class=Brush Name=(\S+)", text)[1]
    check("the world subtract brush survives as real geometry", second, "WorldSubtract")

    print("closedness with T-junctions")
    # An edge test calls a brush open the moment one face spans what two faces
    # split, which is a shape editors produce constantly. The area residue does
    # not care: for any closed surface the face area vectors sum to zero.
    from ut3.objects.model import is_closed_solid, surface_closes

    class _Poly:
        def __init__(self, vertices):
            self.vertices = vertices

    # A unit cube whose top face is split in two along y, leaving the bottom
    # face spanning what the neighbours divide -- a T-junction on four edges.
    def cube(split):
        lo, hi = 0.0, 100.0
        mid = 50.0
        faces = [
            [(lo, lo, lo), (lo, hi, lo), (hi, hi, lo), (hi, lo, lo)],   # bottom
            [(lo, lo, hi), (hi, lo, hi), (hi, hi, hi), (lo, hi, hi)],   # top
            [(lo, lo, lo), (hi, lo, lo), (hi, lo, hi), (lo, lo, hi)],
            [(hi, hi, lo), (lo, hi, lo), (lo, hi, hi), (hi, hi, hi)],
            [(lo, hi, lo), (lo, lo, lo), (lo, lo, hi), (lo, hi, hi)],
        ]
        if split:
            faces.append([(hi, lo, lo), (hi, mid, lo), (hi, mid, hi), (hi, lo, hi)])
            faces.append([(hi, mid, lo), (hi, hi, lo), (hi, hi, hi), (hi, mid, hi)])
        else:
            faces.append([(hi, lo, lo), (hi, hi, lo), (hi, hi, hi), (hi, lo, hi)])
        return [_Poly(f) for f in faces]

    check_that("a plain cube is closed", is_closed_solid(cube(False)))
    check_that("and so is the same cube with a face split in two",
               is_closed_solid(cube(True)))
    check_that("the residue test sees both as closed",
               surface_closes(cube(False)) and surface_closes(cube(True)))
    # The thing the guard exists for must still be refused: an open sheet added
    # with CSG solidifies a half-space and makes a chunk of the map lethal.
    open_box = cube(False)[:-1]
    check_that("a hull with a face missing is still open",
               not is_closed_solid(open_box) and not surface_closes(open_box))
    check_that("and so is a single sheet repeated",
               not is_closed_solid([_Poly([(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)])] * 4))

    print("scaled brushes")
    # A scaled brush is baked, not handed to UE2's MainScale, for the same
    # reason PrePivot is: the brush transform stays identity so nothing can
    # apply it twice or not at all. CTF-Coret's BlockingVolume_200 is the case
    # -- DrawScale3D 0.4279, and emitted as MainScale it reached the editor
    # with no outline, could not be selected, and blocked a doorway 2.34x
    # further out than its own shape.
    from convert.geometry import brush_scale

    class _P(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    class _V:
        def __init__(self, value):
            self.value = value

    check("an unscaled brush scales by one", brush_scale(_P()), (1.0, 1.0, 1.0))
    check("DrawScale3D is taken per axis",
          brush_scale(_P({"DrawScale3D": _V((2.0, 3.0, 4.0))})), (2.0, 3.0, 4.0))
    check("DrawScale multiplies all three",
          brush_scale(_P({"DrawScale": 2.0})), (2.0, 2.0, 2.0))
    check("and the two compose",
          brush_scale(_P({"DrawScale": 2.0, "DrawScale3D": _V((1.0, 0.5, 4.0))})),
          (2.0, 1.0, 8.0))
    check("Coret's volume is the shrink it looked like",
          [round(c, 4) for c in brush_scale(_P({"DrawScale3D": _V((0.4279053,) * 3)}))],
          [0.4279] * 3)
    # Nothing may reach the t3d as MainScale any more, or it would be applied
    # on top of the baked vertices.
    check_that("no brush emits a MainScale property",
               all("MainScale" not in dict(b.properties) for b in brushes),
               "%d brushes" % len(brushes))

    print("forced direction volumes")
    # A UT3 ForcedDirVolume steers vehicles along ArrowDirection with
    # VehiclePushMag, which UE2 cannot express; only the separate bBlockPawns
    # half is a wall, and only that half converts. WAR-PowerSurge has two of
    # each -- the blocking pair wall off ledges at the map edge, and one of the
    # others is a diagonal lane volume over ground players walk on, so
    # converting all four would wall off legal footing.
    from convert.geometry import VOLUME_CLASSES, forced_dir_blocks_pawns

    check("one that blocks players becomes a BlockingVolume",
          VOLUME_CLASSES["ForcedDirVolume"], "BlockingVolume")
    check_that("bBlockPawns is what decides it",
               forced_dir_blocks_pawns({"bBlockPawns": True}))
    check_that("a plain steering volume is left out",
               not forced_dir_blocks_pawns({"ArrowDirection": (1.0, 0.0, 0.0)}))
    check_that("and so is one that only denies exiting a vehicle",
               not forced_dir_blocks_pawns({"bDenyExit": True}))
    check_that("bBlockActors does not stand in for it -- every one sets that",
               not forced_dir_blocks_pawns({"bBlockActors": True}))

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
