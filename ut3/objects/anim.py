"""UAnimSet / UAnimSequence reader, including the compressed key stream.

An AnimSet is all tagged properties -- `TrackBoneNames` and `Sequences` -- so
nothing native has to be parsed for it. A cooked AnimSequence keeps its
settings in properties too and follows them with one native block:

    i32          length of the compressed byte stream
    u8[length]   the stream

`RawAnimData` (the editor's uncompressed keys) does not survive cooking and is
not written at all, so the stream begins immediately after the property list.

`CompressedTrackOffsets` indexes into that stream, four entries per track:

    TranslationOffset, NumTranslationKeys, RotationOffset, NumRotationKeys

**A track with exactly one key is stored raw, whatever the declared format.**
UT3's own data proves it: ActiveStill declares ACF_IntervalFixed32NoW across
40 single-key tracks and its stream is exactly 40 x (12 + 12) bytes -- twelve
for the position and twelve for an uncompressed quaternion. Reading that one
key through the interval decoder walks off by 16 bytes per track.

The decoders below were checked by decoding every track of every sequence and
requiring the offsets to consume the stream exactly: all nine of the
DarkWalker's sequences account for every byte, none short and none over.
"""

import math
import struct

from ..props import read_object_properties

# Rotations only; UT3 leaves translation uncompressed (ACF_None) in everything
# measured, which is a raw FVector per key.
FLOAT96 = "ACF_Float96NoW"
INTERVAL32 = "ACF_IntervalFixed32NoW"

# The 11/11/10-bit split FQuatIntervalFixed32NoW packs X, Y and Z into, with
# the offset and divisor each component is quantised against.
X_BITS, Y_BITS, Z_BITS = 0x7FF, 0x7FF, 0x3FF
X_SHIFT, Y_SHIFT = 21, 10
XY_OFFSET, XY_DIV = 1023.0, 1023.0
Z_OFFSET, Z_DIV = 511.0, 511.0


class AnimError(Exception):
    """The export did not parse as an animation."""


def _quat_from_xyz(x, y, z):
    """Restore W, which every NoW format drops -- the quaternion is unit."""
    square = 1.0 - (x * x + y * y + z * z)
    return (x, y, z, math.sqrt(square) if square > 0.0 else 0.0)


def _float96(data, at):
    x, y, z = struct.unpack_from("<3f", data, at)
    return _quat_from_xyz(x, y, z)


def _interval32(data, at, mins, ranges):
    packed, = struct.unpack_from("<I", data, at)
    x = (((packed >> X_SHIFT) & X_BITS) - XY_OFFSET) / XY_DIV * ranges[0] + mins[0]
    y = (((packed >> Y_SHIFT) & Y_BITS) - XY_OFFSET) / XY_DIV * ranges[1] + mins[1]
    z = ((packed & Z_BITS) - Z_OFFSET) / Z_DIV * ranges[2] + mins[2]
    return _quat_from_xyz(x, y, z)


class Sequence:
    """One animation: a name, a length, and a key list per track."""

    def __init__(self, name, frames, length, tracks):
        self.name = name
        self.frames = max(1, frames)
        self.length = length or 0.0
        # track index -> (positions, rotations), each a list of keys
        self.tracks = tracks

    @property
    def rate(self):
        if self.length <= 0.0:
            return float(self.frames)
        return self.frames / self.length

    def sample(self, track, frame):
        """Position and rotation of one track at one frame.

        AKF_ConstantKeyLerp spaces a track's keys evenly over the sequence
        rather than storing one per frame, so a track with 18 keys across 36
        frames is sampled at every other frame. Nearest key rather than a slerp:
        UT2004 stores a key per frame anyway, so the frames that land between
        two compressed keys are the only ones that lose anything, and a quat
        lerp here would be inventing detail the compressor already discarded.
        """
        positions, rotations = self.tracks.get(track, ((), ()))
        return (_nearest(positions, frame, self.frames),
                _nearest(rotations, frame, self.frames))


def _nearest(keys, frame, frames):
    if not keys:
        return None
    if len(keys) == 1 or frames <= 1:
        return keys[0]
    at = int(round(frame * (len(keys) - 1) / float(frames - 1)))
    return keys[min(max(at, 0), len(keys) - 1)]


def read_anim_sequence(pkg, export):
    """Parse one AnimSequence into per-track key lists."""
    data = pkg.export_data(export)
    props, _start, end = read_object_properties(pkg, export)
    if not end:
        raise AnimError("no property list found")
    offsets = props.get("CompressedTrackOffsets")
    if offsets is None:
        raise AnimError("no CompressedTrackOffsets")
    try:
        size, = struct.unpack_from("<i", data, end)
        stream = end + 4
        if size < 0 or stream + size > len(data):
            raise AnimError("compressed stream runs past the export")
        table = offsets.as_ints()
        rotation_format = props.get("RotationCompressionFormat", FLOAT96)
        tracks = {}
        for track in range(len(table) // 4):
            t_offset, t_keys, r_offset, r_keys = table[track * 4: track * 4 + 4]
            positions = [struct.unpack_from("<3f", data, stream + t_offset + k * 12)
                         for k in range(t_keys)]
            rotations = []
            if r_keys == 1 or rotation_format == FLOAT96:
                for k in range(r_keys):
                    rotations.append(_float96(data, stream + r_offset + k * 12))
            elif rotation_format == INTERVAL32:
                mins = struct.unpack_from("<3f", data, stream + r_offset)
                ranges = struct.unpack_from("<3f", data, stream + r_offset + 12)
                for k in range(r_keys):
                    rotations.append(
                        _interval32(data, stream + r_offset + 24 + k * 4, mins, ranges))
            else:
                raise AnimError("unsupported rotation format %s" % rotation_format)
            tracks[track] = (positions, rotations)
    except AnimError:
        raise
    except (struct.error, IndexError) as exc:
        raise AnimError("key stream did not parse: %s" % exc)
    return Sequence(props.get("SequenceName") or export.name,
                    props.get("NumFrames") or 1,
                    props.get("SequenceLength") or 0.0,
                    tracks)


def _fname_list(pkg, array):
    """An FName array is eight bytes an entry -- a name index and a number."""
    out = []
    if array is None:
        return out
    for i in range(array.count):
        index, _number = struct.unpack_from("<2i", array.raw, i * 8)
        out.append(pkg.names[index] if 0 <= index < len(pkg.names) else "")
    return out


class AnimSet:
    """An AnimSet's tracks and the sequences that play on them."""

    def __init__(self, bones, sequences, mesh_name, rotation_only,
                 translated_bones):
        self.bones = bones
        self.sequences = sequences
        self.mesh_name = mesh_name
        # UAnimSet.bAnimRotationOnly defaults to TRUE, and a UT3 package only
        # writes the property when it is FALSE -- so an absent property means
        # rotation-only, not the other way round. The DarkWalker has one of
        # each: the torso sets it False and animates its turret's translation,
        # the legs leave it out and must take translation from the bind pose.
        self.rotation_only = rotation_only
        # The exceptions bAnimRotationOnly allows: these bones keep the
        # animation's own translation.
        self.translated_bones = set(n.lower() for n in translated_bones)

    def uses_translation(self, bone_name):
        if not self.rotation_only:
            return True
        return str(bone_name).lower() in self.translated_bones


def read_anim_set(pkg, export):
    """Parse one AnimSet."""
    props, _start, _end = read_object_properties(pkg, export)
    names = props.get("TrackBoneNames")
    sequences = props.get("Sequences")
    if names is None or sequences is None:
        raise AnimError("AnimSet has no tracks or no sequences")
    rotation_only = props.get("bAnimRotationOnly")
    if rotation_only is None:
        rotation_only = True
    return AnimSet(_fname_list(pkg, names), sequences.as_ints(),
                   props.get("PreviewSkelMeshName"), bool(rotation_only),
                   _fname_list(pkg, props.get("UseTranslationBoneNames")))
