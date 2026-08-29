#!/usr/bin/env python3
"""Package built maps for distribution: copy, compress, verify.

    ./tools/package_maps.py --list              what would be packaged
    ./tools/package_maps.py --match BL-Dekk     that map
    ./tools/package_maps.py --match BL-          every BloodLust map

A UT2004 server hands maps to joining clients as .uz2 files, so shipping one
means four files in one folder: the .ut2, its .utx, and a .uz2 of each. This
does that for a map `batch.py` has already converted and the editor has already
built into a .ut2.

The map is named the way `batch.py --match` names it -- by its *source* name
(BL-Dekk), matched case-insensitively as a substring -- and the converted name
and package name are asked of `batch` rather than restated here, for the reason
its own `package_name` gives.

Two things this checks that doing it by hand does not:

Staleness. The .ut2 is built by hand in UnrealEd, so nothing connects it to the
.t3d it came from, and a converter fix that never got re-imported is invisible:
the map looks built and is a version behind. DM-Dekk shipped exactly that way
in testing -- its .ut2 predated the skysphere fix, so its dome was still
SM_skybox_up_high at 1:224 instead of SM_SkySphere at 1:667. A .ut2 older than
its .t3d is refused, and --force is the way to say you meant it.

Size. UCC reports its ratio through a signed 32-bit percentage that overflows
on large files -- BLGaneshaTex.utx compresses to 43% of its size and UCC calls
it -28%. The ratio printed here is measured from the two files instead.
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import batch

DEFAULT_FOLDER = "TOXIKKMaps"


def compress(system, folder, filename):
    """Run UCC's compress commandlet, which writes <file>.uz2 beside the source.

    The path has to be given with backslashes: the commandlet does its own
    parsing and a forward slash makes it fail to find the file. That applies
    inside `folder` too, so a nested output folder (UT3Maps/Redirect) is
    translated rather than passed through.
    """
    windows_path = "..\\%s\\%s" % (folder.replace("/", "\\"), filename)
    result = subprocess.run(["./UCC.exe", "compress", windows_path],
                            cwd=system, capture_output=True, text=True)
    return result.returncode == 0


def package(entry, out_folder, force=False, map_only=False):
    """Copy one map's two files into the output folder and compress each.

    `map_only` ships the .ut2 alone. A converter change that moves only actor
    properties -- an ambient's SoundVolume, a Skins reference -- rewrites the
    .t3d and leaves the package identical, and recompressing 150MB of textures
    to prove it costs minutes. The .utx already in the folder still matches.
    """
    _path, base, name, tex = entry
    maps = os.path.join(batch.EDITOR, "Maps", name + ".ut2")
    textures = os.path.join(batch.EDITOR, "Textures", tex + ".utx")
    out = os.path.join(batch.EDITOR, out_folder)

    wanted = (maps,) if map_only else (maps, textures)
    for source in wanted:
        if not os.path.isfile(source):
            missing = os.path.relpath(source, batch.EDITOR)
            if source is maps:
                print("  %-15s no %s -- import the t3d in UnrealEd and save it"
                      % (base, missing))
            else:
                print("  %-15s no %s -- run batch.py --match %s --build"
                      % (base, missing, base))
            return False

    t3d = os.path.join(batch.EDITOR, "Converted", name + ".t3d")
    if os.path.isfile(t3d) and os.path.getmtime(maps) < os.path.getmtime(t3d) \
            and not force:
        print("  %-15s STALE: %s.ut2 is older than the %s.t3d it came from."
              % (base, name, name))
        print("  %-15s re-import it in UnrealEd, or --force to ship it anyway."
              % "")
        return False

    os.makedirs(out, exist_ok=True)
    print("  %s" % base)
    for source in wanted:
        filename = os.path.basename(source)
        destination = os.path.join(out, filename)
        archive = destination + ".uz2"
        # A .uz2 left from a previous run would be silently kept if the
        # commandlet failed, and shipped alongside a map it does not match.
        if os.path.exists(archive):
            os.remove(archive)
        with open(source, "rb") as src, open(destination, "wb") as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
        if not compress(os.path.join(batch.EDITOR, "System"), out_folder, filename):
            print("      %-24s COMPRESS FAILED" % filename)
            return False
        if not os.path.isfile(archive) or os.path.getsize(archive) == 0:
            print("      %-24s no .uz2 produced" % filename)
            return False
        raw, packed = os.path.getsize(destination), os.path.getsize(archive)
        print("      %-24s %9s -> %9s  (%d%%)"
              % (filename, size(raw), size(packed), round(100.0 * packed / raw)))
    return True


def size(count):
    """Byte count as a short human-readable string."""
    scaled = float(count)
    for unit in ("B", "K", "M", "G"):
        if scaled < 1024 or unit == "G":
            if unit == "B":
                return "%dB" % count
            return "%.1f%s" % (scaled, unit)
        scaled /= 1024.0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--match", help="only maps whose source name contains this")
    parser.add_argument("--list", action="store_true", help="show what would be packaged")
    parser.add_argument("--folder", default=DEFAULT_FOLDER,
                        help="output folder under the editor install (default: %s)"
                             % DEFAULT_FOLDER)
    parser.add_argument("--map-only", action="store_true",
                        help="ship the .ut2 alone, leaving the .utx already in "
                             "the folder -- for a change that moved only the t3d")
    parser.add_argument("--force", action="store_true",
                        help="package a .ut2 that is older than its .t3d")
    args = parser.parse_args()

    entries = batch.discover()
    if args.match:
        entries = [e for e in entries if args.match.lower() in e[1].lower()]
    if not entries:
        print("nothing matched")
        return 1

    if args.list:
        for _path, base, name, tex in entries:
            print("  %-15s -> %s.ut2 + %s.utx" % (base, name, tex))
        return 0

    print("packaging into %s/%s" % (batch.EDITOR, args.folder))
    done = [package(entry, args.folder, args.force, args.map_only)
            for entry in entries]
    print("\n%d of %d packaged" % (sum(done), len(done)))
    return 0 if all(done) else 1


if __name__ == "__main__":
    sys.exit(main())
