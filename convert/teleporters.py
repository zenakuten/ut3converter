"""UT3 teleporters -> UT2004 Teleporter, dressed the way DM-Deck17 dresses its own.

The pairing needs no invention at all: both engines say it the same way. A
teleporter that sends names its destination in `URL`, and the destination
answers to that name in `Tag`. DM-Deck's UT3 pair is `URL=RedeemME` against
`Tag=RedeemME`, and DM-Deck17 -- the stock UT2004 version of the same map --
does exactly that with `upstairsred`. So URL and Tag copy straight across.

What does not come across is the look. UT3's `UTTeleporter` carries a
`PortalEffect` particle system and a `TeleporterBaseMesh` as components of the
actor, and neither converts: UE2 has no equivalent of a UE3 particle system,
and the base mesh belongs to a UT3 content package. UT2004's `Teleporter` draws
nothing whatsoever on its own, so a converted teleporter is invisible -- you
walk into empty air and come out somewhere else.

DM-Deck17 solves that with two stock static meshes rather than an emitter, and
since it is the same map the sizes are known to suit: `teleporter-proc` wearing
the shield shell as a FinalBlend for the portal itself, and `TelePorterbase`
under it. Both come out of packages UT2004 ships, so nothing has to be built.
"""

import re

from ut2.t3d import Actor, rot, vec
from ut3.objects.level import ordered_exports
from ut3.props import read_object_properties

_SANITIZE = re.compile(r"[^A-Za-z0-9_]")

TELEPORTER_CLASSES = ("UTTeleporter", "Teleporter")

PORTAL_MESH = "XGame_StaticMeshes.GameObjects.teleporter-proc"
PORTAL_SKIN = "FinalBlend'XEffectMat.Shield.RedShell'"
BASE_MESH = "XGame_StaticMeshes.TelePorterbase"

# DM-Deck17's arrangement, but measured from the *floor* rather than from the
# teleporter, because the two engines do not stand their teleporter at the same
# height: DM-Deck17's sits 56.25 above the floor (its own CollisionHeight,
# confirmed by the weapon base beside it at -305.63), a UT3 one sits 34.
#
# Getting that wrong buries the base. Both meshes hang below their pivot --
# TelePorterbase spans -81..-56 and teleporter-proc -59..+71, read out of
# XGame_StaticMeshes.usx -- so placing the base by the teleporter's Location put
# all 25uu of it underground.
#
# DM-Deck17 has its floor at -306, the portal pivot at -241 and the base pivot
# at -229, which is where these two come from.
PORTAL_ABOVE_FLOOR = 65.0
BASE_ABOVE_FLOOR = 77.0

# How far a UT3 teleporter stands above its own floor. This is the Translation
# on `Default__UTTeleporter`'s base mesh component, which UT3 uses to drop that
# mesh onto the ground, and DM-Deck's two teleporters corroborate it: the path
# nodes level with them sit 34 and 36 below.
UT3_FLOOR_DROP = 34.0

# DM-Deck17's own teleporter cylinder. UT2004's Teleporter inherits
# NavigationPoint's, which is smaller than a teleporter wants.
COLLISION_RADIUS = 45.0
COLLISION_HEIGHT = 56.25


def sanitize(name):
    out = _SANITIZE.sub("_", name or "")
    if out and out[0].isdigit():
        out = "_" + out
    return out or "Teleporter"


class TeleporterStats:
    def __init__(self):
        self.teleporters = 0
        self.senders = 0
        self.destinations = 0
        self.effects = 0
        self.unpaired = []

    def __str__(self):
        out = "%d teleporters (%d sending, %d destinations)" % (
            self.teleporters, self.senders, self.destinations)
        if self.effects:
            out += "; %d given DM-Deck17's portal meshes (senders only)" % self.effects
        if self.unpaired:
            out += "; %d sending nowhere: %s" % (len(self.unpaired),
                                                 ", ".join(self.unpaired[:3]))
        return out


def convert_teleporters(pkg, scale=1.0, taken=(), with_effect=True, stats=None):
    """Emit UT2004 Teleporters (and their portal meshes); returns (actors, stats)."""
    stats = stats or TeleporterStats()
    out = []
    names = set(taken)
    tags = set()
    sending = []

    def unique(name):
        name = sanitize(name)
        if name in names:
            n = 2
            while "%s_%d" % (name, n) in names:
                n += 1
            name = "%s_%d" % (name, n)
        names.add(name)
        return name

    for export in ordered_exports(pkg, TELEPORTER_CLASSES):
        props, start, _end = read_object_properties(pkg, export)
        if start is None:
            continue
        location = props.get("Location")
        if location is None or not location.value:
            continue
        here = [c * scale for c in location.value]

        properties = [("Location", vec(here))]
        rotation = props.get("Rotation")
        if rotation is not None and rotation.value and any(rotation.value):
            properties.append(("Rotation", rot(rotation.value)))
        url = props.get("URL")
        if url:
            properties.append(("URL", '"%s"' % url))
            sending.append((export.name, str(url)))
            stats.senders += 1
        tag = props.get("Tag")
        if tag and str(tag) not in TELEPORTER_CLASSES:
            properties.append(("Tag", '"%s"' % tag))
            tags.add(str(tag))
            stats.destinations += 1
        properties.extend([("CollisionRadius", "%f" % (COLLISION_RADIUS * scale)),
                           ("CollisionHeight", "%f" % (COLLISION_HEIGHT * scale))])

        name = unique(export.name)
        out.append(Actor("Teleporter", name, properties))
        stats.teleporters += 1

        # Only a teleporter you can walk into is worth drawing a portal on. A
        # destination-only one is somewhere you arrive, and UT3 marks it the
        # same way -- it has a Tag for others to name, and no URL of its own.
        if not with_effect or not url:
            continue
        floor = here[2] - UT3_FLOOR_DROP * scale
        # Neither blocks. DM-Deck17's do, but its teleporter is recessed into a
        # wall where that is harmless; dropped into an arbitrary map, a solid
        # portal can wall the teleporter off and the base's 21uu lip can snag a
        # player walking onto it. They are decoration either way.
        out.append(Actor("StaticMeshActor", unique(name + "_Portal"), [
            ("StaticMesh", "StaticMesh'%s'" % PORTAL_MESH),
            ("Location", vec([here[0], here[1], floor + PORTAL_ABOVE_FLOOR * scale])),
            ("Skins(0)", PORTAL_SKIN),
            ("AmbientGlow", "255"),
            ("bCollideActors", "False"),
            ("bBlockActors", "False"),
            ("bBlockKarma", "False"),
        ]))
        out.append(Actor("StaticMeshActor", unique(name + "_Base"), [
            ("StaticMesh", "StaticMesh'%s'" % BASE_MESH),
            ("Location", vec([here[0], here[1], floor + BASE_ABOVE_FLOOR * scale])),
            ("bCollideActors", "False"),
            ("bBlockActors", "False"),
            ("bBlockKarma", "False"),
        ]))
        stats.effects += 1

    # A teleporter whose URL names nothing in the map sends the player nowhere,
    # which is worth saying rather than leaving to be discovered in game.
    for name, url in sending:
        if url not in tags:
            stats.unpaired.append("%s -> %s" % (name, url))
    return out, stats
