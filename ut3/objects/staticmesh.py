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

UDK (v868) adds two native fields UT3 does not carry: a 24-byte box holding the
kDOP tree's root bound, between BodySetup and the kDOP arrays -- Min and Max
only, with none of the validity byte a tagged FBox carries -- and four ints between
Version and LODCount. Everything else -- the element struct, the vertex buffers
and the index buffer -- is unchanged, so a TOXIKK mesh reads with the same code
once those two are skipped.

RawTriangles -- the editor's source geometry -- does not survive cooking, so
geometry is reconstructed from the render buffers. Vertex UVs are 16-bit halves
unless bUseFullPrecisionUVs is set.
"""

import struct
from collections import namedtuple

from ..props import read_object_properties

# The package version at which the two UDK-only native fields appear. As with
# the header offsets in package.py the exact version is not known, only that
# 512 (UT3) is before and 868 (TOXIKK's UDK) is after. Kept only to pick which
# layout to try first.
UDK_STATICMESH_EXTRAS = 584

# Three independent things a build may or may not write around the LOD table.
# They were one version flag until Gears of War Reloaded (835) turned out to
# mix them: it writes the kDOP root bound like UDK but no per-element Fragments
# and no vertex-colour payload elision, like UT3. Neither pure path read a
# single one of MP_Courtyard's 852 meshes.
#
#   kdop_bound      24 bytes of Min/Max after the BodySetup reference
#   element_extras  a Fragments array plus a trailing byte on every element
#   colour_always   the vertex-colour array header is written even when empty
#
# So the combination is measured per package rather than derived from a
# version, and cached once a mesh reads.
Layout = namedtuple("Layout", "kdop_bound element_extras colour_always")

UT3_LAYOUT = Layout(kdop_bound=False, element_extras=False, colour_always=True)
UDK_LAYOUT = Layout(kdop_bound=True, element_extras=True, colour_always=False)
GEARS_LAYOUT = Layout(kdop_bound=True, element_extras=False, colour_always=True)

_LAYOUTS = (
    UT3_LAYOUT, UDK_LAYOUT, GEARS_LAYOUT,
    Layout(True, False, False), Layout(False, True, False),
    Layout(False, False, False), Layout(False, True, True), Layout(True, True, True),
)


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
    """Read a bulk-serialized array header: (ElementSize, Count, dataOffset).

    Checked against the buffer it is being read from, because this is called
    while *searching* for the LOD table: at a wrong candidate offset the two
    numbers are arbitrary, and a count in the hundreds of millions becomes a
    list comprehension that takes the whole process out rather than failing the
    candidate. Converting Gears' MP_Courtyard was killed by the OOM reaper here
    -- no traceback, just exit 137 -- until the header had to fit.
    """
    elem_size, count = struct.unpack_from("<2i", data, o)
    # A zero element size passes any "does it fit" test while still asking for
    # `count` entries, so it has to be rejected explicitly rather than left to
    # the multiplication.
    if count > 0 and elem_size <= 0:
        raise ValueError("array of %d elements with element size %d" % (count, elem_size))
    if elem_size < 0 or count < 0 or o + 8 + elem_size * count > len(data):
        raise ValueError("array header %d x %d does not fit the export" % (count, elem_size))
    return elem_size, count, o + 8


def read_static_mesh(pkg, export, want_lod=0):
    """Parse a StaticMesh export, measuring the build's layout if need be.

    The layout that works is a property of the package, so the first mesh that
    reads settles it for the rest and is tried first from then on.
    """
    if pkg.class_name_of(export) != "StaticMesh":
        return None
    cached = getattr(pkg, "_staticmesh_layout", None)
    order = ([cached] if cached is not None else []) + [
        l for l in _LAYOUTS if l != cached]
    for layout in order:
        mesh = _read_with_layout(pkg, export, want_lod, layout)
        if mesh is not None:
            pkg._staticmesh_layout = layout
            return mesh
    return None


def _read_with_layout(pkg, export, want_lod, layout):
    """Parse a StaticMesh export. Returns None if the layout does not hold."""
    data = pkg.export_data(export)
    props, start, end = read_object_properties(pkg, export)
    if start is None:
        return None
    o = end
    try:
        bounds = struct.unpack_from("<7f", data, o)
        o += 28
        o += 4  # BodySetup
        if layout.kdop_bound:
            o += 24  # the kDOP tree's root bound: Min and Max, no validity byte

        for _ in range(2):  # kDOP nodes, kDOP triangles
            elem_size, count, o = _array(data, o)
            o += elem_size * count
        after_kdop = o

        o += 4  # Version
        if layout.element_extras:
            o += 16
        lods = _read_lods(pkg, data, o, want_lod, layout)
        if lods is None:
            # What sits between the kDOP tree and the LOD table is not a fixed
            # length. UDK normally writes sixteen bytes after `Version` --
            # every mesh in four maps does -- but BL-Artifact's
            # `terrain_sheets_polySurface12` writes none, and nothing in the
            # class says which. Rather than guess at another optional field,
            # the table is found: it announces itself with a small LOD count
            # followed by a whole LOD that parses into elements whose indices
            # address its own vertices, which is a far stronger signature than
            # any stride. The search starts at the kDOP tree because the
            # difference can be negative as well as positive.
            for candidate in range(after_kdop, after_kdop + LOD_SEARCH_WINDOW):
                if candidate == o:
                    continue
                lods = _read_lods(pkg, data, candidate, want_lod, layout)
                if lods is not None:
                    break
        if lods is None:
            return None
    except (struct.error, IndexError, ValueError):
        return None

    if not lods:
        return None
    return StaticMesh(export.name, bounds, lods)


# How far past the expected position to look for the LOD table.
LOD_SEARCH_WINDOW = 4096


def _read_lods(pkg, data, o, want_lod, layout):
    """Parse the LOD table at `o`, or None if it does not hold together."""
    try:
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
                if layout.element_extras:
                    # TArray<FFragmentRange> Fragments, then a one-byte
                    # bUsesFragments. Every stock mesh carries exactly one
                    # fragment spanning the whole element, so the array is
                    # skipped rather than kept -- but it has to be stepped
                    # over, and its trailing byte leaves every buffer after
                    # this point unaligned.
                    n_fragments = struct.unpack_from("<i", data, o)[0]
                    o += 4 + n_fragments * 8
                    o += 1
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
            uv_format = "<2f" if full_precision else "<2e"
            uv_stride = 8 if full_precision else 4
            # UT3 puts three packed normals ahead of the UVs and UDK two, so a
            # UDK block starts four bytes earlier. Reading a UDK mesh at 12
            # does not fail -- on a two-channel mesh it silently returns the
            # lightmap UVs instead of the diffuse ones -- so this was worth
            # more than a version guess. The vertex stride and the UV count are
            # both in the buffer's own header, which makes the offset
            # arithmetic: whatever the UVs do not occupy is what precedes them.
            # Gears writes 20-byte vertices with two 4-byte UV sets, i.e.
            # UT3's twelve.
            derived = elem_size - max(1, num_texcoords) * uv_stride
            uv_offset = derived if derived >= 0 else (
                8 if pkg.version >= UDK_STATICMESH_EXTRAS else 12)
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

            # Colour vertex buffer. UDK writes its payload only when the mesh
            # actually has vertex colours; UT3 always writes the array header,
            # empty or not, so the skip stays unconditional there.
            _stride, colour_vertices = struct.unpack_from("<2i", data, o)
            o += 8
            if colour_vertices > 0 or layout.colour_always:
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
    # A stride that is merely plausible still has to produce a mesh whose
    # indices address its own vertices; that is what makes the search safe.
    lod = lods[0]
    if not lod.elements or not lod.positions or not lod.indices:
        return None
    if max(lod.indices) >= len(lod.positions):
        return None
    return lods


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
