"""ULevel reader -- specifically the actor order, which CSG depends on.

`Level.Actors` is natively serialized (a TTransArray: count, max, owner, then
one PackageIndex per actor) rather than exposed as a tagged property. The order
matters: both engines apply brush CSG in level-actor order, and the export table
is sorted by name, so converting in export order applies subtractive brushes
before the additive brushes they are meant to carve.
"""

import struct

from ..props import read_object_properties


# Placed actors live under TheWorld.PersistentLevel. Anything else with an
# actor class is a class default object cooked into the package -- UT3 ships
# `Default__UTArmorPickup_ShieldBelt` and friends inside the map -- and
# converting one puts a phantom pickup or light at the world origin.
LEVEL_PATH = "PersistentLevel"


def is_placed_actor(pkg, export):
    """Is this export an actor in the level, rather than a class default?"""
    return LEVEL_PATH in pkg.path_of(export.index)


def actor_order(pkg):
    """Export indices of the level's actors, in the order CSG applies them."""
    levels = pkg.exports_of_class("Level")
    if not levels:
        return []
    level = levels[0]
    data = pkg.export_data(level)
    _props, start, end = read_object_properties(pkg, level)
    if start is None:
        end = 0
    if end + 12 > len(data):
        return []
    count, _max, _owner = struct.unpack_from("<3i", data, end)
    if not (0 <= count <= 1_000_000) or end + 12 + count * 4 > len(data):
        return []
    refs = struct.unpack_from("<%di" % count, data, end + 12)
    return [r for r in refs if 0 < r <= pkg.export_count]


def ordered_exports(pkg, class_names):
    """Exports of the given classes in level order, with any stragglers appended."""
    wanted = set(class_names)
    order = actor_order(pkg)
    seen = set()
    out = []
    for index in order:
        export = pkg.exports[index - 1]
        if pkg.class_name_of(export) in wanted and index not in seen:
            seen.add(index)
            out.append(export)
    for export in pkg.exports:
        if pkg.class_name_of(export) in wanted and export.index not in seen:
            out.append(export)
    return out


def is_builder_brush(pkg, export, props=None):
    """Is this UnrealEd's builder brush rather than real level geometry?

    It exists only as the editor's shape template and must not be converted:
    built as solid it is a slab of BSP standing in mid-air, wearing the
    placeholder texture because a template has no materials, and with no brush
    to select in the editor because UT2004's own builder brush has taken its
    place. DM-Deimos had one beside PathNode_76.

    Two signals, either of which settles it.

    The absent CsgOper is the reliable one. `ABrush` defaults it to CSG_Active,
    which is the builder brush's own value, so a real brush always serializes an
    explicit CSG_Add or CSG_Subtract and the builder never serializes anything.
    Across the 55 stock maps that picks out exactly one brush per map, no more
    and no fewer -- and the same holds for a TOXIKK UDK map, 83 additive and 68
    subtractive brushes with one template among them.

    The model name is the weaker one and only a partial signal: the builder's
    model is sometimes named "Brush" where a real brush's is "Model_<n>", but in
    26 of those maps -- DM-Deimos among them -- it is named "Model_4" or the like
    and looks no different from real geometry. It is kept because it costs
    nothing and covers a map whose CsgOper somehow survives.
    """
    if pkg.class_name_of(export) != "Brush":
        return False
    if props is None:
        props, start, _end = read_object_properties(pkg, export)
        if start is None:
            return False
    # Absence only means anything when the list was read. An export whose
    # properties could not be parsed at all has no CsgOper either, and calling
    # that the builder brush deletes real geometry.
    if not len(props):
        return False
    if props.get("CsgOper") is None:
        return True
    model = props.get("Brush")
    return bool(model is not None and not model.is_null and model.name == "Brush")


def world_info(pkg):
    """The map's WorldInfo properties, or None.

    UE3 keeps level-wide settings here rather than on the level object: it is
    where `KillZ` lives, among others.
    """
    from ..props import read_object_properties

    for export in pkg.exports:
        if pkg.class_name_of(export) != "WorldInfo":
            continue
        props, start, _end = read_object_properties(pkg, export)
        if start is not None:
            return props
    return None


def kill_z(pkg):
    """The height below which UT3 kills anything that falls, or None.

    UT2004's own default is -10000, which for a map that kills at -1554 means
    a player who walks off the edge falls a very long way and then lands on the
    bottom of the world instead of dying.
    """
    props = world_info(pkg)
    if props is None:
        return None
    value = props.get("KillZ")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
