"""Pickups and bot paths: UT3 factories -> UT2004 bases, PathNodes and JumpPads.

UT2004 does not place weapons and powerups directly. Checked against the stock
maps (DM-Rankin, DM-Antalus, DM-1on1-Albatross), the convention is a *base*
actor that spawns the item -- `xWeaponBase` with a `WeaponType`, or an
`xPickUpBase` subclass whose `PowerUp` names the pickup class:

    HealthCharger       -> XPickups.HealthPack        (25 health)
    ShieldCharger       -> XPickups.ShieldPack        (50 shield)
    SuperShieldCharger  -> XPickups.SuperShieldPack   (100 shield)
    UDamageCharger      -> XPickups.UDamagePack

Only the small items are placed bare, and stock maps do exactly that with
`MiniHealthPack`, ammo and adrenaline. UT3 works the other way round: every item
is a `UTPickupFactory` subclass, with weapons naming their weapon in
`WeaponPickupClass`.

Bot paths convert directly -- UT2004 has `PathNode` and a `UTJumpPad` of its own
-- but the jump velocity does not. UT2004 computes it during Build Paths from
the pad's first forced path (`AJumpPad::addReachSpecs`,
Engine/Src/UnNavigationPoint.cpp:1281), so what has to survive the conversion is
UT3's `JumpTarget` link, as a `ForcedPaths` entry naming the target PathNode.
"""

import re

from ut2.t3d import Actor, ObjectActor, rot, vec
from ut3.objects.level import is_placed_actor
from ut3.props import read_object_properties

# UT3 weapon class -> UT2004 weapon class for xWeaponBase.WeaponType.
# UT2004's `XWeapons.SniperRifle` is the Lightning Gun (its ItemName says so);
# the UT2003-style rifle is not a separate class.
WEAPON_CLASSES = {
    "UTWeap_ShockRifle": "XWeapons.ShockRifle",
    "UTWeap_LinkGun": "XWeapons.LinkGun",
    "UTWeap_BioRifle_Content": "XWeapons.BioRifle",
    "UTWeap_BioRifle": "XWeapons.BioRifle",
    "UTWeap_FlakCannon": "XWeapons.FlakCannon",
    "UTWeap_RocketLauncher": "XWeapons.RocketLauncher",
    "UTWeap_Stinger": "XWeapons.Minigun",
    "UTWeap_SniperRifle": "XWeapons.SniperRifle",
    "UTWeap_ShockRifle_Content": "XWeapons.ShockRifle",
    "UTWeap_Enforcer": "XWeapons.AssaultRifle",
    "UTWeap_Redeemer": "XWeapons.Redeemer",
    "UTWeap_Redeemer_Content": "XWeapons.Redeemer",
    "UTWeap_Translocator": "XWeapons.TransPickup",
    "UTWeap_Translocator_Content": "XWeapons.TransPickup",
    # The AVRiL is one of the few UT3 weapons UT2004 has under its own name,
    # in the Onslaught package rather than XWeapons.
    "UTWeap_Avril": "Onslaught.ONSAVRiL",
    "UTWeap_Avril_Content": "Onslaught.ONSAVRiL",
}

# Both engines put an actor's Location at the *centre* of its collision
# cylinder, so a placed actor rests CollisionHeight above the floor -- and the
# two engines disagree on those heights, which is why copying Location straight
# across leaves everything hanging in the air.
#
# UT3 side, read from the class defaults in CookedPC/UTGame.u:
#   UTPickupFactory             CollisionHeight 44   (its BaseMeshComp, the part
#                                                     that sits on the floor, is
#                                                     translated by -44 to match)
#   UTPickupFactory_HealthVial  CollisionHeight 20
#   NavigationPoint             CollisionHeight 50
#   PlayerStart                 CollisionHeight 80
UT3_PICKUP_HEIGHT = 44.0
UT3_HEIGHTS = {"UTPickupFactory_HealthVial": 20.0}
UT3_NAV_HEIGHT = 50.0

# UT2004 side, where each converted class wants its Location above the floor.
# The bases are the odd ones: `AxPickUpBase::CheckForErrors` traces a mere 8uu
# down from Location and warns "xPickUpBase is floating" if it misses
# (Engine/Src/UnErrorChecking.cpp:204), so a base sits *on* the floor rather
# than a collision height above it -- which is also why xWeaponBase's own
# CollisionHeight is only 3.
UT2_BASE_HEIGHT = 2.0
UT2_HEIGHTS = {
    "xWeaponBase": 3.0,         # its own CollisionHeight
    "MiniHealthPack": 23.0,     # TournamentHealth's
    "PathNode": 43.0,           # NavigationPoint's
    "UTJumpPad": 43.0,
}

# UT3 pickup factory -> UT2004 actor. Values are (class, note); a note means the
# two items are not equivalent and the substitution is worth reporting.
PICKUP_CLASSES = {
    "UTPickupFactory_HealthVial": ("XPickups.MiniHealthPack", None),
    "UTPickupFactory_MediumHealth": ("XGame.HealthCharger", None),
    "UTPickupFactory_SuperHealth": ("XGame.SuperHealthCharger", None),
    "UTArmorPickup_ShieldBelt": ("XGame.SuperShieldCharger", None),
    "UTArmorPickup_Vest": ("XGame.ShieldCharger", None),
    # UT3's helmet is 20 armour and UT2004 has no item that small, so it
    # becomes the 50-point pack.
    "UTArmorPickup_Helmet": ("XGame.ShieldCharger", "helmet -> 50 shield"),
    "UTArmorPickup_Thighpads": ("XGame.ShieldCharger", "thighpads -> 50 shield"),
    # UT2004 has no invisibility, so the powerup spot stays a powerup spot.
    "UTPickupFactory_Invisibility": ("XGame.UDamageCharger", "invisibility -> UDamage"),
    "UTPickupFactory_UDamage": ("XGame.UDamageCharger", None),
    "UTPickupFactory_Berserk": ("XGame.UDamageCharger", "berserk -> UDamage"),
}

WEAPON_FACTORIES = ("UTWeaponPickupFactory", "UTPickupFactory_Weapon")
PATH_CLASSES = {"PathNode": "PathNode"}
JUMP_PAD_CLASSES = {"UTJumpPad": "UTJumpPad"}

# Weapon lockers convert one for one: both engines hold the same
# `Weapons` array of (class, extra ammo) and use the same 50x80 cylinder.
LOCKER_CLASSES = ("UTWeaponLocker_Content", "UTWeaponLocker")
LOCKER = "WeaponLocker"

# UT3 draws its locker mesh 50 below the actor (BaseMeshComp's Translation).
# UT2004's is base-pivoted (WeaponLockerM, bounds Z 0..188) and drawn through
# PrePivot=(Z=105) -- which is applied *before* DrawScale in the transform
# (Engine/Inc/AActor.h:65), so at DrawScale 0.5 the mesh starts 52.5 below the
# actor. Its 50-tall collision cylinder agrees to within 2.5.
UT3_LOCKER_FLOOR = 50.0
UT2_LOCKER_RISE = 52.5

# UT3 states no ammo, so the amounts are UT2004's own: the value stock ONS maps
# give each weapon, taken as the most common across twelve of them (all of these
# are dominant by better than ten to one).
LOCKER_AMMO = {
    "BioRifle": 50,
    "FlakCannon": 30,
    "LinkGun": 300,
    "Minigun": 100,
    "ONSAVRiL": 10,
    "ONSGrenadeLauncher": 50,
    "RocketLauncher": 18,
    "ShockRifle": 50,
    "SniperRifle": 40,
}

# What stands in for the marker UT3 draws on its jump pads. Both are stock
# UT2004 content, so neither costs the converted package anything: the plate is
# the charger base every UT2004 pickup stands on, and the effect is the one from
# standard-jumppad-effect.txt -- a rising grid plate plus a column of streaks.
JUMP_PAD_MESH = "StaticMesh'XGame_rc.AmmoChargerMesh'"
# AmmoChargerMesh is pivoted at its centre (bounds Z -6.0..6.0), so it has to
# come up half its height to rest on the floor.
JUMP_PAD_PLATE_RISE = 6.0
# Fallback for where the floor is when the pad class says nothing: UT3's pad
# cylinder is 50 tall and centred on the actor.
JUMP_PAD_FLOOR = 50.0

JUMP_PAD_EFFECT = [
    ("SpriteEmitter", "Grid", [
        ("UseDirectionAs", "PTDU_Normal"),
        ("UseColorScale", "True"),
        ("FadeOut", "True"),
        ("FadeIn", "True"),
        ("Acceleration", "(Z=50.000000)"),
        ("ColorScale(0)", "(Color=(G=162,R=185,A=255))"),
        ("ColorScale(1)", "(RelativeTime=1.000000,Color=(B=92,G=44,R=69,A=50))"),
        ("FadeOutStartTime", "0.800000"),
        ("FadeInEndTime", "0.100000"),
        ("MaxParticles", "5"),
        ("StartSizeRange", "(X=(Min=50.000000,Max=50.000000),"
                           "Y=(Min=50.000000,Max=50.000000),"
                           "Z=(Min=50.000000,Max=50.000000))"),
        ("Texture", "Texture'EpicParticles.JumpPad.GridPlate'"),
        ("LifetimeRange", "(Min=1.400000,Max=1.400000)"),
        ("WarmupTicksPerSecond", "2.000000"),
        ("RelativeWarmupTime", "2.000000"),
    ]),
    ("SpriteEmitter", "Streaks", [
        ("UseDirectionAs", "PTDU_Right"),
        ("UseColorScale", "True"),
        ("FadeOut", "True"),
        ("FadeIn", "True"),
        ("Acceleration", "(Z=80.000000)"),
        ("ColorScale(0)", "(Color=(B=27,G=95,R=124))"),
        ("ColorScale(1)", "(RelativeTime=1.000000,Color=(B=82,G=123,R=169))"),
        ("FadeOutStartTime", "0.750000"),
        ("FadeInEndTime", "0.200000"),
        ("MaxParticles", "100"),
        ("StartLocationRange", "(X=(Min=-30.000000,Max=30.000000),"
                               "Y=(Min=-24.000000,Max=24.000000))"),
        ("StartSpinRange", "(X=(Min=-0.250000,Max=-0.250000))"),
        ("StartSizeRange", "(X=(Min=10.000000,Max=10.000000),"
                           "Y=(Min=8.000000,Max=8.000000),"
                           "Z=(Min=150.000000,Max=150.000000))"),
        ("Texture", "Texture'EpicParticles.Beams.WhiteStreak01aw'"),
        ("LifetimeRange", "(Min=1.250000,Max=1.500000)"),
        ("StartVelocityRange", "(Z=(Max=20.000000))"),
        ("WarmupTicksPerSecond", "2.000000"),
        ("RelativeWarmupTime", "2.000000"),
    ]),
]

_CLASS_REF = re.compile(r"Class'([^']+)'")


class PickupStats:
    def __init__(self):
        self.weapons = 0
        self.items = 0
        self.path_nodes = 0
        self.jump_pads = 0
        self.jump_pad_markers = 0
        self.lockers = 0
        self.jump_pads_linked = 0
        self.substitutions = {}
        self.unmapped = {}

    def __str__(self):
        parts = []
        if self.weapons or self.items:
            parts.append("%d pickups (%d weapon bases, %d items)"
                         % (self.weapons + self.items, self.weapons, self.items))
        if self.lockers:
            parts.append("%d weapon lockers" % self.lockers)
        if self.path_nodes or self.jump_pads:
            parts.append("%d path nodes, %d jump pads (%d linked)"
                         % (self.path_nodes, self.jump_pads, self.jump_pads_linked))
            if self.jump_pad_markers:
                parts[-1] += (", %d given the plate and effect UT2004's own jump "
                              "pad draws nothing for" % self.jump_pad_markers)
        out = "; ".join(parts) or "no pickups or paths"
        if self.substitutions:
            out += "; substituted " + ", ".join(
                "%s x%d" % (note, n) for note, n in sorted(self.substitutions.items()))
        if self.unmapped:
            out += "; skipped " + ", ".join(
                "%s x%d" % (name, n) for name, n in sorted(self.unmapped.items()))
        return out


def _sanitize(name, fallback):
    out = re.sub(r"[^A-Za-z0-9_]", "_", name or "") or fallback
    if out[0].isdigit():
        out = "_" + out
    return out


def _unique(name, taken):
    if name not in taken:
        taken.add(name)
        return name
    n = 2
    while "%s_%d" % (name, n) in taken:
        n += 1
    taken.add("%s_%d" % (name, n))
    return "%s_%d" % (name, n)


def ground_offset(ut3_class, ut2_class):
    """How far to drop an actor so it rests on the floor in UT2004.

    Positive means "move down": the floor is `Location.Z - <UT3 height>`, and
    the converted actor wants to sit `<UT2004 height>` above that.
    """
    if ut3_class in PATH_CLASSES or ut3_class in JUMP_PAD_CLASSES:
        ut3_height = UT3_NAV_HEIGHT
    else:
        ut3_height = UT3_HEIGHTS.get(ut3_class, UT3_PICKUP_HEIGHT)
    return ut3_height - UT2_HEIGHTS.get(ut2_class, UT2_BASE_HEIGHT)


def _placement(props, scale, drop=0.0):
    """Location and Rotation, ready for a t3d actor."""
    out = []
    location = props.get("Location")
    if location is not None and location.value:
        x, y, z = location.value
        out.append(("Location", vec([x * scale, y * scale, (z - drop) * scale])))
    rotation = props.get("Rotation")
    if rotation is not None and rotation.value and any(rotation.value):
        out.append(("Rotation", rot(rotation.value)))
    return out


def _weapon_class(props):
    """The UT2004 weapon class a UT3 weapon factory should hold."""
    ref = props.get("WeaponPickupClass")
    if ref is None:
        return None, None
    match = _CLASS_REF.search(str(ref))
    if not match:
        return None, None
    ut3_name = match.group(1).split(".")[-1]
    return WEAPON_CLASSES.get(ut3_name), ut3_name


def convert_pickups(pkg, scale=1.0, stats=None, taken=None):
    """Convert UT3 pickup factories into UT2004 bases and pickups."""
    stats = stats or PickupStats()
    taken = taken if taken is not None else set()
    out = []
    for export in pkg.exports:
        cls = pkg.class_name_of(export)
        is_weapon = cls in WEAPON_FACTORIES
        if not is_weapon and cls not in PICKUP_CLASSES:
            continue
        if not is_placed_actor(pkg, export):
            continue
        props, start, _end = read_object_properties(pkg, export)
        if start is None:
            continue

        extra = []
        if is_weapon:
            weapon, ut3_name = _weapon_class(props)
            if weapon is None:
                stats.unmapped[ut3_name or cls] = stats.unmapped.get(ut3_name or cls, 0) + 1
                continue
            extra.append(("WeaponType", "Class'%s'" % weapon))
            ut2_class = "xWeaponBase"
            stats.weapons += 1
        else:
            ut2_class, note = PICKUP_CLASSES[cls]
            ut2_class = ut2_class.split(".")[-1]
            if note:
                stats.substitutions[note] = stats.substitutions.get(note, 0) + 1
            stats.items += 1

        properties = _placement(props, scale, ground_offset(cls, ut2_class)) + extra
        out.append(Actor(ut2_class, _unique(_sanitize(export.name, ut2_class), taken),
                         properties))
    return out, stats


def convert_paths(pkg, scale=1.0, stats=None, taken=None):
    """Convert PathNodes and jump pads, preserving each pad's destination.

    UT2004 recomputes reachspecs and jump velocities during Build Paths, so
    UT3's 1069 ReachSpecs are thrown away -- but a jump pad's destination is a
    design decision, not a derived value, and the editor only rediscovers it
    from a forced path. `ForcedPaths` matches on the target's *object name*
    (`Nav->GetFName()`, Engine/Src/UnNavigationPoint.cpp:544), which is the
    `Name=` in the t3d, so the PathNodes are converted first and their emitted
    names are what the pads point at.
    """
    stats = stats or PickupStats()
    taken = taken if taken is not None else set()
    out = []
    emitted = {}    # UT3 export index -> emitted t3d name

    for export in pkg.exports:
        cls = pkg.class_name_of(export)
        if cls not in PATH_CLASSES or not is_placed_actor(pkg, export):
            continue
        props, start, _end = read_object_properties(pkg, export)
        if start is None:
            continue
        name = _unique(_sanitize(export.name, "PathNode"), taken)
        emitted[export.index] = name
        out.append(Actor(PATH_CLASSES[cls], name,
                         _placement(props, scale,
                                    ground_offset(cls, PATH_CLASSES[cls]))))
        stats.path_nodes += 1

    for export in pkg.exports:
        cls = pkg.class_name_of(export)
        if cls not in JUMP_PAD_CLASSES or not is_placed_actor(pkg, export):
            continue
        props, start, _end = read_object_properties(pkg, export)
        if start is None:
            continue
        properties = _placement(props, scale,
                                ground_offset(cls, JUMP_PAD_CLASSES[cls]))
        target = props.get("JumpTarget")
        if target is not None and not target.is_null and target.is_export:
            destination = emitted.get(target.export.index)
            if destination:
                properties.append(("ForcedPaths(0)", destination))
                stats.jump_pads_linked += 1
        out.append(Actor(JUMP_PAD_CLASSES[cls],
                         _unique(_sanitize(export.name, "JumpPad"), taken),
                         properties))
        stats.jump_pads += 1
    return out, stats


def jump_pad_markers(pkg, index, scale=1.0, stats=None):
    """The plate and effect that show where a jump pad is.

    UT2004's JumpPad is pure gameplay -- no DrawType, no mesh (Engine/JumpPad.uc
    defaultproperties) -- so a converted pad is an invisible trigger on the
    floor with nothing to say a launch is there. UT3 keeps its marker on the
    class rather than the instance: Default__UTJumpPad's components hold a
    static mesh translated down 47 and a particle system beside it, so no placed
    pad states either one.

    Neither UT3 asset is reused. The stock UT2004 plate and a hand-built emitter
    stand in for them (see the standard-jumppad-*.txt references), which keeps
    the pads looking like the rest of the game and costs the package nothing.
    Only the *offset* is read from UT3 -- where it draws its own plate -- with
    the pad's collision height as the fallback.

    The plate is emitted non-colliding: the pad's own cylinder is what launches
    the player, and a solid mesh over it would be another lip to walk up.
    """
    out = []
    offsets = {}

    def floor_offset(export):
        """How far below the pad UT3 draws its plate."""
        cls = pkg.class_name_of(export)
        if cls in offsets:
            return offsets[cls]
        offsets[cls] = -JUMP_PAD_FLOOR
        owner, class_export = index.resolve(pkg, pkg.ref(export.class_index))
        if class_export is None:
            return offsets[cls]
        defaults = [e for e in owner.exports if e.name == "Default__" + class_export.name]
        if not defaults:
            return offsets[cls]
        props, start, _end = read_object_properties(owner, defaults[0])
        if start is None:
            return offsets[cls]
        components = props.get("Components")
        if components is None:
            return offsets[cls]
        from ut3.objects.sound import _archetype_props

        for ref in components.as_objects():
            comp_owner, comp = index.resolve(owner, ref)
            if comp is None or comp_owner.class_name_of(comp) != "StaticMeshComponent":
                continue
            merged = _archetype_props(comp_owner, index, comp)
            if merged.get("StaticMesh") is None:
                continue
            translation = merged.get("Translation")
            if translation is not None and translation.value:
                offsets[cls] = translation.value[2]
            break
        return offsets[cls]

    for export in pkg.exports:
        if pkg.class_name_of(export) not in JUMP_PAD_CLASSES \
                or not is_placed_actor(pkg, export):
            continue
        props, start, _end = read_object_properties(pkg, export)
        if start is None:
            continue
        location = props.get("Location")
        if location is None or not location.value:
            continue
        x, y, z = location.value
        floor = z + floor_offset(export)
        base = _sanitize(export.name, "JumpPad")

        # The stock plate is pivoted at its centre (bounds Z -6..6 in
        # XGame_rc), so it rests on the floor half its height up.
        out.append(Actor("StaticMeshActor", base + "Plate", [
            ("StaticMesh", JUMP_PAD_MESH),
            ("Location", vec([x * scale, y * scale,
                              (floor + JUMP_PAD_PLATE_RISE) * scale])),
            ("bCollideActors", "False"), ("bBlockActors", "False"),
            ("bBlockKarma", "False"),
        ]))
        # The objects belong to the level, not to the emitter, so their names
        # have to be unique map-wide or the importer renames the duplicates and
        # every pad after the first refers to the wrong one.
        emitters = [(cls, base + suffix, props)
                    for (cls, suffix, props) in JUMP_PAD_EFFECT]
        out.append(ObjectActor("Emitter", base + "Effect", emitters, "Emitters", [
            ("Location", vec([x * scale, y * scale, floor * scale])),
            ("DrawScale", "2.000000"),
        ]))
        if stats is not None:
            stats.jump_pad_markers += 1
    return out


def convert_weapon_lockers(pkg, scale=1.0, stats=None, taken=None):
    """UT3 weapon lockers -> UT2004 WeaponLockers.

    The two are the same actor: `array<WeaponEntry>` of a weapon class and its
    spare ammo, on a 50x80 cylinder. Only the ammo has to be invented, since UT3
    never states it -- see LOCKER_AMMO.
    """
    stats = stats or PickupStats()
    taken = taken if taken is not None else set()
    out = []
    for export in pkg.exports:
        if pkg.class_name_of(export) not in LOCKER_CLASSES \
                or not is_placed_actor(pkg, export):
            continue
        props, start, _end = read_object_properties(pkg, export)
        if start is None:
            continue
        location = props.get("Location")
        if location is None or not location.value:
            continue
        weapons = props.get("Weapons")
        entries = []
        if weapons is not None and len(weapons):
            try:
                listed = weapons.as_props()
            except (ValueError, IndexError):
                listed = []
            for entry in listed:
                ref = entry.get("WeaponClass")
                if ref is None or ref.is_null:
                    continue
                ut2 = WEAPON_CLASSES.get(ref.name)
                if not ut2:
                    stats.unmapped[ref.name] = stats.unmapped.get(ref.name, 0) + 1
                    continue
                entries.append("(WeaponClass=Class'%s',ExtraAmmo=%d)"
                               % (ut2, LOCKER_AMMO.get(ut2.split(".")[-1], 0)))
        if not entries:
            continue
        x, y, z = location.value
        properties = [("Location", vec([x * scale, y * scale,
                                        (z - UT3_LOCKER_FLOOR + UT2_LOCKER_RISE) * scale]))]
        for i, entry in enumerate(entries):
            properties.append(("Weapons(%d)" % i, entry))
        rotation = props.get("Rotation")
        if rotation is not None and rotation.value and any(rotation.value):
            properties.append(("Rotation", rot(rotation.value)))
        out.append(Actor(LOCKER, _unique(_sanitize(export.name, "WeaponLocker"), taken),
                         properties))
        stats.lockers += 1
    return out, stats
