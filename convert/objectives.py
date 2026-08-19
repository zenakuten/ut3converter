"""Team game objectives: CTF flag bases, and anything else a game mode scores on.

These map across cleanly because both engines model the same thing -- a
navigation point that a flag stands on, one per team, which the game mode finds
by class. UT2004's `xRedFlagBase` already carries its flag type, objective name
and team shader in its defaults, so placing it is the whole job.

The one number that matters is the height. Both engines put the actor's Location
at the centre of its collision cylinder, and the cylinders differ: UT3's
navigation points are 50 high, UT2004's `xRealCTFBase` is 80 (XGame/
xRealCTFBase.uc:36). Copying Location straight over sinks the base 30uu into the
floor, which on a flag base is enough to bury the stand.
"""

import re

from ut2.t3d import Actor, rot, vec
from ut3.objects.level import is_placed_actor, ordered_exports
from ut3.props import read_object_properties

_SANITIZE = re.compile(r"[^A-Za-z0-9_]")

# UT3 class -> (UT2004 class, that class's CollisionHeight).
OBJECTIVE_CLASSES = {
    "UTCTFRedFlagBase": ("xRedFlagBase", 80.0),
    "UTCTFBlueFlagBase": ("xBlueFlagBase", 80.0),
    "UTCTFBase_Content": (None, 0.0),   # the game's own template, never placed
}

# UT3 navigation points stand this far above the floor.
UT3_NAV_HEIGHT = 50.0


def sanitize(name):
    out = _SANITIZE.sub("_", name or "")
    if out and out[0].isdigit():
        out = "_" + out
    return out or "Objective"


class ObjectiveStats:
    def __init__(self):
        self.objectives = 0
        self.by_class = {}
        self.skipped = 0

    def __str__(self):
        out = "%d game objectives" % self.objectives
        if self.by_class:
            out += " (%s)" % ", ".join("%d %s" % (n, c)
                                       for c, n in sorted(self.by_class.items()))
        return out


def convert_objectives(pkg, scale=1.0, taken=(), stats=None):
    """Emit UT2004 flag bases for a team map; returns (actors, stats)."""
    stats = stats or ObjectiveStats()
    out = []
    names = set(taken)

    for export in ordered_exports(pkg, OBJECTIVE_CLASSES):
        target, height = OBJECTIVE_CLASSES[pkg.class_name_of(export)]
        # UT3 cooks its class defaults into the map; converting one would leave
        # a phantom flag base at the world origin.
        if target is None or not is_placed_actor(pkg, export):
            stats.skipped += 1
            continue
        props, start, _end = read_object_properties(pkg, export)
        if start is None:
            continue
        location = props.get("Location")
        if location is None or not location.value:
            continue
        # Positive means "move down": UT3's floor is Location.Z - UT3_NAV_HEIGHT
        # and the converted actor wants to stand `height` above it.
        drop = UT3_NAV_HEIGHT - height
        here = [location.value[0] * scale, location.value[1] * scale,
                (location.value[2] - drop) * scale]

        properties = [("Location", vec(here))]
        rotation = props.get("Rotation")
        if rotation is not None and rotation.value and any(rotation.value):
            properties.append(("Rotation", rot(rotation.value)))

        name = sanitize(export.name)
        if name in names:
            n = 2
            while "%s_%d" % (name, n) in names:
                n += 1
            name = "%s_%d" % (name, n)
        names.add(name)
        out.append(Actor(target, name, properties))
        stats.objectives += 1
        stats.by_class[target] = stats.by_class.get(target, 0) + 1
    return out, stats
