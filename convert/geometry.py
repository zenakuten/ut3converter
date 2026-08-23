"""Brush (CSG) conversion: UE3 UModel/UPolys -> UT2004 t3d brushes."""

import math
import re

from ut2.t3d import Brush, PF_FAKE_BACKDROP, PF_INVISIBLE, Polygon, SHARED_POLY_FLAGS, vec, rot
from ut3.objects.level import is_builder_brush, ordered_exports
from ut3.objects.model import find_polys, is_closed_solid, read_polys
from ut3.props import read_object_properties

# Brush-derived actor classes worth converting. Volumes are opt-in: UT2004 has
# BlockingVolume, but the rest have no equivalent.
BRUSH_CLASSES = ("Brush",)
VOLUME_CLASSES = {
    "BlockingVolume": "BlockingVolume",
    "UTKillZVolume": "PhysicsVolume",
    # UT2004 has exact counterparts for two of these -- XGame.LavaVolume is
    # already a pain volume with FellLava and the right fog, and
    # Gameplay.WaterVolume already carries the splash effects and drag -- so
    # they convert to those rather than to a bare PhysicsVolume dressed up.
    "UTLavaVolume": "LavaVolume",
    "UTWaterVolume": "WaterVolume",
    "WaterVolume": "WaterVolume",
    "UTSlimeVolume": "PhysicsVolume",
    "PhysicsVolume": "PhysicsVolume",
    # Only the ones that block players -- see forced_dir_blocks_pawns.
    "ForcedDirVolume": "BlockingVolume",
    # PhysicsVolume, not Volume, because VolumeEffect lives there -- and its
    # defaults are the level's own, so it changes nothing but the sound.
    # See convert/reverb.py.
    "ReverbVolume": "PhysicsVolume",
}


def forced_dir_blocks_pawns(props):
    """Is this UT3 ForcedDirVolume a wall for players, or only for vehicles?

    A ForcedDirVolume steers vehicles rather than stopping them: it pushes
    anything of `TypeToForce` (UTVehicle by default) along `ArrowDirection` with
    `VehiclePushMag`, which UE2 has nothing to express. `bBlockPawns` is the
    separate half that does convert -- it makes the volume solid to a player on
    foot, and a BlockingVolume is exactly that.

    The distinction matters because the two kinds are placed for opposite
    reasons. WAR-PowerSurge has four: two with bBlockPawns, walling off the
    ledges at the map edge, and two without, one of them a lane volume pointing
    diagonally that keeps vehicles on a route players are free to walk across.
    Converting all four would wall off ground UT3 lets you walk on.

    Vehicles are blocked rather than pushed either way, since bBlockKarma has no
    direction. On an edge volume that is the same result; on a lane volume it
    would not be, which is the other reason those are left out.
    """
    return props.get("bBlockPawns") is True

# UT2004 has no kill volume, so UT3's is rebuilt out of a PhysicsVolume's pain
# damage. Without these a UTKillZVolume converts to a volume that does nothing
# at all, and the pit it was guarding just drops the player out of the world.
# `Fell` is the damage type UT2004 itself uses for falling out of the world
# (Engine/Src/UnPhysic.cpp:332 -> Pawn.FellOutOfWorld -> Died(class'Fell')), so
# the death message matches what the map would have said in UT3.
VOLUME_PROPERTIES = {
    "UTKillZVolume": [
        ("bPainCausing", "True"),
        ("DamagePerSec", "10000.000000"),
        ("DamageType", "Class'Engine.Fell'"),
        ("bAlwaysRelevant", "True"),
    ],
    # UT3's green goo. UT2004 has no stock slime volume, so this follows
    # XGame.LavaVolume's pattern with bio in place of fire. The numbers are
    # UT3's own (UTSlimeVolume defaults: DamagePerSec 7, FluidFriction 5,
    # TerminalVelocity 1500); the damage type is the bio rifle's, which is what
    # UT2004 uses for anything green that dissolves you; and ViewFog is the
    # green of XEffects.GoopSmoke (R=20, G=120..150, B=20) so the screen tints
    # the way the weapon's goop does rather than a colour picked by eye.
    "UTSlimeVolume": [
        ("bPainCausing", "True"),
        ("DamagePerSec", "7.000000"),
        ("DamageType", "Class'XWeapons.DamTypeBioGlob'"),
        ("bWaterVolume", "True"),
        ("FluidFriction", "5.000000"),
        ("TerminalVelocity", "1500.000000"),
        ("ViewFog", "(X=0.078431,Y=0.529412,Z=0.078431)"),
        ("LocationName", '"in the slime"'),
    ],
    "UTLavaVolume": [("TerminalVelocity", "1500.000000")],
    "UTWaterVolume": [("TerminalVelocity", "1500.000000")],
    # A reverb volume is a PhysicsVolume only because VolumeEffect lives there,
    # so it must never win the physics election against a volume that means it.
    # UE2 picks the overlapping PhysicsVolume with the highest Priority
    # (Engine/Src/UnLevAct.cpp:2052) and every stock volume leaves Priority at
    # 0, which would make an overlap with water decide itself by iteration
    # order. -1 loses to all of them and still beats DefaultPhysicsVolume's
    # -1000000, so the reverb applies everywhere except where a real volume
    # takes over -- and there the water should be doing the talking anyway.
    "ReverbVolume": [("Priority", "-1")],
}

# Volume settings a UT3 map may state per instance. Where it does, the instance
# wins over the table above -- the table only supplies the UE3 class default.
VOLUME_OVERRIDES = {
    "bPainCausing": ("bPainCausing", lambda v: "True" if v else "False"),
    "DamagePerSec": ("DamagePerSec", lambda v: "%f" % float(v)),
    "FluidFriction": ("FluidFriction", lambda v: "%f" % float(v)),
    "TerminalVelocity": ("TerminalVelocity", lambda v: "%f" % float(v)),
}

_SANITIZE = re.compile(r"[^A-Za-z0-9_]")


def sanitize(name):
    """UT2004 object names allow no punctuation beyond underscore."""
    out = _SANITIZE.sub("_", name or "")
    if out and out[0].isdigit():
        out = "_" + out
    return out or "None"


def material_texture_name(pkg, ref, texture_package):
    """Placeholder texture reference for a UE3 material.

    Phase 1c replaces this with the real resolved diffuse texture; the name is
    kept stable so the same t3d resolves once the texture package exists.
    """
    if ref is None or ref.is_null or not texture_package:
        return None
    return "%s.%s" % (texture_package, sanitize(ref.name))


# UE3 mappers hide a BSP face by applying this engine material to it; the
# surface still blocks, it just is not drawn. UE2 expresses the same thing as
# PF_Invisible, so the material has to become a flag rather than a texture.
# On DM-HeatRay this covers 393 polygons -- more surface area than any real
# material in the map -- which otherwise render as large blank walls.
INVISIBLE_MATERIALS = ("RemoveSurfaceMaterial",)


def is_invisible_material(ref):
    return ref is not None and not ref.is_null and ref.name in INVISIBLE_MATERIALS


# BSP surface UVs need TWO corrections between the engines, fixed by measuring
# two surfaces whose textures differ 2:1 in size:
#
#   texture                          UT3 size / exported   UT3 TU   correct TU
#   T_LT_Floors_BSP_Organic05b_D     512 / 512             0.5      2.0
#   T_HU_Walls_BSP_BrickA01_blue_D   2048 / 1024           0.25     0.5
#
# Both satisfy TU2/Size2 = 4 * TU3/Size3. So the surface scale is a flat factor
# of four, *and* a texture exported below its declared size needs its UVs scaled
# by exported/declared on top of that.
#
# Reading the editor correctly matters here. UnrealEd's scale dialog inverts its
# input (UnrealEd/Src/SurfacePropSheet.cpp:507), and with "Relative" unchecked
# polyTexScale normalises TextureU to unit length before multiplying
# (Editor/Src/UnEdCsg.cpp:1428) -- so entering 0.5 sets |TextureU| to exactly
# 2.0 whatever it was, which is what makes the two readings comparable.
#
# Exposed as --surface-scale: this rests on measurement, not on engine source.
# A global fudge on top of the real rule, which lives in convert/textures.py
# (UE3_BSP_UV_SCALE). 1.0 reproduces UT3's tiling exactly; this is here to tune
# by eye if a map wants it, not to carry the conversion.
DEFAULT_SURFACE_SCALE = 1.0

# Planes a BlockingVolume brush may have before Karma's hull decomposition
# overruns FPoly::Vertex[16]: AddConvexPrim starts from a 4-vertex quad and
# clips it against the other planes on the BSP path, one vertex at most per
# clip, so 4 + (n - 1) <= 16. See the bBlockKarma note in convert_brushes.
MAX_KARMA_PLANES = 13

# A t3d polygon may carry at most this many vertices. The importer reads them
# into FPoly::Vertex, a flat array of MAX_VERTICES=16 (Engine/Inc/UnObj.h:384),
# and *silently drops* every vertex past the sixteenth:
#
#     if( Poly.NumVertices < FPoly::MAX_VERTICES )   // Editor/Src/UnEdFact.cpp:1624
#
# UE3 has no such limit, so a round cap comes through as one big n-gon --
# WAR-PowerSurge's core pits are 64-vertex cylinder caps, and those caps are the
# floor of the core room. Truncated to 16 they arrive as a wedge, leaving most
# of the floor missing: it renders as a hole and the player falls through it.
MAX_POLY_VERTICES = 16


def split_polygon(polygon):
    """`polygon` as a list of polygons of at most MAX_POLY_VERTICES vertices.

    Split as a fan from the first vertex, each piece sharing an edge with the
    next, so the pieces are coplanar and tile the original exactly. Everything
    that decides how the surface looks and cuts -- plane, texture axes, origin,
    flags -- is carried across unchanged, so CSG and texturing see what UE3
    described. Brush faces are convex in both engines, which is what makes a fan
    from one vertex safe.
    """
    count = len(polygon.vertices)
    if count <= MAX_POLY_VERTICES:
        return [polygon]
    out = []
    first = polygon.vertices[0]
    # Each piece is the apex plus a run of the boundary, and consecutive runs
    # overlap by one vertex so the pieces meet edge to edge with no gap.
    run_length = MAX_POLY_VERTICES - 1
    start = 1
    while start < count - 1:
        run = polygon.vertices[start:start + run_length]
        piece = Polygon(
            origin=polygon.origin, normal=polygon.normal,
            texture_u=polygon.texture_u, texture_v=polygon.texture_v,
            vertices=[first] + run, texture=polygon.texture,
            flags=polygon.flags, link=polygon.link, item=polygon.item,
            pan_u=polygon.pan_u, pan_v=polygon.pan_v,
        )
        out.append(piece)
        start += run_length - 1
    return out


def convert_poly_flags(ue3_flags):
    return ue3_flags & SHARED_POLY_FLAGS


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


# FPoly.Base is the texture origin in BOTH engines and nothing else. UE2 derives
# every CSG plane from Vertex[0], not Base (Editor/Src/UnBsp.cpp:231, 292, 436,
# 548), so an off-plane Base -- which UE3 produces routinely, by up to 31500uu on
# DM-HeatRay -- is harmless and must be passed through untouched. Projecting it
# onto the polygon plane, as this converter used to, shifts the texture origin on
# every face whose TextureU/V are not perpendicular to the normal.


# A unit cube's six faces, wound so that (v1-v0) x (v2-v0) gives the outward
# normal -- the convention UE2 and UE3 share.
_BOX_FACES = [
    ((1, 0, 0), (0, 1, 0), (0, 0, -1), [(1, -1, -1), (1, 1, -1), (1, 1, 1), (1, -1, 1)]),
    ((-1, 0, 0), (0, 1, 0), (0, 0, -1), [(-1, -1, -1), (-1, -1, 1), (-1, 1, 1), (-1, 1, -1)]),
    ((0, 1, 0), (1, 0, 0), (0, 0, -1), [(1, 1, -1), (-1, 1, -1), (-1, 1, 1), (1, 1, 1)]),
    ((0, -1, 0), (1, 0, 0), (0, 0, -1), [(-1, -1, -1), (1, -1, -1), (1, -1, 1), (-1, -1, 1)]),
    ((0, 0, 1), (1, 0, 0), (0, 1, 0), [(-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]),
    ((0, 0, -1), (1, 0, 0), (0, 1, 0), [(-1, -1, -1), (-1, 1, -1), (1, 1, -1), (1, -1, -1)]),
]


def make_builder_brush(size=128.0, name="Brush"):
    """A throwaway brush to absorb the level's builder-brush slot.

    UT2004's level importer consumes the *first* actor of exact class Brush as
    the active (builder) brush: it is moved into Actors(1) and never built into
    the BSP (Editor/Src/UnEdFact.cpp:647, with Actors(0)=LevelInfo and
    Actors(1)=builder asserted at :533). UnrealEd's own map exports satisfy this
    by writing the builder brush first, so a converted map must too -- otherwise
    the first real brush is silently eaten. That used to be the world subtract
    brush, which meant no subtraction happened at all and every additive brush
    stayed buried in solid space.
    """
    polygons = []
    for normal, tu, tv, corners in _BOX_FACES:
        verts = [tuple(c[i] * size for i in range(3)) for c in corners]
        polygons.append(
            Polygon(origin=verts[0], normal=normal, texture_u=tu, texture_v=tv,
                    vertices=verts, flags=0, link=-1)
        )
    return Brush(
        name=name,
        model_name=name,
        polygons=polygons,
        csg="CSG_Active",
        properties=[("Location", vec((0.0, 0.0, 0.0)))],
    )


# Tiling the void into cells is OFF by default, and the history is worth
# keeping. It was added to cut down "Node side limit reached" warnings, which
# turned out to be harmless -- they come from BspOptGeom, which runs after CSG
# and zoning, so they leave visual T-junction cracks and nothing more. Reading
# the built .ut2 back afterwards showed what tiling actually cost: cells that
# share exactly coincident faces with their neighbours do not reliably carve,
# and DM-HeatRay ended up with regions inside WorldSubtract_3_1_1 that the
# engine still called solid. Solid space kills anything that enters it.
#
# If cells are used anyway, they are grown by CELL_OVERLAP so neighbours
# interpenetrate instead of abutting -- subtracting already-void space is a
# no-op, whereas coincident subtract faces are the thing UE2 handles badly.
DEFAULT_WORLD_CELL = 0.0
CELL_OVERLAP = 64.0


def make_world_brushes(bounds, margin=1024.0, texture=None, name="WorldSubtract",
                       fake_backdrop=False, cell=DEFAULT_WORLD_CELL):
    """The enclosing void, tiled into cells no larger than `cell` per axis."""
    (min_x, min_y, min_z), (max_x, max_y, max_z) = bounds
    lo = [min_x - margin, min_y - margin, min_z - margin]
    hi = [max_x + margin, max_y + margin, max_z + margin]
    counts = [1, 1, 1]
    if cell and cell > 0:
        for i in range(3):
            counts[i] = max(1, int(math.ceil((hi[i] - lo[i]) / cell)))
    if counts == [1, 1, 1]:
        return [make_world_brush(bounds, margin, texture, name, fake_backdrop)]

    out = []
    step = [(hi[i] - lo[i]) / counts[i] for i in range(3)]
    for ix in range(counts[0]):
        for iy in range(counts[1]):
            for iz in range(counts[2]):
                index = (ix, iy, iz)
                cell_lo = [lo[i] + step[i] * index[i] - CELL_OVERLAP for i in range(3)]
                cell_hi = [lo[i] + step[i] * (index[i] + 1) + CELL_OVERLAP for i in range(3)]
                out.append(make_world_brush(
                    (tuple(cell_lo), tuple(cell_hi)), margin=0.0, texture=texture,
                    name="%s_%d_%d_%d" % (name, ix, iy, iz),
                    fake_backdrop=fake_backdrop))
    return out


def make_world_brush(bounds, margin=1024.0, texture=None, name="WorldSubtract",
                     fake_backdrop=False):
    """An enclosing subtractive box -- the bridge between the two CSG paradigms.

    UT2004 levels are subtractive: the world starts as solid rock and rooms are
    carved out of it. UT3 levels are additive: the world starts empty and
    geometry is added. Dropping UT3's additive brushes into UT2004 as-is leaves
    them entombed in solid space, so the converted map needs one large
    subtractive brush around everything, applied before the rest.
    """
    (min_x, min_y, min_z), (max_x, max_y, max_z) = bounds
    center = ((min_x + max_x) / 2, (min_y + max_y) / 2, (min_z + max_z) / 2)
    ext = (
        (max_x - min_x) / 2 + margin,
        (max_y - min_y) / 2 + margin,
        (max_z - min_z) / 2 + margin,
    )
    # With a skybox present the enclosing box shows sky instead of a grey wall.
    flags = PF_FAKE_BACKDROP if fake_backdrop else 0
    polygons = []
    for normal, tu, tv, corners in _BOX_FACES:
        verts = [tuple(c[i] * ext[i] for i in range(3)) for c in corners]
        polygons.append(
            Polygon(
                origin=verts[0],
                normal=normal,
                texture_u=tu,
                texture_v=tv,
                vertices=verts,
                texture=texture,
                flags=flags,
                link=-1,
            )
        )
    return Brush(
        name=name,
        model_name="Model_%s" % name,
        polygons=polygons,
        csg="CSG_Subtract",
        properties=[("Location", vec(center)), ("Group", '"Converted"')],
    )


class BrushStats:
    def __init__(self):
        self.brushes = 0
        self.polygons = 0
        self.vertices = 0
        self.subtractive = 0
        self.skipped_no_model = 0
        self.skipped_empty = 0
        self.skipped_open = []
        self.aligned = 0
        self.align_skipped = 0
        self.redundant = []
        self.volumes = 0
        self.volume_classes = {}
        self.unkarma = []
        self.node_pads = []
        self.steering_volumes = []
        self.skipped_reverb = []
        self.reverb_presets = {}
        self.split_polys = []
        self.dropped_flag_bits = 0
        self.world_min = [float("inf")] * 3
        self.world_max = [float("-inf")] * 3
        self.max_brush_radius = 0.0
        self.builder_brushes = 0
        self.invisible_polys = 0

    def note_vertex(self, world_v):
        for i in range(3):
            if world_v[i] < self.world_min[i]:
                self.world_min[i] = world_v[i]
            if world_v[i] > self.world_max[i]:
                self.world_max[i] = world_v[i]

    @property
    def world_bounds(self):
        if self.world_min[0] == float("inf"):
            return None
        return tuple(self.world_min), tuple(self.world_max)

    def __str__(self):
        out = (
            "%d brushes (%d subtractive), %d polygons, %d vertices; "
            "%d volumes; %d faces UT3 hid (drawn anyway, see convert/geometry.py); "
            "skipped %d without a model, %d empty, %d builder"
            % (
                self.brushes,
                self.subtractive,
                self.polygons,
                self.vertices,
                self.volumes,
                self.invisible_polys,
                self.skipped_no_model,
                self.skipped_empty,
                self.builder_brushes,
            )
        )
        if self.split_polys:
            out += ("; %d face(s) split to fit the importer's 16-vertex limit "
                    "(largest %d verts): %s"
                    % (len(self.split_polys), max(v for _n, v, _p in self.split_polys),
                       ", ".join(sorted({n for n, _v, _p in self.split_polys}))[:60]))
        if self.node_pads:
            out += ("; %d power node pad(s) dropped, UT2004's node brings its own"
                    % len(self.node_pads))
        if self.reverb_presets:
            out += ("; %d reverb volume(s) carrying an I3DL2 room effect (%s)"
                    % (sum(self.reverb_presets.values()),
                       ", ".join("%s x%d" % (k.lower(), n)
                                 for k, n in sorted(self.reverb_presets.items()))))
        if self.skipped_reverb:
            out += ("; %d reverb volume(s) left out (switched off in UT3, or a "
                    "preset I3DL2 has no entry for)" % len(self.skipped_reverb))
        if self.steering_volumes:
            out += ("; %d ForcedDirVolume(s) left out -- they steer vehicles "
                    "rather than blocking anyone, which UE2 cannot express: %s"
                    % (len(self.steering_volumes), ", ".join(self.steering_volumes[:3])))
        if self.unkarma:
            out += ("; %d blocking volume(s) too complex or not closed for "
                    "Karma's hull decomposition (it crashes the game), so they "
                    "block players but not vehicles: %s"
                    % (len(self.unkarma), ", ".join(self.unkarma[:3])
                       + (", ..." if len(self.unkarma) > 3 else "")))
        if self.redundant:
            out += ("; %d redundant (already inside another brush): %s"
                    % (len(self.redundant), ", ".join(self.redundant[:4])
                       + (", ..." if len(self.redundant) > 4 else "")))
        if self.aligned:
            out += ("; %d brushes had near-coplanar faces aligned" % self.aligned
                    + ("; %d left alone" % self.align_skipped if self.align_skipped else ""))
        if self.skipped_open:
            out += ("; %d not closed (would solidify a half-space): %s"
                    % (len(self.skipped_open), ", ".join(self.skipped_open[:6])
                       + (", ..." if len(self.skipped_open) > 6 else "")))
        return out


def brush_scale(props):
    """UE3's DrawScale/DrawScale3D on a brush, as a plain per-axis factor.

    Baked into the vertices rather than emitted as UE2's MainScale, for the
    same reason PrePivot is: it leaves the brush's own transform identity, so
    nothing downstream can apply it twice or not at all. CTF-Coret's
    BlockingVolume_200 is why -- a 34-poly volume at DrawScale3D 0.4279 that
    arrived in the editor with no outline and could not be selected, while
    still blocking a doorway 2.34x further out than its shape suggests.

    Rare enough to have gone unnoticed: 4 of CTF-Coret's 1530 brushes are
    scaled, and none at all in WAR-Torlan or WAR-PowerSurge.
    """
    draw_scale = props.get("DrawScale", 1.0) or 1.0
    s3d = props.get("DrawScale3D")
    if s3d is not None and s3d.value:
        sx, sy, sz = s3d.value
    else:
        sx = sy = sz = 1.0
    return (sx * draw_scale, sy * draw_scale, sz * draw_scale)


def convert_brushes(pkg, texture_package=None, scale=1.0, include_volumes=False, stats=None,
                    texture_set=None, surface_scale=DEFAULT_SURFACE_SCALE,
                    align_faces=True, skip=()):
    """Convert every brush actor in `pkg` into t3d Brush actors.

    `skip` names volumes handled elsewhere -- the pad UT3 lays over a power
    node's scenery, which UT2004's own node base replaces.
    """
    stats = stats or BrushStats()
    out = []
    wanted = set(BRUSH_CLASSES)
    if include_volumes:
        wanted |= set(VOLUME_CLASSES)

    # CSG is order-dependent and the export table is sorted by name, so walk the
    # level's actor list instead -- that is the order the map was authored in.
    for export in ordered_exports(pkg, wanted):
        if export.name in skip:
            stats.node_pads.append(export.name)
            continue
        cls = pkg.class_name_of(export)
        props, _start, _end = read_object_properties(pkg, export)
        if is_builder_brush(pkg, export, props):
            stats.builder_brushes += 1
            continue
        if cls == "ForcedDirVolume" and not forced_dir_blocks_pawns(props):
            stats.steering_volumes.append(export.name)
            continue
        model_ref = props.get("Brush")
        if model_ref is None or not model_ref.is_export:
            stats.skipped_no_model += 1
            continue
        polys_export = find_polys(pkg, model_ref.export)
        if polys_export is None:
            stats.skipped_no_model += 1
            continue
        polys = read_polys(pkg, polys_export)
        if not polys:
            stats.skipped_empty += 1
            continue
        # A brush that does not enclose a volume must never reach UE2's CSG.
        # A flat sheet added with CSG_Add solidifies an entire half-space, which
        # in game is an invisible plane that kills whatever crosses it: the pawn
        # ends up in zone 0 and the engine calls FellOutOfWorld
        # (Engine/Src/UnPhysic.cpp:336). Dropping the brush loses a face or two;
        # keeping it makes a chunk of the map lethal.
        if cls not in VOLUME_CLASSES and not is_closed_solid(polys):
            stats.skipped_open.append(export.name)
            continue

        name = sanitize(export.name)
        # Baked into the vertices below, not emitted -- see brush_scale.
        sizing = brush_scale(props)
        objects, ref_property = (), None
        location = props.get("Location")
        origin = [c * scale for c in location.value] if (location and location.value) else [0.0] * 3
        pre_pivot = props.get("PrePivot")
        pivot = [c * scale for c in pre_pivot.value] if (pre_pivot and pre_pivot.value) else [0.0] * 3
        rotation = props.get("Rotation")
        rotated = bool(rotation is not None and rotation.value and any(rotation.value))
        polygons = []
        for poly in polys:
            flags = convert_poly_flags(poly.flags)
            # UT3's RemoveSurfaceMaterial marks a face as not drawn, and the
            # obvious translation is PF_Invisible. It is a trap. UE2 turns that
            # flag into NF_NotVisBlocking (Editor/Src/UnBsp.cpp:242), and zone
            # assignment then stops treating the face as the boundary between
            # inside and outside:
            #
            #     AssignAllZones( iFront, Outside ||  IsCsg(NF_NotVisBlocking) )
            #     AssignAllZones( iBack,  Outside && !IsCsg(NF_NotVisBlocking) )
            #         -- Editor/Src/UnVisi.cpp:1170
            #
            # so the open space beyond an invisible face inherits "inside" and
            # comes out as zone 0, which is solid. Anything entering it is
            # killed on the spot as having fallen out of the world. The flag is
            # meant for invisible collision hulls, where the far side really is
            # inside the same solid; on an ordinary brush face it is poison.
            # Measured on DM-HeatRay: 10.51% of the play volume wrongly solid
            # with it, 0.00% without. The face renders instead, which for
            # geometry UT3 had hidden is almost always buried out of sight.
            hidden = is_invisible_material(poly.material)
            if hidden:
                stats.invisible_polys += 1
            if poly.flags & ~SHARED_POLY_FLAGS:
                stats.dropped_flag_bits += 1
            # PrePivot is baked into the vertices rather than emitted as an
            # actor property. UE2 builds a brush as (v - PrePivot) * Coords +
            # Location (FPoly::Transform via UnBsp.cpp:1860), so subtracting it
            # here is exactly equivalent -- but it leaves the brush's pivot at
            # the brush instead of thousands of units away, which matters
            # because 282 of DM-HeatRay's 313 brushes share one Location and are
            # placed entirely by PrePivot.
            verts = [tuple((v[i] - pivot[i] / scale) * sizing[i] * scale for i in range(3))
                     for v in poly.vertices]
            # UE3 states surface UVs against a fixed constant, UE2 against the
            # texture size, so every surface has to be restated in terms of the
            # size actually exported -- see UE3_BSP_UV_SCALE.
            su, sv = texture_set.scale_for(poly.material) if texture_set else (1.0, 1.0)
            su *= surface_scale
            sv *= surface_scale
            pieces = split_polygon(
                Polygon(
                    origin=tuple((poly.base[i] - pivot[i] / scale) * sizing[i] * scale
                                 for i in range(3)),
                    normal=poly.normal,
                    texture_u=tuple(c * su / scale for c in poly.texture_u),
                    texture_v=tuple(c * sv / scale for c in poly.texture_v),
                    vertices=verts,
                    # material_for, not name_for: a translucent or additive
                    # BSP surface gets the FinalBlend built for it, and
                    # everything else falls through to its plain texture. The
                    # t3d polygon importer names no class, so the bare path is
                    # what goes here -- it searches ANY_PACKAGE by name
                    # (Editor/Src/UnEdFact.cpp:1602).
                    texture=(texture_set.material_for(poly.material) if texture_set
                             else material_texture_name(pkg, poly.material, texture_package)),
                    flags=flags,
                    link=poly.link,
                    item=sanitize(poly.item_name) if poly.item_name != "None" else None,
                )
            )
            polygons.extend(pieces)
            if len(pieces) > 1:
                stats.split_polys.append((name, len(verts), len(pieces)))
            stats.polygons += 1
            stats.vertices += len(verts)
            for v in verts:
                stats.note_vertex([origin[i] + v[i] for i in range(3)])
                if rotated:
                    # A rotated brush can reach beyond its unrotated AABB; track
                    # how far so the world brush can be padded to cover it.
                    radius = max(abs(v[i]) for i in range(3))
                    if radius > stats.max_brush_radius:
                        stats.max_brush_radius = radius

        csg_prop = props.get("CsgOper")
        csg = "CSG_Subtract" if csg_prop == "CSG_Subtract" else "CSG_Add"
        if csg == "CSG_Subtract":
            stats.subtractive += 1

        properties = []
        if location is not None and location.value:
            properties.append(("Location", vec(origin)))
        if rotated:
            properties.append(("Rotation", rot(rotation.value)))
        group = props.get("Group")
        if group and group != "None":
            properties.append(("Group", '"%s"' % sanitize(group)))
        if cls in VOLUME_CLASSES:
            out_class = VOLUME_CLASSES[cls]
            csg = "CSG_Active"  # how UE2 volumes mark their brush
            settings = dict(VOLUME_PROPERTIES.get(cls, ()))
            # Karma decomposes a BlockingVolume's brush into convex hulls at
            # BeginPlay -- and only a BlockingVolume's (KarmaSupport.cpp:1020) --
            # by walking its BSP and clipping a quad against every plane on the
            # path to each leaf (KUtils.cpp:1040, :903). That quad is an FPoly,
            # whose Vertex array is a flat 16 (UnObj.h:384), and the clipper
            # writes into it with no bounds check at all (FPoly::
            # SplitWithPlaneFast, UnFPoly.cpp). Each clip can add a vertex, so
            # a leaf more than 13 planes deep overruns the array and takes the
            # game down inside AddConvexPrim -- which is how WAR-PowerSurge,
            # with 128-poly cylinder volumes, crashed on load.
            #
            # Depth is bounded by the brush's distinct plane count (a plane
            # cannot repeat on one root-to-leaf path), so the poly count is a
            # conservative stand-in for it. bBlockKarma gates the whole thing
            # (KarmaSupport.cpp:1211) and is separate from bBlockActors, so a
            # volume goes on blocking players either way; what it costs is
            # Karma bodies -- ragdolls and vehicles -- passing through.
            if out_class == "BlockingVolume" and (
                    len(polys) > MAX_KARMA_PLANES or not is_closed_solid(polys)):
                settings["bBlockKarma"] = "False"
                stats.unkarma.append(name)
            for key, (name_out, render) in VOLUME_OVERRIDES.items():
                value = props.get(key)
                if value is None:
                    continue
                try:
                    settings[name_out] = render(value)
                except (TypeError, ValueError):
                    continue
            if cls == "ReverbVolume":
                from convert.reverb import effect_object, settings_of

                effect = effect_object(name, props)
                if effect is None:
                    # Switched off in UT3, or a preset with no I3DL2 entry: a
                    # PhysicsVolume with no effect would be a silent no-op
                    # actor, so leave the volume out entirely.
                    stats.skipped_reverb.append(export.name)
                    continue
                objects = [effect]
                ref_property = "VolumeEffect"
                key = settings_of(props)[0]
                stats.reverb_presets[key] = stats.reverb_presets.get(key, 0) + 1
            properties.extend(sorted(settings.items()))
            stats.volumes += 1
            stats.volume_classes[out_class] = stats.volume_classes.get(out_class, 0) + 1
        else:
            out_class = "Brush"
            stats.brushes += 1

        out.append(
            Brush(
                name=name,
                model_name="Model_%s" % name,
                polygons=polygons,
                csg=csg,
                properties=properties,
                cls=out_class,
                objects=objects,
                ref_property=ref_property,
            )
        )
    # A brush wholly inside another adds nothing but cospatial faces for UE2's
    # CSG to trip over, so it goes before the alignment pass sees it.
    from convert.redundant import find_redundant

    csg_only = [b for b in out if getattr(b, "cls", "Brush") == "Brush"]
    dropped = {inner for inner, _outer in find_redundant(csg_only)}
    if dropped:
        stats.redundant = sorted(dropped)
        for brush in out:
            if getattr(brush, "cls", "Brush") != "Brush" or brush.name not in dropped:
                continue
            stats.brushes -= 1
            stats.polygons -= len(brush.polygons)
            stats.vertices -= sum(len(p.vertices) for p in brush.polygons)
            # PF_Invisible is no longer emitted, so nothing to subtract here.
        out = [b for b in out if getattr(b, "cls", "Brush") != "Brush" or b.name not in dropped]

    if align_faces:
        # UT3's sub-unit float drift leaves faces that were authored flush a few
        # thousandths apart -- inside UE2's 0.1 plane tolerance but not equal to
        # it, which is what turns open space solid. Volumes take no part in CSG,
        # so only the real brushes are aligned.
        from convert.align import align_brushes

        csg_brushes = [b for b in out if getattr(b, "cls", "Brush") == "Brush"]
        stats.aligned, stats.align_skipped = align_brushes(csg_brushes)
    return out, stats
