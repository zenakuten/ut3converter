"""Moving UT3's distant backdrop geometry into a UT2004 skybox.

UT3 builds its horizon from real geometry parked far outside the play area --
DM-HeatRay's city blocks reach X -39604 -- and simply lets the player see it at
distance. Converted straight across, those actors land outside the world
subtract brush, in solid space, where nothing renders them.

A UT2004 skybox is drawn with the viewer's rotation but *not* translation, so
everything in the sky room appears infinitely far away. That means a uniform
scale about the play area's centre preserves every apparent angle exactly: an
object at distance d with size s subtends s/d, and at distance d*S with size s*S
it subtends the same. So the horizon can be reproduced by shrinking the distant
geometry into the sky room rather than by faking it.

The scale comes from the sky dome, which has to fit the room:

    S = (dome DrawScale in the sky room) / (dome DrawScale in UT3)

On DM-HeatRay that is 1.4243 / 300 = 0.004748, which brings geometry 40,564uu
out to 193uu from the sky origin -- comfortably inside the dome's 1536uu radius,
in the same proportion UT3 had it.
"""

import re

from ut2.t3d import Actor, vec

_LOCATION = re.compile(r"\(X=(\S+?),Y=(\S+?),Z=(\S+?)\)")


def parse_location(actor):
    for key, value in actor.properties:
        if key == "Location":
            m = _LOCATION.match(value)
            if m:
                return [float(x) for x in m.groups()]
    return None


def parse_scale(actor):
    for key, value in actor.properties:
        if key == "DrawScale":
            try:
                return float(value)
            except ValueError:
                return 1.0
    return 1.0


def furthest_from(bounds, location):
    """Worst-case distance from anywhere inside `bounds` to `location`.

    The far clipping plane is measured from the camera, so what decides whether
    a backdrop mesh can be drawn is not where it sits but how far it can get
    from a player -- the far corner of the play area, not the near one.
    """
    total = 0.0
    for i in range(3):
        lo, hi = bounds[0][i], bounds[1][i]
        total += max(abs(location[i] - lo), abs(location[i] - hi)) ** 2
    return total ** 0.5


def is_outside(location, bounds, slack=0.0):
    (lo, hi) = bounds
    return any(location[i] < lo[i] - slack or location[i] > hi[i] + slack for i in range(3))


def merge_close(actors, distance):
    """Drop actors of the same mesh that land within `distance` of another.

    Shrinking a whole horizon into a small room puts many actors a fraction of a
    unit apart, which is invisible but makes UnrealEd warn about co-located
    actors on every build. Only same-mesh neighbours are merged, so distinct
    geometry that happens to overlap is kept.
    """
    kept = []          # (actor, location, mesh); location may be None
    for actor in actors:
        location = parse_location(actor)
        mesh = next((v for k, v in actor.properties if k == "StaticMesh"), None)
        if location is not None and any(
            other_loc is not None and other_mesh == mesh
            and max(abs(location[i] - other_loc[i]) for i in range(3)) < distance
            for _a, other_loc, other_mesh in kept
        ):
            continue
        kept.append((actor, location, mesh))
    return [actor for actor, _loc, _mesh in kept]


def move_to_skybox(actors, map_center, sky_center, scale, name_prefix="Sky_"):
    """Rescale actors about the map centre into the sky room.

    Position and size are scaled by the same factor, so the geometry keeps the
    angular size it had in UT3 when viewed from the play area.
    """
    moved = []
    for actor in actors:
        location = parse_location(actor)
        if location is None:
            continue
        new_location = tuple(
            sky_center[i] + (location[i] - map_center[i]) * scale for i in range(3)
        )
        properties = []
        for key, value in actor.properties:
            if key == "Location":
                properties.append((key, vec(new_location)))
            elif key == "DrawScale":
                properties.append((key, "%f" % (float(value) * scale)))
            else:
                properties.append((key, value))
        if not any(k == "DrawScale" for k, _v in properties):
            properties.append(("DrawScale", "%f" % scale))
        # Skybox geometry is never lit by the level's lights.
        if not any(k == "bUnlit" for k, _v in properties):
            properties.append(("bUnlit", "True"))
        moved.append(Actor(actor.cls, name_prefix + actor.name, properties))
    return moved


def drop_distant_effects(actors, play_area, far_plane):
    """Remove volumetric effect actors the far plane would clip anyway.

    UT3 parks fog sheets and falloff spheres out at the horizon alongside the
    backdrop, and once the converter started drawing them (Phase 14) one of
    DM-Deck's landed 114,621uu from the play area. That is past UE2's far plane,
    so it cannot be drawn where it stands whatever else happens -- but leaving
    it in the actor list answered "is there scenery too far to draw?" with yes,
    and moved the map's entire distant city into the skybox on the strength of
    one invisible fog sheet.

    Dropping it is the honest answer twice over: the effect cannot render at
    that distance, and a map should not be restructured around geometry that
    contributes nothing. Real backdrop scenery is untouched and still decides
    the question on its own.

    Only actors *outside* the play area are candidates, which is the same guard
    the backdrop measurement uses and for the same reason: `furthest_from`
    answers "how far can a player get from this", so for anything standing in
    the level it returns the map's own diagonal. DM-HeatRay is 131,761uu across
    and every one of its 22 light beams was being thrown away on that.

    Returns (kept actors, dropped count).
    """
    kept, dropped = [], 0
    for actor in actors:
        if getattr(actor, "is_effect", False):
            location = parse_location(actor)
            if location is not None and is_outside(location, play_area) \
                    and furthest_from(play_area, location) > far_plane:
                dropped += 1
                continue
        kept.append(actor)
    return kept, dropped
