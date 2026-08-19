"""A radar background for a converted Onslaught map, drawn from its terrain.

UT2004's Onslaught HUD draws `Level.RadarMapImage` behind the node graph
(Onslaught/ONSHUDOnslaught.uc:276) and simply returns when it is None, which is
why a converted map has an empty minimap. The image is not a screenshot: the
HUD treats it as a plain top-down plan at a *known* scale.

    MapSize  = Image.MaterialUSize()
    MapScale = MapSize / (Dimensions.Y * 2)          -- Dimensions.Y is RadarRange
    DrawTile(..., (PlayerX - Range) * MapScale + MapSize/2, ...)

With the radar centred on the world origin (`MapCenter = vect(0,0,0)`, :270)
that reduces to drawing the whole texture, so the image must span exactly
`[-RadarRange, +RadarRange]` on both axes, U increasing with world X and V with
world Y. Nothing else about it is prescribed, so it can be generated.

What is drawn is the terrain, shaded: height decides the tint and the surface
normal decides the light, which is enough to read a landscape's shape at a
glance. Quads UT3 cut away are left transparent, so the holes a map cuts for its
buildings read as holes rather than as ground.

**This is terrain only.** BSP and static meshes are not in it, so a map's
structures are missing and a map with no terrain gets no image at all. That is a
deliberate first cut: the terrain is the part with a height field to shade, and
on these maps it is most of the playable ground.
"""

# Where the sun is, for the hillshade: down and to the north-west, the
# convention every relief map uses, so slopes read as slopes rather than as
# stripes.
LIGHT = (-0.5, -0.5, 0.7071)

# How much of the final pixel is shading against flat tint. All shade loses the
# height information; none of it loses the shape.
SHADE = 0.55

# Ground tint at the terrain's lowest and highest points. Deliberately muted --
# this sits *behind* the node icons and link lines, which have to stay readable.
LOW_COLOUR = (58, 66, 54)
HIGH_COLOUR = (146, 150, 138)

# UT2004 wants a square power of two, and the radar is a small HUD element.
DEFAULT_SIZE = 256


def _height_at(rendered, x, y):
    """(height, visible) of one converted terrain at a world point, or None."""
    size = rendered["size"]
    origin = rendered["origin"]
    fx = (x - origin[0]) / rendered["unit_x"]
    fy = (y - origin[1]) / rendered["unit_y"]
    if not (0 <= fx <= size - 1 and 0 <= fy <= size - 1):
        return None
    x0 = int(fx)
    y0 = int(fy)
    x1 = min(x0 + 1, size - 1)
    y1 = min(y0 + 1, size - 1)
    tx = fx - x0
    ty = fy - y0
    heights = rendered["heights"]
    top = heights[y0 * size + x0] * (1 - tx) + heights[y0 * size + x1] * tx
    bottom = heights[y1 * size + x0] * (1 - tx) + heights[y1 * size + x1] * tx
    height = origin[2] + ((top * (1 - ty) + bottom * ty) - 32768.0) * rendered["z_unit"]

    visible = True
    words = rendered.get("visible")
    if words:
        bit = x0 + y0 * size
        index = bit >> 5
        if index < len(words):
            visible = bool((words[index] >> (bit & 31)) & 1)
    return height, visible


def sample(terrains, x, y):
    """The most detailed terrain covering this point, as (height, visible).

    A map may carry more than one and they overlap: WAR-Torlan lays a 1032uu
    landscape under a 193uu playfield. The finer grid wins wherever it reaches,
    which is what puts the played-in part of the map on the radar rather than
    the scenery around it.
    """
    best = None
    for rendered in sorted(terrains, key=lambda r: r["unit_x"] * r["unit_y"]):
        found = _height_at(rendered, x, y)
        if found is None:
            continue
        if found[1]:
            return found
        if best is None:
            best = found
    return best


def render(terrains, radar_range, size=DEFAULT_SIZE):
    """A top-down BGRA image of the terrain over [-range, +range] on both axes."""
    if not terrains or radar_range <= 0:
        return None
    step = (radar_range * 2.0) / size

    heights = [None] * (size * size)
    lo = hi = None
    for j in range(size):
        y = -radar_range + (j + 0.5) * step
        for i in range(size):
            x = -radar_range + (i + 0.5) * step
            found = sample(terrains, x, y)
            if found is None or not found[1]:
                continue
            heights[j * size + i] = found[0]
            if lo is None or found[0] < lo:
                lo = found[0]
            if hi is None or found[0] > hi:
                hi = found[0]
    if lo is None:
        return None
    span = (hi - lo) or 1.0

    out = bytearray(size * size * 4)
    for j in range(size):
        for i in range(size):
            height = heights[j * size + i]
            if height is None:
                continue        # a hole UT3 cut, or off the terrain: transparent
            # Central differences give the surface normal; missing neighbours
            # fall back to this pixel so an edge shades flat instead of dark.
            def neighbour(di, dj):
                value = heights[(j + dj) * size + (i + di)] if (
                    0 <= i + di < size and 0 <= j + dj < size) else None
                return height if value is None else value
            dzdx = (neighbour(1, 0) - neighbour(-1, 0)) / (2.0 * step)
            dzdy = (neighbour(0, 1) - neighbour(0, -1)) / (2.0 * step)
            length = (dzdx * dzdx + dzdy * dzdy + 1.0) ** 0.5
            shade = (-dzdx * LIGHT[0] - dzdy * LIGHT[1] + LIGHT[2]) / length
            shade = max(0.0, min(1.0, shade))
            level = (height - lo) / span
            at = (j * size + i) * 4
            for channel in range(3):
                tint = LOW_COLOUR[channel] + (HIGH_COLOUR[channel]
                                              - LOW_COLOUR[channel]) * level
                lit = tint * ((1.0 - SHADE) + SHADE * shade * 1.4)
                # BGRA, which is what ut2.images.write_tga writes.
                out[at + 2 - channel] = int(max(0, min(255, round(lit))))
            out[at + 3] = 255
    return bytes(out)


def write_minimap(terrain_dir, package, group, base, image, size):
    """Write the radar image and the exec line that imports it."""
    import os

    from ut2.images import write_tga

    name = "%s_Radar" % base
    os.makedirs(terrain_dir, exist_ok=True)
    write_tga(os.path.join(terrain_dir, name + ".tga"), size, size, image)
    line = ("#exec TEXTURE IMPORT NAME=%s GROUP=%s FILE=Terrain\\%s.tga ALPHA=1"
            % (name, group, name))
    return name, line, "%s.%s.%s" % (package, group, name)


def insert_exec(uc_path, line):
    """Add an exec line to the generated package, after it has been written.

    The radar image cannot be made until the play area is known, and the play
    area comes from the brushes, which are converted after the package is
    written. Nothing is circular about that -- the image is drawn from UT3's
    heightmap, not from a built map -- it is only that our own two passes run in
    that order, so the line goes in afterwards. The file is ours and its shape
    is fixed: exec lines, then `defaultproperties`.
    """
    with open(uc_path, "rb") as handle:
        text = handle.read().decode("latin-1")
    marker = "defaultproperties"
    at = text.find(marker)
    if at < 0:
        return False
    patched = text[:at] + line + "\r\n\r\n" + text[at:]
    with open(uc_path, "wb") as handle:
        handle.write(patched.encode("latin-1", "replace"))
    return True
