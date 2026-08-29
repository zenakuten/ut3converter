"""PSK writer -- the ActorX skin format UT2004's editor imports.

`#exec MESH MODELIMPORT` reads this. The structures are UnSkeletalMesh.h's
(VPoint, VVertex, VTriangle, VMaterial, VBone, VRawBoneInfluence) under
`#pragma pack(push,4)`, so VVertex is sixteen bytes rather than the twelve its
fields add up to -- the WORD is followed by two bytes of padding and the pair
of trailing BYTEs by another two.

**Everything is written Y-flipped, and the winding is reversed.** UnMeshEd.cpp
(:398-421) does this to every skin it imports, to bring Max/Maya data into
Unreal's left-handed space:

    Points(p).Y = -Points(p).Y
    Faces(f).WedgeIndex[1] <-> WedgeIndex[2]
    BonePos.Orientation.W = -W, .Y = -Y
    BonePos.Position.Y = -Y

UE3 data is already in Unreal space, so writing it unchanged would import
mirrored and inside out. Applying the same flip here makes the importer's flip
cancel it -- the file on disk is in Max space, which is what a .psk is
supposed to be.

**UE2 stores quaternions conjugated relative to UE3.** FQuatToFCoordsFast
(UnMath.h:4540, and FastQuatToFCoords at :3546 identically) builds

    XAxis.Y = xy - wz        YAxis.X = xy + wz

where UE3's FQuatRotationTranslationMatrix -- and every other engine's
row-vector form -- has those two the other way round. So a UE2 FCoords built
from q is the rotation UE3 would build from conj(q), and every rotation comes
out inverted if the quaternion is passed across as-is. Composed up a chain of
leg bones that renders as a splayed cage rather than a tucked leg.

Both this and the Y-mirror are folded into what gets written: to land conj(q)
in UE2 after the importer's (X, -Y, Z, -W), the file carries (X, -Y, Z, +W) --
W is the one component the importer's negation must *not* be undone for. The
reference skeleton and the animation keys all go through it, which matters:
skinning is RefBases^-1 * SpaceBases, so a convention applied to only one of
the two would leave the rest pose wrong.
"""

import struct

HEADER = struct.Struct("<20siii")

# What ActorX stamps in every chunk header. The importer does not check it,
# but the value is what every other .psk carries.
TYPE_FLAG = 1999801


def _chunk(handle, chunk_id, data_size, count, payload):
    handle.write(HEADER.pack(chunk_id.encode("ascii"), TYPE_FLAG, data_size, count))
    handle.write(payload)


def _name(text, length=64):
    """A fixed-width NUL-padded ANSI name, truncated if it will not fit."""
    raw = text.encode("ascii", "replace")[: length - 1]
    return raw + b"\0" * (length - len(raw))


def write_psk(path, points, wedges, faces, materials, bones, influences):
    """Write one skin.

    points      [(x, y, z)]
    wedges      [(point_index, u, v, material_index)]
    faces       [(w0, w1, w2, material_index)]
    materials   [name]
    bones       [(name, flags, parent, (qx, qy, qz, qw), (x, y, z))]
    influences  [(weight, point_index, bone_index)]
    """
    with open(path, "wb") as handle:
        _chunk(handle, "ACTRHEAD", 0, 0, b"")

        payload = bytearray()
        for x, y, z in points:
            payload += struct.pack("<3f", x, -y, z)
        _chunk(handle, "PNTS0000", 12, len(points), bytes(payload))

        payload = bytearray()
        for point, u, v, material in wedges:
            # WORD + 2 pad, two floats, two BYTEs + 2 pad = 16 under pack(4).
            payload += struct.pack("<HHffBBH", point, 0, u, v, material, 0, 0)
        _chunk(handle, "VTXW0000", 16, len(wedges), bytes(payload))

        payload = bytearray()
        for w0, w1, w2, material in faces:
            # Winding reversed, to be reversed back on import.
            payload += struct.pack("<3HBBI", w0, w2, w1, material, 0, 1)
        _chunk(handle, "FACE0000", 12, len(faces), bytes(payload))

        payload = bytearray()
        for index, name in enumerate(materials):
            payload += _name(name) + struct.pack("<iIiIii", index, 0, 0, 0, 0, 0)
        _chunk(handle, "MATT0000", 88, len(materials), bytes(payload))

        payload = bytearray()
        for name, flags, parent, quat, pos in bones:
            qx, qy, qz, qw = quat
            payload += _name(name)
            payload += struct.pack("<Iii", flags, 0, parent)
            # VJointPos: FQuat, FVector, Length, XSize, YSize, ZSize.
            payload += struct.pack("<4f", qx, -qy, qz, qw)
            payload += struct.pack("<3f", pos[0], -pos[1], pos[2])
            payload += struct.pack("<4f", 0.0, 0.0, 0.0, 0.0)
        _chunk(handle, "REFSKELT", 120, len(bones), bytes(payload))

        payload = bytearray()
        for weight, point, bone in influences:
            payload += struct.pack("<fii", weight, point, bone)
        _chunk(handle, "RAWWEIGHTS", 12, len(influences), bytes(payload))
    return path


def write_psa(path, bones, sequences):
    """Write the animation companion to a .psk, read by `#exec ANIM IMPORT`.

    `bones` is the same list write_psk was given -- the animation binds to the
    mesh by bone list, so it has to be the mesh's whole skeleton and in the
    same order, not just the tracks that happen to move.

    `sequences` is [(name, group, frames, rate, keys)] where `keys` is
    frame-major: frame 0's key for every bone, then frame 1's, matching
    UnMeshEd.cpp:724 --

        KeyIdx = (FirstRawFrame + f) * RefBones.Num() + b

    All sequences share one key array and each names its slice through
    FirstRawFrame, which is what that expression is reading.

    Keys are Y-flipped exactly as the skin is, and for the same reason:
    UnMeshEd.cpp:592-603 flips every key it reads, so writing UE3 data
    unflipped animates mirrored.
    """
    with open(path, "wb") as handle:
        _chunk(handle, "ANIMHEAD", 0, 0, b"")

        payload = bytearray()
        for name, flags, parent, quat, pos in bones:
            qx, qy, qz, qw = quat
            payload += _name(name)
            payload += struct.pack("<Iii", flags, 0, parent)
            payload += struct.pack("<4f", qx, -qy, qz, qw)
            payload += struct.pack("<3f", pos[0], -pos[1], pos[2])
            payload += struct.pack("<4f", 0.0, 0.0, 0.0, 0.0)
        _chunk(handle, "BONENAMES", 120, len(bones), bytes(payload))

        payload = bytearray()
        first_frame = 0
        for seq_name, group, frames, rate, _seq_keys in sequences:
            payload += _name(seq_name) + _name(group)
            payload += struct.pack(
                "<iiiifffiii",
                len(bones),      # TotalBones
                1,               # RootInclude
                0,               # KeyCompressionStyle
                len(bones) * frames,   # KeyQuotum
                0.0,             # KeyReduction
                frames / rate if rate else 0.0,   # TrackTime
                rate,            # AnimRate
                0,               # StartBone
                first_frame,     # FirstRawFrame
                frames,          # NumRawFrames
            )
            first_frame += frames
        _chunk(handle, "ANIMINFO", 168, len(sequences), bytes(payload))

        payload = bytearray()
        count = 0
        for _seq_name, _group, _frames, rate, keys in sequences:
            step = 1.0 / rate if rate else 0.0
            for position, quat in keys:
                qx, qy, qz, qw = quat
                payload += struct.pack("<3f", position[0], -position[1], position[2])
                payload += struct.pack("<4f", qx, -qy, qz, qw)
                payload += struct.pack("<f", step)
                count += 1
        _chunk(handle, "ANIMKEYS", 32, count, bytes(payload))
    return path
