"""Matinee-driven InterpActors -> UT2004 Movers.

UT3 animates scenery with Kismet: a `SeqAct_Interp` holds an `InterpData`, whose
`InterpGroup`s each own an `InterpTrackMove`, and the group is bound to an actor
through the action's variable links. UT2004 has no Kismet, but `Mover` stores
almost exactly the same thing on the actor itself -- `KeyPos[24]`/`KeyRot[24]`
offsets from where the mover is placed, walked at `MoveTime` seconds a leg
(Engine/Mover.uc; `Location = BasePos + KeyPos[KeyNum]`, UnMover.cpp:63).

So a move track converts to keys. Three things do not survive the trip and are
decided from the map rather than assumed:

**When it runs.** Kismet does not convert, so a mover has to be given a state.
A Matinee with `bLooping`, or one whose own `Completed` output leads back to
itself, runs forever in UT3 and becomes `ConstantLoop`. Anything else is a
scripted one-shot -- DM-HeatRay's cinematic ship fires 120s after a specific
death -- and becomes a dormant `TriggerToggle` that keeps its path for a mapper
to hook up.

**Where the path sits.** UE3 offers `IMF_World`, which states world transforms,
and `IMF_RelativeToInitial`, which states motion about the actor's initial one.
Both become the `KeyPos`/`KeyRot` offset UT2004 adds to the placed transform, so
a world track subtracts where the actor sits and a relative track subtracts its
own value at t=0. Either way `KeyPos[0]` comes out (0,0,0), which matters for
more than tidiness: UnrealEd draws a mover at `BasePos + KeyPos[KeyNum]`, so a
non-zero key 0 moves the actor off its mark in the editor as well as in game.

A relative track states its motion in the actor's own frame, so those offsets
are turned by the placed rotation on the way out; see make_keys for the maps
that establish it and the one exception that does not follow it.

**Attachments.** UE3 hard-attaches the four trailing carriages to the lead one
and moves them by parenting. UT2004 movers cannot be parented usefully in a
t3d, but they do not need to be: the keys are relative to each mover's own
placed position, so giving every follower the same key list moves the whole
train rigidly. That is exact for translation and only approximate once the
leader rotates, since a follower turns on its own pivot instead of orbiting.
"""

import math

from ut2.t3d import Actor, vec
from convert.curve import played_range, read_vector_curve, sample
from ut3.objects.level import ordered_exports
from ut3.props import read_object_properties

# Engine/Mover.uc: var vector KeyPos[24].
MAX_KEYS = 24

# UE2 rotator units per degree.
ROT_UNITS = 65536.0 / 360.0

# How close to zero a relative track's first key has to be to count as stating
# motion in the actor's own frame. Exact in every map checked -- the keys are
# authored, not computed -- so this only has to absorb float noise.
LOCAL_ORIGIN = 1e-3

# How far to walk the Kismet graph forward from a Matinee looking for a path
# back to itself. DM-HeatRay's train is two hops (Completed -> Delay -> Play);
# a deeper walk starts calling unrelated scripted chains "looping".
LOOP_SEARCH_DEPTH = 3

# Variable links that name something other than a group to animate.
NON_GROUP_LINKS = ("Data", "PlayRate")

MOVER_CLASSES = ("InterpActor", "StaticMeshActor")

# UT3 navigation classes that describe a lift, and their UT2004 counterparts.
LIFT_CENTER_CLASSES = ("LiftCenter", "UTLiftCenter")
LIFT_EXIT_CLASSES = ("LiftExit", "UTJumpLiftExit")
JUMP_EXIT_CLASSES = ("UTJumpLiftExit",)

# UT2004 mover states. LiftCenter.SpecialHandling tests for StandOpenTimed by
# name (Engine/LiftCenter.uc:38), so a lift really does have to be in it.
LIFT_STATE = "StandOpenTimed"
BUMP_STATE = "BumpOpenTimed"
# What the parts of a lift run in. They cannot be bSlave movers sharing the
# lift's tag, because `ALiftCenter::FindBase` scans for actors whose Tag matches
# its LiftTag and errors with "Lift has same tag as another lift" the moment it
# finds a second Mover -- leaving MyLift unset, so bots never use the lift.
# So the parts carry their own tag and their own copy of the path, and the lift
# triggers them: DoOpen fires OpeningEvent as it starts moving
# (Engine/Mover.uc:372), and TriggerOpenTimed runs the identical
# open/StayOpenTime/close cycle, so they stay in step.
PART_STATE = "TriggerOpenTimed"
PART_SUFFIX = "Parts"
# Both ends of that pairing have to agree on it; it is Mover's own default.
STAY_OPEN_TIME = 4.0
LOOP_STATE = "ConstantLoop"
DORMANT_STATE = "TriggerToggle"


def find_lifts(pkg, index):
    """UT3 lift actor export index -> its LiftCenter exports.

    `LiftCenter.MyLift` names the lift outright, which is a far better signal
    than anything the Matinee itself carries: it is what UT3's own bot paths
    use, and UT2004 has the same class with the same job.
    """
    lifts = {}
    for export in pkg.exports:
        if pkg.class_name_of(export) not in LIFT_CENTER_CLASSES:
            continue
        props = _props(pkg, export)
        if props is None:
            continue
        ref = props.get("MyLift")
        if ref is None or ref.is_null or not ref.is_export:
            continue
        lifts.setdefault(ref.export.index, []).append(export)
    return lifts


def find_touch_movers(pkg):
    """Actors a SeqEvent_Mover reports on -- UT3's "a pawn used this mover"."""
    found = set()
    for export in pkg.exports:
        if pkg.class_name_of(export) != "SeqEvent_Mover":
            continue
        props = _props(pkg, export)
        if props is None:
            continue
        ref = props.get("Originator")
        if ref is not None and not ref.is_null and ref.is_export:
            found.add(ref.export.index)
    return found


class MoverStats:
    def __init__(self):
        self.movers = 0
        self.looping = 0
        self.dormant = 0
        self.followers = 0
        self.keys = 0
        self.skipped_no_mesh = 0
        self.skipped_not_placed = 0
        self.tracks = 0
        self.lifts = 0
        self.bumped = 0
        self.lift_nav = 0

    def __str__(self):
        out = "%d movers from %d move track(s)" % (self.movers, self.tracks)
        if self.followers:
            out += " (%d attached, moving with a leader)" % self.followers
        parts = []
        if self.lifts:
            parts.append("%d lift(s)" % self.lifts)
        if self.bumped:
            parts.append("%d bump-opened" % self.bumped)
        if self.looping:
            parts.append("%d looping" % self.looping)
        if self.dormant:
            parts.append("%d dormant awaiting a trigger" % self.dormant)
        if parts:
            out += "; " + ", ".join(parts)
        if self.lift_nav:
            out += "; %d lift nav point(s)" % self.lift_nav
        if self.skipped_no_mesh:
            out += "; %d animated actors without a mesh" % self.skipped_no_mesh
        return out


class MoveTrack:
    """One InterpTrackMove, resolved and ready to become keys."""

    __slots__ = ("pos", "euler", "relative", "length", "looping", "name")

    def __init__(self, pos, euler, relative, length, looping, name):
        self.pos = pos
        self.euler = euler
        self.relative = relative
        self.length = length
        self.looping = looping
        self.name = name


def _props(pkg, export):
    props, start, _end = read_object_properties(pkg, export)
    return props if start is not None else None


def _linked_ops(pkg, props):
    """(link description, [linked SeqOp export]) for every output link."""
    out = []
    links = props.get("OutputLinks")
    if links is None or not len(links):
        return out
    try:
        entries = links.as_props()
    except (ValueError, IndexError):
        return out
    for entry in entries:
        targets = []
        inner = entry.get("Links")
        if inner is not None and len(inner):
            try:
                for link in inner.as_props():
                    ref = link.get("LinkedOp")
                    if ref is not None and not ref.is_null and ref.is_export:
                        targets.append(ref.export)
            except (ValueError, IndexError):
                pass
        out.append((str(entry.get("LinkDesc", "")), targets))
    return out


def _self_sustaining(pkg, export, depth=LOOP_SEARCH_DEPTH):
    """True when following this Matinee's outputs leads back to itself.

    A Kismet chain of `Completed -> Delay -> Play` is a loop with a pause in it,
    which is how UT3 spaces out repeating scenery; UT2004's ConstantLoop is the
    same behaviour without the pause.
    """
    start = export.index
    frontier = [export]
    seen = {start}
    for _ in range(depth):
        following = []
        for op in frontier:
            props = _props(pkg, op)
            if props is None:
                continue
            for _desc, targets in _linked_ops(pkg, props):
                for target in targets:
                    if target.index == start:
                        return True
                    if target.index not in seen:
                        seen.add(target.index)
                        following.append(target)
        frontier = following
    return False


def _rotation_matrix(rotator):
    """UE's FRotationMatrix rows, from a (pitch, yaw, roll) in rotator units."""
    pitch, yaw, roll = (float(c) * math.tau / 65536.0 for c in rotator)
    sp, cp = math.sin(pitch), math.cos(pitch)
    sy, cy = math.sin(yaw), math.cos(yaw)
    sr, cr = math.sin(roll), math.cos(roll)
    return (
        (cp * cy, cp * sy, sp),
        (sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp),
        (-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp),
    )


def _rotate(v, matrix):
    return tuple(sum(v[axis] * matrix[axis][i] for axis in range(3)) for i in range(3))


def find_move_tracks(pkg, index):
    """UT3 actor export index -> MoveTrack, for every Matinee-animated actor."""
    tracks = {}
    for export in pkg.exports:
        if pkg.class_name_of(export) != "SeqAct_Interp":
            continue
        if not pkg.path_of(export.index).startswith("TheWorld"):
            continue
        props = _props(pkg, export)
        if props is None:
            continue
        links = props.get("VariableLinks")
        if links is None or not len(links):
            continue
        try:
            entries = links.as_props()
        except (ValueError, IndexError):
            continue

        data = None
        bindings = []
        for entry in entries:
            desc = str(entry.get("LinkDesc", ""))
            variables = entry.get("LinkedVariables")
            targets = []
            if variables is not None and len(variables):
                for ref in variables.as_objects():
                    if ref.is_export:
                        targets.append(ref.export)
            if desc == "Data":
                data = targets[0] if targets else None
            elif desc not in NON_GROUP_LINKS:
                bindings.append((desc, targets))
        if data is None:
            continue

        groups = _move_groups(pkg, data)
        if not groups:
            continue
        looping = bool(props.get("bLooping")) or _self_sustaining(pkg, export)
        length = _props(pkg, data).get("InterpLength", 0.0) if _props(pkg, data) else 0.0

        for desc, variables in bindings:
            track_props = groups.get(desc)
            if track_props is None:
                continue
            pos = read_vector_curve(track_props.get("PosTrack"))
            euler = read_vector_curve(track_props.get("EulerTrack"))
            if not pos and not euler:
                continue
            relative = str(track_props.get("MoveFrame", "IMF_World")) == "IMF_RelativeToInitial"
            for variable in variables:
                actor = _object_value(pkg, variable)
                if actor is None:
                    continue
                tracks[actor.index] = MoveTrack(pos, euler, relative, length, looping, desc)
    return tracks


def _object_value(pkg, variable):
    props = _props(pkg, variable)
    if props is None:
        return None
    ref = props.get("ObjValue")
    if ref is None or ref.is_null or not ref.is_export:
        return None
    return ref.export


def _move_groups(pkg, data):
    """Group name -> its InterpTrackMove properties, for one InterpData."""
    props = _props(pkg, data)
    if props is None:
        return {}
    groups = props.get("InterpGroups")
    if groups is None or not len(groups):
        return {}
    out = {}
    for ref in groups.as_objects():
        if not ref.is_export:
            continue
        group_props = _props(pkg, ref.export)
        if group_props is None:
            continue
        name = str(group_props.get("GroupName", ""))
        tracks = group_props.get("InterpTracks")
        if tracks is None or not len(tracks):
            continue
        for track_ref in tracks.as_objects():
            if not track_ref.is_export:
                continue
            if pkg.class_name_of(track_ref.export) != "InterpTrackMove":
                continue
            track_props = _props(pkg, track_ref.export)
            if track_props is not None:
                out[name] = track_props
    return out


def make_keys(track, rotation, location, max_keys=MAX_KEYS, scale=1.0):
    """(KeyPos, KeyRot, MoveTime) for a move track on an actor placed at `location`.

    UT2004 evaluates a mover as `BasePos + KeyPos[i]` and `BaseRot + KeyRot[i]`,
    where the base is where the actor is placed, so each key is whatever offset
    reproduces UE3's world transform at that time. The two move frames each give
    that offset a different origin:

      IMF_World               the track states world transforms, so subtract
                              where the actor is placed.
      IMF_RelativeToInitial   the track states motion about the actor's initial
                              transform, so subtract the track's own value at
                              t=0. Key 0 is then exactly (0,0,0) and the mover
                              starts where UT3 shows the actor at rest.

    A relative track also states its motion in the actor's *own* frame, so the
    offsets have to be turned by the placed rotation before UT2004 can add them
    to BasePos. Two maps show what happens otherwise, both unambiguous because
    the actor says in its own name which way it must go:

      CTF-Vertebrae  LowerLift1  local (0,0,-992), placed at 180 degrees of
                                 roll. Unturned, the lift descends 992uu.
      DM-RisingSun   Lift1       local (0,380,0), placed at -90 roll. Unturned,
                                 the lift travels 380uu sideways and not up at
                                 all.

    DM-Defiance is the same fault at a scale that cannot be missed: its trains
    are placed along tracks laid at 5.625 degrees, and 68544uu of travel at that
    angle leaves the train 6718uu to the side of its rails by the end of the run.

    The rule is confined to tracks whose first key is the origin, which is what
    a relative track states when the actor is at rest. Some are not -- DM-HeatRay
    holds world coordinates in a track flagged relative, its bullet train running
    from x=-50528 to x=19493 along the y=3284 its viaduct is built on -- and for
    those the delta between keys is already a world offset. Composing those
    through the placed transform (180 degrees of yaw) drives the train backwards
    off the end of the line, and composing the full transform additionally lifts
    it 244uu off the deck and 3284uu sideways into open air. Both were tried in
    the editor and neither is what UT3 does.
    """
    points = track.pos or track.euler
    # Only the window Matinee plays, which is not always the curve's own extent.
    start, end = played_range(points, track.length)
    played = [pt for pt in points if start <= pt.t <= end]
    count = 2 if len(played) <= 2 else max_keys

    # `sample` returns one value for a single-key track whatever is asked of
    # it, and a track may hold one key for position while holding several for
    # rotation -- WAR-ColdHarbor has a mover that only turns. One key means a
    # constant, so it is held for every frame rather than indexed past the end.
    def _frames(track_points):
        if not track_points:
            return None
        values = sample(track_points, count, start, end)
        if not values:
            return None
        while len(values) < count:
            values.append(values[-1])
        return values

    positions = _frames(track.pos)
    eulers = _frames(track.euler)

    # What each frame measures its offsets from.
    pos_origin = positions[0] if track.relative else location
    rot_origin = eulers[0] if track.relative else None

    # And what frame those offsets are *in*. A relative track anchored at the
    # origin states motion in the actor's own space, so it has to be turned by
    # the placed rotation to become the world offset UT2004 adds to BasePos.
    frame = None
    if positions is not None and track.relative and any(rotation):
        if all(abs(c) < LOCAL_ORIGIN for c in positions[0]):
            frame = _rotation_matrix(rotation)

    key_pos, key_rot = [], []
    for i in range(count):
        if positions is None:
            offset = (0.0, 0.0, 0.0)
        else:
            offset = tuple(positions[i][j] - pos_origin[j] for j in range(3))
            if frame is not None:
                offset = _rotate(offset, frame)
        key_pos.append(tuple(c * scale for c in offset))

        # UE3 stores rotation as euler degrees ordered (roll, pitch, yaw); UE2
        # rotators are (pitch, yaw, roll) in 65536ths of a turn.
        if eulers is None:
            key_rot.append((0, 0, 0))
            continue
        e = eulers[i]
        turn = [int(round(e[1] * ROT_UNITS)), int(round(e[2] * ROT_UNITS)),
                int(round(e[0] * ROT_UNITS))]
        if rot_origin is not None:
            base = [int(round(rot_origin[1] * ROT_UNITS)),
                    int(round(rot_origin[2] * ROT_UNITS)),
                    int(round(rot_origin[0] * ROT_UNITS))]
            turn = [turn[j] - base[j] for j in range(3)]
        else:
            turn = [turn[j] - int(rotation[j]) for j in range(3)]
        key_rot.append(tuple(turn))

    span = end - start
    move_time = (span / (count - 1)) if count > 1 and span > 0 else 1.0
    return key_pos, key_rot, move_time


def _followers(pkg, leader_index, placed):
    """Actors hard-attached to `leader_index`, directly or through another."""
    found = []
    frontier = {leader_index}
    while frontier:
        children = set()
        for export in placed:
            if export.index in frontier:
                continue
            props = _props(pkg, export)
            if props is None:
                continue
            base = props.get("Base")
            if base is None or base.is_null or not base.is_export:
                continue
            if base.export.index in frontier and export.index not in children:
                children.add(export.index)
                found.append(export)
        frontier = children
    return found


def convert_movers(pkg, index, mesh_set, texture_set=None, scale=1.0, stats=None,
                   max_keys=MAX_KEYS, skip_effects=True):
    """Emit UT2004 Movers for Matinee-animated actors; returns (actors, moved, stats).

    `moved` is the set of UT3 actor names that became movers, so the static mesh
    pass can leave them alone rather than placing a second copy.
    """
    from convert.meshes import _component_of, _material_overrides, sanitize
    from convert.shaders import mesh_is_effect

    stats = stats or MoverStats()
    effect_cache = {}
    tracks = find_move_tracks(pkg, index)
    stats.tracks = len(tracks)
    if not tracks:
        return [], set(), stats

    lifts = find_lifts(pkg, index)
    touched = find_touch_movers(pkg)
    placed = ordered_exports(pkg, MOVER_CLASSES)
    by_index = {e.index: e for e in placed}

    out = []
    moved = set()
    names = set()
    lift_tags = {}
    for leader_index, track in tracks.items():
        leader = by_index.get(leader_index)
        if leader is None:
            stats.skipped_not_placed += 1
            continue
        props = _props(pkg, leader)
        rotation = (0, 0, 0)
        location = (0.0, 0.0, 0.0)
        if props is not None:
            rot_prop = props.get("Rotation")
            if rot_prop is not None and rot_prop.value:
                rotation = tuple(rot_prop.value)
            loc_prop = props.get("Location")
            if loc_prop is not None and loc_prop.value:
                location = tuple(loc_prop.value)
        key_pos, key_rot, move_time = make_keys(track, rotation, location, max_keys, scale)

        # What the mover does when the level starts, in order of how directly
        # the map states it.
        if leader_index in lifts:
            state = LIFT_STATE
        elif leader_index in touched:
            state = BUMP_STATE
        elif track.looping:
            state = LOOP_STATE
        else:
            state = DORMANT_STATE
        tag = sanitize(leader.name)
        followers = _followers(pkg, leader_index, placed)
        # A lift drives its parts by event rather than by attachment, so the
        # lift itself is the only Mover wearing the tag its LiftCenter names.
        is_lift = state == LIFT_STATE
        part_tag = tag + PART_SUFFIX

        group = [(leader, False)] + [(f, True) for f in followers]
        for export, is_follower in group:
            if export.index in moved:
                continue
            if not is_follower:
                actor_state, actor_tag, slave = state, tag, False
            elif is_lift:
                actor_state, actor_tag, slave = PART_STATE, part_tag, False
            else:
                actor_state, actor_tag, slave = state, tag, True
            actor = _make_mover(pkg, index, export, mesh_set, key_pos, key_rot,
                                move_time, track, scale, names,
                                _component_of, _material_overrides, sanitize,
                                mesh_is_effect if skip_effects else None, effect_cache,
                                state=actor_state, tag=actor_tag, slave=slave,
                                opening_event=(part_tag if is_lift and not is_follower
                                               and followers else None),
                                timed=is_lift)
            if actor is None:
                # Animated or not, an unlit translucent effect mesh has no UE2
                # equivalent -- DM-HeatRay's wobbling light beams would arrive as
                # solid opaque cones. Same rule as the static pass.
                stats.skipped_no_mesh += 1
                # Still claim it, so the static pass does not place it either.
                moved.add(export.index)
                continue
            moved.add(export.index)
            out.append(actor)
            stats.movers += 1
            stats.keys += len(key_pos)
            if is_follower:
                stats.followers += 1
            if is_follower:
                continue
            if state == LIFT_STATE:
                stats.lifts += 1
                lift_tags[leader_index] = tag
            elif state == BUMP_STATE:
                stats.bumped += 1
            elif state == LOOP_STATE:
                stats.looping += 1
            else:
                stats.dormant += 1

    nav, nav_stats = convert_lift_nav(pkg, index, lifts, lift_tags, scale, stats)
    out.extend(nav)
    return out, {by_index[i].name for i in moved if i in by_index}, stats


def _make_mover(pkg, index, export, mesh_set, key_pos, key_rot, move_time, track,
                scale, names, component_of, material_overrides, sanitize,
                is_effect=None, effect_cache=None, state=DORMANT_STATE, tag=None,
                slave=False, opening_event=None, timed=False):
    props = _props(pkg, export)
    if props is None:
        return None
    comp = component_of(pkg, export, props)
    if comp is None:
        return None
    mesh_ref = comp.get("StaticMesh")
    if mesh_ref is None or mesh_ref.is_null:
        return None
    if is_effect is not None and is_effect(pkg, index, mesh_ref, effect_cache):
        return None
    mesh_name = mesh_set.add(pkg, index, mesh_ref, material_overrides(pkg, comp))
    if mesh_name is None:
        return None

    properties = [
        ("StaticMesh", "StaticMesh'%s.%s'" % (mesh_set.package_name, mesh_name)),
        ("DrawType", "DT_StaticMesh"),
    ]
    location = props.get("Location")
    if location is not None and location.value:
        properties.append(("Location", vec([c * scale for c in location.value])))
    rotation = props.get("Rotation")
    if rotation is not None and rotation.value and any(rotation.value):
        properties.append(("Rotation", "(Pitch=%d,Yaw=%d,Roll=%d)" % tuple(rotation.value)))
    draw_scale = props.get("DrawScale", 1.0)
    if draw_scale != 1.0:
        properties.append(("DrawScale", "%f" % draw_scale))
    s3d = props.get("DrawScale3D")
    if s3d is not None and s3d.value and tuple(s3d.value) != (1.0, 1.0, 1.0):
        properties.append(("DrawScale3D", vec(s3d.value)))

    # A follower needs no path of its own. A non-slave Mover attaches every
    # bSlave Mover sharing its Tag to itself at PostBeginPlay
    # (Engine/Mover.uc:454), which is UE2's own idiom for a multi-part mover and
    # carries rotation properly -- a duplicated key list would only translate.
    if slave:
        properties.extend([("Tag", '"%s"' % tag), ("bSlave", "True")])
    else:
        for i, p in enumerate(key_pos):
            properties.append(("KeyPos(%d)" % i, vec(p)))
        for i, r in enumerate(key_rot):
            if any(r):
                properties.append(("KeyRot(%d)" % i, "(Pitch=%d,Yaw=%d,Roll=%d)" % r))
        properties.extend([
            ("Tag", '"%s"' % tag),
            ("NumKeys", str(len(key_pos))),
            ("MoveTime", "%f" % move_time),
            # Resampled keys already carry the curve's own acceleration; gliding
            # into and out of each one on top turns a smooth path into a stutter.
            ("MoverGlideType", "MV_MoveByTime"),
            ("bDynamicLightMover", "True"),
            ("InitialState", '"%s"' % state),
        ])
        # A lift and its parts each run their own open/wait/close cycle, so both
        # ends have to agree on how long the wait is.
        if timed:
            properties.append(("StayOpenTime", "%f" % STAY_OPEN_TIME))
        if opening_event:
            properties.append(("OpeningEvent", '"%s"' % opening_event))
        # Background scenery on a fixed path should never shove or crush, which
        # is UT2004's default. A lift or a door is the opposite case: it carries
        # players, so leave it the engine's own ME_ReturnWhenEncroach.
        if state in (LOOP_STATE, DORMANT_STATE):
            properties.append(("MoverEncroachType", "ME_IgnoreWhenEncroach"))

    name = sanitize(export.name)
    if name in names:
        n = 2
        while "%s_%d" % (name, n) in names:
            n += 1
        name = "%s_%d" % (name, n)
    names.add(name)
    actor = Actor("Mover", name, properties)
    actor.ut3_name = export.name
    # A slave writes no keys but still travels the leader's path, so the void
    # calculation needs them even though the t3d does not.
    actor.key_offsets = key_pos
    return actor


def attach_sounds(movers, sound_actors, stats=None):
    """Fold ambient sounds attached to a mover onto the mover itself.

    UT3 hangs the engine hum of each bullet-train carriage off the carriage.
    Left as separate actors those stay parked where the train started, so the
    sound properties move onto the Mover -- they are plain Actor properties and
    a Mover carries them just as well.
    """
    by_name = {getattr(m, "ut3_name", m.name): m for m in movers}
    kept = []
    absorbed = 0
    for actor in sound_actors:
        mover = by_name.get(getattr(actor, "base_name", None))
        if mover is None or any(k.startswith("SoundEmitters") for k, _v in actor.properties):
            kept.append(actor)
            continue
        for key, value in actor.properties:
            if key in ("AmbientSound", "SoundRadius", "SoundVolume", "SoundPitch"):
                mover.properties.append((key, value))
        absorbed += 1
    if stats is not None and absorbed:
        stats.actors -= absorbed
        stats.attached_to_movers = absorbed
    return kept


def key_extent(actor):
    """(low, high) world corners a mover reaches over its whole path.

    A mover outside the level's subtract brush is in solid space and renders
    nowhere, so the void has to cover the path, not just the placed position --
    DM-HeatRay's bullet train travels 70,000uu from where it sits. Slaves are
    included: they write no keys of their own but ride the leader's.
    """
    import re

    location = None
    for key, value in actor.properties:
        if key == "Location":
            m = re.match(r"\(X=(\S+?),Y=(\S+?),Z=(\S+?)\)", value)
            if m:
                location = [float(c) for c in m.groups()]
    if location is None:
        return None
    low, high = list(location), list(location)
    for offset in getattr(actor, "key_offsets", ()):
        for i in range(3):
            low[i] = min(low[i], location[i] + offset[i])
            high[i] = max(high[i], location[i] + offset[i])
    return tuple(low), tuple(high)


def convert_lift_nav(pkg, index, lifts, lift_tags, scale=1.0, stats=None):
    """UT3 LiftCenter/LiftExit -> the UT2004 classes of the same name.

    They map across almost exactly: both engines mark the spot on a lift a bot
    rides from and the spots it steps off at, and both bind to the lift itself.
    The binding differs only in mechanism -- UE3 points `MyLift` straight at the
    actor, UE2 matches `LiftTag` against the mover's `Tag` -- so the tag the
    mover was given is what goes on the nav points.

    UT3 leaves `MyLift` unset on its exits and works out the pairing at runtime,
    so exits are assigned to the nearest lift centre here. DM-Deck's two lifts
    sit 832uu apart with their exits clustered around each, which that resolves
    cleanly.
    """
    from convert.pickups import UT2_HEIGHTS, UT3_NAV_HEIGHT

    drop = UT3_NAV_HEIGHT - UT2_HEIGHTS["PathNode"]
    centres = []
    for lift_index, exports in lifts.items():
        tag = lift_tags.get(lift_index)
        if tag is None:
            continue
        for export in exports:
            props = _props(pkg, export)
            location = props.get("Location") if props else None
            if location is None or not location.value:
                continue
            centres.append((tuple(location.value), tag, export.name))

    out = []
    names = set()

    def add(cls, name, location, tag, extra=()):
        safe = name
        if safe in names:
            n = 2
            while "%s_%d" % (safe, n) in names:
                n += 1
            safe = "%s_%d" % (safe, n)
        names.add(safe)
        properties = [("Location", vec([location[0] * scale, location[1] * scale,
                                        (location[2] - drop) * scale])),
                      ("LiftTag", '"%s"' % tag)]
        properties.extend(extra)
        out.append(Actor(cls, safe, properties))

    for location, tag, name in centres:
        add("LiftCenter", name, location, tag)

    if centres:
        for export in pkg.exports:
            cls = pkg.class_name_of(export)
            if cls not in LIFT_EXIT_CLASSES:
                continue
            props = _props(pkg, export)
            location = props.get("Location") if props else None
            if location is None or not location.value:
                continue
            here = tuple(location.value)
            _distance, tag, _name = min(
                ((sum((here[i] - c[0][i]) ** 2 for i in range(3)), c[1], c[2])
                 for c in centres), key=lambda entry: entry[0])
            extra = [("bLiftJumpExit", "True")] if cls in JUMP_EXIT_CLASSES else []
            add("LiftExit", export.name, here, tag, extra)

    if stats is not None:
        stats.lift_nav = len(out)
    return out, stats
