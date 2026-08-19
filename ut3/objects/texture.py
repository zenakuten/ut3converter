"""UTexture2D reader, including bulk (streaming) mip payloads.

Layout after the property list:

    FByteBulkData SourceArt          (flags, count, sizeOnDisk, offsetInFile)
    i32           MipCount
    per mip:      FByteBulkData, [inline payload], i32 SizeX, i32 SizeY

Bulk flags: 0x00 inline, 0x11 stored in the owning content package (LZO
compressed chunk at a physical offset), 0x21 stripped by the cooker.
"""

import ctypes
import struct
import zlib

from ..package import PACKAGE_TAG
from ..props import read_object_properties

BULK_STORED_ELSEWHERE = 0x01
BULK_COMPRESSED = 0x10
BULK_UNUSED = 0x20

# Bytes per 4x4 block for the block-compressed formats.
BLOCK_BYTES = {"PF_DXT1": 8, "PF_DXT3": 16, "PF_DXT5": 16}
BYTES_PER_PIXEL = {"PF_A8R8G8B8": 4, "PF_G8": 1}

_lzo = None


def _lzo_decompress(src, out_len):
    global _lzo
    if _lzo is None:
        _lzo = ctypes.CDLL("liblzo2.so.2")
    out = ctypes.create_string_buffer(out_len)
    n = ctypes.c_ulong(out_len)
    rc = _lzo.lzo1x_decompress_safe(src, ctypes.c_ulong(len(src)), out, ctypes.byref(n), None)
    if rc != 0:
        raise ValueError("lzo1x_decompress_safe failed with %d" % rc)
    return out.raw[: n.value]


def decompress_chunk(data, offset=0):
    """Decode a standard compressed chunk (magic, blockSize, sizes, LZO blocks)."""
    magic, block_size, _csize, usize = struct.unpack_from("<4I", data, offset)
    if magic != PACKAGE_TAG:
        raise ValueError("not a compressed chunk (magic 0x%08X)" % magic)
    n_blocks = (usize + block_size - 1) // block_size
    infos = [struct.unpack_from("<2I", data, offset + 16 + i * 8) for i in range(n_blocks)]
    pos = offset + 16 + n_blocks * 8
    out = bytearray()
    for csz, usz in infos:
        block = data[pos : pos + csz]
        pos += csz
        if csz == usz:
            out += block
        else:
            try:
                out += _lzo_decompress(block, usz)
            except ValueError:
                out += zlib.decompress(block)
    return bytes(out)


class Mip:
    __slots__ = ("width", "height", "flags", "count", "size_on_disk", "offset", "data")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def present(self):
        return self.data is not None

    def __repr__(self):
        return "<Mip %dx%d %s>" % (
            self.width,
            self.height,
            "%d bytes" % len(self.data) if self.data else "absent",
        )


class Texture:
    def __init__(self, name, width, height, fmt, mips, props):
        self.name = name
        self.width = width
        self.height = height
        self.format = fmt
        self.mips = mips
        self.props = props

    @property
    def largest(self):
        """The biggest mip whose payload we actually have."""
        available = [m for m in self.mips if m.present]
        if not available:
            return None
        return max(available, key=lambda m: m.width * m.height)

    def __repr__(self):
        return "<Texture %s %dx%d %s, %d/%d mips>" % (
            self.name,
            self.width,
            self.height,
            self.format,
            sum(1 for m in self.mips if m.present),
            len(self.mips),
        )


def expected_size(fmt, width, height):
    if fmt in BLOCK_BYTES:
        return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * BLOCK_BYTES[fmt]
    if fmt in BYTES_PER_PIXEL:
        return width * height * BYTES_PER_PIXEL[fmt]
    return None


def read_texture(pkg, export, index=None, load_external=True):
    """Read a Texture2D. `index` (a PackageIndex) enables external mip loading."""
    props, start, end = read_object_properties(pkg, export)
    if start is None:
        return None
    fmt = props.get("Format", "PF_A8R8G8B8")
    width = props.get("SizeX", 0)
    height = props.get("SizeY", 0)
    data = pkg.export_data(export)

    pos = end + 16  # skip the SourceArt bulk header
    if pos + 4 > len(data):
        return None
    n_mips = struct.unpack_from("<i", data, pos)[0]
    pos += 4
    if not (0 <= n_mips <= 32):
        return None

    owner_package = pkg.path_of(export.index).split(".")[0]
    mips = []
    for _ in range(n_mips):
        if pos + 16 > len(data):
            break
        flags, count, size_on_disk, offset = struct.unpack_from("<4i", data, pos)
        pos += 16
        payload = None
        if not (flags & BULK_STORED_ELSEWHERE) and count > 0:
            payload = data[pos : pos + size_on_disk]
            pos += size_on_disk
            # Inline payloads can be compressed too, with the same chunk format
            # used for the external ones. Slicing the bytes raw and comparing
            # the length against the uncompressed size then silently discards
            # the mip -- and a texture whose mips are all compressed is dropped
            # entirely, leaving its mesh untextured.
            if flags & BULK_COMPRESSED:
                try:
                    payload = decompress_chunk(payload)
                except (ValueError, struct.error, zlib.error):
                    payload = None
        if pos + 8 > len(data):
            break
        mw, mh = struct.unpack_from("<2i", data, pos)
        pos += 8

        if payload is None and load_external and index is not None and count > 0:
            if not (flags & BULK_UNUSED) and offset >= 0 and size_on_disk > 0:
                raw = index.raw_bytes(owner_package, offset, size_on_disk + 24)
                if raw:
                    try:
                        payload = decompress_chunk(raw)
                    except (ValueError, struct.error):
                        payload = None
        want = expected_size(fmt, mw, mh)
        if payload is not None and want is not None and len(payload) != want:
            payload = None
        mips.append(
            Mip(width=mw, height=mh, flags=flags, count=count,
                size_on_disk=size_on_disk, offset=offset, data=payload)
        )
    return Texture(export.name, width, height, fmt, mips, props)
