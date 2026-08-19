"""UE3 tagged-property reader.

Actors, components and most non-native objects serialize as a list of tagged
properties terminated by a "None" name. Natively-serialized objects (UModel,
UPolys, UTexture2D mip data, UStaticMesh render data) are *not* covered here --
those get dedicated readers.
"""

import struct

from .package import Reader

# Structs with native serializers, i.e. plain binary rather than tagged properties.
_STRUCT_SIZES = {
    "Vector": 12,
    "Vector2D": 8,
    "Vector4": 16,
    "Plane": 16,
    "Quat": 16,
    "Rotator": 12,
    "Color": 4,
    "LinearColor": 16,
    "Guid": 16,
    "Matrix": 64,
    "TwoVectors": 24,
    "IntPoint": 8,
    "Box": 25,
    "BoxSphereBounds": 28,
}


class Struct:
    """A decoded struct value; unknown structs keep their raw bytes."""

    __slots__ = ("type", "value", "raw")

    def __init__(self, type_name, value=None, raw=None):
        self.type = type_name
        self.value = value
        self.raw = raw

    def __repr__(self):
        if self.value is not None:
            return "%s%r" % (self.type, self.value)
        return "%s(<%d bytes>)" % (self.type, len(self.raw or b""))


class Array:
    """An array property. Element type is not in the tag, so decode on demand."""

    __slots__ = ("count", "raw", "pkg")

    def __init__(self, pkg, count, raw):
        self.pkg = pkg
        self.count = count
        self.raw = raw

    @property
    def stride(self):
        return len(self.raw) // self.count if self.count else 0

    def as_ints(self):
        return list(struct.unpack("<%di" % self.count, self.raw[: self.count * 4]))

    def as_floats(self):
        return list(struct.unpack("<%df" % self.count, self.raw[: self.count * 4]))

    def as_objects(self):
        return [self.pkg.ref(i) for i in self.as_ints()]

    def as_vectors(self):
        out = []
        for i in range(self.count):
            out.append(struct.unpack_from("<3f", self.raw, i * 12))
        return out

    def as_props(self):
        """Decode as an array of struct properties (tagged property lists)."""
        r = Reader(self.raw)
        out = []
        for _ in range(self.count):
            out.append(read_properties(self.pkg, r))
        return out

    def __len__(self):
        return self.count

    def __repr__(self):
        return "Array(%d x %db)" % (self.count, self.stride)


class Properties:
    """Ordered tagged-property list with dict-style access."""

    def __init__(self):
        self.items = []  # (name, array_index, type, value)

    def add(self, name, idx, type_name, value):
        self.items.append((name, idx, type_name, value))

    def get(self, name, default=None):
        for n, _i, _t, v in self.items:
            if n == name:
                return v
        return default

    def get_all(self, name):
        return [v for n, _i, _t, v in self.items if n == name]

    def type_of(self, name):
        for n, _i, t, _v in self.items:
            if n == name:
                return t
        return None

    def __contains__(self, name):
        return any(n == name for n, _i, _t, _v in self.items)

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __repr__(self):
        return "Properties(%s)" % ", ".join(n for n, _i, _t, _v in self.items)


def _decode_struct(pkg, type_name, raw):
    r = Reader(raw)
    try:
        if type_name == "Vector":
            return Struct(type_name, (r.f32(), r.f32(), r.f32()), raw)
        if type_name == "Vector2D":
            return Struct(type_name, (r.f32(), r.f32()), raw)
        if type_name in ("Vector4", "Plane", "Quat"):
            return Struct(type_name, (r.f32(), r.f32(), r.f32(), r.f32()), raw)
        if type_name == "Rotator":
            return Struct(type_name, (r.i32(), r.i32(), r.i32()), raw)
        if type_name == "Color":
            b, g, rr, a = r.u8(), r.u8(), r.u8(), r.u8()
            return Struct(type_name, (rr, g, b, a), raw)
        if type_name == "LinearColor":
            return Struct(type_name, (r.f32(), r.f32(), r.f32(), r.f32()), raw)
        if type_name == "IntPoint":
            return Struct(type_name, (r.i32(), r.i32()), raw)
    except (EOFError, struct.error):
        return Struct(type_name, None, raw)
    # Unknown struct: in UE3 these serialize as a nested tagged-property list.
    if type_name not in _STRUCT_SIZES:
        try:
            nested = read_properties(pkg, Reader(raw))
            if len(nested):
                return Struct(type_name, nested, raw)
        except (EOFError, IndexError, struct.error, ValueError):
            pass
    return Struct(type_name, None, raw)


def read_properties(pkg, r, limit=None):
    """Read a tagged-property list from `r` until the terminating None."""
    props = Properties()
    while True:
        if r.eof:
            break
        name = pkg.fname(r)
        if name == "None":
            break
        type_name = pkg.fname(r)
        size = r.i32()
        array_index = r.i32()

        struct_name = None
        if type_name == "StructProperty":
            struct_name = pkg.fname(r)

        start = r.p
        if type_name == "IntProperty":
            value = r.i32()
        elif type_name == "FloatProperty":
            value = r.f32()
        elif type_name == "BoolProperty":
            # UT3 (v512): Size is 0 and the value follows the tag as an INT.
            value = r.i32() != 0
            props.add(name, array_index, type_name, value)
            continue
        elif type_name == "ByteProperty":
            # Enums serialize their value as an FName, plain bytes as one byte.
            value = pkg.fname(r) if size == 8 else r.u8()
        elif type_name == "NameProperty":
            value = pkg.fname(r)
        elif type_name == "StrProperty":
            value = r.fstring()
        elif type_name in ("ObjectProperty", "ClassProperty", "ComponentProperty", "InterfaceProperty"):
            value = pkg.ref(r.i32())
        elif type_name == "StructProperty":
            value = _decode_struct(pkg, struct_name, r.bytes(size))
        elif type_name == "ArrayProperty":
            count = r.i32()
            value = Array(pkg, count, r.bytes(size - 4))
        else:
            value = r.bytes(size)

        # Trust the tag's size field over our per-type decoding.
        r.p = start + size
        props.add(name, array_index, type_name, value)
        if limit is not None and len(props) >= limit:
            break
    return props


def _looks_like_tag(pkg, data, pos):
    """Does a property tag plausibly start at `pos`?"""
    if pos + 8 > len(data):
        return False
    ni, nn = struct.unpack_from("<ii", data, pos)
    if not (0 <= ni < pkg.name_count) or not (0 <= nn < 1_000_000):
        return False
    if pkg.names[ni] == "None":
        return True
    if pos + 16 > len(data):
        return False
    ti, tn = struct.unpack_from("<ii", data, pos + 8)
    if not (0 <= ti < pkg.name_count) or tn != 0:
        return False
    return pkg.names[ti].endswith("Property")


def _try_parse(pkg, data, pos):
    """Parse from `pos`; return (props, end) only if it terminates cleanly."""
    if not _looks_like_tag(pkg, data, pos):
        return None
    r = Reader(data, pos)
    try:
        props = read_properties(pkg, r)
    except (EOFError, IndexError, struct.error, ValueError):
        return None
    if r.eof and len(props) == 0:
        return None
    if r.p > len(data):
        return None
    return props, r.p


def find_property_start(pkg, data):
    """Locate the start of the tagged-property list within an object's data.

    Objects may prefix their serial data (a NetIndex, or a script state frame
    when RF_HasStack is set). Rather than model every case, probe the plausible
    offsets and keep the first that parses through to the terminating None.
    """
    for pos in range(0, min(len(data), 64), 4):
        if _try_parse(pkg, data, pos):
            return pos
    return None


def read_object_properties(pkg, export):
    """Read the tagged properties of an export. Returns (Properties, start, end)."""
    data = pkg.export_data(export)
    for pos in range(0, min(len(data), 64), 4):
        parsed = _try_parse(pkg, data, pos)
        if parsed:
            return parsed[0], pos, parsed[1]
    return Properties(), None, 0
