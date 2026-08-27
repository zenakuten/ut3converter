#!/usr/bin/env python3
"""Regression tests for tools/repoint_package.py.

    python3 tests/test_repoint.py [path/to/DM-Dekk.ut2]

Reads a real built map when one is to hand and skips cleanly when it is not.
The engine-level check this cannot do -- that UT2004 actually loads the result --
was done by hand: `ucc dumpint` on a renamed DM-Dekk/BLDekkTex pair, which fails
when only the map is renamed and succeeds when both are.
"""

import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

from repoint_package import Package, Reader, encode_index, encode_string, repoint

DEFAULT_MAP = os.path.expanduser("~/UT2004_p23win/Maps/DM-Dekk.ut2")

_failures = []


def check(label, got, want):
    if got == want:
        print("  ok    %s = %r" % (label, got))
    else:
        print("  FAIL  %s = %r (expected %r)" % (label, got, want))
        _failures.append(label)


def check_that(label, cond, detail=""):
    if cond:
        print("  ok    %s %s" % (label, detail))
    else:
        print("  FAIL  %s %s" % (label, detail))
        _failures.append(label)


def main(path):
    print("compact index round-trip")
    # UE2's FCompactIndex: bit 7 of the first byte is the sign, bit 6 says
    # another byte follows, and every later byte carries seven bits.
    for value in (0, 1, 63, 64, 8191, 8192, 1 << 20, -1, -64, -70000):
        raw = encode_index(value)
        check("%d survives %d byte(s)" % (value, len(raw)),
              Reader(raw).index(), value)
    check("a name is length-prefixed including its terminator",
          encode_string("AB"), b"\x03AB\x00")

    if not os.path.isfile(path):
        print("no map at %s -- skipping the rest" % path)
        return 0

    print("a real package")
    package = Package(path)
    imports = package.imported_packages()
    check_that("its imports include a texture package",
               any(n.lower().endswith("tex") for n in imports), str(imports))
    target = next(n for n in imports if n.lower().endswith("tex"))

    # Both tables have to re-encode to exactly what was read, or a rename would
    # corrupt a file it never meant to touch.
    table = bytearray()
    for text, flags in package.names:
        table += encode_string(text) + struct.pack("<I", flags)
    check_that("the name table round-trips byte for byte",
               bytes(table) == package.data[package.name_offset:package.names_end])

    print("renaming")
    same = target[:-1] + ("X" if target[-1] != "X" else "Y")
    rewritten, hits, delta = repoint(package, target, same)
    check("a same-length rename moves nothing", delta, 0)
    check("and touches one name", hits, 1)
    check("so the file is the same size", len(rewritten), len(package.data))
    differing = sum(1 for a, b in zip(package.data, rewritten) if a != b)
    check("differing by one character", differing, 1)

    # The length is a hard constraint: UT2004 refuses a shifted file, because
    # UE2 export data carries absolute file positions of its own.
    for bad in (target + "X", target[:-1]):
        try:
            repoint(package, target, bad)
            check_that("a %d-character replacement is refused" % len(bad), False)
        except ValueError as exc:
            check_that("a %d-character replacement is refused" % len(bad),
                       "same length" in str(exc))

    try:
        repoint(package, "NoSuchPackage", "NoSuchPackage")
        check_that("an absent name is refused", False)
    except ValueError as exc:
        check_that("an absent name is refused", "no name" in str(exc))

    print("what the rewritten file parses as")
    with tempfile.NamedTemporaryFile(suffix=".ut2", delete=False) as tmp:
        tmp.write(rewritten)
        temp_path = tmp.name
    try:
        after = Package(temp_path)
        check("the same number of names", after.name_count, package.name_count)
        check("the same number of exports", after.export_count, package.export_count)
        check_that("the new name is what it imports now",
                   same in after.imported_packages(),
                   str(after.imported_packages()))
        moved = sum(1 for before, now in zip(package.exports, after.exports)
                    if before[6] != now[6])
        check("no export payload moved", moved, 0)
    finally:
        os.unlink(temp_path)

    if _failures:
        print("%d check(s) failed: %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
