"""Streaming sub-levels: the rest of the map, in other packages.

Angels Fall First builds a map as a nearly empty persistent level that streams
in the parts. AFF-Errah.udk holds 12 StaticMeshActors and 9 lights in a world
48,000 x 85,000 uu across; the map itself is three packages beside it --
LOC-Errah-Terrain (3,506 mesh actors, 50 lights), LOC-Errah-CampLewis (5,491
and 132) and BRF-generic-assets -- named only from the WorldInfo.

    WorldInfo.StreamingLevels -> LevelStreaming* objects -> PackageName

UT2004 has no level streaming, so a converted map has to be the union. Only the
levels the game always has loaded are merged: `LevelStreamingAlwaysLoaded` is
part of the map by definition, while `LevelStreamingAuto` is conditional --
AFF-Errah's four are briefing rooms, which are not places the map is played and
would land on top of it.

Nothing here merges *packages*. Each sub-level is opened as itself and run
through the same converters as the persistent level, accumulating into the
shared mesh and texture sets; object references stay inside the package that
made them, which is the only way they resolve correctly.
"""

from ut3.props import read_object_properties

# Loaded whenever the map is. The other kinds (LevelStreamingAuto,
# LevelStreamingDistance, LevelStreamingKismet) come and go at runtime.
ALWAYS_LOADED = ("LevelStreamingAlwaysLoaded",)


def streaming_level_names(pkg, kinds=ALWAYS_LOADED):
    """The package names this level always streams in, in declaration order."""
    names = []
    for export in pkg.exports:
        if pkg.class_name_of(export) != "WorldInfo":
            continue
        props, start, _end = read_object_properties(pkg, export)
        if start is None:
            continue
        array = props.get("StreamingLevels")
        if array is None or not array.count:
            continue
        for ref in array.as_objects():
            if not ref.is_export:
                continue
            if pkg.class_name_of(ref.export) not in kinds:
                continue
            level, level_start, _e = read_object_properties(pkg, ref.export)
            if level_start is None:
                continue
            name = level.get("PackageName")
            if name and name not in names:
                names.append(str(name))
    return names


def open_levels(pkg, index, kinds=ALWAYS_LOADED):
    """Open every always-loaded sub-level. Returns (packages, missing names).

    A sub-level that is not installed is reported rather than raised on: the
    map still converts, just without that piece, and the caller says so.
    """
    levels, missing = [], []
    for name in streaming_level_names(pkg, kinds):
        sub = index.package(name)
        if sub is None:
            missing.append(name)
        else:
            levels.append((name, sub))
    return levels, missing
