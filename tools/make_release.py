#!/usr/bin/env python3
"""Build the distributable zips for the GUI and the converter behind it.

    ./tools/make_release.py v3                  both platforms
    ./tools/make_release.py v3 --only linux

What ships is the interface, the scripts it drives, and nothing else: no tests,
no gui/ sources, no build tree. The layout is one folder inside the zip named
for the archive, so unzipping anywhere leaves a self-contained directory.

Two things this exists to stop happening again.

`__pycache__` is excluded at *copy* time rather than cleaned beforehand. The
first v2 Linux zip shipped 50 .pyc files because the smoke test that proved the
scripts imported regenerated them after the clean, and the only reason it was
caught was diffing the file list against v1.

And `tools/` ships. The Repoint tab runs `tools/repoint_package.py` and the
interface looks for it under the folder the user points at, so a release without
it offers a tab that cannot work.

The Windows zip carries four DLLs. SDL3 and libssp come out of the cross-build;
lzo2 and lz4 are the codecs `ut3/package.py` loads through ctypes to decompress
UDK and Gears packages, and Windows looks for them beside python.exe rather than
beside the script -- which is why they are shipped rather than assumed. See
gui/README.md.
"""

import argparse
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Everything a released tree needs, relative to the converter root.
SCRIPTS = ["ut3conv.py", "batch.py"]
PACKAGES = ["convert", "ut2", "ut3", "tools"]
DOCS = [("README.md", "README.md"), ("gui/README.md", "README-gui.md")]

WINDOWS_DLLS = [
    "gui/build-win/SDL3.dll",
    "gui/build-win/libssp-0.dll",
    "/usr/x86_64-w64-mingw32/bin/liblzo2-2.dll",
    "/usr/x86_64-w64-mingw32/bin/liblz4.dll",
]

BINARY = {"linux": ("gui/build/ut3convgui", "ut3convgui"),
          "windows": ("gui/build-win/ut3convgui.exe", "ut3convgui.exe")}


def copy_tree(source, target):
    """Copy a package, leaving bytecode and anything hidden behind."""
    for base, dirs, files in os.walk(source):
        dirs[:] = [d for d in sorted(dirs)
                   if d != "__pycache__" and not d.startswith(".")]
        rel = os.path.relpath(base, source)
        out = target if rel == "." else os.path.join(target, rel)
        os.makedirs(out, exist_ok=True)
        for name in sorted(files):
            if name.endswith(".pyc") or name.startswith("."):
                continue
            shutil.copy2(os.path.join(base, name), os.path.join(out, name))


def stage(platform, version, into):
    name = "ut3convgui-%s-%s" % (platform, version)
    tree = os.path.join(into, name)
    if os.path.isdir(tree):
        shutil.rmtree(tree)
    os.makedirs(tree)

    source, leaf = BINARY[platform]
    binary = os.path.join(ROOT, source)
    if not os.path.isfile(binary):
        raise SystemExit("no %s -- build it first (see gui/README.md)" % source)
    shutil.copy2(binary, os.path.join(tree, leaf))
    os.chmod(os.path.join(tree, leaf), 0o755)

    if platform == "windows":
        for dll in WINDOWS_DLLS:
            path = dll if os.path.isabs(dll) else os.path.join(ROOT, dll)
            if not os.path.isfile(path):
                raise SystemExit("no %s -- see gui/README.md for where it comes "
                                 "from" % dll)
            shutil.copy2(path, os.path.join(tree, os.path.basename(path)))

    for source, leaf in DOCS:
        shutil.copy2(os.path.join(ROOT, source), os.path.join(tree, leaf))
    for script in SCRIPTS:
        shutil.copy2(os.path.join(ROOT, script), os.path.join(tree, script))
    for package in PACKAGES:
        copy_tree(os.path.join(ROOT, package), os.path.join(tree, package))
    return name, tree


def zip_tree(name, tree, into):
    archive = os.path.join(into, name + ".zip")
    if os.path.exists(archive):
        os.remove(archive)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
        for base, dirs, files in os.walk(tree):
            dirs[:] = sorted(dirs)
            for leaf in sorted(files):
                path = os.path.join(base, leaf)
                out.write(path, os.path.join(
                    name, os.path.relpath(path, tree)))
    return archive


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="the version to stamp, e.g. v3")
    parser.add_argument("--only", choices=("linux", "windows"),
                        help="just this platform")
    parser.add_argument("--out", default=os.path.join(ROOT, "releases"),
                        help="where the zips go (default: releases/)")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    platforms = [args.only] if args.only else ["linux", "windows"]
    for platform in platforms:
        name, tree = stage(platform, args.version, args.out)
        archive = zip_tree(name, tree, args.out)
        shutil.rmtree(tree)
        with zipfile.ZipFile(archive) as check:
            entries = check.namelist()
            bad = [e for e in entries if "__pycache__" in e or e.endswith(".pyc")]
            if bad:
                raise SystemExit("%s shipped bytecode: %s" % (archive, bad[:3]))
            if not any(e.endswith("tools/repoint_package.py") for e in entries):
                raise SystemExit("%s has no tools/repoint_package.py, which the "
                                 "Repoint tab runs" % archive)
        print("%-40s %d files, %.1f MB"
              % (os.path.basename(archive), len(entries),
                 os.path.getsize(archive) / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
