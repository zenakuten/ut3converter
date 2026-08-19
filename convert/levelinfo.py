"""The level's LevelInfo, carrying its game type.

A converted map has no LevelInfo of its own, so the editor supplies one at its
class defaults -- and `PreCacheGame` defaults to `"xGame.xDeathMatch"`
(Engine/LevelInfo.uc:522). That string is what the level precaches textures and
static meshes through (LevelInfo.uc:300 and :336, via DynamicLoadObject), so an
Onslaught map left at the default precaches for deathmatch and never touches
the vehicle and node content it actually needs.

The importer takes a LevelInfo from the t3d as the real thing rather than as an
extra actor: `if( Cast<ALevelInfo>(Actor) )` moves it to Actors(0) and drops the
one the level came with (Editor/Src/UnEdFact.cpp:637), which is the same path a
stock map's own exported t3d goes through.

Only PreCacheGame is set, and deliberately so: on save the editor fills
LevelSummary.ExtraInfo with the map's Onslaught link setups, but only when
`LevelInfo.ExtraInfo` is still empty (Editor/Src/UnEdSrv.cpp:2862). Setting it
here would suppress that and cost the map its setup list in the menus.

The game type is taken from what the conversion actually produced -- power
cores mean Onslaught, flag bases mean CTF -- rather than from the map's name,
since the name is a convention and the actors are the fact.
"""

from ut2.t3d import Actor

DEATHMATCH = "xGame.xDeathMatch"
CTF = "xGame.xCTFGame"
# Stock UT2004, from the bonus pack: CTF with vehicles, and the only difference
# that matters here is that it disables the translocator.
VEHICLE_CTF = "XGame.xVehicleCTFGame"
ONSLAUGHT = "Onslaught.ONSOnslaughtGame"


def game_type(onslaught_stats=None, objective_stats=None):
    """The UT2004 game class a converted map should precache for."""
    if onslaught_stats is not None and getattr(onslaught_stats, "cores", 0):
        return ONSLAUGHT
    by_class = getattr(objective_stats, "by_class", None) or {}
    if any("FlagBase" in cls for cls in by_class):
        # Flags *and* vehicles is VCTF, which UT2004 has as stock. The vehicles
        # need nothing else: outside Onslaught an ONSVehicleFactory activates
        # itself for the team of the nearest GameObjective
        # (Onslaught/ONSVehicleFactory.uc:41), and a flag base is one.
        if getattr(onslaught_stats, "vehicles", 0):
            return VEHICLE_CTF
        return CTF
    return DEATHMATCH


# What LevelInfo.CustomRadarRange may be, once set: ONSHUDOnslaught clamps it
# to Clamp(Level.CustomRadarRange, 500.0, RadarMaxRange) (ONSHUDOnslaught.uc:85,
# with RadarMaxRange=500000 at :876).
MIN_RADAR_RANGE = 500.0
MAX_RADAR_RANGE = 500000.0


def radar_range(bounds):
    """How far the Onslaught radar has to reach, as a half-extent.

    The radar is centred on the world origin -- `MapCenter = vect(0,0,0)`
    (ONSHUDOnslaught.uc:270) -- and RadarRange is the distance it covers from
    there, so what matters is the furthest the play area gets from the origin on
    either axis, not how wide it is.
    """
    (lo, hi) = bounds
    reach = max(abs(lo[0]), abs(hi[0]), abs(lo[1]), abs(hi[1]))
    return max(MIN_RADAR_RANGE, min(MAX_RADAR_RANGE, reach))


def make_level_info(game, name="LevelInfo0", bounds=None, radar_image=None):
    """The level's LevelInfo, naming the game type twice over.

    `DefaultGameType` is the one that decides what the map launches as -- it is
    all ONS-Torlan sets, leaving PreCacheGame at its deathmatch default -- and
    it has to be stated here rather than left to the editor. On save the editor
    does fill it in from the map's filename prefix, but through
    `TObjectIterator<ALevelInfo>` (Engine/Src/UnLevel.cpp:1203), which walks
    every LevelInfo *object* and returns after fixing the first one that needs
    it. Importing this actor leaves the level's original LevelInfo orphaned but
    still in memory, so the iterator finds that one, fixes it, and returns --
    and the map saves with no game type at all, loading as team deathmatch with
    none of its nodes.
    """
    properties = [
        ("DefaultGameType", '"%s"' % game),
        ("PreCacheGame", '"%s"' % game),
    ]
    # The Onslaught radar sizes itself from the terrain by default --
    # `RadarRange = |PrimaryTerrain.TerrainScale.X * TerrainMap.USize| / 2`
    # (ONSHUDOnslaught.uc:83), gated on `bUseTerrainForRadarRange`, which
    # LevelInfo defaults to true. That is a poor measure of a converted map: a
    # UT3 terrain is sized for the landscape it draws, not for the part you
    # play in, and WAR-Torlan carries a second one covering the distant scenery
    # at 1032uu quads. Sized from that, the radar spans 132,096uu of a map whose
    # play area is 54,372 wide, and the whole level is drawn into the middle
    # fifth of the minimap. Measuring the play area instead is exact, so the
    # terrain heuristic is turned off rather than corrected.
    if game == ONSLAUGHT and bounds is not None:
        properties.extend([
            ("bUseTerrainForRadarRange", "False"),
            ("CustomRadarRange", "%f" % radar_range(bounds)),
        ])
    # The HUD draws this behind the node graph and returns early when it is
    # None (ONSHUDOnslaught.uc:276), which is why a converted map's minimap has
    # no background at all.
    if radar_image:
        properties.append(("RadarMapImage", "Texture'%s'" % radar_image))
    return Actor("LevelInfo", name, properties)
