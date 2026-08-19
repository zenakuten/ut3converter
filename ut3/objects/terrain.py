"""ATerrain reader.

After the tagged property list (which carries NumVerticesX/Y, NumPatchesX/Y,
Layers, Location, DrawScale, DrawScale3D) the native data is:

    i32 Count, u16 Heights[Count]          Count == NumVerticesX * NumVerticesY
    i32 Count, u8  InfoData[Count]         bit 0 set = that quad is cut away
    i32 AlphaMapCount
      per alpha map: i32 Count, u8 Data[Count]

Heights are 16-bit with 32768 as the zero point and 128 units per local unit,
so a vertex sits at `Location.Z + (h - 32768) / 128 * DrawScale * DrawScale3D.Z`.
Each layer names the alpha map it uses through its AlphaMapIndex.
"""

import struct

from ..props import read_object_properties

# Default__Terrain.DrawScale3D in CookedPC/Engine.u -- a patch is 256uu square.
TERRAIN_DRAW_SCALE_3D = 256.0


class Terrain:
    def __init__(self, name, width, height, heights, alpha_maps, layers, props,
                 info=b""):
        self.name = name
        self.width = width          # vertices across
        self.height = height
        self.heights = heights      # u16, row-major, width*height
        self.alpha_maps = alpha_maps
        self.layers = layers        # list of Properties, one per layer
        self.props = props
        self.info = info            # u8 per vertex, bit 0 set = quad hidden

    @property
    def location(self):
        v = self.props.get("Location")
        return tuple(v.value) if v is not None and v.value else (0.0, 0.0, 0.0)

    @property
    def draw_scale(self):
        return self.props.get("DrawScale", 1.0) or 1.0

    @property
    def draw_scale_3d(self):
        # Terrain overrides Actor's (1,1,1): Default__Terrain in CookedPC/Engine.u
        # sets DrawScale3D=(256,256,256), one patch per 256uu. A terrain that
        # keeps the default elides the property, so falling back to Actor's
        # default would shrink it 256-fold into an invisible speck.
        v = self.props.get("DrawScale3D")
        if v is None or not v.value:
            return (TERRAIN_DRAW_SCALE_3D,) * 3
        return tuple(v.value)

    def height_at(self, x, y):
        return self.heights[y * self.width + x]

    def quad_hidden(self, x, y):
        """Is the quad with this vertex as its corner cut out of the terrain?

        InfoData's bit 0 marks a quad *hidden*, not visible -- the sense is
        inverted from what the flag name suggests. Measured on WAR-PowerSurge:
        the bit agrees with "no terrain layer is painted here" 84.7% of the
        time read as hidden and 15.3% read as visible, and every walkable node
        position reads visible only under the first reading.
        """
        i = y * self.width + x
        if i >= len(self.info):
            return False
        return bool(self.info[i] & 1)

    def __repr__(self):
        return "<Terrain %s %dx%d, %d layers, %d alpha maps>" % (
            self.name, self.width, self.height, len(self.layers), len(self.alpha_maps))


def read_terrain(pkg, export):
    """Parse a Terrain export, or None if the layout does not hold."""
    if pkg.class_name_of(export) != "Terrain":
        return None
    props, start, end = read_object_properties(pkg, export)
    if start is None:
        return None
    width = props.get("NumVerticesX", 0)
    height = props.get("NumVerticesY", 0)
    if not (0 < width <= 4096 and 0 < height <= 4096):
        return None

    data = pkg.export_data(export)
    o = end
    try:
        count = struct.unpack_from("<i", data, o)[0]
        o += 4
        if count != width * height:
            return None
        heights = list(struct.unpack_from("<%dH" % count, data, o))
        o += count * 2

        info_count = struct.unpack_from("<i", data, o)[0]
        info = data[o + 4 : o + 4 + info_count]
        o += 4 + info_count

        alpha_maps = []
        map_count = struct.unpack_from("<i", data, o)[0]
        o += 4
        if not (0 <= map_count <= 32):
            map_count = 0
        for _ in range(map_count):
            n = struct.unpack_from("<i", data, o)[0]
            o += 4
            if n < 0 or o + n > len(data):
                break
            alpha_maps.append(data[o : o + n])
            o += n
    except (struct.error, IndexError):
        return None

    layers = []
    layer_array = props.get("Layers")
    if layer_array is not None:
        try:
            layers = layer_array.as_props()
        except (struct.error, IndexError, ValueError):
            layers = []

    return Terrain(export.name, width, height, heights, alpha_maps, layers, props,
                   info)
