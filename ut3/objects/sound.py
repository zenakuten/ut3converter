"""SoundNodeWave payloads and the distributions the ambient nodes are built on.

A cooked SoundNodeWave serializes four bulk blocks after its property list:

    FByteBulkData RawData              editor-only PCM; stripped by the cooker
    FByteBulkData CompressedPCData     Ogg Vorbis
    FByteBulkData CompressedXbox360Data
    FByteBulkData CompressedPS3Data

All 107 of DM-HeatRay's waves carry their PC payload inline, so nothing has to
be chased into a content package -- but the block headers are the same
(flags, count, sizeOnDisk, offsetInFile) as a texture mip's, and are read the
same way, external payloads included.

The ambient nodes state their radii, volume and pitch as RawDistributionFloat,
which points at a DistributionFloatUniform whose Min/Max are *elided when they
match the archetype*. Reading only the instance therefore yields 0 for anything
the mapper left alone -- a pitch of 0 rather than 1, a MaxRadius of 0 rather
than 5000. `distribution_value` follows the archetype chain into Engine.u
instead, which is where those defaults actually live.
"""

import struct
import zlib

from .texture import BULK_COMPRESSED, BULK_STORED_ELSEWHERE, BULK_UNUSED, decompress_chunk
from ..props import read_object_properties

# How many bulk blocks to walk before giving up on finding the Ogg.
MAX_BULK_BLOCKS = 6

# Cap on the archetype chain walk, which is a linked list in the file and so
# could in principle loop.
MAX_ARCHETYPE_DEPTH = 8


class SoundWave:
    """A UE3 sound asset and its compressed PC payload."""

    __slots__ = ("name", "channels", "sample_rate", "duration", "ogg")

    def __init__(self, name, channels, sample_rate, duration, ogg):
        self.name = name
        self.channels = channels
        self.sample_rate = sample_rate
        self.duration = duration
        self.ogg = ogg

    def __repr__(self):
        return "<SoundWave %s %dch %dHz %.2fs, %s>" % (
            self.name, self.channels, self.sample_rate, self.duration,
            "%d bytes ogg" % len(self.ogg) if self.ogg else "no payload")


def read_sound_wave(pkg, export, index=None):
    """Read a SoundNodeWave; None if it has no readable Ogg payload."""
    props, start, end = read_object_properties(pkg, export)
    if start is None:
        return None
    data = pkg.export_data(export)
    owner_package = pkg.path_of(export.index).split(".")[0]

    ogg = None
    pos = end
    for _ in range(MAX_BULK_BLOCKS):
        if pos + 16 > len(data):
            break
        flags, count, size_on_disk, offset = struct.unpack_from("<4i", data, pos)
        pos += 16
        payload = None
        if not (flags & BULK_STORED_ELSEWHERE) and count > 0 and size_on_disk > 0:
            payload = data[pos : pos + size_on_disk]
            pos += size_on_disk
            if flags & BULK_COMPRESSED:
                try:
                    payload = decompress_chunk(payload)
                except (ValueError, struct.error, zlib.error):
                    payload = None
        elif (index is not None and count > 0 and size_on_disk > 0
                and not (flags & BULK_UNUSED) and offset >= 0):
            raw = index.raw_bytes(owner_package, offset, size_on_disk)
            if raw and flags & BULK_COMPRESSED:
                try:
                    raw = decompress_chunk(raw)
                except (ValueError, struct.error, zlib.error):
                    raw = None
            payload = raw
        if payload and payload[:4] == b"OggS":
            ogg = payload
            break
        if pos >= len(data):
            break

    if ogg is None:
        return None
    # The Ogg's own header wins over NumChannels: that property is elided when
    # it matches the archetype, so two of DM-HeatRay's stereo beds claim mono.
    return SoundWave(export.name, ogg_channels(ogg) or props.get("NumChannels", 1),
                     props.get("SampleRate", 44100), props.get("Duration", 0.0), ogg)


def ogg_channels(ogg):
    """Channel count from the Vorbis identification header; 0 if unreadable."""
    at = ogg.find(b"\x01vorbis")
    # packet type + "vorbis" (7) + vorbis_version (4), then the channel byte.
    if at < 0 or at + 11 >= len(ogg):
        return 0
    return ogg[at + 11]


def _archetype_props(pkg, index, export, depth=0):
    """An object's properties, with anything elided filled in from its archetype."""
    props, start, _end = read_object_properties(pkg, export)
    merged = {}
    if start is not None:
        for name, idx, _type, value in props:
            if idx == 0:
                merged.setdefault(name, value)
    if depth >= MAX_ARCHETYPE_DEPTH or index is None or not export.archetype:
        return merged
    owner, parent = index.resolve(pkg, pkg.ref(export.archetype))
    if parent is None:
        return merged
    for name, value in _archetype_props(owner, index, parent, depth + 1).items():
        merged.setdefault(name, value)
    return merged


def distribution_value(pkg, index, raw, default=0.0):
    """The nominal value of a RawDistributionFloat: its constant, or Min/Max mean."""
    if raw is None:
        return default
    value = getattr(raw, "value", None)
    if not hasattr(value, "get"):
        return default
    ref = value.get("Distribution")
    if ref is None or ref.is_null:
        return default
    owner, export = index.resolve(pkg, ref)
    if export is None:
        return default
    props = _archetype_props(owner, index, export)
    if "Constant" in props:
        return props["Constant"]
    low, high = props.get("Min"), props.get("Max")
    if low is None and high is None:
        return default
    if low is None:
        low = high
    if high is None:
        high = low
    return (low + high) / 2.0


def distribution_range(pkg, index, raw, default=0.0):
    """(min, max) of a RawDistributionFloat; both equal for a constant."""
    if raw is not None:
        value = getattr(raw, "value", None)
        if hasattr(value, "get"):
            ref = value.get("Distribution")
            if ref is not None and not ref.is_null:
                owner, export = index.resolve(pkg, ref)
                if export is not None:
                    props = _archetype_props(owner, index, export)
                    if "Constant" in props:
                        return props["Constant"], props["Constant"]
                    low, high = props.get("Min"), props.get("Max")
                    if low is not None or high is not None:
                        low = high if low is None else low
                        high = low if high is None else high
                        return low, high
    return default, default
