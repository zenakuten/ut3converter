"""Skeletal meshes: UE3 USkeletalMesh -> UT2004 PSK, imported by `#exec MESH
MODELIMPORT`.

The reader gives a vertex per rendered corner -- position, UV and bone links --
because that is how a GPU skin is laid out. A PSK separates the two: `points`
are positions and `wedges` are the UV corners that reference them, exactly so
that several corners can share one point. That sharing is not cosmetic. UT2004
derives vertex normals from the faces meeting at a *point*, so writing one
point per corner makes every edge a hard edge and the mesh renders faceted.
Positions are therefore merged, and only genuine UV seams stay split.

Bone weights are per point for the same reason; the corners that merge into one
point are the same mesh vertex and always carry the same weights.

A rigid vertex has one bone and no weight byte -- it is implicitly 1.0. A soft
vertex has up to four, weighted 0..255, and UE3 leaves the unused slots zero.
Both index the chunk's BoneMap rather than the skeleton directly, so every bone
number is mapped through it on the way out.
"""

import os

from ut2.psk import write_psk
from ut3.objects.skeletalmesh import SkeletalError, read_skeletal_mesh
from .meshes import sanitize


class SkeletalStats:
    def __init__(self):
        self.meshes = 0
        self.bones = 0
        self.points = 0
        self.faces = 0
        self.failed = []
        self.merged = 0
        self.skinned = 0

    def summary(self):
        return ("%d skeletal mesh(es), %d bone(s), %d point(s), %d face(s)"
                % (self.meshes, self.bones, self.points, self.faces))


def _material_names(pkg, mesh, index=None):
    """Resolve the mesh's material references to bare names."""
    names = []
    for i, ref in enumerate(mesh.materials):
        name = None
        if ref > 0 and ref - 1 < len(pkg.exports):
            name = pkg.exports[ref - 1].name
        elif ref < 0:
            path = pkg.path_of(ref)
            if path:
                name = path.rsplit(".", 1)[-1]
        names.append(sanitize(name or "Material%d" % i))
    return names or ["Material0"]


def build_psk(pkg, mesh, stats=None):
    """Turn one parsed SkeletalMesh into the six PSK arrays."""
    # Global vertex number -> point number, merging equal positions.
    points, point_of, by_position = [], {}, {}
    wedges, influences = [], []
    seen_points = set()

    material_of_chunk = {}
    for section in mesh.sections:
        material_of_chunk.setdefault(section.chunk, section.material)

    for chunk_index, chunk in enumerate(mesh.chunks):
        verts = mesh.vertices[chunk_index]
        material = material_of_chunk.get(chunk_index, 0)
        for local, vertex in enumerate(verts):
            key = vertex.position
            point = by_position.get(key)
            if point is None:
                point = len(points)
                by_position[key] = point
                points.append(key)
            else:
                if stats:
                    stats.merged += 1
            global_index = chunk.base_vertex + local
            point_of[global_index] = point
            wedges.append((point, vertex.u, vertex.v, material))

            if point in seen_points:
                continue
            seen_points.add(point)
            for slot in range(4):
                weight = vertex.weights[slot]
                if not weight:
                    continue
                local_bone = vertex.bones[slot]
                if local_bone >= len(chunk.bone_map):
                    continue
                influences.append((weight / 255.0, point,
                                   chunk.bone_map[local_bone]))

    # A wedge per corner, in global vertex order, so the index buffer can be
    # used as wedge numbers directly.
    wedge_of = {}
    ordered = []
    for chunk_index, chunk in enumerate(mesh.chunks):
        for local in range(len(mesh.vertices[chunk_index])):
            global_index = chunk.base_vertex + local
            wedge_of[global_index] = len(ordered)
            ordered.append(None)
    # Rebuild in that order rather than trusting chunk order to be contiguous.
    rebuilt = [None] * len(ordered)
    at = 0
    for chunk_index, chunk in enumerate(mesh.chunks):
        for local in range(len(mesh.vertices[chunk_index])):
            rebuilt[wedge_of[chunk.base_vertex + local]] = wedges[at]
            at += 1
    wedges = rebuilt

    faces = []
    for section in mesh.sections:
        for triangle in range(section.triangles):
            at = section.base_index + triangle * 3
            if at + 2 >= len(mesh.indices):
                break
            trio = []
            for corner in range(3):
                vertex_index = mesh.indices[at + corner]
                trio.append(wedge_of.get(vertex_index, 0))
            faces.append((trio[0], trio[1], trio[2], section.material))

    bones = [(sanitize(b.name), b.flags, b.parent, b.orientation, b.position)
             for b in mesh.bones]
    return points, wedges, faces, bones, influences


def export_skeletal_meshes(pkg, exports, out_dir, package_name, index=None,
                           texture_set=None, stats=None):
    """Write a .psk per skeletal mesh; returns the #exec lines and the stats."""
    stats = stats or SkeletalStats()
    meshes_dir = os.path.join(out_dir, package_name, "Meshes")
    lines = []
    if not exports:
        return lines, stats
    os.makedirs(meshes_dir, exist_ok=True)

    for export in exports:
        try:
            mesh = read_skeletal_mesh(pkg, export)
        except SkeletalError as exc:
            stats.failed.append((export.name, str(exc)))
            continue
        points, wedges, faces, bones, influences = build_psk(pkg, mesh, stats)
        if not points or not faces:
            stats.failed.append((export.name, "no geometry"))
            continue
        name = sanitize(export.name)
        materials = _material_names(pkg, mesh, index)
        write_psk(os.path.join(meshes_dir, name + ".psk"),
                  points, wedges, faces, materials, bones, influences)
        # What each material index should draw. `MESHMAP SETTEXTURE` binds
        # during class parsing, so it can only name a Texture an earlier
        # `#exec TEXTURE IMPORT` in the same file has already created -- not a
        # Shader from defaultproperties, which is why static meshes go through
        # Skins instead. The flat diffuse is what add_material returns, and it
        # is available by then.
        skins = []
        if texture_set is not None:
            for ref in mesh.materials:
                skins.append(texture_set.add_material(pkg, index, pkg.ref(ref)))
        stats.meshes += 1
        stats.bones += len(bones)
        stats.points += len(points)
        stats.faces += len(faces)
        # RIGID=0: these carry real per-vertex weights, not one bone per vertex.
        lines.append("#exec MESH MODELIMPORT MESH=%s MODELFILE=Meshes\\%s.psk"
                     % (name, name))
        lines.append("#exec MESH ORIGIN MESH=%s X=0 Y=0 Z=0" % name)
        lines.append("#exec MESHMAP NEW MESHMAP=%s MESH=%s" % (name, name))
        lines.append("#exec MESHMAP SCALE MESHMAP=%s X=1 Y=1 Z=1" % name)
        for slot, texture in enumerate(skins):
            if not texture:
                continue
            lines.append("#exec MESHMAP SETTEXTURE MESHMAP=%s NUM=%d TEXTURE=%s.%s"
                         % (name, slot, package_name, texture))
            stats.skinned += 1
    return lines, stats
