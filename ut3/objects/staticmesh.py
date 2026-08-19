"""UStaticMesh reader.

Almost entirely native. Layout after the property list (UT3, package v512):

    FBoxSphereBounds Bounds                  28 bytes
    i32              BodySetup               (PackageIndex)
    kDOP nodes       i32 ElementSize, i32 Count, data
    kDOP triangles   i32 ElementSize, i32 Count, data
    i32              Version
    i32              LODCount
    per LOD:
        FByteBulkData RawTriangles           (stripped by the cooker: count 0)
        i32           ElementCount
        per element:  9 x i32 (see Element)
        Position VB:  i32 Stride, i32 NumVertices, i32 ElemSize, i32 Count, data
        Vertex VB:    i32 NumTexCoords, i32 Stride, i32 NumVertices,
                      i32 bUseFullPrecisionUVs, i32 ElemSize, i32 Count, data
        Colour VB:    i32 Stride, i32 NumVertices, i32 ElemSize, i32 Count, data
        Index buffer: i32 NumVertices, i32 ElemSize, i32 Count, u16 data
        (wireframe indices, shadow extrusion and shadow volumes follow; the
         converter has no use for them, so parsing stops here)

RawTriangles -- the editor's source geometry -- does not survive cooking, so
geometry is reconstructed from the render buffers. Vertex UVs are 16-bit halves
unless bUseFullPrecisionUVs is set.
"""

import struct

from ..props import read_object_properties


class Element:
    """One draw call: a material and a run of triangles in the index buffer."""

    __slots__ = ("material", "first_index", "num_triangles", "min_vertex",
                 "max_vertex", "material_index")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __repr__(self):
        return "<Element %d tris from %d>" % (self.num_triangles, self.first_index)


class LOD:
    __slots__ = ("elements", "positions", "uvs", "indices", "num_texcoords",
                 "uv_sets")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def triangles(self):
        """(i0, i1, i2) tuples."""
        return [tuple(self.indices[i : i + 3]) for i in range(0, len(self.indices) - 2, 3)]

    def __repr__(self):
        return "<LOD %d verts, %d tris, %d elements>" % (
            len(self.positions), len(self.indices) // 3, len(self.elements)
        )


class StaticMesh:
    def __init__(self, name, bounds, lods):
        self.name = name
        self.bounds = bounds
        self.lods = lods

    @property
    def lod0(self):
        return self.lods[0] if self.lods else None

    def __repr__(self):
        return "<StaticMesh %s %r>" % (self.name, self.lod0)


def _array(data, o):
    """Read a bulk-serialized array header: (ElementSize, Count, dataOffset)."""
    elem_size, count = struct.unpack_from("<2i", data, o)
    return elem_size, count, o + 8


def read_static_mesh(pkg, export, want_lod=0):
    """Parse a StaticMesh export. Returns None if the layout does not hold."""
    if pkg.class_name_of(export) != "StaticMesh":
        return None
    data = pkg.export_data(export)
    props, start, end = read_object_properties(pkg, export)
    if start is None:
        return None
    o = end
    try:
        bounds = struct.unpack_from("<7f", data, o)
        o += 28
        o += 4  # BodySetup

        for _ in range(2):  # kDOP nodes, kDOP triangles
            elem_size, count, o = _array(data, o)
            o += elem_size * count

        o += 4  # Version
        lod_count = struct.unpack_from("<i", data, o)[0]
        o += 4
        if not (0 < lod_count <= 16):
            return None

        lods = []
        for lod_index in range(lod_count):
            # RawTriangles: editor-only, stripped when cooked.
            flags, raw_count, size_on_disk, _offset = struct.unpack_from("<4i", data, o)
            o += 16
            if raw_count > 0 and not (flags & 0x01):
                o += size_on_disk

            element_count = struct.unpack_from("<i", data, o)[0]
            o += 4
            elements = []
            for _ in range(element_count):
                (material, _collision, _old_collision, _shadow, first_index,
                 num_triangles, min_vertex, max_vertex, material_index) = struct.unpack_from(
                    "<9i", data, o
                )
                o += 36
                elements.append(Element(
                    material=pkg.ref(material), first_index=first_index,
                    num_triangles=num_triangles, min_vertex=min_vertex,
                    max_vertex=max_vertex, material_index=material_index,
                ))

            # Position vertex buffer
            _stride, _num_vertices = struct.unpack_from("<2i", data, o)
            o += 8
            elem_size, count, o = _array(data, o)
            positions = [struct.unpack_from("<3f", data, o + i * elem_size) for i in range(count)]
            o += elem_size * count

            # Tangents + UVs
            num_texcoords, stride, _num_vertices, full_precision = struct.unpack_from("<4i", data, o)
            o += 16
            elem_size, count, o = _array(data, o)
            # A mesh can carry several UV sets -- UT3 sky domes have a polar map
            # in channel 0 and a flat one in channel 1 -- so read them all and
            # let the caller choose. They are interleaved per vertex, after the
            # three packed normals.
            uv_offset = 12
            uv_format = "<2f" if full_precision else "<2e"
            uv_stride = 8 if full_precision else 4
            uv_sets = []
            for channel in range(max(1, num_texcoords)):
                base_offset = o + uv_offset + channel * uv_stride
                if uv_offset + (channel + 1) * uv_stride > elem_size:
                    break
                uv_sets.append([struct.unpack_from(uv_format, data,
                                                   base_offset + i * elem_size)
                                for i in range(count)])
            uvs = uv_sets[0] if uv_sets else []
            o += elem_size * count

            # Colour vertex buffer
            _stride, _num_vertices = struct.unpack_from("<2i", data, o)
            o += 8
            elem_size, count, o = _array(data, o)
            o += elem_size * count

            # Index buffer
            o += 4  # vertex count hint
            elem_size, count, o = _array(data, o)
            fmt = "<%dH" % count if elem_size == 2 else "<%dI" % count
            indices = list(struct.unpack_from(fmt, data, o))
            o += elem_size * count

            lods.append(LOD(elements=elements, positions=positions, uvs=uvs,
                            indices=indices, num_texcoords=num_texcoords,
                            uv_sets=uv_sets))
            if lod_index >= want_lod:
                break  # later LODs are not needed and trail more buffers
    except (struct.error, IndexError, ValueError):
        return None

    if not lods:
        return None
    return StaticMesh(export.name, bounds, lods)


def validate(mesh):
    """Cheap sanity checks: indices in range, triangles accounted for, verts in bounds."""
    lod = mesh.lod0
    if lod is None or not lod.positions or not lod.indices:
        return False, "empty"
    n = len(lod.positions)
    if any(i >= n for i in lod.indices):
        return False, "index out of range"
    if len(lod.indices) % 3:
        return False, "index count not a multiple of 3"
    expected = sum(e.num_triangles for e in lod.elements)
    if expected and expected != len(lod.indices) // 3:
        return False, "element triangle counts disagree with the index buffer"
    ox, oy, oz, ex, ey, ez, _radius = mesh.bounds
    slack = 1.0 + 0.01 * max(ex, ey, ez)
    for px, py, pz in lod.positions:
        if (abs(px - ox) > ex + slack or abs(py - oy) > ey + slack
                or abs(pz - oz) > ez + slack):
            return False, "vertex outside the mesh's own bounds"
    return True, "ok"
