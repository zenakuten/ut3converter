"""ASE writer for UT2004's `#exec STATICMESH IMPORT`.

The importer (Editor/Src/UnStaticMesh.cpp) is a hand-rolled line scanner, so the
output has to match its expectations exactly:

* Positions are multiplied by FVector(-1,1,1) on import (line 1013), so X is
  written negated here and the two cancel. Face order is untouched, so winding
  survives.
* V is flipped on import (`V = 1.0 - ST.Y`, line 1048), so V is written flipped.
* `*BITMAP` has its path stripped to the filename and its last 5 characters
  removed (`.bmp"`), then the remainder is matched against the *object name* of
  an already-loaded texture. So the file stem must equal the UT2004 texture name,
  and the texture must be imported earlier in the same package.
* A material is only committed to the list when `*UVW_V_TILING` is parsed
  (line 706), so every *MAP_DIFFUSE block must carry BITMAP, U tiling and V
  tiling, in that order. Repeating *MAP_DIFFUSE inside one *MATERIAL appends
  further materials, which is what per-face *MESH_MTLID indexes into.
* Vertex/face list sections end on a line containing "\t\t}".
"""


def _material_block(index, name, texture):
    bitmap = "C:\\%s.dds" % (texture or "None")
    return [
        "\t\t*MAP_DIFFUSE {",
        "\t\t\t*MAP_NAME \"%s_%d\"" % (name, index),
        "\t\t\t*MAP_CLASS \"Bitmap\"",
        "\t\t\t*BITMAP \"%s\"" % bitmap,
        "\t\t\t*UVW_U_TILING 1.0000",
        "\t\t\t*UVW_V_TILING 1.0000",
        "\t\t}",
    ]


def write_ase(path, name, positions, uvs, faces, textures, scale=1.0):
    """Write one mesh.

    positions -- [(x, y, z), ...]
    uvs       -- [(u, v), ...] parallel to positions (may be empty)
    faces     -- [(i0, i1, i2, material_index), ...]
    textures  -- [texture name per material index]
    """
    out = []
    out.append("*3DSMAX_ASCIIEXPORT\t200")
    out.append("*COMMENT \"Converted from UT3 by ut3conv\"")
    out.append("*SCENE {")
    out.append("\t*SCENE_FILENAME \"%s\"" % name)
    out.append("}")

    out.append("*MATERIAL_LIST {")
    out.append("\t*MATERIAL_COUNT 1")
    out.append("\t*MATERIAL 0 {")
    out.append("\t\t*MATERIAL_NAME \"%s\"" % name)
    out.append("\t\t*MATERIAL_CLASS \"Standard\"")
    for i, texture in enumerate(textures or [None]):
        out.extend(_material_block(i, name, texture))
    out.append("\t}")
    out.append("}")

    out.append("*GEOMOBJECT {")
    out.append("\t*NODE_NAME \"%s\"" % name)
    out.append("\t*MESH {")
    out.append("\t\t*TIMEVALUE 0")
    out.append("\t\t*MESH_NUMVERTEX %d" % len(positions))
    out.append("\t\t*MESH_NUMFACES %d" % len(faces))

    out.append("\t\t*MESH_VERTEX_LIST {")
    for i, (x, y, z) in enumerate(positions):
        # X is negated to cancel the importer's FVector(-1,1,1).
        out.append("\t\t\t*MESH_VERTEX    %d\t%.4f\t%.4f\t%.4f"
                   % (i, -x * scale, y * scale, z * scale))
    out.append("\t\t}")

    out.append("\t\t*MESH_FACE_LIST {")
    for i, (a, b, c, material) in enumerate(faces):
        out.append("\t\t\t*MESH_FACE %d:    A: %d B: %d C: %d"
                   " AB:    1 BC:    1 CA:    1\t*MESH_SMOOTHING 1\t*MESH_MTLID %d"
                   % (i, a, b, c, material))
    out.append("\t\t}")

    if uvs:
        out.append("\t\t*MESH_NUMTVERTEX %d" % len(uvs))
        out.append("\t\t*MESH_TVERTLIST {")
        for i, (u, v) in enumerate(uvs):
            # V is flipped to cancel the importer's 1.0 - V.
            out.append("\t\t\t*MESH_TVERT %d\t%.5f\t%.5f\t0.0000" % (i, u, 1.0 - v))
        out.append("\t\t}")

        out.append("\t\t*MESH_NUMTVFACES %d" % len(faces))
        out.append("\t\t*MESH_TFACELIST {")
        for i, (a, b, c, _material) in enumerate(faces):
            out.append("\t\t\t*MESH_TFACE %d\t%d\t%d\t%d" % (i, a, b, c))
        out.append("\t\t}")

    out.append("\t}")
    out.append("\t*MATERIAL_REF 0")
    out.append("}")

    out.append("")

    with open(path, "wb") as f:
        f.write("\r\n".join(out).encode("latin-1", "replace"))
    return path
