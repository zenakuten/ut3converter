"""Sky conversion: UT3's sky dome, either kept inline or moved to a sky room.

UT3 has no skybox. The horizon is a genuinely huge dome mesh sitting in the
level -- DM-HeatRay's `S_UN_Sky_SM_Dome01` is at DrawScale 300, a 323,520uu
radius -- with the distant city blocks as ordinary actors parked outside the
play area.

UE2 cannot draw that. FAR_CLIPPING_PLANE below is the far plane of the
projection matrix itself, so a dome five times past it is depth-clipped and the
sky visibly stops partway up. `fit_inline_dome` keeps UT3's model at the largest
size the engine will actually render; `make_skybox` implements UT2004's own
idiom instead, where the sky is a separate little room drawn with the viewer's
rotation but not translation and so has no distance limit at all.
"""

import re

from ut2.t3d import Actor, Brush, Polygon, vec

# Engine/Inc/Engine.h: UE2 clamps every coordinate to +/-HALF_WORLD_MAX.
HALF_WORLD_MAX = 262144.0

# The smallest the dome may be, as a multiple of the distance to the furthest
# geometry: a floor, not a target. UT3's own size is the target, and it is far
# larger than this -- DM-HeatRay's dome is roughly 8x the extent of everything
# it covers, which is what gives the horizon its distance.
DEFAULT_DOME_MARGIN = 1.25

# The world brush has to enclose the dome and still land inside UE2's world, so
# keep its outermost vertex a little short of the hard limit.
WORLD_SAFETY = 0.98

# Core/Src/Core.cpp:197 -- and it is never reassigned anywhere in the engine.
# This is not a cull distance that can be tuned: it is the far plane of the
# perspective projection matrix itself (Engine/Src/UnRender.cpp:1510), so the
# hardware depth-clips anything further from the camera than this. The zone's
# bDistanceFog/DistanceFogEnd only move the frustum *culling* plane
# (UnRender.cpp:1066) and cannot push geometry past it.
#
# This single constant is why UE2 has the skybox idiom at all: a sky big enough
# to look distant cannot be drawn in the level.
FAR_CLIPPING_PLANE = 65536.0

# Leave a little room rather than putting the dome exactly on the far plane.
VIEW_SAFETY = 0.95

# Mesh names that look like sky geometry.
SKY_NAME_HINTS = ("_sky_", "skydome", "_dome", "skybox")

# Room size and how much of it the dome should fill.
#
# Proportions are what a skybox shows -- it draws with the viewer's rotation but
# not translation -- and a uniform scale preserves them at any room size:
# DM-HeatRay keeps UT3's 8:1 dome-to-backdrop ratio at both 2048 and 16384.
# A large room is *not* equivalent in practice though: measured in the editor, a
# 16384 room (dome 12288uu from the sky viewpoint) looks visibly worse than a
# 2048 one (dome at 1536uu), presumably depth precision or fog over that range.
# So keep the room small and deal with co-located actors by merging them instead.
DEFAULT_ROOM_EXTENT = 2048.0

# Backdrop actors closer together than this in the sky room are visually
# indistinguishable -- a fraction of a unit inside a 1536uu dome -- and only
# produce "same location" warnings on build, so duplicates of a mesh are merged.
DEFAULT_MERGE_DISTANCE = 1.0
DOME_FILL = 0.75


def looks_like_sky(mesh_name):
    low = mesh_name.lower()
    return any(hint in low for hint in SKY_NAME_HINTS)


def find_sky_meshes(pkg, index, mesh_set):
    """Mesh names in the set that look like sky geometry, largest first."""
    from ut3.objects.staticmesh import read_static_mesh

    found = []
    for name, (owner, export, _overrides) in mesh_set.meshes.items():
        if not looks_like_sky(name):
            continue
        mesh = read_static_mesh(owner, export)
        radius = mesh.bounds[6] if mesh is not None else 0.0
        found.append((radius, name))
    found.sort(reverse=True)
    return [name for _radius, name in found]


def mesh_radius(pkg, index, mesh_set, mesh_name):
    from ut3.objects.staticmesh import read_static_mesh

    entry = mesh_set.meshes.get(mesh_name)
    if entry is None:
        return None
    owner, export = entry[0], entry[1]
    mesh = read_static_mesh(owner, export)
    return mesh.bounds[6] if mesh is not None else None


def _box_brush(name, center, extent, csg, texture=None, flags=0):
    from convert.geometry import _BOX_FACES

    polygons = []
    for normal, tu, tv, corners in _BOX_FACES:
        verts = [tuple(c[i] * extent for i in range(3)) for c in corners]
        polygons.append(Polygon(origin=verts[0], normal=normal, texture_u=tu,
                                texture_v=tv, vertices=verts, texture=texture,
                                flags=flags, link=-1))
    return Brush(name=name, model_name="Model_%s" % name, polygons=polygons,
                 csg=csg, properties=[("Location", vec(center)),
                                      ("Group", '"Skybox"')])


def make_skybox(world_bounds, package_name, mesh_name, dome_radius,
                room_extent=DEFAULT_ROOM_EXTENT, ambient=None, texture=None,
                clear_of=None):
    """Build the sky room: subtract box, SkyZoneInfo, and the dome mesh.

    The UT2004-idiomatic alternative to `fit_inline_dome`: surfaces flagged
    PF_FakeBackdrop show whatever a SkyZoneInfo sees, drawn with the viewer's
    rotation but not translation, so the dome only has to surround the
    SkyZoneInfo rather than the map. Cheaper than a 100,000uu dome, but it is
    not what UT3 authored -- the horizon has to be shrunk into the room too.

    The room is an independent void -- UE2 carves each subtraction out of
    infinite solid, so it neither contains nor is contained by the map (a stock
    UT2004 skybox sits tens of thousands of units away; ONS-Adara's is at
    -40608, 37252, -22516 with the play area near the origin). It only has to
    avoid overlapping anything, including actors placed far outside the play
    area -- UT3 scatters distant backdrop meshes well beyond the map bounds.
    """
    (min_x, _min_y, _min_z), (_max_x, _max_y, max_z) = clear_of or world_bounds
    # Well clear of everything, and comfortably inside UE2's +/-262144 world.
    center = (min_x - room_extent * 4.0, 0.0, max_z + room_extent * 4.0)

    actors = [_box_brush("SkyRoom", center, room_extent, "CSG_Subtract", texture)]

    # The dome is a hemisphere sitting on z=0, so put the viewpoint at its base.
    scale = (room_extent * DOME_FILL / dome_radius) if dome_radius else 1.0
    zone_props = [("Location", vec(center)), ("bTerrainZone", "False")]
    if ambient:
        brightness, hue, saturation = ambient
        zone_props.extend([("AmbientBrightness", str(brightness)),
                           ("AmbientHue", str(hue)),
                           ("AmbientSaturation", str(saturation))])
    actors.append(Actor("SkyZoneInfo", "SkyZoneInfo0", zone_props))

    if mesh_name:
        actors.append(Actor("StaticMeshActor", "SkyDome", [
            ("StaticMesh", "StaticMesh'%s.%s'" % (package_name, mesh_name)),
            ("Location", vec(center)),
            ("DrawScale", "%f" % scale),
            ("bUnlit", "True"),
        ]))
    return actors


def _reach(center, bounds):
    """Straight-line distance from `center` to the furthest corner of `bounds`.

    All eight corners, not just the two the bounds are written as: the furthest
    is whichever end of each axis is further, which need not be either named
    corner when the centre sits inside the box.
    """
    lo, hi = bounds
    return sum(max(abs(lo[i] - center[i]), abs(hi[i] - center[i])) ** 2
               for i in range(3)) ** 0.5


def fit_inline_dome(world_bounds, dome_location, dome_radius, locations,
                    margin=DEFAULT_DOME_MARGIN, native_scale=1.0,
                    world_margin=0.0, map_center=None):
    """Keep UT3's sky dome in the level, as close to its authored size as fits.

    Returns (scale, radius, location, world_bounds, limited_by) -- the
    DrawScale for the dome, its resulting radius, where to put it, the bounds
    the world subtract brush must cover so the dome ends up in open space, and
    the name of whichever engine limit forced it below UT3's size ("" if none).

    Two limits bind. UE2's 262144uu world has to contain the subtract brush, and
    -- almost always the tighter one -- the whole dome has to stay inside the
    65536uu far plane from anywhere a player can stand, or the hardware
    depth-clips it and the sky visibly stops partway up.

    Size is not derived from the level. UT3's dome is deliberately much larger
    than what it covers, which is exactly what makes the horizon read as
    distant, so shrink-wrapping it around the geometry brings the sky far too
    close. The authored `native_scale` is the target; `margin` is only a floor,
    so a dome that would not even enclose the level is grown rather than left
    cutting through it.

    When the world limit does bite, the dome's *offset* from the play area is
    scaled by the same factor as its radius, not left where UT3 had it. UT3
    parks DM-HeatRay's dome 50,728uu off to one side and gets away with it
    because the dome is 323,520uu; keeping that offset under a shrunken dome
    both skews the sky and wastes the radius budget, since the offset counts
    against HALF_WORLD_MAX too. Scaling both together is what preserves the
    view: a dome at offset d with radius R subtends exactly the same angles
    from the play area as one at offset s*d with radius s*R, so a player at the
    centre of the map sees the sky UT3 authored.
    """
    center = list(map_center or [(world_bounds[0][i] + world_bounds[1][i]) / 2.0
                                 for i in range(3)])
    if dome_location is None:
        dome_location = center
    offset = [dome_location[i] - center[i] for i in range(3)]

    dome_radius = dome_radius or 1.0
    native_radius = dome_radius * native_scale
    # Everything -- the dome's far side, its offset, and the brush margin -- has
    # to land inside HALF_WORLD_MAX on every axis.
    budget = HALF_WORLD_MAX * WORLD_SAFETY - world_margin
    world_fit = min((budget - abs(center[i])) / (abs(offset[i]) + native_radius)
                    for i in range(3))

    # And the whole dome has to stay inside the far plane from anywhere a player
    # can stand, or it is depth-clipped and the sky simply stops partway up.
    # Worst case is the far side of the dome seen from the far corner of the
    # play area: play_reach + |offset|*s + native_radius*s.
    play_reach = _reach(center, world_bounds)
    offset_length = sum(v * v for v in offset) ** 0.5
    view_fit = ((FAR_CLIPPING_PLANE * VIEW_SAFETY - play_reach)
                / (offset_length + native_radius))
    # The offset costs radius directly, so when the far plane is what bites,
    # spend the whole budget on size and centre the dome on the play area.
    # Only then -- a dome that fits at UT3's size stays exactly where UT3 put it.
    if view_fit < min(world_fit, 1.0):
        offset = [0.0, 0.0, 0.0]
        view_fit = (FAR_CLIPPING_PLANE * VIEW_SAFETY - play_reach) / native_radius

    shrink = max(0.0, min(1.0, world_fit, view_fit))
    if shrink >= 1.0:
        limited_by = ""
    elif view_fit <= world_fit:
        limited_by = "the 65536uu far plane"
    else:
        limited_by = "UE2's 262144uu world"
    radius = native_radius * shrink
    location = tuple(center[i] + offset[i] * shrink for i in range(3))

    # Floor: whatever else happens, the dome has to contain the level -- but
    # never past the far plane, since a dome that is not drawn is worse than a
    # dome the odd backdrop mesh pokes through.
    reach = max(abs(point[i] - location[i])
                for point in list(locations) + [world_bounds[0], world_bounds[1]]
                if point is not None for i in range(3))
    room = min(min(budget - abs(location[i]) for i in range(3)),
               FAR_CLIPPING_PLANE * VIEW_SAFETY - _reach(location, world_bounds))
    radius = min(max(radius, reach * margin), room)

    scale = radius / dome_radius
    bounds = (
        tuple(min(world_bounds[0][i], location[i] - radius) for i in range(3)),
        tuple(max(world_bounds[1][i], location[i] + radius) for i in range(3)),
    )
    if radius >= native_radius - 1e-6:
        limited_by = ""
    return scale, radius, location, bounds, limited_by
