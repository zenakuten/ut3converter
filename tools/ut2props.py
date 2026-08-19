"""Reading UE2 tagged properties out of a built .ut2, for reference values.

Only enough to look at what a stock UT2004 map does -- the converter never
writes packages, so this is a research tool rather than part of the pipeline.

A property is: name index, then an info byte holding the type in bits 0-3, a
size code in bits 4-6 and an array flag in bit 7. Struct properties carry a
struct name after the info byte. The size code indexes a fixed table, with the
top three codes meaning "a byte/word/int of length follows".
"""

import struct

TYPES = {1: "Byte", 2: "Int", 3: "Bool", 4: "Float", 5: "Object", 6: "Name",
         7: "String", 8: "Class", 9: "Array", 10: "Struct", 11: "Vector",
         12: "Rotator", 13: "Str", 14: "Map", 15: "FixedArray"}
SIZES = {0: 1, 1: 2, 2: 4, 3: 12, 4: 16}


class R:
    def __init__(self, d, p=0):
        self.d, self.p = d, p

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def f32(self):
        v = struct.unpack_from("<f", self.d, self.p)[0]
        self.p += 4
        return v

    def idx(self):
        """UE2's compact index."""
        b = self.u8()
        neg, value, shift = b & 0x80, b & 0x3F, 6
        if b & 0x40:
            while True:
                c = self.u8()
                value |= (c & 0x7F) << shift
                shift += 7
                if not (c & 0x80):
                    break
        return -value if neg else value


# Engine/Inc/UnObjBas.h: an object saved mid-state writes its execution stack
# before its properties.
RF_HAS_STACK = 0x02000000


def property_start(data, export):
    """Where an export's tagged properties begin, past any state frame."""
    if not export.get("flags", 0) & RF_HAS_STACK:
        return export["off"]
    r = R(data, export["off"])
    node = r.idx()
    r.idx()                 # StateNode
    r.p += 8                # ProbeMask
    r.p += 4                # LatentAction
    if node != 0:
        r.idx()             # Offset into the node's bytecode
    return r.p


def read_properties(data, names, offset, limit=64):
    """[(name, type, value)] from a tagged property list."""
    r = R(data, offset)
    out = []
    for _ in range(limit):
        name = names[r.idx()]
        if name == "None":
            break
        info = r.u8()
        kind = TYPES.get(info & 0x0F, "?")
        struct_name = names[r.idx()] if kind == "Struct" else None
        code = (info >> 4) & 0x07
        size = SIZES.get(code)
        if size is None:
            size = [None, None, None, None, None, r.u8, lambda: struct.unpack_from(
                "<H", data, r.p)[0], r.i32][code]
            size = size() if callable(size) else size
            if code == 6:
                r.p += 2
        if info & 0x80 and kind != "Bool":
            r.idx()
        start = r.p
        if kind == "Bool":
            value = bool((info >> 4) & 1)
            out.append((name, kind, value))
            continue
        if kind == "Float":
            value = round(r.f32(), 4)
        elif kind == "Int":
            value = r.i32()
        elif kind == "Byte":
            value = r.u8()
        elif kind in ("Object", "Class"):
            value = r.idx()
        elif kind == "Name":
            value = names[r.idx()]
        elif kind == "Str":
            n = r.idx()
            value = data[r.p:r.p + n].rstrip(b"\0").decode("latin-1")
        elif kind == "Vector" or struct_name == "Vector":
            value = tuple(round(v, 2) for v in struct.unpack_from("<3f", data, r.p))
        elif kind == "Rotator" or struct_name == "Rotator":
            value = struct.unpack_from("<3i", data, r.p)
        elif kind == "Struct":
            # A non-native struct is itself a tagged property list.
            try:
                value = read_properties(data, names, r.p, limit=16)
            except (IndexError, KeyError, struct.error):
                value = "%s<%d bytes>" % (struct_name, size)
        else:
            value = "%s<%d bytes>" % (struct_name or "", size)
        r.p = start + size
        out.append((name, struct_name or kind, value))
    return out
