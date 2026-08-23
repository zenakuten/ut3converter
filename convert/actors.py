"""PlayerStart conversion. Pickups and paths live in `convert/pickups.py`.

Both engines put an actor's Location at the centre of its collision cylinder, so
a placed start rests CollisionHeight above the floor -- 80 in UT3
(Default__PlayerStart in CookedPC/Engine.u), 43 in UT2004 (inherited from
NavigationPoint, which PlayerStart does not override). Copying Location straight
across therefore leaves every start hanging 37uu in the air.
"""

import re

from ut2.t3d import Actor, vec, rot
from ut3.objects.level import is_placed_actor
from ut3.props import read_object_properties

UT3_START_HEIGHT = 80.0
UT2_START_HEIGHT = 43.0
GROUND_OFFSET = UT3_START_HEIGHT - UT2_START_HEIGHT

PLAYER_START_CLASSES = {
    "PlayerStart": "PlayerStart",
    "UTPlayerStart": "PlayerStart",
    "UTTeamPlayerStart": "PlayerStart",
    # Warfare's node-associated spawn. UT2004 Onslaught has no separate class --
    # a power node claims the ordinary PlayerStarts nearest to it -- so this is
    # a plain start placed where UT3 put it.
    "UTWarfarePlayerStart": "PlayerStart",
    # Gears of War Reloaded names its spawns after its own modes. Wingman is a
    # five-team two-player mode, so its starts are team-assigned like the
    # others; UT2004 reads the team off each actor's TeamNumber either way, so
    # both convert to a plain PlayerStart and keep whatever team they carry.
    "WarTeamPlayerStart": "PlayerStart",
    "WarTeamPlayerStart_Wingman": "PlayerStart",
}


class ActorStats:
    def __init__(self):
        self.player_starts = 0
        self.team_starts = 0

    def __str__(self):
        out = "%d player starts" % self.player_starts
        if self.team_starts:
            out += " (%d team-assigned)" % self.team_starts
        return out


def _name(base, existing):
    name = re.sub(r"[^A-Za-z0-9_]", "_", base) or "PlayerStart"
    if name[0].isdigit():
        name = "_" + name
    taken = {a.name for a in existing}
    if name not in taken:
        return name
    n = 2
    while "%s_%d" % (name, n) in taken:
        n += 1
    return "%s_%d" % (name, n)


def convert_player_starts(pkg, scale=1.0, stats=None):
    """Convert UT3 PlayerStarts into UT2004 PlayerStarts."""
    stats = stats or ActorStats()
    out = []
    for export in pkg.exports:
        cls = pkg.class_name_of(export)
        if cls not in PLAYER_START_CLASSES or not is_placed_actor(pkg, export):
            continue
        props, start, _end = read_object_properties(pkg, export)
        if start is None:
            continue

        properties = []
        location = props.get("Location")
        if location is not None and location.value:
            x, y, z = location.value
            properties.append(("Location", vec([x * scale, y * scale,
                                                (z - GROUND_OFFSET) * scale])))
        rotation = props.get("Rotation")
        if rotation is not None and rotation.value and any(rotation.value):
            properties.append(("Rotation", rot(rotation.value)))

        team = props.get("TeamIndex", props.get("TeamNumber"))
        if isinstance(team, int) and team >= 0:
            properties.append(("TeamNumber", str(team)))
            stats.team_starts += 1
        if props.get("bPrimaryStart") is True:
            properties.append(("bPrimaryStart", "True"))
        if props.get("bEnabled") is False:
            properties.append(("bEnabled", "False"))

        out.append(Actor(PLAYER_START_CLASSES[cls], _name(export.name, out), properties))
        stats.player_starts += 1
    return out, stats
