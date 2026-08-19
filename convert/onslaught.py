"""UT3 Warfare -> UT2004 Onslaught: power cores, nodes and the link graph.

The two game types are the same game. UT3 renamed Onslaught to Warfare and kept
the classes -- `UTOnslaughtPowerCore`, `UTOnslaughtPowernode` -- so cores and
nodes convert one for one, and the power link graph converts as data.

Three things need more than a class swap:

**The link graph.** UT3 keeps it in `UTOnslaughtMapInfo.LinkSetups`, as pairs of
object references. UT2004's `ONSPowerLinkOfficialSetup` holds the same graph as
`PowerLinkSetup { name BaseNode; array<name> LinkedNodes; }` -- resolved against
the actor's *Name* (`PowerCores[z].Name == ...BaseNode`,
Onslaught/ONSOnslaughtGame.uc:238), so the emitted actors are named for their
objective and the setup is written in terms of those names. UT3's pairs are
directed and listed one per link; UT2004 groups them under a base node, so they
are collected by source.

Every core and node gets an entry even when it has no links, which is what
ONS-Tyrant's own setup does. The assignment only runs where a name matches, so
a node omitted from a setup keeps whatever links it had -- listing them all is
what makes switching between setups clear the old graph.

**Cores.** Always the stock `ONSPowerCoreRed`/`Blue`, even alongside the
OnslaughtSpecials2 nodes below: the mod's own core classes carry no mesh, so a
map placed with them has cores that render nothing and cannot be shot.

**Countdown nodes.** UT3's `UTOnslaughtCountdownNode` -- WAR-PowerSurge's "Mine
Node" -- has no stock UT2004 equivalent. OnslaughtSpecials2 supplies one, but
not as a class you can place: `ONSCountdownNode` is a compatibility stub whose
own comment says the behaviour "moved to ONSPowerNodeSpecial and the link
setups", and `CountdownTime` on the node is a plain `var`, settable only through
a supplement. So a countdown node is an ordinary `ONSPowerNodeSpecial` plus an
entry in an `ONSPowerlinkOfficialSetupSupplement` that names it -- and since the
supplement expects every node to be that class, it is the whole set or none.

That makes it opt-in (`--onslaught-specials`), because the cost of guessing
wrong is total and silent: the editor drops an actor whose class it cannot
resolve without printing anything, so on an install without the mod every core
and node simply vanishes on import and the map loads with "Onslaught: Level
doesn't have any PowerCores!". The default is the stock set ONS-Torlan uses --
`ONSPowerCoreRed`/`Blue` and `ONSPowerNodeNeutral` -- which costs only the
countdown timer and the standalone flag.

**Which core is red.** UT3 does not say. Neither of PowerSurge's cores carries
`DefenderTeamIndex`, so both sit at the archetype default and the game derives
the teams. UT2004 needs the answer up front, because red and blue are different
classes. Placement order decides it here -- first core red, second blue -- and
the choice is reported, since swapping it is a one-line edit if it looks wrong.
"""

import math
import re

from ut2.t3d import Actor, ObjectActor, rot, vec
from ut3.objects.level import is_placed_actor, ordered_exports
from ut3.props import read_object_properties

_SANITIZE = re.compile(r"[^A-Za-z0-9_]")

CORE_CLASSES = ("UTOnslaughtPowerCore_Content", "UTOnslaughtPowerCore")
NODE_CLASSES = ("UTOnslaughtPowernode_Content", "UTOnslaughtPowerNode")
COUNTDOWN_CLASSES = ("UTOnslaughtCountdownNode",)
LINK_INFO_CLASSES = ("UTOnslaughtMapInfo",)
TELEPORTER_CLASSES = ("UTOnslaughtNodeTeleporter_Content",
                      "UTOnslaughtNodeTeleporter")

# Stock Onslaught, as ONS-Torlan places them: ONSPowerCoreRed/Blue and
# ONSPowerNodeNeutral (ONSPowerNode with bStartNeutral). These are the default
# because a class the install cannot resolve is dropped *silently* on T3D
# import -- no warning, the actor is simply absent -- and the game then fails
# with "Onslaught: Level doesn't have any PowerCores!".
RED_CORE = "ONSPowerCoreRed"
BLUE_CORE = "ONSPowerCoreBlue"
NODE = "ONSPowerNodeNeutral"
# Also the name the actors are given, not just their class. On save the editor
# fills LevelSummary.ExtraInfo with "LinkSetups=<name>;<name>" by scanning the
# level for actors whose *name* contains this string (Editor/Src/UnEdSrv.cpp:
# 2874, a substring test on GetName(), not a class test). That summary is what
# the map list reads to mark a map as having several setups and to let one be
# picked before the match -- so a setup actor named anything else is invisible
# outside the in-game link designer, which iterates by class instead.
LINK_SETUP = "ONSPowerLinkOfficialSetup"

# OnslaughtSpecials2, which only some installs have. Only the node class is ever
# used -- the core variants draw no mesh. Its node class is what
# makes a countdown node possible: CountdownTime is a plain var, settable only
# through a supplement, and the supplement's ForcedCloseActors expects every
# node to be the special class, so the set is all-or-nothing.
SPECIAL_RED_CORE = "ONSPowerCoreSpecialRed"
SPECIAL_BLUE_CORE = "ONSPowerCoreSpecialBlue"
SPECIAL_NODE = "ONSPowerNodeSpecial"
SUPPLEMENT = "ONSPowerlinkOfficialSetupSupplement"
# Stock Onslaught. Both engines place these near a node and let the node claim
# them -- `ONSPowerCore` takes every pad it is ClosestTo (ONSPowerCore.uc:228) --
# so no binding has to be written, only the position.
TELEPORT_PAD = "ONSTeleportPad"

# UT3 stands a node teleporter this far above its floor: the FloorMesh component
# translates down by it. ONSTeleportPad's own mesh (VMStructures BaseNodeSM)
# has its pivot at the base, so the pad goes on the floor itself.
UT3_TELEPORTER_DROP = 34.0

# Height correction is a question of where each engine's *mesh pivot* sits, not
# of collision, and the two differ for a node and for a core.
#
# Node: UT3 translates its base mesh (S_GP_Ons_Power_Node_Base) down by 34, the
# same offset a node teleporter's floor mesh uses, so the floor is 34 below the
# actor -- not the 30 its collision cylinder would suggest. UT2004 draws
# powerNodeBaseSM, whose pivot is at its base (bounds Z 0..15 in VMStructures),
# through ONSPowerNode's PrePivot=(Z=25), so its plate rests on the floor when
# Location is floor plus 25. Net: 9 down.
UT3_NODE_HEIGHT = 34.0
UT2_NODE_PREPIVOT = 25.0

# ONSPowerNode centres its touch cylinder on Location with this half-height
# (ONSPowerNode.uc defaults, alongside CollisionRadius 160). Resting the *plate*
# on the floor leaves the bottom 5 units of that cylinder inside it, so the node
# is captured over a shorter span than it looks -- WAR-Torlan's Prime had to be
# jumped on. Standing the node on whichever of the two reaches lower puts both
# the mesh and the cylinder clear of the floor.
UT2_NODE_COLLISION_HEIGHT = 30.0

# ...and PrePivot is in *mesh* space, applied before the scale:
# LocalToWorld translates by -PrePivot and only then multiplies by
# DrawScale3D * DrawScale (Engine/Inc/AActor.h:68). ONSPowerNode draws at
# DrawScale 2, so its plate hangs 50 below Location, not 25 -- while
# CollisionHeight is already world units and stays 30. Missing the scale is what
# left every node's base under the surface it stands on.
UT2_NODE_DRAW_SCALE = 2.0
NODE_REST_OFFSET = max(UT2_NODE_PREPIVOT * UT2_NODE_DRAW_SCALE,
                       UT2_NODE_COLLISION_HEIGHT)

# How far out from a node, and how far above or below its floor, scenery counts
# as part of the node's pad rather than as level geometry. The radius is the
# node's own touch cylinder (160) with room for a pad laid slightly wider --
# WAR-PowerSurge's are 316 square, so their corners reach 223. The band is kept
# tight: the same maps put a floor 470 below a core, and that has to survive.
# The extra setup that links UT3's standalone nodes in, and how many neighbours
# each gets. Two makes a centrally placed node a link in the chain rather than a
# dead end.
ALL_NODES_SETUP = "AllNodes"
LINKS_PER_ADOPTED_NODE = 2

# What a finished countdown does. OnslaughtSpecials2 fires the supplement's
# Red/BlueCompletedEvent and stops there -- the damage itself is map scripting,
# which is how ONS-Tyrant does it: a ScriptedTrigger waits on the event, hits
# the enemy core with ACTION_DamagePowerNode, and loops back to wait again.
# Tyrant uses 901 against a core whose DamageCapacity is 4500, so five
# completed countdowns take a core down.
COUNTDOWN_DAMAGE = 901
WAIT_ACTION = "ACTION_WaitForEvent"
DAMAGE_ACTION = "ACTION_DamagePowerNode"
GOTO_ACTION = "ACTION_GotoAction"
SCRIPTED_TRIGGER = "ScriptedTrigger"

PAD_RADIUS = 230.0
PAD_BAND = 60.0

# The rotation passes through untouched, roll included: WAR-PowerSurge hangs
# its cores upside down on purpose. A centre-pivoted mesh is symmetric about
# Location, so the rise below still rests it correctly either way up.
#
# Core: UT2004's CoreDivided is pivoted at its *centre* (bounds Z -191.4..190.0
# in VMStructures), so a core standing on the ground sits 191 above it -- which
# is what ONS-Torlan's two cores measure above the BSP under them, 188 and 192.
# UT3 places its core at floor level instead, so the core has to come up by the
# half-height or it sinks: the top half shows above ground and the energy
# effect, spawned at Location-20 (ONSPowerCore.uc:720), surfaces below the mesh.
UT2_CORE_PIVOT_HEIGHT = 191.4

# Cores are left at their stock size on purpose. UT3's core is a custom mesh
# built for this map's core room and much bigger than UT2004's -- measured from
# the two meshes' own bounds, SK_GP_Ons_Power_Core is 2078 x 2166 x 1379 against
# CoreDivided's 405 x 405 x 381 -- so a converted core does leave the room
# around it looking bare. Scaling CoreDivided up to match (x3.62 on height, the
# only axis that can match, since width would want x5.1) was tried and looked
# worse: it is a different shape, and a big one reads as wrong rather than as
# full. The room being oversized is the lesser of the two.
UT2_CORE_RADIUS = 120.0
UT2_CORE_HEIGHT = 150.0

# UT3 vehicle factory -> (UT2004 class, note). A note means the two are not the
# same vehicle and the substitution is worth reporting. UT2004's names are
# internal: ONSHoverBike is the Manta, ONSRV the Scorpion, ONSPRV the Hellbender
# and ONSHoverTank the Goliath, so those four are exact.
VEHICLE_FACTORIES = {
    "UTVehicleFactory_Manta": ("ONSHoverCraftFactory", None),
    "UTVehicleFactory_Scorpion": ("ONSRVFactory", None),
    "UTVehicleFactory_HellBender": ("ONSPRVFactory", None),
    "UTVehicleFactory_Goliath": ("ONSTankFactory", None),
    "UTVehicleFactory_Raptor": ("ONSAttackCraftFactory", None),
    "UTVehicleFactory_Leviathan": ("ONSMASFactory", None),
    # UT3-only vehicles. The Paladin is a one-seat shield tank with no UE2
    # counterpart; a Goliath keeps the spawn point doing something tank-shaped
    # rather than leaving a hole in the vehicle balance.
    "UTVehicleFactory_Paladin": ("ONSTankFactory", "Paladin -> Goliath"),
    "UTVehicleFactory_Cicada": ("ONSAttackCraftFactory", "Cicada -> Raptor"),
    "UTVehicleFactory_SPMA": ("ONSMASFactory", "SPMA -> Leviathan"),
    # Turrets, and in UT2004 a placed pawn rather than a factory. The plain
    # UT3 turret is the same thing as UT2004's plasma turret, so it converts
    # without a note; the shielded variants are the ones being stood in for.
    "UTVehicleFactory_Turret": ("ONSManualGunPawn", None),
    "UTVehicleFactory_ShieldedTurret_Rocket": ("ONSManualGunPawn",
                                               "rocket turret -> plasma turret"),
    "UTVehicleFactory_ShieldedTurret": ("ONSManualGunPawn",
                                        "shielded turret -> plasma turret"),
}

# UT3 states no countdown length on the node -- the class default carries it and
# the cooker elides it. 120s is OnslaughtSpecials2's own documented example and
# a sane mine-node timer; `--countdown-time` overrides.
DEFAULT_COUNTDOWN_TIME = 120

# How close a factory already has to be to the objective its ONSObjectiveOverride
# names for the placement to be taken as deliberate, and how far in front of the
# objective it is put otherwise. The offset clears both collision cylinders --
# the Leviathan's radius is 260 (ONSMobileAssaultStation.uc:370) and a core's is
# 120 (ONSPowerCore.uc:1424) -- while staying far nearer that objective than any
# other, which is all UT2004's ClosestTo binding asks. See _override_location.
# How far above its stated spot a vehicle factory is placed. UT2004 drops a
# spawning vehicle through a static mesh it is resting exactly on often enough
# that the standard mapper's fix is to lift the factory clear and let the
# vehicle fall the last bit -- WAR-Torlan's two Raptors, beside each core, do it
# on meshes with nothing wrong with them. Small enough that a ground vehicle
# just settles.
DEFAULT_VEHICLE_RISE = 32.0

# ...and how far the bigger ones need, which is their own CollisionHeight: a
# vehicle spawned with its centre on the surface starts half inside it, and the
# taller it is the further in that reaches. WAR-Torlan's Raptors kept falling
# through at 32 because ONSAttackCraft is 70 tall (ONSAttackCraft.uc). Values
# are the UT2004 defaults for what each factory spawns; anything not listed
# leaves its CollisionHeight to a parent class and takes the flat rise.
VEHICLE_RISE = {
    "ONSAttackCraftFactory": 70.0,      # Raptor
    "ONSTankFactory": 60.0,             # Goliath
    "ONSMASFactory": 60.0,              # Leviathan
    "ONSRVFactory": 40.0,               # Scorpion
}

OVERRIDE_NEAR = 1024.0
OVERRIDE_OFFSET = 600.0


def sanitize(name):
    out = _SANITIZE.sub("", name or "")
    if out and out[0].isdigit():
        out = "_" + out
    return out or "Node"


class OnslaughtStats:
    def __init__(self):
        self.cores = 0
        self.nodes = 0
        self.countdown = 0
        self.links = 0
        self.standalone = 0
        self.red = None
        self.blue = None
        self.unlinked = []
        self.vehicles = 0
        self.by_vehicle = {}
        self.substitutions = []
        self.rehomed = []
        self.rested = []
        self.unmapped = {}
        self.teleport_pads = 0
        self.specials = False
        self.setups = []
        self.adopted = []
        self.countdown_triggers = 0

    def __str__(self):
        out = "%d power cores, %d nodes" % (self.cores, self.nodes)
        if self.countdown:
            out += " (%d countdown)" % self.countdown
        out += ", %d power links" % self.links
        if self.adopted:
            out += ("; %s left out of every setup, so an extra \"%s\" setup links "
                    "%s in" % (", ".join(self.adopted), ALL_NODES_SETUP,
                               "them" if len(self.adopted) > 1 else "it"))
        if len(self.setups) > 1:
            out += " across %d setups (%s)" % (len(self.setups), ", ".join(self.setups))
        if self.standalone:
            out += "; %d standalone" % self.standalone
        if self.specials:
            out += "; OnslaughtSpecials2 classes (that mod must be installed)"
            if self.countdown_triggers:
                out += (", with %d scripted trigger(s) damaging the enemy core "
                        "when a countdown finishes" % self.countdown_triggers)
        elif self.countdown or self.standalone:
            out += ("; stock classes, so the countdown timer and standalone flag "
                    "are dropped -- and a node left unlinked is disabled and "
                    "hidden outright (ONSPowerCore.uc:130), so it only appears "
                    "in the setup that links it (--onslaught-specials keeps the "
                    "countdown instead)")
        if self.unlinked:
            out += "; %d in no link setup: %s" % (len(self.unlinked),
                                                  ", ".join(self.unlinked[:3]))
        if self.vehicles:
            out += "\n  %d vehicle factories (%s)" % (
                self.vehicles, ", ".join("%d %s" % (n, c)
                                         for c, n in sorted(self.by_vehicle.items())))
        if self.rested:
            out += ("; %d node(s) lifted out of the brush they stand on (%s)"
                    % (len(self.rested),
                       ", ".join("%s by %d" % r for r in self.rested[:3])))
        if self.rehomed:
            out += ("; %d factory(ies) UT3 parked off-map moved beside the "
                    "objective they name (%s)"
                    % (len(self.rehomed),
                       ", ".join("%s -> %s" % r for r in self.rehomed[:2])))
        if self.substitutions:
            out += "; substituted " + ", ".join(sorted(set(self.substitutions)))
        if self.unmapped:
            out += "; no UT2004 equivalent for " + ", ".join(
                "%s x%d" % (c, n) for c, n in sorted(self.unmapped.items()))
        if self.teleport_pads:
            out += "\n  %d node teleport pads" % self.teleport_pads
        return out


def _props(pkg, export):
    props, start, _end = read_object_properties(pkg, export)
    return props if start is not None else None


def setup_labels(setups):
    """A UT2004 SetupName per UT3 link setup: exactly one Default, all distinct.

    UT2004 picks a setup by name from the URL and falls back to the one called
    "Default" (Onslaught/ONSOnslaughtGame.uc:188), so a map with no such setup
    would start with no link graph at all. UT3 does not promise the name: three
    of its maps here call the first one something else.

    Renaming the first setup unconditionally is what got WAR-Torlan wrong. Its
    setups are `short, Classic, Default, TwoFronts` -- it already has a Default,
    two positions along -- so `short` was renamed on top of it and the map
    offered "Default" twice, one of which could never be selected. The rename
    only happens when nothing else claims the name, and whatever is left is
    deduplicated, since a name that repeats is a setup the player cannot reach.
    """
    labels = []
    for position, entry in enumerate(setups):
        name = (str(entry[0]) if entry[0] else "").strip()
        labels.append(name or "Setup%d" % (position + 1))
    if labels and not any(l.lower() == "default" for l in labels):
        labels[0] = "Default"
    seen = set()
    for i, label in enumerate(labels):
        if label.lower() in seen:
            n = 2
            while ("%s%d" % (label, n)).lower() in seen:
                n += 1
            labels[i] = "%s%d" % (label, n)
        seen.add(labels[i].lower())
    return labels


def read_link_setups(pkg):
    """[(setup name, links, standalone)] from UTOnslaughtMapInfo.

    `links` is [(from_index, to_index)] and `standalone` a set of indices, both
    as export indices. Both engines let a map ship several named graphs and
    pick between them at match start, so all of them convert -- UT2004's own
    ONS-Torlan carries two.
    """
    out = []
    for export in pkg.exports:
        if pkg.class_name_of(export) not in LINK_INFO_CLASSES:
            continue
        props = _props(pkg, export)
        if props is None:
            continue
        setups = props.get("LinkSetups")
        if setups is None or not len(setups):
            continue
        try:
            entries = setups.as_props()
        except (ValueError, IndexError):
            continue
        for entry in entries:
            name, links, standalone = None, [], set()
            for key, _idx, _type, value in entry:
                if key == "SetupName":
                    name = str(value).strip() or None
                elif key == "NodeLinks" and hasattr(value, "as_props"):
                    for link in value.as_props():
                        source = link.get("FromNode")
                        target = link.get("ToNode")
                        if (source is not None and target is not None
                                and source.is_export and target.is_export):
                            links.append((source.export.index, target.export.index))
                elif key == "StandaloneNodes" and hasattr(value, "as_objects"):
                    for ref in value.as_objects():
                        if ref.is_export:
                            standalone.add(ref.export.index)
            out.append((name, links, standalone))
        break
    return out


# How far above or below its own plate a node will look for the floor it should
# be standing on, and how nearly level a polygon has to be to count as floor.
REST_WINDOW = 96.0
REST_LEVEL = 0.9


def _top_at(brushes, x, y, low, high):
    """The highest additive brush surface under (x, y) within [low, high]."""
    best = None
    for brush in brushes:
        if getattr(brush, "csg", "CSG_Add") != "CSG_Add":
            continue
        origin = (0.0, 0.0, 0.0)
        for key, value in brush.properties:
            if key == "Location":
                nums = re.findall(r"(-?\d+\.?\d*)", value)
                if len(nums) >= 3:
                    origin = tuple(float(n) for n in nums[:3])
                break
        for poly in brush.polygons:
            if poly.normal[2] < REST_LEVEL:
                continue
            points = [(v[0] + origin[0], v[1] + origin[1], v[2] + origin[2])
                      for v in poly.vertices]
            if len(points) < 3:
                continue
            z = sum(pt[2] for pt in points) / len(points)
            if not (low <= z <= high):
                continue
            if not _contains(points, x, y):
                continue
            if best is None or z > best:
                best = z
    return best


def _contains(points, x, y):
    """Is (x, y) inside this polygon, seen from above?"""
    inside = False
    count = len(points)
    for i in range(count):
        ax, ay = points[i][0], points[i][1]
        bx, by = points[(i + 1) % count][0], points[(i + 1) % count][1]
        if (ay > y) != (by > y):
            span = by - ay
            if span and x < ax + (y - ay) / span * (bx - ax):
                inside = not inside
    return inside


def rest_on_brushes(actors, brushes, stats=None, rise=0.0):
    """Raise a node whose base is buried in the brush it stands on.

    UT2004 draws a node's plate at `Location - PrePivot.Z` and centres its touch
    cylinder on Location (CollisionHeight 30), so a node whose plate is under the
    floor has its base hidden inside the geometry and its cylinder half in solid
    -- which is what makes it unreachable rather than merely ugly.

    UT3 does not line these up for us. WAR-Torlan's two Prime nodes each stand on
    their own platform brush topped at -7880, yet UT3 puts one at -7858 and the
    other at -7850, so applying the mesh-pivot correction faithfully leaves their
    plates 12 and 4 units *inside* the platform. UT3 gets away with it because
    its node model is bigger than the hole; UE2's is not.

    Only a node already close to a level additive surface is moved, and only
    upwards, so a node standing on terrain or on a mesh -- which is most of them
    -- is left exactly where the pivot correction put it.
    """
    moved = []
    for actor in actors:
        if not actor.cls.startswith("ONSPowerNode"):
            continue
        location = None
        for i, (key, value) in enumerate(actor.properties):
            if key == "Location":
                nums = re.findall(r"(-?\d+\.?\d*)", value)
                if len(nums) >= 3:
                    location = (i, [float(n) for n in nums[:3]])
                break
        if location is None:
            continue
        i, (x, y, z) = location[0], location[1]
        plate = z - NODE_REST_OFFSET
        top = _top_at(brushes, x, y, plate - REST_WINDOW, plate + REST_WINDOW)
        if top is None or top <= plate:
            continue
        actor.properties[i] = ("Location", vec((x, y, top + NODE_REST_OFFSET + rise)))
        moved.append((actor.name, round(top + NODE_REST_OFFSET + rise - z)))
    if stats is not None:
        stats.rested = moved
    return moved


def node_pads(pkg):
    """Actors making up the little pad a UT3 node stands on.

    A UT3 node is placed on scenery: WAR-PowerSurge stands every one of its
    nodes on a vent mesh with a flat 316x316x20 BlockingVolume laid over it, so
    walking onto the node is smooth. UT2004's ONSPowerNode brings its own base
    plate and a 160-radius touch cylinder that starts at the node's floor, so
    that pad lands *inside* the cylinder and holds the player above the region
    that triggers the node -- WAR-PowerSurge's West Tank could only be taken by
    jumping onto it.

    The meshes stay (they are what the ground looks like) but lose their
    collision; the pad volume goes. Both are recognised by position rather than
    by name, so this is not specific to one map: within a node's radius, and
    within PAD_BAND of the node's own floor. Cores are left alone -- theirs is
    a whole building, not a pad.

    A mesh only counts when a pad volume covers the same node. That volume is
    UT3's own statement that the scenery under it is too bumpy to walk on, so
    where there is none the mesh is the real floor and keeps its collision --
    WAR-PowerSurge's countdown node stands on four walkway pipes with no volume
    over them, and stripping those would drop the player through the level.

    Returns (mesh names, volume names).
    """
    floors = []
    for export in ordered_exports(pkg, NODE_CLASSES + COUNTDOWN_CLASSES):
        if not is_placed_actor(pkg, export):
            continue
        props = _props(pkg, export)
        if props is None:
            continue
        location = props.get("Location")
        if location is None or not location.value:
            continue
        x, y, z = location.value
        floors.append((x, y, z - UT3_NODE_HEIGHT))

    def node_at(x, y, z):
        for i, (fx, fy, fz) in enumerate(floors):
            if (x - fx) ** 2 + (y - fy) ** 2 <= PAD_RADIUS ** 2 \
                    and abs(z - fz) <= PAD_BAND:
                return i
        return None

    volumes, padded, candidates = set(), set(), []
    for export in pkg.exports:
        cls = pkg.class_name_of(export)
        if cls not in ("StaticMeshActor", "InterpActor", "BlockingVolume"):
            continue
        if not is_placed_actor(pkg, export):
            continue
        props = _props(pkg, export)
        if props is None:
            continue
        location = props.get("Location")
        if location is None or not location.value:
            continue
        x, y, z = location.value
        if cls == "BlockingVolume":
            # Measured at the surface the player stands on, not the centre.
            top = _brush_top(pkg, props, location.value)
            if top is None:
                continue
            node = node_at(x, y, top)
            if node is not None:
                volumes.add(export.name)
                padded.add(node)
        else:
            node = node_at(x, y, z)
            if node is not None:
                candidates.append((node, export.name))
    meshes = {name for node, name in candidates if node in padded}
    return meshes, volumes


def _brush_top(pkg, props, location):
    """World Z of the highest vertex of a volume's brush."""
    from ut3.objects.model import find_polys, read_polys

    brush = props.get("Brush")
    if brush is None or not brush.is_export:
        return None
    polys_export = find_polys(pkg, brush.export)
    if polys_export is None:
        return None
    try:
        polys = read_polys(pkg, polys_export)
    except (ValueError, IndexError, KeyError):
        return None
    if not polys:
        return None
    pivot = props.get("PrePivot")
    pivot = pivot.value if pivot is not None and pivot.value else (0.0, 0.0, 0.0)
    return max(v[2] for q in polys for v in q.vertices) - pivot[2] + location[2]


def convert_onslaught(pkg, scale=1.0, taken=(), countdown_time=DEFAULT_COUNTDOWN_TIME,
                      vehicle_rise=DEFAULT_VEHICLE_RISE,
                      specials=False, countdown_damage=COUNTDOWN_DAMAGE,
                      stats=None):
    """Emit cores, nodes, the link setup and any countdown supplement.

    `specials` switches to the OnslaughtSpecials2 classes, which carry the
    countdown node and standalone flags. Off by default: those classes are
    absent from a stock install and the import drops them without a word.
    """
    stats = stats or OnslaughtStats()
    stats.specials = specials
    # Only the nodes switch. OnslaughtSpecials2's core variants draw no mesh of
    # their own -- UT3's core is an animated thing the mod leaves to the map --
    # so a map using them has cores that cannot be seen or shot. The stock cores
    # bring VMStructures' CoreDivided with them and the supplement never names a
    # core, so mixing costs nothing.
    red_core, blue_core = RED_CORE, BLUE_CORE
    node_class = SPECIAL_NODE if specials else NODE
    out = []
    names = set(taken)
    tags = {}          # UT3 export index -> UT2004 Tag
    positions = {}     # UT3 export index -> emitted world position
    emitted = {}       # UT3 export index -> emitted actor

    def unique(base):
        name = sanitize(base)
        if name in names:
            n = 2
            while "%s_%d" % (name, n) in names:
                n += 1
            name = "%s_%d" % (name, n)
        names.add(name)
        return name

    setups = read_link_setups(pkg)
    # Everything below that is not per-setup -- which node is standalone, which
    # is a countdown node -- comes from the map's first setup, the one it ships
    # as its default.
    links = setups[0][1] if setups else []
    standalone = setups[0][2] if setups else set()

    wanted = CORE_CLASSES + NODE_CLASSES + COUNTDOWN_CLASSES
    cores = []
    for export in ordered_exports(pkg, wanted):
        cls = pkg.class_name_of(export)
        if not is_placed_actor(pkg, export):
            continue
        props = _props(pkg, export)
        if props is None:
            continue
        location = props.get("Location")
        if location is None or not location.value:
            continue

        is_core = cls in CORE_CLASSES
        if is_core:
            drop = -UT2_CORE_PIVOT_HEIGHT
        else:
            # NODE_REST_OFFSET, not the plate's PrePivot: the touch cylinder
            # reaches lower than the mesh does, and a node has to clear the
            # floor by whichever part reaches lowest or it is captured over a
            # shorter span than it looks. Sinking every node by the extra 5 is
            # what made WAR-Torlan's have to be jumped on.
            drop = UT3_NODE_HEIGHT - NODE_REST_OFFSET
        here = [location.value[0] * scale, location.value[1] * scale,
                (location.value[2] - drop) * scale]

        # The objective name is what players and the HUD call it, and it makes a
        # far better tag than "UTOnslaughtPowernode_Content_2". Cores carry no
        # objective name, so they are named for their team -- which also makes
        # the emitted link setup readable.
        label = str(props.get("ObjectiveName") or "").strip()
        if is_core:
            base = "RedCore" if not cores else "BlueCore"
        else:
            base = label or export.name
        name = unique(base)
        tag = name

        positions[export.index] = here
        properties = [("Location", vec(here)), ("Tag", '"%s"' % tag)]
        rotation = props.get("Rotation")
        if rotation is not None and rotation.value and any(rotation.value):
            properties.append(("Rotation", rot(rotation.value)))
        if label:
            properties.append(("ObjectiveName", '"%s"' % label))

        if is_core:
            cores.append((export.index, name, properties))
            continue

        actor = Actor(node_class, name, properties)
        out.append(actor)
        emitted[export.index] = actor
        tags[export.index] = tag
        stats.nodes += 1
        if cls in COUNTDOWN_CLASSES:
            actor.countdown = True
            stats.countdown += 1

    # Red first, blue second: UT3 states no team on either, so order is all
    # there is, and the two are different classes in UT2004.
    for i, (index, name, properties) in enumerate(cores):
        team_class = red_core if i == 0 else blue_core
        actor = Actor(team_class, name, properties)
        out.append(actor)
        emitted[index] = actor
        tags[index] = name
        stats.cores += 1
        if i == 0:
            stats.red = name
        elif i == 1:
            stats.blue = name

    # UT3 lists one directed pair per link; UT2004 groups them under a base.
    # One ONSPowerLinkOfficialSetup per UT3 setup: the game picks between them
    # by name from the URL, falling back to the one called "Default"
    # (ONSOnslaughtGame.uc:188).
    grouped = {}
    labels = setup_labels(setups)
    for position, (setup_name, setup_links, _standalone) in enumerate(setups):
        by_base = {}
        for source, target in setup_links:
            if source not in tags or target not in tags:
                continue
            by_base.setdefault(tags[source], []).append(tags[target])
            stats.links += 1
        if not tags:
            continue
        if position == 0:
            grouped = by_base
        label = labels[position]
        setup = Actor(LINK_SETUP, unique(LINK_SETUP),
                      [("SetupName", '"%s"' % label), ("DrawScale", "4.000000")])
        for i, tag in enumerate(sorted(tags.values())):
            linked = by_base.get(tag)
            if linked:
                entry = "(BaseNode=\"%s\",LinkedNodes=(%s))" % (
                    tag, ",".join('"%s"' % n for n in linked))
            else:
                entry = '(BaseNode="%s")' % tag
            setup.properties.append(("LinkSetups(%d)" % i, entry))
        out.append(setup)
        stats.setups.append(label)

    # A node UT3 keeps out of the graph can never be taken in stock Onslaught:
    # it is adjacent to nothing, so no team is ever allowed to attack it, and it
    # sits on the map as scenery. UT3 gets away with it because its countdown
    # node has a mechanic of its own; UT2004 has none, so the node is simply
    # dead. WAR-PowerSurge's Mine Node, in the centre of the map, is one.
    #
    # Rather than change the graph UT3 shipped, the default setup is left exactly
    # as the map has it and one extra setup is added with those nodes linked in,
    # each to its two nearest neighbours -- which for a node placed centrally is
    # the pair it sits between. Picking it is a menu choice at match start.
    if tags:
        adopted = sorted(
            index for index in tags
            if index in positions
            and (index in standalone or tags[index] not in
                 (set(grouped) | {t for v in grouped.values() for t in v})))
        if adopted:
            extra = {base: list(links) for base, links in grouped.items()}
            for index in adopted:
                x, y, z = positions[index]
                others = [(other, positions[other]) for other in tags
                          if other != index and other in positions]
                others.sort(key=lambda o: (o[1][0] - x) ** 2 + (o[1][1] - y) ** 2
                            + (o[1][2] - z) ** 2)
                for other, _p in others[:LINKS_PER_ADOPTED_NODE]:
                    extra.setdefault(tags[index], []).append(tags[other])
            setup = Actor(LINK_SETUP, unique(LINK_SETUP),
                          [("SetupName", '"%s"' % ALL_NODES_SETUP),
                           ("DrawScale", "4.000000")])
            for i, tag in enumerate(sorted(tags.values())):
                linked = extra.get(tag)
                if linked:
                    entry = "(BaseNode=\"%s\",LinkedNodes=(%s))" % (
                        tag, ",".join('"%s"' % n for n in linked))
                else:
                    entry = '(BaseNode="%s")' % tag
                setup.properties.append(("LinkSetups(%d)" % i, entry))
            out.append(setup)
            stats.setups.append(ALL_NODES_SETUP)
            stats.adopted = [tags[i] for i in adopted]

    # A standalone node is meant to sit outside the network, so its absence
    # from the graph is the point rather than something to warn about.
    linked_tags = set(grouped) | {t for targets in grouped.values() for t in targets}
    for index, tag in tags.items():
        if tag not in linked_tags and index not in standalone:
            stats.unlinked.append(tag)

    # Countdown length and standalone-ness are not properties of the node in
    # UT2004 -- only a supplement can set them. The flag set is ONS-Tyrant's,
    # read out of its working countdown node: a standalone node is permanently
    # isolated, so it must be told never to shield (it would otherwise sit
    # invulnerable) and to keep its vehicles, and a countdown node needs the
    # two flags that make the timer mean something.
    settings = []
    countdowns = []
    for index, actor in emitted.items():
        entry = []
        if index in standalone:
            entry.extend(["bIsStandalone=True", "bNeverShielded=True",
                          "bNotShieldedIfIsolated=True",
                          "bKeepVehiclesWhenDestroyed=True"])
            stats.standalone += 1
        if getattr(actor, "countdown", False):
            # CountdownTime is a byte in OnslaughtSpecials2 -- ONS-Tyrant uses
            # 35 -- so a longer timer than 255s cannot be expressed.
            entry.extend(["CountdownTime=%d" % max(1, min(countdown_time, 255)),
                          "bCountdownDamagesEnemyCore=True",
                          "bStopCountdownIfIsolated=True"])
            # The flag alone does nothing; the mod only fires these events, and
            # a ScriptedTrigger has to be there to act on them.
            red_event = "%sRedDone" % actor.name
            blue_event = "%sBlueDone" % actor.name
            entry.extend(['RedCompletedEvent="%s"' % red_event,
                          'BlueCompletedEvent="%s"' % blue_event])
            countdowns.append((actor, red_event, blue_event, positions.get(index)))
        if not entry or not specials:
            continue
        # An edfindable reference to another actor in the same map.
        entry.insert(0, "Node=%s'MyLevel.%s'" % (actor.cls, actor.name))
        settings.append("(%s)" % ",".join(entry))
    if settings:
        supplement = Actor(SUPPLEMENT, unique("PowerLinkSupplement"),
                           [("SetupName", '"Default"'), ("DrawScale", "6.000000")])
        for i, entry in enumerate(settings):
            supplement.properties.append(("PowernodeSettings(%d)" % i, entry))
        out.append(supplement)

    # One trigger per team per countdown node: wait for that team's completed
    # event, damage the *other* team's core, then loop back to waiting.
    for actor, red_event, blue_event, position in countdowns:
        for event, target in ((red_event, stats.blue), (blue_event, stats.red)):
            if not target:
                continue
            base = unique("%sCountdown" % actor.name)
            trigger = ObjectActor(SCRIPTED_TRIGGER, base, [
                (WAIT_ACTION, "%sWait" % base, [("ExternalEvent", '"%s"' % event)]),
                (DAMAGE_ACTION, "%sDamage" % base,
                 [("NodeTag", '"%s"' % target),
                  ("DamageAmount", "%d" % countdown_damage)]),
                (GOTO_ACTION, "%sLoop" % base, [("ActionNumber", "0")]),
            ], "Actions")
            if position:
                trigger.properties.append(("Location", vec(position)))
            out.append(trigger)
            stats.countdown_triggers += 1

    out.extend(_vehicle_factories(pkg, scale, unique, stats, vehicle_rise))
    out.extend(_teleport_pads(pkg, scale, unique, stats))
    return out, stats


def _teleport_pads(pkg, scale, unique, stats):
    """UT3 node teleporters as UT2004 teleport pads."""
    out = []
    for export in ordered_exports(pkg, TELEPORTER_CLASSES):
        if not is_placed_actor(pkg, export):
            continue
        props = _props(pkg, export)
        if props is None:
            continue
        location = props.get("Location")
        if location is None or not location.value:
            continue
        here = [location.value[0] * scale, location.value[1] * scale,
                (location.value[2] - UT3_TELEPORTER_DROP) * scale]
        properties = [("Location", vec(here))]
        rotation = props.get("Rotation")
        if rotation is not None and rotation.value and any(rotation.value):
            properties.append(("Rotation", rot(rotation.value)))
        out.append(Actor(TELEPORT_PAD, unique("TeleportPad"), properties))
        stats.teleport_pads += 1
    return out


def _override_location(pkg, props, location):
    """Where a factory belongs when UT3 parks it and names an objective instead.

    UE2 has no equivalent of `ONSObjectiveOverride`, but it does not need one:
    UT2004 binds a factory to whichever objective is nearest, full stop --
    `FindCloseActors` keeps every ONSVehicleFactory whose `ClosestTo(A) == self`
    (Onslaught/ONSPowerCore.uc:213), then Activates or Deactivates it as that
    objective changes hands (:882, :782). There is no radius to fall outside of.
    So stating the association *is* placing the factory next to the objective.

    That matters because UT3 sometimes does not place these at all. Both of
    WAR-Serenity's Leviathan factories sit at (-4, 554.9, 159) and (4, 554.9,
    159) -- eight units apart at the world origin -- because they are
    `bDisabled` until Kismet enables them, and their real home is the core each
    one's override names. Converted literally that is two Leviathans stacked in
    the void, belonging to no objective and never activating.

    Only a factory that is *not already* beside its objective is moved, so a map
    that states an override and places the factory properly is left alone.
    """
    ref = props.get("ONSObjectiveOverride")
    if ref is None or ref.is_null or not ref.is_export:
        return None, None
    target = _props(pkg, ref.export)
    if target is None:
        return None, None
    anchor = target.get("Location")
    if anchor is None or not anchor.value:
        return None, None
    anchor = list(anchor.value)
    offset = math.sqrt(sum((location[i] - anchor[i]) ** 2 for i in range(3)))
    if offset <= OVERRIDE_NEAR:
        return None, None
    # Out in front of the objective, along the way the factory faces, which is
    # the direction UT3 has its vehicle drive out.
    yaw = 0.0
    rotation = props.get("Rotation")
    if rotation is not None and rotation.value:
        yaw = rotation.value[1] * math.pi / 32768.0
    return ([anchor[0] + math.cos(yaw) * OVERRIDE_OFFSET,
             anchor[1] + math.sin(yaw) * OVERRIDE_OFFSET,
             anchor[2]], ref.export.name)


def _vehicle_factories(pkg, scale, unique, stats, rise=DEFAULT_VEHICLE_RISE):
    """UT3 vehicle factories as their UT2004 counterparts."""
    out = []
    for export in ordered_exports(pkg, tuple(VEHICLE_FACTORIES)):
        cls = pkg.class_name_of(export)
        if not is_placed_actor(pkg, export):
            continue
        target, note = VEHICLE_FACTORIES[cls]
        props = _props(pkg, export)
        if props is None:
            continue
        location = props.get("Location")
        if location is None or not location.value:
            continue
        location = list(location.value)
        moved, objective = _override_location(pkg, props, location)
        if moved is not None:
            location = moved
            stats.rehomed.append((export.name, objective))
        lift = max(rise, VEHICLE_RISE.get(target, 0.0))
        placed = [location[0] * scale, location[1] * scale, location[2] * scale + lift]
        properties = [("Location", vec(placed))]
        rotation = props.get("Rotation")
        if rotation is not None and rotation.value and any(rotation.value):
            properties.append(("Rotation", rot(rotation.value)))
        out.append(Actor(target, unique(cls.replace("UTVehicleFactory_", "") + "Spawn"),
                         properties))
        stats.vehicles += 1
        stats.by_vehicle[target] = stats.by_vehicle.get(target, 0) + 1
        if note:
            stats.substitutions.append(note)

    for export in pkg.exports:
        cls = pkg.class_name_of(export)
        if (cls.startswith("UTVehicleFactory_") and cls not in VEHICLE_FACTORIES
                and is_placed_actor(pkg, export)):
            stats.unmapped[cls] = stats.unmapped.get(cls, 0) + 1
    return out
