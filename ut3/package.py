"""UE3 package file reader.

Handles the parts of the format UT3 (package version 512, cooker 57) actually
uses: the header, LZO-compressed chunks, and the name/import/export tables.

The whole file is addressed in *uncompressed* coordinates -- export table
offsets refer to that space, and read_range() transparently decompresses the
chunks that cover a requested range.
"""

import ctypes
import ctypes.util
import os
import struct
import zlib

# CompressionFlags
COMPRESS_ZLIB = 0x01
COMPRESS_LZO = 0x02
COMPRESS_LZX = 0x04
# Not stock UE3. Gears of War Reloaded (version 835, licensee 76) is a
# licensee fork that compresses its chunks with LZ4 and flags it 0x20, where
# stock UE3 has COMPRESS_BiasSpeed. Everything around it -- the header, the
# chunk framing, the block table -- is unchanged, so the one codec is the
# whole difference.
COMPRESS_LZ4 = 0x20

PACKAGE_TAG = 0x9E2A83C1

# Package versions at which UE3's header and export table changed shape. Both
# were found by reading a TOXIKK map (UDK 868) against a UT3 one (512) and
# checking that every table ends exactly where the next one begins; the exact
# version each landed at is not known, only that 512 is before and 868 after.
UDK_EXTRA_OFFSETS = 584
UDK_NO_COMPONENT_MAP = 639

_lzo = None
_lz4 = None

# Where the release puts its bundled DLLs: beside ut3conv.py, one level up from
# this file. Windows will not look there on its own -- LoadLibrary searches the
# directory of the running executable, which is python.exe, not the folder the
# scripts were unzipped into -- so the full path has to be offered explicitly.
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Real filenames per platform, because a hard-coded Linux soname is what shipped
# in v1 and a Windows user got "Could not find module 'liblzo2.so.2'" for every
# UT3 map, which are all LZO. find_library() knows each system's conventions but
# returns None often enough on Windows that actual names have to back it up.
_LZO_NAMES = ("lzo2.dll", "liblzo2-2.dll", "liblzo2.dll",
              "liblzo2.so.2", "liblzo2.so",
              "liblzo2.2.dylib", "liblzo2.dylib")
_LZ4_NAMES = ("lz4.dll", "liblz4.dll", "liblz4-1.dll",
              "liblz4.so.1", "liblz4.so",
              "liblz4.1.dylib", "liblz4.dylib")

_INSTALL_HINT = {
    "lzo2": "Arch: lzo   Debian/Ubuntu: liblzo2-2   macOS: brew install lzo",
    "lz4": "Arch: lz4   Debian/Ubuntu: liblz4-1    macOS: brew install lz4",
}


def _load_codec(soname, names):
    """Load a compression library, trying every name a platform might use.

    Raises with what was tried and what to install, rather than letting a
    ctypes OSError name a file the user has never heard of.
    """
    tried = []
    found = ctypes.util.find_library(soname)
    for name in ([found] if found else []) + list(names):
        for candidate in (name, os.path.join(_HERE, name)):
            try:
                return ctypes.CDLL(candidate)
            except OSError:
                tried.append(candidate)
    raise RuntimeError(
        "cannot load the %s library, which this package needs to decompress.\n"
        "Install it (%s), or on Windows put %s beside ut3conv.py.\n"
        "Tried: %s"
        % (soname, _INSTALL_HINT.get(soname, "see your package manager"),
           names[0], ", ".join(tried)))


def _lzo_decompress(src, out_len):
    global _lzo
    if _lzo is None:
        _lzo = _load_codec("lzo2", _LZO_NAMES)
        # __lzo_init_v2 is what the lzo_init() macro expands to; harmless if absent.
        if hasattr(_lzo, "__lzo_init_v2"):
            _lzo.__lzo_init_v2(1, -1, -1, -1, -1, -1, -1, -1, -1, -1)
    out = ctypes.create_string_buffer(out_len)
    n = ctypes.c_ulong(out_len)
    rc = _lzo.lzo1x_decompress_safe(src, ctypes.c_ulong(len(src)), out, ctypes.byref(n), None)
    if rc != 0:
        raise ValueError("lzo1x_decompress_safe failed with %d" % rc)
    return out.raw[: n.value]


def _lz4_decompress(src, out_len):
    global _lz4
    if _lz4 is None:
        _lz4 = _load_codec("lz4", _LZ4_NAMES)
        _lz4.LZ4_decompress_safe.restype = ctypes.c_int
    out = ctypes.create_string_buffer(out_len)
    n = _lz4.LZ4_decompress_safe(src, out, ctypes.c_int(len(src)),
                                 ctypes.c_int(out_len))
    if n < 0:
        raise ValueError("LZ4_decompress_safe failed with %d" % n)
    return out.raw[:n]


class Reader:
    """Little-endian cursor over a bytes object."""

    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos

    def __len__(self):
        return len(self.d)

    @property
    def eof(self):
        return self.p >= len(self.d)

    def bytes(self, n):
        v = self.d[self.p : self.p + n]
        if len(v) != n:
            raise EOFError("read past end of buffer")
        self.p += n
        return v

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

    def u64(self):
        v = struct.unpack_from("<Q", self.d, self.p)[0]
        self.p += 8
        return v

    def f32(self):
        v = struct.unpack_from("<f", self.d, self.p)[0]
        self.p += 4
        return v

    def fstring(self):
        n = self.i32()
        if n == 0:
            return ""
        if n > 0:
            return self.bytes(n).rstrip(b"\0").decode("latin-1")
        return self.bytes(-n * 2).decode("utf-16-le").rstrip("\0")


class ObjRef:
    """A resolved PackageIndex: >0 export, <0 import, 0 = None."""

    __slots__ = ("pkg", "index")

    def __init__(self, pkg, index):
        self.pkg = pkg
        self.index = index

    @property
    def is_null(self):
        return self.index == 0

    @property
    def is_export(self):
        return self.index > 0

    @property
    def is_import(self):
        return self.index < 0

    @property
    def export(self):
        return self.pkg.exports[self.index - 1] if self.index > 0 else None

    @property
    def imp(self):
        return self.pkg.imports[-self.index - 1] if self.index < 0 else None

    @property
    def name(self):
        if self.index == 0:
            return "None"
        return (self.export or self.imp).name

    @property
    def class_name(self):
        if self.index == 0:
            return "None"
        if self.index > 0:
            return self.pkg.class_name_of(self.export)
        return self.imp.class_name

    def __str__(self):
        if self.index == 0:
            return "None"
        return "%s'%s'" % (self.class_name, self.pkg.path_of(self.index))

    def __repr__(self):
        return "<ObjRef %d %s>" % (self.index, self)


class Import:
    __slots__ = ("package_name", "class_name", "outer", "name")

    def __init__(self, package_name, class_name, outer, name):
        self.package_name = package_name
        self.class_name = class_name
        self.outer = outer
        self.name = name


class Export:
    __slots__ = (
        "index",
        "class_index",
        "super_index",
        "outer",
        "name",
        "archetype",
        "flags",
        "size",
        "offset",
        "components",
        "export_flags",
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class Package:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.raw = f.read()
        self._chunk_cache = {}
        self._read_header()
        self._read_names()
        self._read_imports()
        self._read_exports()

    # ---------------------------------------------------------------- header

    def _read_header(self):
        r = Reader(self.raw)
        tag = r.u32()
        if tag != PACKAGE_TAG:
            raise ValueError("%s: not an Unreal package (tag %08X)" % (self.path, tag))
        v = r.i32()
        self.version = v & 0xFFFF
        self.licensee = (v >> 16) & 0xFFFF
        if self.version < 400:
            raise ValueError("%s: package version %d is not UE3" % (self.path, self.version))
        self.header_size = r.i32()
        self.folder_name = r.fstring()
        self.package_flags = r.u32()
        self.name_count = r.i32()
        self.name_offset = r.i32()
        self.export_count = r.i32()
        self.export_offset = r.i32()
        self.import_count = r.i32()
        self.import_offset = r.i32()
        self.depends_offset = r.i32()
        # Some builds carry four more offsets here that UT3 (512) does not: the
        # import/export GUID table, its two counts, and the thumbnail table.
        # UDK (868) has them, and skipping them is what lets a TOXIKK .udk read
        # with the same code. A version threshold was a guess, though, and it
        # guessed wrong: Gears of War Reloaded is version 835 and has *no*
        # extra offsets, so it read its GUID 16 bytes late, found no chunk
        # table, and handed back raw compressed bytes as if they were names.
        #
        # Probe instead of guessing. The first generation restates the export
        # and name counts the header has just given, which lines up only when
        # the GUID starts where it is being read from -- so the header checks
        # itself, at any version and any licensee.
        start = r.p
        for skip in (0, 16):
            r.p = start + skip
            guid = r.bytes(16)
            gens = r.i32()
            if 1 <= gens <= 64:
                probe = Reader(self.raw, r.p)
                if probe.i32() == self.export_count and probe.i32() == self.name_count:
                    break
        else:
            r.p = start + (16 if self.version >= UDK_EXTRA_OFFSETS else 0)
            guid = r.bytes(16)
            gens = r.i32()
        self.guid = guid
        r.p += gens * 12  # export count, name count, net object count
        self.engine_version = r.u32()
        self.cooker_version = r.u32()
        self.compression_flags = r.u32()
        n_chunks = r.i32()
        # (uncompressed offset, uncompressed size, compressed offset, compressed size)
        self.chunks = [(r.i32(), r.i32(), r.i32(), r.i32()) for _ in range(n_chunks)]
        self._uncompressed_prefix = self.chunks[0][0] if self.chunks else len(self.raw)

    # -------------------------------------------------------- decompression

    def _decompress_chunk(self, ci):
        cached = self._chunk_cache.get(ci)
        if cached is not None:
            return cached
        _uo, _us, co, _cs = self.chunks[ci]
        r = Reader(self.raw, co)
        magic = r.u32()
        if magic != PACKAGE_TAG:
            raise ValueError("bad compressed chunk magic %08X at %d" % (magic, co))
        block_size = r.u32()
        r.u32()  # total compressed size
        total_uncompressed = r.u32()
        n_blocks = (total_uncompressed + block_size - 1) // block_size
        blocks = [(r.u32(), r.u32()) for _ in range(n_blocks)]
        out = bytearray()
        p = r.p
        for csize, usize in blocks:
            src = self.raw[p : p + csize]
            p += csize
            if self.compression_flags & COMPRESS_LZO:
                out += _lzo_decompress(src, usize)
            elif self.compression_flags & COMPRESS_ZLIB:
                out += zlib.decompress(src)
            elif self.compression_flags & COMPRESS_LZ4:
                out += _lz4_decompress(src, usize)
            else:
                raise ValueError("unsupported compression flags %X" % self.compression_flags)
        data = bytes(out)
        self._chunk_cache[ci] = data
        return data

    def read_range(self, offset, size):
        """Read `size` bytes at `offset` in uncompressed-file coordinates."""
        if not self.chunks:
            return self.raw[offset : offset + size]
        out = bytearray()
        pos = offset
        end = offset + size
        # Bytes before the first chunk live uncompressed at the head of the file.
        if pos < self._uncompressed_prefix:
            take = min(end, self._uncompressed_prefix) - pos
            out += self.raw[pos : pos + take]
            pos += take
        while pos < end:
            for ci, (uo, us, _co, _cs) in enumerate(self.chunks):
                if uo <= pos < uo + us:
                    data = self._decompress_chunk(ci)
                    take = min(end, uo + us) - pos
                    out += data[pos - uo : pos - uo + take]
                    pos += take
                    break
            else:
                raise ValueError("offset %d is not covered by any chunk" % pos)
        return bytes(out)

    def reader(self, offset, size):
        return Reader(self.read_range(offset, size))

    # ----------------------------------------------------------- name table

    def _read_names(self):
        r = self.reader(self.name_offset, self.import_offset - self.name_offset)
        self.names = []
        for _ in range(self.name_count):
            name = r.fstring()
            r.u64()  # name flags
            self.names.append(name)

    def fname(self, r):
        """Read an FName (name index + instance number) and render it."""
        i = r.i32()
        n = r.i32()
        base = self.names[i]
        return base if n == 0 else "%s_%d" % (base, n - 1)

    # --------------------------------------------------------- import table

    def _read_imports(self):
        r = self.reader(self.import_offset, self.export_offset - self.import_offset)
        self.imports = []
        for _ in range(self.import_count):
            pkg_name = self.fname(r)
            cls_name = self.fname(r)
            outer = r.i32()
            name = self.fname(r)
            self.imports.append(Import(pkg_name, cls_name, outer, name))

    # --------------------------------------------------------- export table

    def _read_exports(self):
        """Parse the export table, working out for itself whether it has a
        ComponentMap.

        UT3 (512) keeps one and UDK (868) does not, and a version threshold
        between them was a guess. It guessed wrong for Gears of War Reloaded,
        which is 835 and *does* keep one -- read without it, the map's first
        class default object consumes its component's name as a net-object
        count and the table falls apart 17 entries in.

        The table has a known length, so it can check itself: exactly one
        reading consumes `export_offset .. depends_offset` to the byte. On
        MP_Courtyard that is 2,271,328 bytes for 30,936 exports, and only the
        ComponentMap reading lands on it.
        """
        size = self.depends_offset - self.export_offset
        data = self.read_range(self.export_offset, size)
        default = self.version < UDK_NO_COMPONENT_MAP
        for with_components in (default, not default):
            try:
                exports, consumed = self._parse_exports(data, with_components)
            except (EOFError, IndexError, ValueError):
                continue
            if consumed == size:
                self.exports = exports
                self.has_component_map = with_components
                return
        # Nothing fitted exactly -- take the version's word for it and let the
        # caller fail on whatever is actually wrong.
        self.exports, _consumed = self._parse_exports(data, default)
        self.has_component_map = default

    def _parse_exports(self, data, with_components):
        r = Reader(data)
        exports = []
        for i in range(self.export_count):
            class_index = r.i32()
            super_index = r.i32()
            outer = r.i32()
            name = self.fname(r)
            archetype = r.i32()
            flags = r.u64()
            size = r.i32()
            offset = r.i32()
            # UT3 keeps a TMap<FName,INT> of the object's components here. The
            # values are 0-based export indices, so bump them to PackageIndex.
            # UE3 dropped the map later on, so a UDK package has no such field
            # and reading one would consume the export flags instead.
            components = {}
            if with_components:
                n_components = r.i32()
                if not 0 <= n_components <= 4096:
                    raise ValueError("implausible component count %d" % n_components)
                for _ in range(n_components):
                    comp_name = self.fname(r)
                    components[comp_name] = r.i32() + 1
            export_flags = r.u32()
            n_net_objects = r.i32()
            if not 0 <= n_net_objects <= 4096:
                raise ValueError("implausible net object count %d" % n_net_objects)
            r.p += n_net_objects * 4
            r.p += 16  # package guid
            r.p += 4  # package flags
            exports.append(
                Export(
                    index=i + 1,
                    class_index=class_index,
                    super_index=super_index,
                    outer=outer,
                    name=name,
                    archetype=archetype,
                    flags=flags,
                    size=size,
                    offset=offset,
                    components=components,
                    export_flags=export_flags,
                )
            )
        return exports, r.p

    # ------------------------------------------------------------- lookups

    def class_name_of(self, export):
        """The export's class name, or "" when the index does not resolve.

        Out of range rather than unreachable: some cooked packages carry a
        class index pointing past their own import table, and the reference
        that reaches it is one this converter has followed across packages.
        DM-Deimos has one, and it used to end a whole batch run with an
        IndexError three hours in. An unreadable class is a material to skip,
        not a reason to stop, so it degrades instead of raising.
        """
        ci = export.class_index
        if ci == 0:
            return "Class"
        if ci < 0:
            index = -ci - 1
            return self.imports[index].name if index < len(self.imports) else ""
        return self.exports[ci - 1].name if ci - 1 < len(self.exports) else ""

    def path_of(self, index):
        """Full dotted path of a PackageIndex, e.g. MyLevel.PointLight_3."""
        parts = []
        seen = set()
        while index != 0 and index not in seen:
            seen.add(index)
            if index > 0:
                e = self.exports[index - 1]
                parts.append(e.name)
                index = e.outer
            else:
                imp = self.imports[-index - 1]
                parts.append(imp.name)
                index = imp.outer
        return ".".join(reversed(parts))

    def ref(self, index):
        return ObjRef(self, index)

    def export_data(self, export):
        return self.read_range(export.offset, export.size)

    def exports_of_class(self, *class_names):
        want = set(class_names)
        return [e for e in self.exports if self.class_name_of(e) in want]

    def find(self, name):
        """Exports matching a name or dotted path (case-insensitive)."""
        low = name.lower()
        hits = [e for e in self.exports if e.name.lower() == low]
        if hits:
            return hits
        return [e for e in self.exports if self.path_of(e.index).lower() == low]

    def class_histogram(self):
        counts = {}
        for e in self.exports:
            cn = self.class_name_of(e)
            counts[cn] = counts.get(cn, 0) + 1
        return counts

    def __repr__(self):
        return "<Package %s ver=%d names=%d imports=%d exports=%d>" % (
            self.path.rsplit("/", 1)[-1],
            self.version,
            self.name_count,
            self.import_count,
            self.export_count,
        )
