#!/usr/bin/env python3
"""Regression tests for Warfare -> Onslaught, on WAR-PowerSurge (Phase 8).

    python3 tests/test_onslaught.py [path/to/WAR-PowerSurge.ut3]
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.onslaught import (BLUE_CORE, NODE, RED_CORE, SPECIAL_BLUE_CORE,
                               SPECIAL_NODE, SPECIAL_RED_CORE, VEHICLE_FACTORIES,
                               convert_onslaught, read_link_setups)
from ut3.package import Package
from ut3.props import read_object_properties

DEFAULT_MAP = ("/home/josh/.steam/steam/steamapps/common/Unreal Tournament 3/"
               "UTGame/CookedPC/Private/Maps/WAR-PowerSurge.ut3")

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


def prop_of(actor, key):
    for k, v in actor.properties:
        if k == key:
            return v
    return None


def main(path):
    p = Package(path)

    print("the link graph")
    setups = read_link_setups(p)
    # Both engines let a map ship several named graphs; WAR-PowerSurge ships one.
    check("link setups read from UTOnslaughtMapInfo", [s[0] for s in setups], ["Default"])
    _name, links, standalone = setups[0]
    check("power links read from UTOnslaughtMapInfo", len(links), 7)
    check("standalone nodes", len(standalone), 1)
    check("and it is the countdown node",
          {p.exports[i - 1].name for i in standalone}, {"UTOnslaughtCountdownNode_0"})

    print("actors")
    actors, stats = convert_onslaught(p)
    check("power cores", stats.cores, 2)
    check("power nodes", stats.nodes, 5)
    check("of which countdown", stats.countdown, 1)
    check("links carried across", stats.links, 7)
    check("red and blue", (stats.red, stats.blue), ("RedCore", "BlueCore"))
    check_that("one core of each class",
               len([a for a in actors if a.cls == RED_CORE]) == 1
               and len([a for a in actors if a.cls == BLUE_CORE]) == 1)
    # UT3 names its nodes, and those names are what the HUD calls them.
    nodes = {a.name for a in actors if a.cls == NODE}
    check("nodes named for their objective", nodes,
          {"WestTank", "EastTank", "Prime", "Prime_2", "MineNode"})

    # Placement follows each engine's mesh pivot, not its collision. Getting it
    # wrong sinks the actor: a core placed at UT3's Location shows only its top
    # half, with the energy effect surfacing below the mesh.
    from convert.onslaught import (UT2_CORE_PIVOT_HEIGHT, UT2_NODE_PREPIVOT,
                                   UT3_NODE_HEIGHT)

    def ut3_z_of(name):
        e = [x for x in p.exports if x.name == name][0]
        pr, _s, _e = read_object_properties(p, e)
        return pr.get("Location").value[2]

    def emitted_z(actor):
        return float(re.search(r"Z=(\S+?)\)", prop_of(actor, "Location")).group(1))

    core = [a for a in actors if a.name == "RedCore"][0]
    check("the core rises to its mesh centre",
          round(emitted_z(core) - ut3_z_of("UTOnslaughtPowerCore_Content_0"), 1),
          round(UT2_CORE_PIVOT_HEIGHT, 1))
    # A node stands on whichever of its parts reaches lowest, and that is the
    # plate -- but PrePivot is in mesh space, applied before the scale
    # (AActor.h:68), so at ONSPowerNode's DrawScale 2 it hangs 50 below Location
    # rather than 25. CollisionHeight is already world units and stays 30.
    # Missing the scale left every node's base under the floor: UT3 puts its own
    # floor 34 below Location, so the node *rises* 16 rather than dropping.
    from convert.onslaught import (NODE_REST_OFFSET, UT2_NODE_COLLISION_HEIGHT,
                                   UT2_NODE_DRAW_SCALE)

    node = [a for a in actors if a.name == "WestTank"][0]
    check("the node stands on whichever part reaches lowest",
          round(ut3_z_of("UTOnslaughtPowernode_Content_0") - emitted_z(node), 1),
          round(UT3_NODE_HEIGHT - NODE_REST_OFFSET, 1))
    check("and that is the plate, once its pivot is scaled",
          NODE_REST_OFFSET, UT2_NODE_PREPIVOT * UT2_NODE_DRAW_SCALE)
    check_that("which reaches lower than the touch cylinder",
               NODE_REST_OFFSET > UT2_NODE_COLLISION_HEIGHT)
    check_that("so the correction lifts the node rather than dropping it",
               NODE_REST_OFFSET > UT3_NODE_HEIGHT)

    # PowerSurge hangs its cores upside down (Roll=32768), so the rotation has
    # to survive intact -- and it costs nothing, since a centre-pivoted mesh is
    # symmetric about its Location either way up.
    core_text = "\n".join(core.lines())
    check_that("UT3's rotation survives, roll included", "Roll=32768" in core_text,
               [l.strip() for l in core_text.splitlines() if "Rotation" in l])
    # Left at the stock size deliberately -- see the note in convert/onslaught.py.
    check_that("and the core keeps the stock mesh size",
               not any(k in ("DrawScale", "CollisionRadius") for k, _v in core.properties))

    print("the emitted setup")
    setup = [a for a in actors if a.cls == "ONSPowerLinkOfficialSetup"][0]
    entries = [v for k, v in setup.properties if k.startswith("LinkSetups(")]
    # ONS-Tyrant lists every node, linked or not: the game only assigns where a
    # name matches (ONSOnslaughtGame.uc:238), so a node left out of a setup
    # keeps the links of whichever setup ran before it.
    check("every core and node has an entry", len(entries), 7)
    named = {re.search(r'BaseNode="([^"]+)"', e).group(1) for e in entries}
    check("covering all of them", named, nodes | {"RedCore", "BlueCore"})
    # BaseNode resolves against the actor's Name, so the two must agree.
    check_that("every base names an emitted actor",
               named <= {a.name for a in actors})
    check_that("the standalone node has no links",
               '(BaseNode="MineNode")' in entries)

    # UT3 keeps its countdown node out of the graph, which in stock Onslaught
    # means no team may ever attack it and it sits there as scenery. The setup
    # the map shipped is left alone and an extra one links it in.
    from convert.onslaught import ALL_NODES_SETUP

    all_setups = [a for a in actors if a.cls == "ONSPowerLinkOfficialSetup"]
    # The editor builds the map's setup list for the menus by scanning actor
    # *names* for this string (Editor/Src/UnEdSrv.cpp:2874). A setup named
    # anything else exists in game but cannot be picked before the match.
    from convert.onslaught import LINK_SETUP

    check_that("every setup is named so the editor's summary scan finds it",
               all(LINK_SETUP in a.name for a in
                   [x for x in actors if x.cls == LINK_SETUP]),
               str([a.name for a in actors if a.cls == LINK_SETUP]))
    check("one setup per UT3 setup, plus the one that adopts loners",
          [prop_of(a, "SetupName") for a in all_setups],
          ['"Default"', '"%s"' % ALL_NODES_SETUP])
    check("and the adopted node is the mine node", stats.adopted, ["MineNode"])
    adopted_entries = [v for k, v in all_setups[1].properties
                       if k.startswith("LinkSetups(")]
    check_that("which is linked to the two nodes it sits between",
               '(BaseNode="MineNode",LinkedNodes=("Prime","Prime_2"))' in adopted_entries,
               str([e for e in adopted_entries if "MineNode" in e]))
    check_that("while the default setup still matches UT3 exactly",
               '(BaseNode="MineNode")' in entries)

    # The classes must be ones a stock install actually has: the editor drops an
    # unresolvable class on T3D import without a word, and the game then says
    # "Onslaught: Level doesn't have any PowerCores!". These are what ONS-Torlan
    # places.
    check("stock core and node classes by default",
          (RED_CORE, BLUE_CORE, NODE),
          ("ONSPowerCoreRed", "ONSPowerCoreBlue", "ONSPowerNodeNeutral"))
    check_that("nothing from OnslaughtSpecials2 is emitted by default",
               not [a for a in actors if "Special" in a.cls
                    or a.cls.endswith("Supplement")],
               str([a.cls for a in actors if "Special" in a.cls]))

    print("the countdown node")
    # CountdownTime is a plain var on ONSPowerNodeSpecial -- only a supplement
    # can set it, which is why ONSCountdownNode is a stub. That means the whole
    # OnslaughtSpecials2 class set, so it is opt-in.
    special, special_stats = convert_onslaught(p, specials=True)
    check("--onslaught-specials switches the nodes over",
          len([a for a in special if a.cls == SPECIAL_NODE]), 5)
    # Never the cores: OnslaughtSpecials2's core classes carry no mesh (its
    # package imports no core mesh at all), so a map placed with them has cores
    # that render nothing and cannot be shot.
    check_that("but leaves the cores stock, since the mod's draw no mesh",
               not [a for a in special if a.cls in (SPECIAL_RED_CORE, SPECIAL_BLUE_CORE)]
               and len([a for a in special if a.cls in (RED_CORE, BLUE_CORE)]) == 2,
               str(sorted({a.cls for a in special if "Core" in a.cls})))
    supplement = [a for a in special
                  if a.cls == "ONSPowerlinkOfficialSetupSupplement"][0]
    settings = [v for k, v in supplement.properties
                if k.startswith("PowernodeSettings(")]
    check("one node needs settings", len(settings), 1)
    check_that("it is the mine node", "MyLevel.MineNode" in settings[0])
    check_that("marked standalone", "bIsStandalone=True" in settings[0])
    check_that("with a countdown", "CountdownTime=" in settings[0])
    # The flag set ONS-Tyrant's working countdown node carries. A standalone
    # node is permanently isolated, so without these it shields itself and sits
    # invulnerable; the countdown pair is what makes the timer do anything.
    for flag in ("bNeverShielded=True", "bNotShieldedIfIsolated=True",
                 "bKeepVehiclesWhenDestroyed=True",
                 "bCountdownDamagesEnemyCore=True", "bStopCountdownIfIsolated=True"):
        check_that("carries %s" % flag, flag in settings[0])
    # CountdownTime is a byte in OnslaughtSpecials2.
    countdown = int(re.search(r"CountdownTime=(\d+)", settings[0]).group(1))
    check_that("the countdown fits in a byte", 1 <= countdown <= 255, str(countdown))
    # The reference form ONS-Tyrant's own supplement uses.
    check_that("referenced the way UT2004 exports it",
               "Node=%s'MyLevel.MineNode'" % SPECIAL_NODE in settings[0])

    # bCountdownDamagesEnemyCore alone does nothing: OnslaughtSpecials2 only
    # fires the completed event, and ONS-Tyrant does the damage with a
    # ScriptedTrigger that waits on it, hits the core and loops back.
    from convert.onslaught import (COUNTDOWN_DAMAGE, DAMAGE_ACTION,
                                   SCRIPTED_TRIGGER, WAIT_ACTION)

    check_that("the completed events are named", "RedCompletedEvent=" in settings[0]
               and "BlueCompletedEvent=" in settings[0])
    triggers = [a for a in special if a.cls == SCRIPTED_TRIGGER]
    check("one trigger per team per countdown node", len(triggers), 2)
    check("counted", special_stats.countdown_triggers, 2)
    fired = {re.search(r'ExternalEvent="([^"]+)"', "\n".join(t.lines())).group(1)
             for t in triggers}
    check_that("each waits on an event the supplement fires",
               all('"%s"' % e in settings[0] for e in fired), str(sorted(fired)))
    # Red finishing the countdown must hit blue's core, not red's.
    red = [t for t in triggers if "RedDone" in "\n".join(t.lines())][0]
    check_that("red completing damages the blue core",
               'NodeTag="BlueCore"' in "\n".join(red.lines()),
               [l.strip() for l in red.lines() if "NodeTag" in l][0])
    text = "\n".join(triggers[0].lines())
    check_that("the actions are level objects, not subobjects",
               "Begin Object Class=%s" % WAIT_ACTION in text
               and "Actions(0)=%s'" % WAIT_ACTION in text)
    check_that("and it loops back to waiting", "ActionNumber=0" in text)
    check("the damage is Tyrant's", COUNTDOWN_DAMAGE, 901)

    print("vehicles")
    check("factories converted", stats.vehicles, 18)
    check("by class", stats.by_vehicle,
          {"ONSTankFactory": 4, "ONSPRVFactory": 4, "ONSRVFactory": 4,
           "ONSHoverCraftFactory": 4, "ONSManualGunPawn": 2})
    # The four that are genuinely the same vehicle under a different name.
    for ut3, ut2 in (("Manta", "ONSHoverCraftFactory"), ("Scorpion", "ONSRVFactory"),
                     ("HellBender", "ONSPRVFactory"), ("Goliath", "ONSTankFactory")):
        check("%s is exact" % ut3,
              VEHICLE_FACTORIES["UTVehicleFactory_" + ut3], (ut2, None))
    check_that("and the substitutions are reported", bool(stats.substitutions),
               str(sorted(set(stats.substitutions))))
    check_that("nothing was silently dropped", not stats.unmapped, str(stats.unmapped))

    print("node teleporters")
    # Both engines place these near a node and let the node claim the nearest
    # (ONSPowerCore.uc:228), so only the position converts -- no binding.
    from convert.onslaught import TELEPORT_PAD, UT3_TELEPORTER_DROP

    pads = [a for a in actors if a.cls == TELEPORT_PAD]
    check("teleport pads", len(pads), 7)
    check("counted", stats.teleport_pads, 7)
    # UT3 stands its teleporter 34 above the floor (its FloorMesh translates
    # down by that); ONSTeleportPad's mesh has its pivot at the base.
    first = [e for e in p.exports if e.name == "UTOnslaughtNodeTeleporter_Content_0"][0]
    props, _s, _e = read_object_properties(p, first)
    ut3_z = props.get("Location").value[2]
    z = float(re.search(r"Z=(\S+?)\)", prop_of(pads[0], "Location")).group(1))
    check("the pad drops onto the floor", round(ut3_z - z), int(UT3_TELEPORTER_DROP))

    print("the mine, as decoration")
    # No UT2004 equivalent for the mining mechanic, so only the geometry
    # converts. Both meshes are inherited from the class rather than stated on
    # the instance, and both are mostly solid with one glowing element.
    from convert.meshes import MeshSet, convert_actors
    from convert.textures import TextureSet
    from ut3.resolve import PackageIndex

    index = PackageIndex.for_map(path)
    mesh_actors, mesh_stats = convert_actors(p, index, MeshSet("TestTex"),
                                             TextureSet("TestTex"))
    meshes = {str(prop_of(a, "StaticMesh")) for a in mesh_actors}
    check_that("the mine's crystal is emitted",
               any("S_UN_Cave_SM_Crystal" in m for m in meshes))
    check_that("and the processing plant",
               any("SM_Processing_Plant" in m for m in meshes))

    print("factories UT3 parked off-map")
    # UE2 has no ONSObjectiveOverride, and needs none: UT2004 binds a factory to
    # whichever objective is *nearest*, with no radius involved -- FindCloseActors
    # keeps every ONSVehicleFactory whose ClosestTo(A) == self
    # (ONSPowerCore.uc:213) and Activates/Deactivates it from there (:882, :782).
    # So stating the association means placing the factory by the objective.
    # WAR-Serenity needs it: both Leviathan factories sit eight units apart at
    # the world origin, bDisabled until Kismet enables them, with their real home
    # named by an override.
    from convert.onslaught import (OVERRIDE_NEAR, OVERRIDE_OFFSET,
                                   _override_location)

    class _Ref:
        is_null = False
        is_export = True

        def __init__(self, export):
            self.export = export

    class _Props(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    class _Value:
        def __init__(self, value):
            self.value = value

    class _Export:
        name = "PowerCore_3"

    core = _Export()
    anchor = _Props({"Location": _Value((1000.0, 2000.0, 300.0))})

    class _Pkg:
        pass

    import convert.onslaught as ons
    real_props = ons._props
    ons._props = lambda pkg, export: anchor
    try:
        # Parked at the origin: moved out in front of the objective it names.
        parked = _Props({"ONSObjectiveOverride": _Ref(core),
                         "Rotation": _Value((0, 0, 0))})
        moved, named = _override_location(_Pkg(), parked, [4.0, 554.9, 159.0])
        check_that("a parked factory is rehomed onto its objective",
                   moved is not None, str(moved))
        check("and reports which objective it was given to", named, "PowerCore_3")
        check("and lands the offset away, in front of it",
              [round(c, 1) for c in moved],
              [1000.0 + OVERRIDE_OFFSET, 2000.0, 300.0])
        check_that("which clears the Leviathan's 260 radius and a core's 120",
                   OVERRIDE_OFFSET > 260 + 120)
        # Yaw decides which way "in front" is.
        turned = _Props({"ONSObjectiveOverride": _Ref(core),
                         "Rotation": _Value((0, 16384, 0))})
        moved, _named = _override_location(_Pkg(), turned, [4.0, 554.9, 159.0])
        check("a quarter turn puts it on the other axis",
              [round(c, 1) for c in moved],
              [1000.0, 2000.0 + OVERRIDE_OFFSET, 300.0])
        # Already placed by its objective: left exactly where the map put it.
        near = _Props({"ONSObjectiveOverride": _Ref(core)})
        moved, _named = _override_location(_Pkg(), near, [1000.0, 2000.0 + 200.0, 300.0])
        check("a factory already beside its objective is left alone", moved, None)
        check_that("the threshold is wider than the offset it would apply",
                   OVERRIDE_NEAR > OVERRIDE_OFFSET)
        # No override at all: nothing to do.
        check("a factory with no override is never moved",
              _override_location(_Pkg(), _Props({}), [0.0, 0.0, 0.0]), (None, None))
    finally:
        ons._props = real_props

    check("UT3's plain turret is UT2004's plasma turret",
          ons.VEHICLE_FACTORIES["UTVehicleFactory_Turret"], ("ONSManualGunPawn", None))

    print("link setup names")
    # UT2004 picks a setup by name from the URL and falls back to the one called
    # "Default" (ONSOnslaughtGame.uc:188), so a map without one starts with no
    # link graph at all. UT3 does not promise that name -- but sometimes it
    # already uses it, and renaming the first setup on top of it is what got
    # WAR-Torlan wrong: its setups are short/Classic/Default/TwoFronts, and the
    # map ended up offering "Default" twice with one unreachable.
    from convert.onslaught import setup_labels

    torlan = [("short", [], 0), ("Classic", [], 0),
              ("Default", [], 0), ("TwoFronts", [], 0)]
    check("a map that already has a Default keeps every name",
          setup_labels(torlan), ["short", "Classic", "Default", "TwoFronts"])
    check("one that does not gets its first setup renamed",
          setup_labels([("short", [], 0), ("Classic", [], 0)]), ["Default", "Classic"])
    check("an unnamed setup still gets a name",
          setup_labels([("", [], 0), ("", [], 0)]), ["Default", "Setup2"])
    # A repeated name is a setup the player cannot reach, whatever its source.
    check("duplicates are separated", setup_labels([("Alpha", [], 0), ("Alpha", [], 0)]),
          ["Default", "Alpha"])
    check("including duplicate Defaults",
          setup_labels([("Default", [], 0), ("Default", [], 0)]),
          ["Default", "Default2"])
    check_that("and exactly one setup is always the fallback",
               sum(1 for l in setup_labels(torlan) if l.lower() == "default") == 1)

    print("nodes resting on their platform")
    # UT2004 draws a node's plate PrePivot.Z below its Location and centres the
    # touch cylinder (CollisionHeight 30) on Location, so a node whose plate is
    # under the floor is both hidden inside the geometry and half in solid --
    # unreachable, not merely ugly. WAR-Torlan's two Prime nodes each stand on
    # their own platform topped at -7880, but UT3 puts them at -7858 and -7850,
    # leaving their plates 12 and 4 units inside it.
    from convert.onslaught import NODE_REST_OFFSET, _contains, rest_on_brushes
    from ut2.t3d import Actor, Brush, Polygon, vec

    def platform(top, half=200.0):
        face = Polygon(origin=(0.0, 0.0, top), normal=(0.0, 0.0, 1.0),
                       texture_u=(1.0, 0.0, 0.0), texture_v=(0.0, 1.0, 0.0),
                       vertices=[(-half, -half, top), (half, -half, top),
                                 (half, half, top), (-half, half, top)])
        return Brush(name="Plat", model_name="M", polygons=[face], csg="CSG_Add")

    check_that("a point over the platform is inside it",
               _contains([(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
                          (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)], 0.0, 0.0))
    check_that("and one beyond it is not",
               not _contains([(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
                              (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)], 5.0, 0.0))

    sunk = Actor("ONSPowerNodeSpecial", "Prime",
                 [("Location", vec((0.0, 0.0, -7867.0)))])
    moved = rest_on_brushes([sunk], [platform(-7880.0)])
    # Plate sat at -7867 - 25 = -7892, inside a platform topping out at -7880.
    # It comes to rest at -7880 + 30, since the touch cylinder reaches lower than
    # the plate does and both have to clear the floor.
    check("a sunk node is lifted clear of the floor", moved, [("Prime", 37)])
    check("standing on the surface by whichever part reaches lowest",
          dict(sunk.properties)["Location"], vec((0.0, 0.0, -7880.0 + NODE_REST_OFFSET)))
    check("which is the scaled mesh pivot, not the touch cylinder",
          NODE_REST_OFFSET, 50.0)

    # Only ever upwards: a node standing proud of the brush is left alone, or
    # every node on a low kerb would be dragged down onto it.
    high = Actor("ONSPowerNodeSpecial", "High",
                 [("Location", vec((0.0, 0.0, -7800.0)))])
    check("a node already clear of the floor is untouched",
          rest_on_brushes([high], [platform(-7880.0)]), [])
    # And only from a surface it is plausibly standing on: most nodes stand on
    # terrain or a mesh, and must not be teleported onto distant geometry.
    far = Actor("ONSPowerNodeSpecial", "Far",
                [("Location", vec((0.0, 0.0, -7867.0)))])
    check("a distant surface is not treated as its floor",
          rest_on_brushes([far], [platform(-7000.0)]), [])
    off = Actor("ONSPowerNodeSpecial", "Off",
                [("Location", vec((5000.0, 0.0, -7867.0)))])
    check("nor is one it does not stand over",
          rest_on_brushes([off], [platform(-7880.0)]), [])
    # Subtractive brushes are space, not floor.
    hollow = platform(-7880.0)
    hollow.csg = "CSG_Subtract"
    void = Actor("ONSPowerNodeSpecial", "Void",
                 [("Location", vec((0.0, 0.0, -7867.0)))])
    check("a subtracted brush is not a floor", rest_on_brushes([void], [hollow]), [])

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
