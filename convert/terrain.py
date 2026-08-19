"""Terrain conversion: UE3 ATerrain -> UT2004 TerrainInfo.

Both engines store a heightfield as 16-bit values on a grid, which makes this
mostly a question of reconciling two scale conventions.

    UE3   Z = Location.Z  + (h - 32768) / 128 * DrawScale * DrawScale3D.Z
    UE2   Z = Location.Z  + (h - 32767) * TerrainScale.Z / 256
    UE2   X = Location.X  + (x - HeightmapX/2) * TerrainScale.X

UE2's mapping is **centred**: CalcCoords builds ToWorld and then re-centres it
with `ToWorld /= FVector(HeightmapX/2, HeightmapY/2, 32767)`
(Engine/Src/UnTerrain.cpp:1462-1468). So Location is the world position of the
heightmap's *centre* at height 32767, not of its corner at height 0, and 32767
is the engine's zero point -- which is why working UT2004 heightmaps are mid-grey
rather than dark.

Passing the raw heights through would give

    TerrainScale.Z = 2 * DrawScale * DrawScale3D.Z
    Location.Z     = UE3 Location.Z - 32768 * DrawScale * DrawScale3D.Z / 128

Both engines centre their heights, so the raw values pass through untouched and
only the origin has to move:

    TerrainScale   = (DrawScale * DrawScale3D.X, ...Y, 2 * DrawScale * DrawScale3D.Z)
    Location.X     = UE3 Location.X + (HeightmapX/2) * TerrainScale.X
    Location.Y     = UE3 Location.Y + (HeightmapY/2) * TerrainScale.Y
    Location.Z     = UE3 Location.Z - DrawScale * DrawScale3D.Z / 128

(the Z term is just UE3's 32768 zero against UE2's 32767).

X and Y are simpler: a UE3 patch is DrawScale * DrawScale3D wide, which is
exactly what TerrainScale.X/Y mean.

The heightmap becomes a 16-bit BMP (the only import path that yields TEXF_G16)
and each layer's alpha map a 32-bit TGA, since TEXF_RGBA8 alpha maps are read
from the alpha channel (UnTerrain.cpp:1516). UE3 sizes its vertex grid freely,
so it is resampled onto a square power-of-two grid, which UT2004 requires (see
_grid_size); TerrainScale.X and .Y then differ to span the original rectangle.
"""

import os

from ut2.bmp import write_bmp16
from ut2.images import write_tga
from ut2.t3d import Actor, vec
from ut3.objects.terrain import read_terrain
from ut3.props import read_object_properties

# UE3 stores heights with this many units per local unit before DrawScale.
UE3_HEIGHT_UNITS = 128.0

# A UE3 TerrainMaterial that says nothing about its tiling still has one:
# Default__TerrainMaterial.MappingScale is 4.0 (Engine.u), not 1.0.
DEFAULT_MAPPING_SCALE = 4.0

# Layer tiling is derived per layer from UE3's MappingScale (see _layer_scale);
# this is only the override --terrain-layer-scale forces when given, and 0 means
# "derive". The old fixed 16 tiled WAR-PowerSurge's ground every 1799uu across
# and 3213uu along, where UT3 asks for 512 both ways.
DEFAULT_LAYER_SCALE = 0.0

# How many scatter attempts each terrain quad gets, and what fraction of them
# take. UE3 states neither: `Density` is stored as 0 on every foliage entry in
# WAR-Serenity, so the count has to come from somewhere else, and the only thing
# UT3 does say is where the foliage goes -- AlphaMapThreshold against the
# layer's own weight. So coverage is taken from UT3 and quantity is a tunable.
DEFAULT_DECO_DENSITY = 0.5
DECO_ATTEMPTS_PER_QUAD = 4

# Where the fade begins, as a fraction of UE3's MaxDrawRadius: UT2004 fades a
# decoration out between FadeoutRadius.Min and .Max, and UE3 gives only the far
# edge (MinTransitionRadius is 0 on both of Serenity's).
DECO_FADE_START = 0.8


class TerrainStats:
    def __init__(self):
        self.terrains = 0
        self.layers = 0
        self.base_layers = 0
        self.hidden_quads = 0
        self.skipped = []
        self.rotated = []
        self.unrotated = []
        self.deco = []
        self.rendered = []  # what the minimap draws from

    def __str__(self):
        out = "%d terrain(s), %d layers" % (self.terrains, self.layers)
        if self.base_layers:
            out += " (%d base layer(s) made opaque)" % self.base_layers
        if self.hidden_quads:
            out += "; %d quad(s) cut away as UT3 has them" % self.hidden_quads
        if self.deco:
            out += ("; %d decoration layer(s) scattering UT3's ground cover (%s)"
                    % (len(self.deco),
                       ", ".join("%s over %d quads" % d for d in self.deco[:3])))
        if self.rotated:
            out += ("; %s turned upright (UE2 terrain cannot be rotated)"
                    % ", ".join("%s %d deg" % r for r in self.rotated))
        if self.unrotated:
            out += ("; %s is rotated off the axes and stays where UE3 put it"
                    % ", ".join(self.unrotated))
        if self.skipped:
            out += "; skipped " + ", ".join(self.skipped)
        return out


def make_zone_info(bounds, margin, ambient=None, name="ZoneInfo0", terrain=False,
                   kill_z=None):
    """The level's ZoneInfo: its ambient light, its KillZ, and terrain if any.

    Emitted for every map, not only terrain ones. A converted map has no
    ZoneInfo of its own, so every actor's zone is the LevelInfo and every
    level-wide setting has to be applied there by hand. One ZoneInfo inside the
    subtracted space claims the whole interior -- there are no zone portals to
    subdivide it -- and carries them instead.

    UT2004 collects terrains per zone and then explicitly empties the
    LevelInfo's list (ULevel::UpdateTerrainArrays, Engine/Src/UnLevel.cpp:845):

        Actors(i)->Region.Zone->Terrains.AddUniqueItem( ... );
        ...
        L->Terrains.Empty();

    A converted map has no ZoneInfo, so every actor's Region.Zone *is* the
    LevelInfo -- the terrain is registered and then wiped, and never renders.
    One ZoneInfo inside the subtracted space claims the whole interior (there
    are no zone portals to subdivide it) and gives the terrain a real zone.

    The zone must also be flagged `bTerrainZone`. Nothing in the engine sets it
    -- it is a mapper-facing `var()` -- and it gates *everything*: rendering
    (Engine/Src/UnRenderVisibility.cpp:2008 and :2232), collision and traces
    (UnLevTic.cpp:902, 1002, 1065) and Karma (KTriListGen.cpp:323). Without it a
    perfectly built terrain is silently skipped, which is also why the terrain
    edit cursor finds nothing to trace against.

    It also takes over the zone's ambient light from the LevelInfo, so the
    SkyLight conversion is applied here rather than left as a manual step.
    """
    (min_x, min_y, min_z), (max_x, max_y, max_z) = bounds
    # High above the geometry but inside the world brush: reliably open space.
    location = ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0, max_z + margin / 2.0)
    properties = [("Location", vec(location))]
    if terrain:
        properties.append(("bTerrainZone", "True"))
    if kill_z is not None:
        # UT2004 defaults to -10000, far below where UT3 stops a fall.
        properties.append(("KillZ", "%f" % kill_z))
    if ambient:
        brightness, hue, saturation = ambient
        properties.extend([
            ("AmbientBrightness", str(brightness)),
            ("AmbientHue", str(hue)),
            ("AmbientSaturation", str(saturation)),
        ])
    return Actor("ZoneInfo", name, properties)


def _grid_size(width, height):
    """The square power-of-two vertex count to resample a terrain onto.

    UT2004 terrain is square-only, and not merely by convention -- alpha maps
    are asserted square outright (UnTerrain.cpp:175, `//!! Alphamaps must be
    square!`) and the layer lookup derives one Ratio from HeightmapX and
    applies it to both axes (UnTerrain.cpp:222), so a non-square heightmap
    mis-samples every layer along Y even where it does not trip the assert.

    The rectangle is not lost: TerrainScale.X and .Y are independent, so a
    square grid still spans a rectangular area once each axis gets its own
    spacing. Sizing from the *longer* axis means the short one is upsampled
    rather than crushed. Capped at 256, the size of UT2004's own terrains.
    """
    n = max(width, height)
    low = 1
    while low * 2 <= n:
        low *= 2
    high = low * 2
    size = low if n * n <= low * high else high
    return max(8, min(size, 256))


def _visibility_bitmap(terrain, size):
    """UT2004's QuadVisibilityBitmap for a terrain resampled onto size x size.

    UE3 cuts holes in a terrain where meshes provide the floor -- 56% of
    WAR-PowerSurge's quads and 45% of DM-HeatRay's are cut away. Rendering
    them all puts ground back inside the buildings that replaced it, which is
    what makes terrain appear over a mesh that was visible a step earlier.

    UT2004 stores the same thing as a bitmap of DWORDs indexed
    `x + y*HeightmapX` (UnTerrain.h:543), 1 = visible, sized exactly
    HeightmapX*HeightmapY/32 or PostLoad throws it away and marks everything
    visible again (UnTerrain.cpp:1670). Returns (words, hidden_quads), or
    (None, 0) when nothing is cut and the default already does the job.
    """
    words = [0xFFFFFFFF] * (size * size // 32)
    step_x = (terrain.width - 1) / float(size - 1)
    step_y = (terrain.height - 1) / float(size - 1)
    hidden = 0
    for y in range(size):
        src_y = min(int(y * step_y), max(terrain.height - 2, 0))
        for x in range(size):
            src_x = min(int(x * step_x), max(terrain.width - 2, 0))
            if terrain.quad_hidden(src_x, src_y):
                bit = x + y * size
                words[bit >> 5] &= ~(1 << (bit & 31)) & 0xFFFFFFFF
                hidden += 1
    if not hidden:
        return None, 0
    return words, hidden


def _resample(values, src_w, src_h, size):
    """Bilinearly resample a row-major grid onto size x size.

    The corner vertices are pinned to the source corners, so the output covers
    exactly the same ground as the input -- the caller's TerrainScale is
    derived from the same mapping.
    """
    out = []
    step_x = (src_w - 1) / float(size - 1)
    step_y = (src_h - 1) / float(size - 1)
    for y in range(size):
        fy = y * step_y
        y0 = min(int(fy), src_h - 1)
        y1 = min(y0 + 1, src_h - 1)
        ty = fy - y0
        for x in range(size):
            fx = x * step_x
            x0 = min(int(fx), src_w - 1)
            x1 = min(x0 + 1, src_w - 1)
            tx = fx - x0
            top = values[y0 * src_w + x0] * (1 - tx) + values[y0 * src_w + x1] * tx
            bottom = values[y1 * src_w + x0] * (1 - tx) + values[y1 * src_w + x1] * tx
            out.append(int(round(top * (1 - ty) + bottom * ty)))
    return out


# Rotator units in a quarter turn, and how far off one a yaw may be and still
# count as square. UT3 maps do not land exactly on the grid: both of
# VCTF-Containment's terrains are a single rotator unit out (yaw 131073 and
# 163841, 0.0055 degrees), which is authoring noise rather than a rotation, but
# an exact test refuses them and leaves the terrain where UE3 put it. The
# tolerance is nowhere near a real off-axis angle -- 45 degrees is 8192 units.
QUARTER_TURN = 16384
QUARTER_TOLERANCE = 64


def quarter_turns(rotation):
    """A terrain's yaw as a count of quarter turns, or None if it is not one.

    UT2004 terrain cannot be rotated. `ATerrainInfo::CalcCoords` builds ToWorld
    out of Location and TerrainScale and nothing else (Engine/Src/UnTerrain.cpp:
    1464) -- Rotation is simply not in the transform. UE3 rotates terrain freely,
    and WAR-Serenity's is turned 90 degrees, which is why it came out lying
    across the wrong part of the map: converted with the yaw ignored, a
    23040 x 46592 terrain anchored at X 23040 covers X 23040..46080, and the map
    itself is at X -20000..20500.

    So the yaw has to go into the data instead. A quarter turn is exact on a
    grid; anything else would need resampling the heightfield through a rotation
    and is left alone with a warning, since no map here does it.
    """
    if rotation is None or not getattr(rotation, "value", None):
        return 0
    pitch, yaw, roll = (int(round(c)) for c in rotation.value)
    if pitch % 65536 or roll % 65536:
        return None
    yaw %= 65536
    nearest = int(round(yaw / float(QUARTER_TURN))) % 4
    if abs(yaw - nearest * QUARTER_TURN) % 65536 > QUARTER_TOLERANCE:
        return None
    return nearest


def rotate_grid(values, width, height, turns):
    """Rotate a row-major grid. Returns (values, width, height).

    Counter-clockwise, matching UE3's yaw: a quarter turn sends the local +X
    axis to world +Y. The index that falls out is new(i, j) = old(j, H-1-i),
    with the grid's width and height swapped.
    """
    turns %= 4
    if not turns or width < 1 or height < 1:
        return list(values), width, height
    out = list(values)
    w, h = width, height
    for _ in range(turns):
        rotated = [0] * (w * h)
        for j in range(w):          # new rows: as many as the old width
            for i in range(h):      # new columns: as many as the old height
                rotated[j * h + i] = out[(h - 1 - i) * w + j]
        out = rotated
        w, h = h, w
    return out, w, h


def rotated_placement(location, width, height, unit_x, unit_y, turns):
    """Where the rotated grid starts, and its per-axis spacing.

    UE3 anchors a terrain at its first vertex and rotates about it, so turning
    the data upright moves that anchor to whichever corner is now first. Both
    axes must end up increasing, since UE2 has no negative TerrainScale.
    """
    x, y = location[0], location[1]
    span_x = (width - 1) * unit_x
    span_y = (height - 1) * unit_y
    turns %= 4
    if turns == 0:
        return (x, y), (unit_x, unit_y), (width, height)
    if turns == 1:
        return (x - span_y, y), (unit_y, unit_x), (height, width)
    if turns == 2:
        return (x - span_x, y - span_y), (unit_x, unit_y), (width, height)
    return (x, y - span_x), (unit_y, unit_x), (height, width)


def _apply_rotation(terrain, rotation, stats, name):
    """Turn a rotated UE3 terrain upright, in place. Returns False if it cannot.

    Heights and alpha maps are per vertex, but `info` is read per *quad* --
    `quad_hidden(x, y)` takes the flag from the quad's first corner -- so the
    visibility grid is rotated at its own (width-1) x (height-1) size and written
    back. Rotating it as vertex data instead would shift every hole by one quad.
    """
    turns = quarter_turns(rotation)
    if turns is None:
        stats.unrotated.append(name)
        return False
    if not turns:
        return True

    width, height = terrain.width, terrain.height
    quads = []
    if terrain.info:
        source = [1 if terrain.quad_hidden(x, y) else 0
                  for y in range(height - 1) for x in range(width - 1)]
        quads, quad_width, quad_height = rotate_grid(source, width - 1, height - 1, turns)

    terrain.heights, new_width, new_height = rotate_grid(
        terrain.heights, width, height, turns)
    terrain.alpha_maps = [rotate_grid(list(a), width, height, turns)[0]
                          if len(a) >= width * height else a
                          for a in terrain.alpha_maps]
    if quads:
        info = [0] * (new_width * new_height)
        for j in range(quad_height):
            for i in range(quad_width):
                info[j * new_width + i] = quads[j * quad_width + i]
        terrain.info = info
    terrain.width, terrain.height = new_width, new_height
    stats.rotated.append((name, turns * 90))
    return True


def convert_terrain(pkg, index, out_dir, package_name, texture_set=None,
                    scale=1.0, layer_scale=DEFAULT_LAYER_SCALE, stats=None,
                    group="Terrain", mesh_set=None,
                    deco_density=DEFAULT_DECO_DENSITY):
    """Convert every Terrain actor. Returns (t3d actors, exec lines, stats)."""
    stats = stats or TerrainStats()
    actors, exec_lines = [], []
    terrain_dir = os.path.join(out_dir, package_name, "Terrain")

    for export in pkg.exports:
        if pkg.class_name_of(export) != "Terrain":
            continue
        terrain = read_terrain(pkg, export)
        if terrain is None:
            stats.skipped.append("%s (unreadable)" % export.name)
            continue

        if terrain.width < 2 or terrain.height < 2:
            stats.skipped.append("%s (too small)" % export.name)
            continue

        # UE2 terrain is axis-aligned, so a rotated UE3 one is turned upright
        # here rather than at draw time -- see quarter_turns.
        rotation = read_object_properties(pkg, export)[0].get("Rotation")
        base_unit_x = terrain.draw_scale * terrain.draw_scale_3d[0]
        base_unit_y = terrain.draw_scale * terrain.draw_scale_3d[1]
        turns = quarter_turns(rotation)
        origin = tuple(terrain.location)
        if turns:
            corner, (base_unit_x, base_unit_y), _dims = rotated_placement(
                terrain.location, terrain.width, terrain.height,
                base_unit_x, base_unit_y, turns)
            origin = (corner[0], corner[1], terrain.location[2])
        _apply_rotation(terrain, rotation, stats, export.name)

        size = _grid_size(terrain.width, terrain.height)
        os.makedirs(terrain_dir, exist_ok=True)

        base = "".join(c for c in export.name if c.isalnum() or c == "_")
        # Same package tag every other generated texture carries: "Terrain_0" is
        # what UE3 calls the first terrain in every map, so without it each
        # converted map defines a Terrain_0_Height and they collide by name.
        if texture_set is not None and getattr(texture_set, "tag", None):
            base = "%s_%s" % (base, texture_set.tag)
        # Heights pass through: UE2 centres on 32767 just as UE3 centres on 32768.
        heights = _resample(terrain.heights, terrain.width, terrain.height, size)
        height_name = "%s_Height" % base
        write_bmp16(os.path.join(terrain_dir, height_name + ".bmp"), size, size, heights)
        exec_lines.append(
            "#exec TEXTURE IMPORT NAME=%s GROUP=Terrain FILE=Terrain\\%s.bmp MIPS=off"
            % (height_name, height_name)
        )

        # An all-zero alpha map is what UE3 leaves on a base layer it never
        # painted; there is nothing to write, and the layer loop below hands it
        # the shared opaque map instead.
        opaque_name = None
        alpha_names = {}
        for map_index, data in enumerate(terrain.alpha_maps):
            if len(data) < terrain.width * terrain.height:
                continue
            if not any(data):
                continue
            fitted = _resample(list(data), terrain.width, terrain.height, size)
            bgra = bytearray(size * size * 4)
            for i, a in enumerate(fitted):
                bgra[i * 4 : i * 4 + 4] = bytes((255, 255, 255, a))
            name = "%s_Alpha%d" % (base, map_index)
            write_tga(os.path.join(terrain_dir, name + ".tga"), size, size, bytes(bgra))
            exec_lines.append(
                "#exec TEXTURE IMPORT NAME=%s GROUP=Terrain FILE=Terrain\\%s.tga ALPHA=1 MIPS=off"
                % (name, name)
            )
            alpha_names[map_index] = name

        def _opaque_map():
            """A fully opaque alpha map, written once and shared."""
            nonlocal opaque_name
            if opaque_name is None:
                opaque_name = "%s_AlphaBase" % base
                bgra = bytes((255, 255, 255, 255)) * (size * size)
                write_tga(os.path.join(terrain_dir, opaque_name + ".tga"),
                          size, size, bgra)
                exec_lines.append(
                    "#exec TEXTURE IMPORT NAME=%s GROUP=Terrain FILE=Terrain\\%s.tga "
                    "ALPHA=1 MIPS=off" % (opaque_name, opaque_name))
            return opaque_name

        # Layers: resolve each setup's material down to a diffuse texture.
        #
        # The first layer is the base and is always emitted opaque, whatever
        # alpha map it names. UE3 gives the bottom layer the weight the others
        # leave over, so its stored map is at most a partial paint and the
        # layers never sum to full coverage: 70% of WAR-PowerSurge's terrain
        # and 91% of DM-HeatRay's is under-covered, and 54% of PowerSurge has
        # no weight from any layer. UT2004 has no implicit base -- GetLayerAlpha
        # returns 0 for a missing map (UnTerrain.cpp:1487) -- so those texels
        # draw nothing at all and the terrain reads as black, then as a hole
        # once the whole sector is empty.
        layer_entries = []
        base_layer = None
        for layer in terrain.layers:
            setup = layer.get("Setup")
            if setup is None or setup.is_null:
                continue
            texture, mapping_scale, mapping_rotation = _resolve_layer(
                pkg, index, setup, texture_set)
            if texture is None:
                continue
            if not layer_entries:
                alpha = _opaque_map()
                base_layer = layer
                stats.base_layers += 1
            else:
                alpha = alpha_names.get(layer.get("AlphaMapIndex", -1)) or _opaque_map()
            layer_entries.append((texture, alpha, mapping_scale, mapping_rotation))
            stats.layers += 1

        # Ground cover. UT3 hangs it off the layer's *material*, not the layer,
        # and it is what makes a terrain read as grass rather than as a painted
        # texture -- see foliage_entries.
        deco_entries = []
        if mesh_set is not None and deco_density > 0:
            for layer in terrain.layers:
                setup = layer.get("Setup")
                if setup is None or setup.is_null:
                    continue
                # The layer's *own* weight, even where it was forced opaque as
                # the base: that map says where UT3 painted the layer, and the
                # opaque one would scatter grass across the dirt paths too.
                try:
                    alpha_index = int(layer.get("AlphaMapIndex", -1))
                except (TypeError, ValueError):
                    alpha_index = -1
                if not (0 <= alpha_index < len(terrain.alpha_maps)):
                    continue
                if len(terrain.alpha_maps[alpha_index]) < terrain.width * terrain.height:
                    continue
                weights = effective_weights(terrain, alpha_index,
                                            layer is base_layer)
                fitted = _resample(weights, terrain.width, terrain.height, size)
                for owner, mesh_ref, foliage in foliage_entries(pkg, index, setup):
                    mesh_name = mesh_set.name_for(mesh_ref)
                    if mesh_name is None:
                        continue
                    def _f(key, default):
                        try:
                            return float(foliage.get(key, default))
                        except (TypeError, ValueError):
                            return default
                    density = _density_map(fitted, _f("AlphaMapThreshold", 0.5))
                    if not any(density):
                        continue
                    name = "%s_Deco%d" % (base, len(deco_entries))
                    bgra = bytearray(size * size * 4)
                    for i, a in enumerate(density):
                        bgra[i * 4:i * 4 + 4] = bytes((255, 255, 255, a))
                    write_tga(os.path.join(terrain_dir, name + ".tga"),
                              size, size, bytes(bgra))
                    exec_lines.append(
                        "#exec TEXTURE IMPORT NAME=%s GROUP=Terrain FILE=Terrain\\%s.tga "
                        "ALPHA=1 MIPS=off" % (name, name))
                    radius = _f("MaxDrawRadius", 4096.0) * scale
                    deco_entries.append((
                        mesh_name, name,
                        _f("MinScale", 1.0), _f("MaxScale", 1.0),
                        radius * DECO_FADE_START, radius,
                        int(_f("Seed", 0)),
                    ))
                    stats.deco.append((mesh_ref.name, sum(1 for a in density if a)))

        z_unit = terrain.draw_scale * terrain.draw_scale_3d[2] / UE3_HEIGHT_UNITS
        location = origin
        # Vertex spacing after the resample: the grid is square but the ground
        # it covers is not, so each axis stretches its own way to span the same
        # (width-1) or (height-1) UE3 patches across (size-1) UT2004 quads.
        step_x = (terrain.width - 1) / float(size - 1)
        step_y = (terrain.height - 1) / float(size - 1)
        unit_x = base_unit_x * step_x
        unit_y = base_unit_y * step_y
        properties = [
            ("TerrainMap", "Texture'%s.%s.%s'" % (package_name, group, height_name)),
            ("TerrainScale", "(X=%f,Y=%f,Z=%f)" % (
                unit_x * scale, unit_y * scale, z_unit * 256.0 * scale)),
        ]
        for i, (texture, alpha, mapping_scale, mapping_rotation) in enumerate(layer_entries):
            entry = "(Texture=Texture'%s'" % (
                texture_set.path(texture) if texture_set else texture)
            if alpha:
                entry += ",AlphaMap=Texture'%s.%s.%s'" % (package_name, group, alpha)
            u_scale, v_scale = _layer_scale(mapping_scale, step_x, step_y, layer_scale)
            entry += ",UScale=%f,VScale=%f,TextureMapAxis=TEXMAPAXIS_XY" % (
                u_scale, v_scale)
            if mapping_rotation:
                # TextureRotation is a rotator yaw, not degrees.
                entry += ",TextureRotation=%d" % int(
                    round(mapping_rotation * 65536.0 / 360.0))
            entry += ")"
            properties.append(("Layers(%d)" % i, entry))
        for i, (mesh, density, min_scale, max_scale,
                fade_min, fade_max, seed) in enumerate(deco_entries):
            scale_range = "(Min=%f,Max=%f)" % (min_scale, max_scale)
            properties.append(("DecoLayers(%d)" % i, (
                "(ShowOnTerrain=1,DensityMap=Texture'%s.%s.%s'"
                ",StaticMesh=StaticMesh'%s'"
                ",ScaleMultiplier=(X=%s,Y=%s,Z=%s)"
                ",FadeoutRadius=(Min=%f,Max=%f)"
                ",DensityMultiplier=(Min=%f,Max=%f)"
                ",MaxPerQuad=%d,Seed=%d,AlignToTerrain=1,RandomYaw=1"
                ",DrawOrder=SORT_NoSort,ShowOnInvisibleTerrain=0"
                ",LitDirectional=0,DisregardTerrainLighting=0,DetailMode=DM_Low)"
                % (package_name, group, density, mesh,
                   scale_range, scale_range, scale_range,
                   fade_min, fade_max,
                   deco_density * 0.8, deco_density * 1.2,
                   DECO_ATTEMPTS_PER_QUAD, seed))))
        # Everything the minimap needs, as actually emitted: the grid the map
        # carries, not UT3's. See convert/minimap.py.
        stats.rendered.append({
            "size": size, "heights": heights, "origin": location,
            "unit_x": unit_x * scale, "unit_y": unit_y * scale,
            "z_unit": z_unit * scale, "terrain": terrain,
            "layers": [t for t, _a, _m, _r in layer_entries],
        })
        words, hidden = _visibility_bitmap(terrain, size)
        if words is not None:
            # The array is `int`, so the all-visible word reads back as -1.
            for i, word in enumerate(words):
                properties.append(("QuadVisibilityBitmap(%d)" % i,
                                   str(word - (1 << 32) if word > 0x7FFFFFFF else word)))
            stats.hidden_quads += hidden
            stats.rendered[-1]["visible"] = words
        # Location is the heightmap centre at height 32767, not the corner.
        properties.append(("Location", vec((
            (location[0] + (size / 2.0) * unit_x) * scale,
            (location[1] + (size / 2.0) * unit_y) * scale,
            (location[2] - z_unit) * scale,
        ))))

        actors.append(Actor("TerrainInfo", base, properties))
        stats.terrains += 1
    return actors, exec_lines, stats


def foliage_entries(pkg, index, setup_ref):
    """The FoliageMeshes on a terrain layer's material.

    This is where UT3 keeps a layer's ground cover -- `TM_Serenity_Floormix_01`
    carries `S_UN_Foliage_SM_Weed01` and `S_UN_Foliage_SM_Grass03` -- and it is
    a different thing from the layer's texture. Converting the layers without it
    leaves the ground bare, which is most of why a converted terrain looks flat.

    UT2004's counterpart is `TerrainInfo.DecoLayers` (Engine/Classes/
    TerrainInfo.uc:98), which scatters a static mesh per quad against a density
    texture (UnTerrain.cpp:3825) -- close enough to map field for field.
    """
    owner, setup = index.resolve(pkg, setup_ref)
    if setup is None:
        return []
    props, start, _end = read_object_properties(owner, setup)
    if start is None:
        return []
    materials = props.get("Materials")
    if materials is None or not len(materials):
        return []
    out = []
    try:
        entries = materials.as_props()
    except (ValueError, IndexError):
        return []
    for entry in entries:
        ref = entry.get("Material")
        if ref is None or ref.is_null:
            continue
        tm_owner, tm = index.resolve(owner, ref)
        if tm is None:
            continue
        tm_props, tm_start, _e = read_object_properties(tm_owner, tm)
        if tm_start is None:
            continue
        meshes = tm_props.get("FoliageMeshes")
        if meshes is None or not len(meshes):
            continue
        try:
            found = meshes.as_props()
        except (ValueError, IndexError):
            continue
        for foliage in found:
            mesh_ref = foliage.get("StaticMesh")
            if mesh_ref is None or mesh_ref.is_null:
                continue
            out.append((tm_owner, mesh_ref, foliage))
    return out


def register_foliage(pkg, index, mesh_set):
    """Add every terrain foliage mesh to `mesh_set`, before the meshes export.

    Nothing else in the map references these -- they are scattered by the
    terrain, not placed as actors -- so without this pass they never reach the
    package and the DecoLayers point at meshes that do not exist.
    """
    added = 0
    for export in pkg.exports:
        if pkg.class_name_of(export) != "Terrain":
            continue
        terrain = read_terrain(pkg, export)
        if terrain is None:
            continue
        for layer in terrain.layers:
            setup = layer.get("Setup")
            if setup is None or setup.is_null:
                continue
            for owner, mesh_ref, _foliage in foliage_entries(pkg, index, setup):
                if mesh_set.add(owner, index, mesh_ref) is not None:
                    added += 1
    return added


def effective_weights(terrain, alpha_index, is_base):
    """A layer's real coverage, not just the map UE3 stored for it.

    UE3 hands the bottom layer whatever weight the layers above leave over, so
    its stored map is only the part the artist painted *extra*. That is already
    why the base layer is emitted opaque; ground cover needs the same
    correction, and needs it more visibly. Serenity's FloorMix carries weeds
    above alpha 0.7 across 4.6% of its stored map and 62.6% of the terrain once
    the leftover is counted -- the difference between a bare map and a grassy
    one with dirt paths through it.
    """
    weights = list(terrain.alpha_maps[alpha_index])
    if not is_base:
        return weights
    others = [a for i, a in enumerate(terrain.alpha_maps) if i != alpha_index]
    for i in range(len(weights)):
        taken = 0
        for a in others:
            if i < len(a):
                taken += a[i]
        weights[i] = min(255, weights[i] + max(0, 255 - taken))
    return weights


def _density_map(alpha, threshold):
    """A UT2004 density texture from a UE3 layer weight and its threshold.

    UT2004 scatters where `appSRand() < alpha/255 * DensityMultiplier`
    (UnTerrain.cpp:3827), so the texture is the *where*. UE3 says the same thing
    with AlphaMapThreshold: foliage appears where the layer outweighs it. Above
    the threshold the weight is rescaled across the remaining range rather than
    flattened to full, so a layer that fades out takes its ground cover with it
    instead of ending on a hard line.
    """
    cut = max(0.0, min(1.0, threshold)) * 255.0
    span = 255.0 - cut
    out = bytearray(len(alpha))
    for i, value in enumerate(alpha):
        if value <= cut:
            continue
        out[i] = 255 if span <= 0 else int(round((value - cut) / span * 255.0))
    return out


def _layer_scale(mapping_scale, step_x, step_y, override=None):
    """UT2004 UScale/VScale reproducing a UE3 layer's tiling.

    UnTerrain.cpp:1874 builds a layer's texture coordinates by transforming the
    world position into *heightmap* space and then dividing by UScale, so a
    layer repeats every UScale quads -- UScale * TerrainScale.X world units.
    UE3 states the same thing as MappingScale, in its own quads, which are
    DrawScale3D wide. Since the resample restated a UE3 quad as `step` UT2004
    quads, the two are one division apart:

        UScale = MappingScale / step_x        VScale = MappingScale / step_y

    Both axes matter separately. The square grid spans a rectangle, so
    TerrainScale.X and .Y differ (112 and 201 on WAR-PowerSurge) -- one scale
    for both would stretch every layer 1.8x along Y, which is what turned that
    map's ground into horizontal smears.
    """
    if override:
        return override, override
    return mapping_scale / step_x, mapping_scale / step_y


def _resolve_layer(pkg, index, setup_ref, texture_set):
    """A TerrainLayerSetup -> (texture name, MappingScale, MappingRotation).

    MappingScale and MappingRotation live on the TerrainMaterial rather than on
    the layer, and are how UE3 states a layer's tiling -- see `_layer_scale`.
    MappingScale is elided when it is the class default, which is 4.0
    (`Default__TerrainMaterial` in Engine.u), not 1.0.
    """
    owner, setup = index.resolve(pkg, setup_ref)
    if setup is None:
        return None, DEFAULT_MAPPING_SCALE, 0.0
    props, start, _end = read_object_properties(owner, setup)
    if start is None:
        return None, DEFAULT_MAPPING_SCALE, 0.0
    materials = props.get("Materials")
    if materials is None or not len(materials):
        return None, DEFAULT_MAPPING_SCALE, 0.0
    try:
        entries = materials.as_props()
    except (ValueError, IndexError):
        return None, DEFAULT_MAPPING_SCALE, 0.0
    for entry in entries:
        terrain_material = entry.get("Material")
        if terrain_material is None or terrain_material.is_null:
            continue
        tm_owner, tm = index.resolve(owner, terrain_material)
        if tm is None:
            continue
        tm_props, tm_start, _e = read_object_properties(tm_owner, tm)
        if tm_start is None:
            continue
        material = tm_props.get("Material")
        if material is None or material.is_null:
            continue
        if texture_set is not None:
            name = texture_set.add_material(tm_owner, index, material)
            if name:
                return (name,
                        tm_props.get("MappingScale") or DEFAULT_MAPPING_SCALE,
                        tm_props.get("MappingRotation") or 0.0)
    return None, DEFAULT_MAPPING_SCALE, 0.0
