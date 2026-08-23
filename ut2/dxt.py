"""DXT surgery: folding a UE3 opacity mask into alpha, and building colour
blocks for a material that states its albedo as one channel of a packed mask.

UE3 materials routinely keep opacity in its own texture -- `..._M` alongside
`..._D` -- because the graph can sample whatever it likes. UE2 has no such
indirection: a surface is masked or blended by the alpha of the one texture it
draws, so the mask has to be baked in before export or the cutout simply does
not happen and the artwork's black background renders solid.

DXT1 carries no usable alpha, which is what most of these diffuse maps are, so
the repack target is DXT5. That is cheaper than it sounds: a DXT5 block is an
8-byte alpha block followed by a colour block in *exactly* DXT1's layout, so the
colour half is copied through untouched and only the alpha half is built. The
one wrinkle is DXT1's punchthrough mode -- a block whose first endpoint is not
greater than the second encodes three colours plus transparent, and reading that
same block as DXT5 would turn those texels into black rather than transparent.
Such blocks are re-encoded onto the four-colour ramp instead.

`encode_dxt1_tinted` is the one place here that writes colour rather than
copying it, and it exists because some materials never ship a colour map at
all -- see `declared_diffuse_channel` in ut3/objects/material.py.
"""

import struct

def alpha_ramp(a0, a1):
    """DXT5's eight alpha levels for a pair of endpoints.

    The interpolated entries weight the endpoints so each pair sums to the
    divisor -- 6:1 .. 1:6 over 7 when a0 > a1, 4:1 .. 1:4 over 5 otherwise,
    where the last two entries are the fixed 0 and 255. Writing (7-i) or (5-i)
    instead overflows a byte outright on a light block.
    """
    if a0 > a1:
        return [a0, a1] + [((6 - i) * a0 + (i + 1) * a1) // 7 for i in range(6)]
    return [a0, a1] + [((4 - i) * a0 + (i + 1) * a1) // 5 for i in range(4)] + [0, 255]


# What the encoder writes: the full 0..255 range, so a mask value maps to an
# index by nearest match. Derived from the same function the decoder uses, so
# the two cannot drift apart.
_ALPHA_CODES = tuple(alpha_ramp(255, 0))


def _rgb565(value):
    return (((value >> 11) & 0x1F) * 255 // 31,
            ((value >> 5) & 0x3F) * 255 // 63,
            (value & 0x1F) * 255 // 31)


def blocks_wide(width):
    return max(1, (width + 3) // 4)


def decode_dxt1_channel(data, width, height, channel=0):
    """One colour channel of a DXT1 surface, as a width*height byte list."""
    return _decode_colour_channel(data, width, height, channel, 8, 0)


def decode_dxt5_channel(data, width, height, channel=0):
    """One colour channel of a DXT5 surface.

    A DXT5 block is sixteen bytes: eight of alpha, then eight that are a DXT1
    colour block exactly. So this is the same decode at an offset.
    """
    return _decode_colour_channel(data, width, height, channel, 16, 8)


def _decode_colour_channel(data, width, height, channel, stride, skip):
    out = bytearray(width * height)
    bw, bh = blocks_wide(width), blocks_wide(height)
    for by in range(bh):
        for bx in range(bw):
            offset = (by * bw + bx) * stride + skip
            if offset + 8 > len(data):
                continue
            c0, c1, bits = struct.unpack_from("<HHI", data, offset)
            a, b = _rgb565(c0), _rgb565(c1)
            if c0 > c1:
                palette = (a, b,
                           tuple((2 * a[i] + b[i]) // 3 for i in range(3)),
                           tuple((a[i] + 2 * b[i]) // 3 for i in range(3)))
            else:
                palette = (a, b,
                           tuple((a[i] + b[i]) // 2 for i in range(3)),
                           (0, 0, 0))
            for ty in range(4):
                y = by * 4 + ty
                if y >= height:
                    break
                for tx in range(4):
                    x = bx * 4 + tx
                    if x >= width:
                        break
                    out[y * width + x] = palette[(bits >> (2 * (ty * 4 + tx))) & 3][channel]
    return out


def decode_dxt5_alpha(data, width, height):
    """The alpha channel of a DXT5 surface, as a width*height byte list."""
    out = bytearray(width * height)
    bw, bh = blocks_wide(width), blocks_wide(height)
    for by in range(bh):
        for bx in range(bw):
            offset = (by * bw + bx) * 16
            if offset + 16 > len(data):
                continue
            a0, a1 = data[offset], data[offset + 1]
            bits = int.from_bytes(data[offset + 2:offset + 8], "little")
            ramp = alpha_ramp(a0, a1)
            for ty in range(4):
                y = by * 4 + ty
                if y >= height:
                    break
                for tx in range(4):
                    x = bx * 4 + tx
                    if x >= width:
                        break
                    out[y * width + x] = ramp[(bits >> (3 * (ty * 4 + tx))) & 7]
    return out


def decode_raw_channel(data, width, height, bytes_per_pixel, offset):
    out = bytearray(width * height)
    for i in range(width * height):
        at = i * bytes_per_pixel + offset
        if at < len(data):
            out[i] = data[at]
    return out


def resample(values, width, height, new_width, new_height):
    """Nearest-neighbour, for when the mask is not the size of the diffuse."""
    if (width, height) == (new_width, new_height):
        return values
    out = bytearray(new_width * new_height)
    for y in range(new_height):
        sy = min(height - 1, y * height // new_height)
        row = sy * width
        for x in range(new_width):
            out[y * new_width + x] = values[row + min(width - 1, x * width // new_width)]
    return out


def _alpha_block(alpha, width, height, bx, by):
    """An 8-byte DXT5 alpha block covering one 4x4 tile."""
    bits = 0
    for i in range(16):
        ty, tx = divmod(i, 4)
        y, x = by * 4 + ty, bx * 4 + tx
        value = alpha[min(y, height - 1) * width + min(x, width - 1)]
        best, best_error = 0, 256
        for code, level in enumerate(_ALPHA_CODES):
            error = abs(level - value)
            if error < best_error:
                best, best_error = code, error
        bits |= best << (3 * i)
    return bytes((255, 0)) + bits.to_bytes(6, "little")


def _to_four_colour(c0, c1, bits):
    """Re-encode a DXT1 punchthrough block onto the opaque four-colour ramp.

    Read as DXT5 the block would keep its three-colour palette but turn index 3
    from transparent into black, so the endpoints are swapped to force the
    four-colour mode and the indices remapped onto the nearest of its entries.
    """
    if c0 > c1:
        return c0, c1, bits
    if c0 == c1:
        # A flat block; bumping one endpoint keeps the colour and the mode.
        return (c0 + 1, c1, 0) if c0 < 0xFFFF else (c0, c1 - 1, 0)
    # After the swap the ramp runs the other way: old 0 and 1 trade places, the
    # old midpoint is nearest the new 1/3 entry, and transparent becomes the
    # endpoint it was drawn over.
    remap = (1, 0, 2, 0)
    new_bits = 0
    for i in range(16):
        new_bits |= remap[(bits >> (2 * i)) & 3] << (2 * i)
    return c1, c0, new_bits


def dxt1_with_alpha(colour, alpha, width, height):
    """A DXT5 surface: DXT1 colour blocks carried over, alpha built from `alpha`."""
    out = bytearray()
    bw, bh = blocks_wide(width), blocks_wide(height)
    for by in range(bh):
        for bx in range(bw):
            offset = (by * bw + bx) * 8
            if offset + 8 > len(colour):
                out += bytes(16)
                continue
            c0, c1, bits = struct.unpack_from("<HHI", colour, offset)
            c0, c1, bits = _to_four_colour(c0, c1, bits)
            out += _alpha_block(alpha, width, height, bx, by)
            out += struct.pack("<HHI", c0, c1, bits)
    return bytes(out)


def dxt5_with_alpha(existing, alpha, width, height):
    """A DXT5 surface with its colour blocks kept and its alpha blocks replaced."""
    out = bytearray()
    bw, bh = blocks_wide(width), blocks_wide(height)
    for by in range(bh):
        for bx in range(bw):
            offset = (by * bw + bx) * 16
            if offset + 16 > len(existing):
                out += bytes(16)
                continue
            out += _alpha_block(alpha, width, height, bx, by)
            out += existing[offset + 8:offset + 16]
    return bytes(out)


def _to_565(r, g, b):
    """Quantise an 8-bit triple to the packed 16-bit value DXT1 stores."""
    return ((r * 31 // 255) << 11) | ((g * 63 // 255) << 5) | (b * 31 // 255)


def encode_dxt1_tinted(values, width, height, tint):
    """Build a DXT1 surface from a single channel multiplied by a colour.

    For the material style this exists to serve -- a packed mask whose red
    channel is the albedo, tinted by a Base Color -- every texel in a block is
    the same colour at a different brightness, so the block's colours are
    exactly collinear and DXT1 represents them with no loss of hue at all. The
    endpoints are then just the block's darkest and brightest texel scaled by
    the tint, and a texel picks whichever of the four ramp entries its own
    brightness is nearest. That is why this encoder can be this short: the hard
    part of DXT1 compression is choosing a line through a cloud of colours, and
    here the line is given.
    """
    red, green, blue = tint
    out = bytearray()
    bw, bh = blocks_wide(width), blocks_wide(height)
    for by in range(bh):
        for bx in range(bw):
            texels = []
            for y in range(4):
                row = by * 4 + y
                for x in range(4):
                    column = bx * 4 + x
                    if row < height and column < width:
                        texels.append(values[row * width + column])
                    else:
                        texels.append(texels[-1] if texels else 0)
            low, high = min(texels), max(texels)
            c0 = _to_565(min(255, int(high * red)), min(255, int(high * green)),
                         min(255, int(high * blue)))
            c1 = _to_565(min(255, int(low * red)), min(255, int(low * green)),
                         min(255, int(low * blue)))
            if c0 <= c1:
                # A flat block, or one the quantiser collapsed. Leaving c0 <= c1
                # would select the three-colour mode, whose fourth entry is
                # transparent black -- so keep every index at 0 and the whole
                # block reads as c0 whichever mode it lands in.
                out += struct.pack("<HHI", c0, c1, 0)
                continue
            # Ramp entries, in DXT1's index order: c0, c1, then the two thirds.
            span = high - low
            bits = 0
            for i, value in enumerate(texels):
                position = (value - low) / span
                if position >= 5.0 / 6.0:
                    code = 0
                elif position >= 0.5:
                    code = 2
                elif position >= 1.0 / 6.0:
                    code = 3
                else:
                    code = 1
                bits |= code << (2 * i)
            out += struct.pack("<HHI", c0, c1, bits)
    return bytes(out)


def encode_dxt1_rgb(pixels, width, height):
    """Build a DXT1 surface from full-colour pixels: [(r, g, b), ...].

    `encode_dxt1_tinted` can be short because its block colours are collinear
    by construction. These are not: a two-tone material puts a red body and a
    pale trim in the same block, and the line has to be found rather than
    given. It is found the cheap way -- the darkest and brightest texel as
    endpoints -- which is exact for a block of one hue and a fair approximation
    for a block spanning two, since a DXT1 block can only ever be a line
    anyway.
    """
    out = bytearray()
    bw, bh = blocks_wide(width), blocks_wide(height)
    for by in range(bh):
        for bx in range(bw):
            texels = []
            for y in range(4):
                row = by * 4 + y
                for x in range(4):
                    column = bx * 4 + x
                    if row < height and column < width:
                        texels.append(pixels[row * width + column])
                    else:
                        texels.append(texels[-1] if texels else (0, 0, 0))
            luma = [0.299 * t[0] + 0.587 * t[1] + 0.114 * t[2] for t in texels]
            low = texels[luma.index(min(luma))]
            high = texels[luma.index(max(luma))]
            c0, c1 = _to_565(*high), _to_565(*low)
            if c0 <= c1:
                out += struct.pack("<HHI", c0, c1, 0)
                continue
            lo, hi = min(luma), max(luma)
            span = hi - lo
            bits = 0
            for i, value in enumerate(luma):
                position = (value - lo) / span
                if position >= 5.0 / 6.0:
                    code = 0
                elif position >= 0.5:
                    code = 2
                elif position >= 1.0 / 6.0:
                    code = 3
                else:
                    code = 1
                bits |= code << (2 * i)
            out += struct.pack("<HHI", c0, c1, bits)
    return bytes(out)
