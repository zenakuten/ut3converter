"""USkeletalMesh reader.

Native, like UStaticMesh, and undocumented in the same way -- the layout below
was derived from UT3's own packages and checked against the bounding box the
mesh declares, which is the only cross-check the data offers.

Layout after the property list (UT3, package v512):

    FBoxSphereBounds Bounds                  28 bytes
    TArray<UMaterialInterface*> Materials    i32 count, count x i32
    FVector          Origin                  12
    FRotator         RotOrigin               12
    TArray<FMeshBone> RefSkeleton            i32 count, count x 48 (see Bone)
    i32              SkeletalDepth
    i32              LODCount
    per LOD:
        TArray<FSkelMeshSection> Sections    i32 count, count x 10
        index buffer   i32 ElementSize, i32 Count, u16 data
        index buffer   i32 Count, u16 data      <- byte-identical second copy
        TArray<u16>    ActiveBoneIndices
        TArray<u8>     ShadowTriangleDoubleSided   (one per triangle)
        TArray<FSkelMeshChunk> Chunks        i32 count, then per chunk:
            i32            BaseVertexIndex
            TArray<FRigidSkinVertex>   i32 count, count x (25 + 8*NumUV)
            TArray<FSoftSkinVertex>    i32 count, count x (32 + 8*NumUV)
            TArray<u16>    BoneMap
            i32 NumRigidVertices, i32 NumSoftVertices, i32 MaxBoneInfluences
        (the GPU skin vertex buffer and the rest of the LOD follow; nothing
         here needs them, so parsing stops at the end of the chunk table)

The index buffer really is written twice -- verified byte-identical across
every mesh measured. Only the first copy is read.

A vertex carries its position, three packed normals, its UVs and its bone
links. UT2004's PSK has no place for a normal (the editor derives them), so
the twelve tangent bytes are stepped over rather than decoded.

`NumTexCoords` is declared later in the stream than the vertices that depend
on it, so the stride is probed instead: the candidate that puts *every*
position inside the mesh's own declared bounds wins. That is the same tactic
UStaticMesh's Layout probing uses, and for the same reason -- a version number
that would tell us directly is not available.
"""

import struct
from collections import namedtuple

from ..props import read_object_properties

Bone = namedtuple("Bone", "name flags orientation position num_children parent")
Section = namedtuple("Section", "material chunk base_index triangles")
Chunk = namedtuple("Chunk", "base_vertex rigid soft bone_map max_influences")
Vertex = namedtuple("Vertex", "position u v bones weights")
SkeletalMesh = namedtuple(
    "SkeletalMesh", "name bounds materials origin rot_origin bones "
                    "sections indices chunks vertices")

BONE_SIZE = 48
SECTION_SIZE = 10

# Position(12) + three FPackedNormal(12) + one byte of bone index, plus the UVs.
RIGID_FIXED = 12 + 12 + 1
# Position(12) + three FPackedNormal(12) + four bone bytes + four weight bytes.
SOFT_FIXED = 12 + 12 + 4 + 4
# UT3 builds with up to four UV sets; every mesh measured uses one.
MAX_TEXCOORDS = 4


class SkeletalError(Exception):
    """The export did not parse as a skeletal mesh."""


def _bounds(data, o):
    b = struct.unpack_from("<7f", data, o)
    return (b[0:3], b[3:6], b[6]), o + 28


def _in_bounds(bounds, position, slack=1.0):
    origin, extent, _radius = bounds
    for i in range(3):
        if not (origin[i] - extent[i] - slack <= position[i]
                <= origin[i] + extent[i] + slack):
            return False
    return True


def _read_bones(pkg, data, o):
    count, = struct.unpack_from("<i", data, o)
    o += 4
    bones = []
    for _ in range(count):
        name_index, = struct.unpack_from("<i", data, o)
        flags, = struct.unpack_from("<I", data, o + 8)
        quat = struct.unpack_from("<4f", data, o + 12)
        pos = struct.unpack_from("<3f", data, o + 28)
        kids, parent = struct.unpack_from("<ii", data, o + 40)
        name = pkg.names[name_index] if 0 <= name_index < len(pkg.names) else "Bone"
        bones.append(Bone(name, flags, quat, pos, kids, parent))
        o += BONE_SIZE
    return bones, o


def _probe_stride(data, o, count, fixed, bounds):
    """Which UV-set count puts every one of `count` positions inside `bounds`.

    A wrong stride walks off into normals and weights, which decode as huge or
    denormal floats and fall outside the box almost immediately -- so this is
    decisive in practice rather than a guess between near-misses.
    """
    for texcoords in range(1, MAX_TEXCOORDS + 1):
        stride = fixed + 8 * texcoords
        if o + count * stride > len(data):
            continue
        if all(_in_bounds(bounds, struct.unpack_from("<3f", data, o + i * stride))
               for i in range(count)):
            return stride, texcoords
    raise SkeletalError("no vertex stride fits the declared bounds")


def _read_vertices(data, o, count, bounds, soft):
    """Read one vertex array. Returns (vertices, new offset)."""
    if not count:
        return [], o + 0
    fixed = SOFT_FIXED if soft else RIGID_FIXED
    stride, _texcoords = _probe_stride(data, o, count, fixed, bounds)
    out = []
    for i in range(count):
        at = o + i * stride
        position = struct.unpack_from("<3f", data, at)
        # The UVs sit after the three packed normals; only the first set is
        # read, since UT2004's PSK carries exactly one.
        u, v = struct.unpack_from("<2f", data, at + 24)
        if soft:
            tail = at + stride - 8
            bones = struct.unpack_from("<4B", data, tail)
            weights = struct.unpack_from("<4B", data, tail + 4)
        else:
            bones = (data[at + stride - 1], 0, 0, 0)
            weights = (255, 0, 0, 0)
        out.append(Vertex(position, u, v, bones, weights))
    return out, o + count * stride


def read_skeletal_mesh(pkg, export):
    """Parse one SkeletalMesh export. Raises SkeletalError if it does not fit."""
    data = pkg.export_data(export)
    _props, _start, end = read_object_properties(pkg, export)
    if not end:
        raise SkeletalError("no property list found")
    o = end
    try:
        bounds, o = _bounds(data, o)
        count, = struct.unpack_from("<i", data, o)
        o += 4
        materials = list(struct.unpack_from("<%di" % count, data, o)) if count else []
        o += 4 * count
        origin = struct.unpack_from("<3f", data, o)
        o += 12
        rot_origin = struct.unpack_from("<3i", data, o)
        o += 12
        bones, o = _read_bones(pkg, data, o)
        if not bones:
            raise SkeletalError("no reference skeleton")
        o += 4                                             # SkeletalDepth
        lod_count, = struct.unpack_from("<i", data, o)
        o += 4
        if lod_count < 1:
            raise SkeletalError("no LOD models")

        # LOD 0 only. UT2004 has no LOD chain for skeletal meshes either, and
        # the highest-detail model is the one to keep.
        count, = struct.unpack_from("<i", data, o)
        o += 4
        sections = []
        for _ in range(count):
            material, chunk = struct.unpack_from("<HH", data, o)
            base, = struct.unpack_from("<I", data, o + 4)
            triangles, = struct.unpack_from("<H", data, o + 8)
            sections.append(Section(material, chunk, base, triangles))
            o += SECTION_SIZE

        _elem_size, index_count = struct.unpack_from("<ii", data, o)
        o += 8
        indices = list(struct.unpack_from("<%dH" % index_count, data, o))
        o += 2 * index_count
        # The identical second copy, skipped.
        second, = struct.unpack_from("<i", data, o)
        o += 4 + 2 * second

        count, = struct.unpack_from("<i", data, o)         # ActiveBoneIndices
        o += 4 + 2 * count
        count, = struct.unpack_from("<i", data, o)         # ShadowTriangleDoubleSided
        o += 4 + count

        chunk_count, = struct.unpack_from("<i", data, o)
        o += 4
        chunks, vertices = [], []
        for _ in range(chunk_count):
            base_vertex, = struct.unpack_from("<i", data, o)
            o += 4
            count, = struct.unpack_from("<i", data, o)
            o += 4
            rigid, o = _read_vertices(data, o, count, bounds, soft=False)
            count, = struct.unpack_from("<i", data, o)
            o += 4
            soft_verts, o = _read_vertices(data, o, count, bounds, soft=True)
            count, = struct.unpack_from("<i", data, o)
            o += 4
            bone_map = list(struct.unpack_from("<%dH" % count, data, o)) if count else []
            o += 2 * count
            _rigid_n, _soft_n, max_influences = struct.unpack_from("<3i", data, o)
            o += 12
            chunks.append(Chunk(base_vertex, len(rigid), len(soft_verts),
                                bone_map, max_influences))
            vertices.append(rigid + soft_verts)
    except SkeletalError:
        raise
    except (struct.error, IndexError) as exc:
        raise SkeletalError("layout did not parse: %s" % exc)
    return SkeletalMesh(export.name, bounds, materials, origin, rot_origin,
                        bones, sections, indices, chunks, vertices)
