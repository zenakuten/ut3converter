"""16-bit BMP writer -- the only route to a TEXF_G16 heightmap.

UT2004's terrain heightmap must be a G16 texture, and the one importer that
produces one is the BMP path: `biBitCount == 16` copies raw WORDs straight into
the mip with no bitfield interpretation (Editor/Src/UnEdFact.cpp:2191). It reads
`biWidth * 2` bytes per row with no padding and flips scanlines, so rows are
written bottom-up here.
"""

import struct


def write_bmp16(path, width, height, values):
    """Write a 16-bit BMP. `values` is width*height u16 in top-down order."""
    if width % 2:
        raise ValueError("width must be even so rows stay 4-byte aligned")
    row_bytes = width * 2
    pixel_bytes = row_bytes * height
    offset = 14 + 40

    header = b"BM" + struct.pack("<IHHI", offset + pixel_bytes, 0, 0, offset)
    info = struct.pack(
        "<IiiHHIIiiII",
        40,            # biSize
        width,
        height,
        1,             # biPlanes
        16,            # biBitCount -> TEXF_G16
        0,             # biCompression (BI_RGB; the importer ignores bitfields)
        pixel_bytes,   # biSizeImage
        2835, 2835,    # pixels per metre
        0, 0,          # colours used / important
    )
    body = bytearray()
    # Bottom-up: the importer writes file row y to texture row (height-1-y).
    for y in range(height - 1, -1, -1):
        row = values[y * width : (y + 1) * width]
        body += struct.pack("<%dH" % width, *row)

    with open(path, "wb") as f:
        f.write(header)
        f.write(info)
        f.write(bytes(body))
    return path
