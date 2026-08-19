"""Read a built UT2004 .ut2 BSP and answer the engine's own PointRegion.

UModel::Serialize (Engine/Src/UnModel.cpp:157):
    Super::Serialize          -- UPrimitive: BoundingBox (FBox), BoundingSphere
    Vectors, Points, Nodes, Surfs, Verts, NumSharedSides, NumZones, Zones[]
    ...

FBspNode (Engine/Src/UnModel.cpp:87, layout in Engine/Inc/UnObj.h:104):
    FPlane Plane; QWORD ZoneMask; BYTE NodeFlags;
    INDEX iVertPool, iSurf, iChild[0..2], iCollisionBound, iRenderBound;
    FSphere ExclusiveSphereBound;
    BYTE iZone[0], iZone[1], NumVertices; INT iLeaf[0], iLeaf[1];
    INT iSection, iFirstVertex, iLightMap;

PointRegion (Engine/Src/UnTrace.cpp:760) walks from node 0 taking iChild[IsFront]
until INDEX_NONE, then reads iZone[IsFront] off the last node. Zone 0 is solid.
"""

import struct
import sys

NF_NOT_CSG = 0x01           # Engine/Inc/UnObj.h node flags
NF_IS_NEW = 0x08


class Reader:
    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def f32(self):
        v = struct.unpack_from("<f", self.d, self.p)[0]
        self.p += 4
        return v

    def skip(self, n):
        self.p += n

    def idx(self):
        """UE2 compact index."""
        b = self.u8()
        neg = b & 0x80
        val = b & 0x3F
        if b & 0x40:
            shift = 6
            while True:
                c = self.u8()
                val |= (c & 0x7F) << shift
                shift += 7
                if not (c & 0x80):
                    break
        return -val if neg else val

    def name(self, names):
        return names[self.idx()]


def load_package(path):
    data = open(path, "rb").read()
    r = Reader(data)
    r.u32()                                   # tag
    ver = r.u32() & 0xFFFF
    r.u32()                                   # package flags
    name_count, name_off = r.i32(), r.i32()
    export_count, export_off = r.i32(), r.i32()
    import_count, import_off = r.i32(), r.i32()

    names = []
    r.p = name_off
    for _ in range(name_count):
        n = r.idx()
        names.append(r.d[r.p:r.p + n].rstrip(b"\0").decode("latin-1"))
        r.p += n
        r.u32()                               # flags

    r.p = import_off
    imports = []
    for _ in range(import_count):
        r.idx(); cls = r.idx(); r.i32(); obj = r.idx()
        imports.append((names[cls], names[obj]))

    r.p = export_off
    exports = []
    for i in range(export_count):
        cls = r.idx(); r.idx(); r.i32(); obj = r.idx()
        flags = r.u32()
        size = r.idx()
        off = r.idx() if size > 0 else 0
        exports.append({"cls": cls, "name": names[obj], "size": size,
                        "off": off, "index": i + 1, "flags": flags})

    def class_of(e):
        c = e["cls"]
        if c < 0:
            return imports[-c - 1][1]
        if c == 0:
            return "Class"
        return exports[c - 1]["name"]

    return data, names, exports, class_of, ver


def read_model(data, names, export):
    """Parse a UModel far enough to answer PointRegion."""
    r = Reader(data, export["off"])
    # Tagged property list: UModel carries none, so this is the "None" terminator.
    start = r.p
    first = r.idx()
    if not (0 <= first < len(names)) or names[first] != "None":
        r.p = start                            # no property list at all
    # UPrimitive: FBox (min, max, BYTE IsValid) then FSphere.
    r.skip(12 + 12 + 1)
    r.skip(16)
    vectors = r.idx(); r.skip(12 * vectors)
    points = r.idx(); r.skip(12 * points)

    node_count = r.idx()
    nodes = []
    for _ in range(node_count):
        plane = (r.f32(), r.f32(), r.f32(), r.f32())
        r.skip(8)                              # ZoneMask
        flags = r.u8()
        r.idx()                                # iVertPool
        surf = r.idx()
        back = r.idx(); front = r.idx(); r.idx()
        r.idx(); r.idx()                       # collision / render bounds
        r.skip(16)                             # ExclusiveSphereBound
        zone_back = r.u8(); zone_front = r.u8()
        num_verts = r.u8()
        leaf_back = r.i32(); leaf_front = r.i32()
        r.skip(12)                             # iSection, iFirstVertex, iLightMap
        # Projectors is only serialised when neither saving nor loading, so a
        # file on disk never carries it (Engine/Src/UnModel.cpp:143).
        nodes.append({"plane": plane, "flags": flags, "surf": surf,
                      "child": (back, front), "zone": (zone_back, zone_front),
                      "verts": num_verts, "leaf": (leaf_back, leaf_front)})
    return nodes, r


def is_csg(node):
    return node["verts"] > 0 and not (node["flags"] & (NF_IS_NEW | NF_NOT_CSG))


def point_region(nodes, location, root_outside=True, trace=False):
    """UModel::PointRegion. Returns (zone_number, path)."""
    outside = root_outside
    is_front = 0
    i_node = 0
    parent = 0
    path = []
    while i_node != -1 and i_node < len(nodes):
        node = nodes[i_node]
        p = node["plane"]
        dot = p[0] * location[0] + p[1] * location[1] + p[2] * location[2] - p[3]
        is_front = 1 if dot >= 0.0 else 0
        outside = (outside or is_csg(node)) if is_front else (outside and not is_csg(node))
        if trace:
            path.append((i_node, round(dot, 3), is_front, outside, node["zone"][is_front],
                         node["surf"], node["verts"]))
        parent = i_node
        i_node = node["child"][is_front]
    return nodes[parent]["zone"][is_front], path


if __name__ == "__main__":
    path = sys.argv[1]
    data, names, exports, class_of, ver = load_package(path)
    models = [e for e in exports if class_of(e) == "Model"]
    models.sort(key=lambda e: -e["size"])
    print("package version %d, %d exports, %d models" % (ver, len(exports), len(models)))
    nodes, r = read_model(data, names, models[0])
    print("largest model '%s': %d nodes" % (models[0]["name"], len(nodes)))


def load_bsp(ut2_path):
    """Convenience: the level's BSP nodes from a built .ut2."""
    data, names, exports, class_of, _ver = load_package(ut2_path)
    models = [e for e in exports if class_of(e) == "Model"]
    models.sort(key=lambda e: -e["size"])
    return read_model(data, names, models[0])[0]
