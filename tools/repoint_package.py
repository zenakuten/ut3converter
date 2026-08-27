#!/usr/bin/env python3
"""Rename the package a built .ut2 and .utx pair refer to each other by.

    ./tools/repoint_package.py --list DM-Dekk.ut2
    ./tools/repoint_package.py BLDekkTex DekkTexV1 DM-Dekk-v1.ut2 DekkTexV1.utx

A mapper who takes a converted map and iterates on it copies both halves --
`DM-Dekk.ut2` to `DM-Dekk-v1.ut2`, `BLDekkTex.utx` to something of their own --
and finds the map still loading the *original* texture package, because the name
it imports is baked into the file.

**Both files have to be renamed, not just the map.** A UE2 package does not
record its own name -- the filename is the name -- so renaming the .utx looks
like enough. It is not: the converter builds its package from a generated script
class of the same name, so the map imports `Class BLDekkTex.BLDekkTex`, the
class as well as the package. Rename only the map and the engine says

    Failed import: Class Class DekkTexV1.DekkTexV1 (file ../Textures/DekkTexV1.utx)
    Failed loading package: Can't find Class in file Class DekkTexV1.DekkTexV1

because the .utx still calls its class the old thing. Renaming the string in both
files fixes it, and that is why this takes a list of files.

**The new name must be exactly as long as the old one.** Everything is
referenced by name *index*, so the string can change freely -- but the name table
sits near the front,

    header 0..64 | names | export data | imports | export table

and a length change shifts everything after it. Moving `ImportOffset`,
`ExportOffset` and every export's `SerialOffset` to match is not enough: UT2004
refuses the result, while a same-length rename of the same file loads. UE2 export
data carries absolute file positions of its own (a `TLazyArray` writes the offset
it can skip to), and there is no way to find those without knowing how every
class serialises. So the length is a hard constraint rather than a limitation
worth working around -- `BLDekkTex` is nine characters, and so must its
replacement be.

Each file is verified before it is written: re-parsed, and every export payload
compared byte for byte against the original.
"""

import argparse
import os
import struct
import sys


class Reader:
    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def index(self):
        """UE2 FCompactIndex."""
        b = self.u8()
        negative, value, shift = b & 0x80, b & 0x3F, 6
        if b & 0x40:
            while True:
                c = self.u8()
                value |= (c & 0x7F) << shift
                shift += 7
                if not c & 0x80:
                    break
        return -value if negative else value

    def string(self):
        n = self.index()
        if n == 0:
            return ""
        if n < 0:                       # UTF-16, which UT2004 never writes here
            raw = self.d[self.p:self.p - 2 * n]
            self.p += -2 * n
            return raw.decode("utf-16-le").rstrip("\x00")
        raw = self.d[self.p:self.p + n]
        self.p += n
        return raw.rstrip(b"\x00").decode("latin-1")


def encode_index(value):
    negative = value < 0
    value = abs(value)
    first = value & 0x3F
    value >>= 6
    if negative:
        first |= 0x80
    if value:
        first |= 0x40
    out = bytearray([first])
    while value:
        b = value & 0x7F
        value >>= 7
        if value:
            b |= 0x80
        out.append(b)
    return bytes(out)


def encode_string(text):
    raw = text.encode("latin-1") + b"\x00"
    return encode_index(len(raw)) + raw


class Package:
    """Just enough of a UT2004 package to move its name table."""

    def __init__(self, path):
        with open(path, "rb") as handle:
            self.data = handle.read()
        self.path = path
        r = Reader(self.data)
        self.tag = r.u32()
        if self.tag != 0x9E2A83C1:
            raise ValueError("%s is not an Unreal package" % path)
        self.version = r.u32() & 0xFFFF
        self.flags = r.u32()
        self.name_count, self.name_offset = r.i32(), r.i32()
        self.export_count, self.export_offset = r.i32(), r.i32()
        self.import_count, self.import_offset = r.i32(), r.i32()

        r.p = self.name_offset
        self.names = []
        for _ in range(self.name_count):
            self.names.append((r.string(), r.u32()))
        self.names_end = r.p

        r.p = self.export_offset
        self.exports = []
        for _ in range(self.export_count):
            entry = [r.index(), r.index(), r.i32(), r.index(), r.u32(), r.index()]
            entry.append(r.index() if entry[5] > 0 else 0)
            self.exports.append(entry)

    def imported_packages(self):
        """Names used as the outer of an import, i.e. packages this file needs."""
        r = Reader(self.data, self.import_offset)
        found = []
        for _ in range(self.import_count):
            _class_package, _class_name = r.index(), r.index()
            outer, object_name = r.i32(), r.index()
            if outer == 0:              # outer 0 means the name *is* a package
                found.append(self.names[object_name][0])
        return sorted(set(found))


def repoint(package, old, new):
    """Return the rewritten file bytes, or raise ValueError."""
    hits = [i for i, (text, _f) in enumerate(package.names)
            if text.lower() == old.lower()]
    if not hits:
        raise ValueError("no name %r in %s -- try --list"
                         % (old, os.path.basename(package.path)))
    if len(new.encode("latin-1")) != len(old.encode("latin-1")):
        raise ValueError(
            "%r is %d characters and %r is %d: the replacement has to be the "
            "same length, or the file shifts and UT2004 will not load it "
            "(see the module docstring)"
            % (old, len(old), new, len(new)))

    names = list(package.names)
    for i in hits:
        names[i] = (new, names[i][1])
    table = bytearray()
    for text, flags in names:
        table += encode_string(text) + struct.pack("<I", flags)
    delta = len(table) - (package.names_end - package.name_offset)

    exports = bytearray()
    for cls, sup, pkg, name, flags, size, offset in package.exports:
        exports += (encode_index(cls) + encode_index(sup)
                    + struct.pack("<i", pkg) + encode_index(name)
                    + struct.pack("<I", flags) + encode_index(size))
        if size > 0:
            exports += encode_index(offset + delta)

    head = bytearray(package.data[:package.name_offset])
    struct.pack_into("<i", head, 24, package.export_offset + delta)
    struct.pack_into("<i", head, 32, package.import_offset + delta)

    middle = package.data[package.names_end:package.export_offset]
    return bytes(head) + bytes(table) + middle + bytes(exports), len(hits), delta


def verify(original, rewritten, old, new):
    """The payload of every export has to survive untouched."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".ut2", delete=False) as tmp:
        tmp.write(rewritten)
        name = tmp.name
    try:
        after = Package(name)
        if after.name_count != original.name_count:
            return "name count changed"
        if after.export_count != original.export_count:
            return "export count changed"
        if [t.lower() for t, _f in after.names].count(new.lower()) < 1:
            return "the new name is not in the table"
        for before, now in zip(original.exports, after.exports):
            if before[5] != now[5]:
                return "an export changed size"
            if before[5] <= 0:
                continue
            if original.data[before[6]:before[6] + before[5]] != \
                    after.data[now[6]:now[6] + now[5]]:
                return "export payload moved but did not survive"
        return None
    finally:
        os.unlink(name)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("old", nargs="?", help="the package name in use now")
    parser.add_argument("new", nargs="?",
                        help="what to call it, same length as the old one")
    parser.add_argument("files", nargs="*",
                        help="every file that names it -- the .ut2 and its .utx")
    parser.add_argument("--list", metavar="FILE",
                        help="show the packages FILE imports and stop")
    args = parser.parse_args()

    if args.list:
        package = Package(args.list)
        print("%s imports:" % os.path.basename(args.list))
        for name in package.imported_packages():
            print("   %s" % name)
        return 0
    if not args.old or not args.new or not args.files:
        parser.error("give the old name, the new name, and the files to edit "
                     "-- or --list FILE")

    # Nothing is written until every file has been rewritten and checked, so a
    # pair cannot be left half renamed.
    pending = []
    for path in args.files:
        try:
            package = Package(path)
            rewritten, hits, _delta = repoint(package, args.old, args.new)
        except (ValueError, OSError, struct.error, IndexError) as exc:
            print("%s: %s" % (os.path.basename(path), exc), file=sys.stderr)
            return 1
        problem = verify(package, rewritten, args.old, args.new)
        if problem:
            print("%s: refusing to write, %s" % (os.path.basename(path), problem),
                  file=sys.stderr)
            return 1
        pending.append((path, rewritten, hits))

    for path, rewritten, hits in pending:
        with open(path + ".tmp", "wb") as handle:
            handle.write(rewritten)
        os.replace(path + ".tmp", path)
        print("%-28s %s -> %s (%d name%s)"
              % (os.path.basename(path), args.old, args.new, hits,
                 "" if hits == 1 else "s"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
