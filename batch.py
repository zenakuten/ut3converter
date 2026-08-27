#!/usr/bin/env python3
"""Convert every UT3 (and UDK) map, and lay the results out the way the build
expects.

    ./batch.py --list                 what would be converted
    ./batch.py --match VCTF           convert those maps
    ./batch.py --match VCTF --build   ...and build each one as it goes
    ./batch.py --build                the lot

**One package per build, always.** `UCC.exe` is a 32-bit binary (PE32), so a
`ucc make` has around 2GB of address space -- and that has to cover every
package EditPackages names, not just the ones being compiled. UCC skips a
package whose .u already exists but still *loads* it to resolve references, so
once a few maps are built the next build runs out of room just opening them.
Seven packages totalling 434MB build today; all 62 would come to roughly 3.7GB.

So each map is converted, EditPackages is rewritten to name that map's package
and nothing else, its .u is deleted so UCC will actually rebuild it, `ucc make`
runs, and the result is copied out. Then the next map. The map packages need no
dependencies of their own -- a generated .uc is `class <Pkg> extends Object` and
a list of #exec imports, referencing no other script -- so naming one at a time
costs nothing. Anything else already in EditPackages (OnslaughtSpecials2, the
user's own mods) is left exactly where it is and skipped by UCC, since those
.u files are present.

Where things go, matching the workflow already in use:

    ut3converter/out-<map>/<Name>.t3d   the map, per map, kept for reference
    /data/dev/UT2004/<Pkg>Tex/          the package source the build compiles
    /data/dev/UT2004/System/<Pkg>Tex.u  what `ucc make` produces
    ~/UT2004_p23win/Textures/<Pkg>.utx  the copy the editor loads
    ~/UT2004_p23win/Converted/          the t3d files, ready to import

The .utx extension is deliberate: these are map assets rather than code, and
keeping them out of the editor's System folder keeps that folder honest.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_ROOT = os.path.dirname(HERE)                      # /data/dev/UT2004
SYSTEM = os.path.join(BUILD_ROOT, "System")
# Overridable from the environment so ut3convgui.py (and a Windows install,
# where none of these defaults exist) can drive this without editing the file.
EDITOR = os.environ.get("UT3CONV_EDITOR") or os.path.expanduser("~/UT2004_p23win")
UT3_ROOT = os.environ.get("UT3CONV_UT3_ROOT") or (
    "/home/josh/.local/share/Steam/steamapps/common/"
    "Unreal Tournament 3/UTGame/CookedPC")
# UDK maps ship uncooked beside the .upk packages they reference, so the tree to
# scan is the game's Maps folder rather than a cooked one. TOXIKK is the UDK
# game to hand; set UT3CONV_UDK_ROOT for another, or to "" to skip UDK entirely.
# ~/TOXIKK first: that is the working copy (the Steam install with the UDK zip
# over it -- see "Running the UDK editor under Wine" in PLAN.md), and its maps
# are not the same files. BL-Dekk.udk differs between the two.
UDK_ROOT = os.environ.get("UT3CONV_UDK_ROOT")
if UDK_ROOT is None:
    for candidate in ("~/TOXIKK/UDKGame/Content/Maps",
                      "~/.steam/steam/steamapps/common/TOXIKK/UDKGame/Content/Maps"):
        UDK_ROOT = os.path.expanduser(candidate)
        if os.path.isdir(UDK_ROOT):
            break
# UDK's Engine/Content, which the game does not ship: BL-Foundation places 118
# EditorMeshes.TexPropPlane and 9 EngineMeshes.Sphere, and every UDK map uses
# EngineVolumetrics for its light beams. Unpacking the UDK zip is enough --
# it does not have to be installed over the game, and doing that stops TOXIKK
# launching. The converter reads it through UT3CONV_EXTRA_CONTENT.
UDK_ENGINE = os.environ.get("UT3CONV_UDK_ENGINE")
if UDK_ENGINE is None:
    for candidate in ("~/TOXIKK/Engine/Content", "~/TOXIKK_UDK/Engine/Content"):
        UDK_ENGINE = os.path.expanduser(candidate)
        if os.path.isdir(UDK_ENGINE):
            break
if os.path.isdir(UDK_ENGINE) and "UT3CONV_EXTRA_CONTENT" not in os.environ:
    os.environ["UT3CONV_EXTRA_CONTENT"] = UDK_ENGINE

# Angels Fall First, a fourth UE3 game (UDK 872, LZO). Its maps are persistent
# levels that stream their content in from CookedPC/Submaps, which the
# converter merges -- see convert/sublevels.py. Only CookedPC/Maps is scanned,
# so a sub-level is never mistaken for a map of its own.
AFF_ROOT = os.environ.get("UT3CONV_AFF_ROOT")
if AFF_ROOT is None:
    AFF_ROOT = os.path.expanduser(
        "~/.steam/steam/steamapps/common/Angels Fall First/AFFGame/CookedPC/Maps")

# Gears of War Reloaded, a third UE3 dialect (version 835, licensee 76, LZ4).
# Its maps are cooked, so unlike UDK they carry their assets and need no
# separate content root.
GEARS_ROOT = os.environ.get("UT3CONV_GEARS_ROOT")
if GEARS_ROOT is None:
    GEARS_ROOT = os.path.expanduser(
        "~/.steam/steam/steamapps/common/Gears of War Reloaded/WarGame/CookedPC/"
        "Maps/MP_Maps")

# Not maps: the front end, the intro cinematic and the tutorials.
SKIP_PREFIXES = ("AFFEntry", "AFFFrontEndMap",
                 "UTFrontEnd", "EnvyEntry", "UTM-", "UTCin",
                 # TOXIKK's front end and the UDK template map.
                 "CRZMainMenu", "ToxikkEntry", "ExampleEntry", "BL-WorkshopMap")

# UDK's own sample maps, which its installer drops into the game's Content tree
# and which are not the game's maps at all. `Maps/UT3/DM-Deck.udk` is a UDK-era
# sample of the map we already convert from the real UT3 install, and it lands
# on the same output name and the same package -- the second run silently
# clobbering the first, and then failing to build against the other's t3d.
SKIP_MAP_DIRS = ("UT3", "Examples", "Mobile", "Showcases", "First")

# UT3 ships campaign variants of some maps beside the versal ones -- _SP is the
# single-player cut, _Necris and _Leviathan are the story reskins. They are
# near-duplicates (VCTF-Containment_SP differs from VCTF-Containment by two
# brushes) and each costs a full package, so they are skipped unless asked for.
VARIANT_SUFFIXES = ("_SP", "_Necris", "_Leviathan")

# Gears splits a map across several .war files: MP_Courtyard.war is the level,
# and MP_Courtyard_Audio, _VFX and _Screenshots sit beside it as streaming
# sub-levels. Only the base file is a map. The _LOC_* files are localised
# variants of one level and _2v2 and _Stitching alternate cuts -- those convert
# on their own, so they stay listed, but a sub-level would come out an empty
# shell.
#
# Nothing here merges a sub-level into its parent. MP_Courtyard keeps its 118
# lights and all 3,362 meshes in the base file, so it loses nothing; a map
# whose lighting lives in a _Lighting file (MP_Depot, MP_Escalation) would.
GEARS_SUBLEVEL_SUFFIXES = ("_Audio", "_Screenshots", "_VFX", "_Lighting",
                           "_Static_Lighting", "_S01", "_S02")

# UT3's Warfare is UT2004's Onslaught, and the map prefix has to say so or the
# game will not list it.
# TOXIKK's BloodLust is deathmatch under another name, and a map whose prefix
# UT2004 does not know is listed under no game type at all.
# TOXIKK's own prefixes are its game modes: BL is Bloodlust (deathmatch) and CC
# is Cell Capture, a team objective mode. Neither mode's objectives convert, so
# both land on DM- -- which is not just a fallback: a CC map's team-assigned
# PlayerStarts are exactly what UT2004's Team Deathmatch wants, and a map left
# under an unknown prefix appears in no gametype's list at all.
# AFF- is Angels Fall First's own prefix and means nothing to UT2004 either.
# Its maps are large team-objective levels; nothing converts the objectives, so
# they land on DM- like TOXIKK's CC maps and keep their team-assigned starts.
PREFIX_MAP = {"WAR-": "ONS-", "BL-": "DM-", "CC-": "DM-", "MP_": "DM-",
              "AFF-": "DM-"}

# Where a converted name would land on a map that already exists. Only Torlan
# does: UT2004 ships its own ONS-Torlan, and both would claim the same file.
RENAMES = {"WAR-Torlan": ("ONS-TorlanUT3", "TorlanUT3Tex")}

# Flags a map needs beyond the defaults, established by converting it.
EXTRA_FLAGS = {
    # One dim SkyLight, so the ambient floor alone leaves it too dark.
    "WAR-Torlan": ["--ambient-gain", "128"],
    # No SkyLight at all -- TOXIKK lights this one with baked lightmaps and 364
    # Lightmass area lights, none of which convert, so there is nothing for
    # --ambient-gain to scale and the zone would get no ambient whatsoever.
    "BL-Dekk": ["--ambient", "64"],
    # Lit by one DirectionalLight and 48 baked lightmaps -- not a single point
    # or spot light in the map -- so without an ambient the only thing lighting
    # a converted BL-Cube is its Sunlight.
    "BL-Cube": ["--ambient", "96"],
    # 547 placed lights, eight times Dekk's, so most of its lighting survives
    # conversion and it needs only a little fill for what the lightmaps did.
    "BL-Foundation": ["--ambient", "32"],
    # No SkyLight either, and 84 placed lights -- close to Dekk's 67, so the
    # same fill. The one Sunlight it has over Dekk lights the open ground and
    # does nothing for the interiors, which is where the darkness was.
    "BL-Artifact": ["--ambient", "64"],
    # 1297 placed lights, more than any other converted map, so most of its
    # lighting survives and it needs no more fill than Foundation's.
    "CC-Citadel": ["--ambient", "32"],
    # Twelve lights, nine of which the mapper set to zero brightness -- so three
    # live ones and a Sunlight for a daylight jungle map, and everything else is
    # baked. As little placed lighting as BL-Cube has.
    "BL-Ganesha": ["--ambient", "96"],
    # Gears keeps its lights in a StaticLightCollectionActor: 118 of them, 93
    # converting and 87 of those spotlights, so most of the map's own lighting
    # survives. No SkyLight, though, so it still needs a floor -- a starting
    # value, not a measured one; this map has not been walked yet.
    "MP_Courtyard": ["--ambient", "32"],
}

# Warfare maps carry countdown nodes and standalone nodes that only the
# OnslaughtSpecials2 classes can express, and the mod is installed here.
ONSLAUGHT_FLAGS = ["--onslaught-specials"]


def out_name(base):
    """The UT2004 map name for a UT3 one."""
    if base in RENAMES:
        return RENAMES[base][0]
    for prefix, replacement in PREFIX_MAP.items():
        if base.startswith(prefix):
            return replacement + base[len(prefix):]
    return base


def package_name(base):
    """The texture package name, asked of the converter rather than guessed.

    Reimplementing this is how VCTF-Containment_SP broke: the converter keeps
    underscores (`re.sub(r"[^A-Za-z0-9_]", "", base)`) and produced
    VCTFContainment_SPTex, while a copy of the rule here dropped them and went
    looking for VCTFContainmentSPTex. One source of truth instead.
    """
    if base in RENAMES:
        return RENAMES[base][1]
    from ut3conv import _texture_package_name

    # The extension is stripped either way; passing one keeps the call honest.
    return _texture_package_name(base + ".ut3")


def discover(variants=False):
    """Every convertible map, as (source, source name, out name, package)."""
    found = []
    sources = [(UT3_ROOT, "*.ut3")]
    if UDK_ROOT:
        sources.append((UDK_ROOT, "*.udk"))
    if GEARS_ROOT:
        sources.append((GEARS_ROOT, "*.war"))
    if AFF_ROOT:
        sources.append((AFF_ROOT, "*.udk"))
    paths = []
    for root, pattern in sources:
        if root and os.path.isdir(root):
            paths += sorted(glob.glob(os.path.join(root, "**", pattern),
                                      recursive=True))
    for path in paths:
        base = os.path.basename(path).rsplit(".", 1)[0]
        if any(base == s or base.startswith(s) for s in SKIP_PREFIXES):
            continue
        if not variants and base.endswith(VARIANT_SUFFIXES):
            continue
        if base.endswith(GEARS_SUBLEVEL_SUFFIXES):
            continue
        if os.path.basename(os.path.dirname(path)) in SKIP_MAP_DIRS:
            continue
        found.append((path, base, out_name(base), package_name(base)))
    # Two maps that want the same package would overwrite each other's source
    # tree and then fail to build, which is a confusing way to find out.
    claimed = {}
    for path, base, out, package in found:
        if package in claimed and claimed[package] != path:
            print("  WARNING: %s and %s both want package %s; keeping the first"
                  % (os.path.basename(claimed[package]), os.path.basename(path), package))
        claimed.setdefault(package, path)
    found = [entry for entry in found if claimed[entry[3]] == entry[0]]
    # By the name the map ends up with, so a build group is a coherent set
    # rather than whatever order the two Maps folders happened to glob in.
    found.sort(key=lambda entry: entry[2].lower())
    return found


def flags_for(base):
    flags = list(EXTRA_FLAGS.get(base, []))
    if base.startswith("WAR-"):
        flags += ONSLAUGHT_FLAGS
    if base in RENAMES:
        flags += ["--texture-package", RENAMES[base][1]]
    return flags


def convert(entry, dry_run=False, extra=()):
    path, base, name, package = entry
    folder = os.path.join(HERE, "out-" + name.lower())
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, name + ".t3d")
    # Last wins in argparse, so anything passed with --flag overrides the
    # per-map entry in EXTRA_FLAGS rather than fighting it.
    command = [sys.executable, os.path.join(HERE, "ut3conv.py"), "t3d", path,
               "-o", target] + flags_for(base) + list(extra)
    if dry_run:
        print("      " + " ".join(command[2:]))
        return None
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print("  FAILED %s" % base)
        print(result.stdout[-2000:])
        print(result.stderr[-2000:])
        return None
    return target


def copy_t3d(target):
    """Put the map where the editor can reach it."""
    destination = os.path.join(EDITOR, "Converted")
    os.makedirs(destination, exist_ok=True)
    shutil.copy2(target, os.path.join(destination, os.path.basename(target)))
    return destination


START = ";--- ut3converter batch start"
END = ";--- ut3converter batch end"


def set_edit_package(package):
    """Point EditPackages at exactly one map package, leaving the rest alone.

    Everything this script manages lives between its own markers, so the user's
    own entries are never rewritten. On the first run any map packages already
    listed outside the block are commented out and adopted -- otherwise every
    previously built map would still be loaded on every build, which is the
    whole thing being avoided.
    """
    ini = os.path.join(SYSTEM, "UT2004.ini")
    with open(ini, "rb") as handle:
        text = handle.read().decode("latin-1")

    if START in text and END in text:
        head, rest = text.split(START, 1)
        # Take the newline that ends the block with it. `head` already carries
        # the one before START, so leaving this behind glues two together and
        # the file grows a blank line on every single build.
        _old, tail = (rest.split(END + "\n", 1) if END + "\n" in rest
                      else rest.split(END, 1))
    else:
        at = text.rfind("EditPackages=")
        at = text.find("\n", at) + 1
        head, tail = text[:at], text[at:]

    adopted = []
    lines = []
    for line in (head + tail).splitlines(True):
        stripped = line.strip()
        if stripped.startswith("EditPackages=") and stripped.endswith("Tex"):
            adopted.append(stripped.split("=", 1)[1])
            lines.append(";" + line.lstrip() if not line.startswith(";") else line)
        else:
            lines.append(line)
    rebuilt = "".join(lines)

    block = "%s\nEditPackages=%s\n%s\n" % (START, package, END)
    if START in rebuilt:
        before, rest = rebuilt.split(START, 1)
        _old, after = rest.split(END + "\n", 1) if END + "\n" in rest else rest.split(END, 1)
        rebuilt = before + block + after
    else:
        at = rebuilt.rfind("EditPackages=")
        at = rebuilt.find("\n", at) + 1
        rebuilt = rebuilt[:at] + block + rebuilt[at:]

    with open(ini, "wb") as handle:
        handle.write(rebuilt.encode("latin-1", "replace"))
    return adopted


def clear_edit_package():
    """Empty the managed block, leaving EditPackages as the user keeps it.

    Worth doing at the end of a run rather than leaving the last map behind.
    The hand-written `makeit` builds the user's own mods against this same ini,
    and a leftover map package would be loaded on every one of those builds --
    65MB of textures for nothing, in a 32-bit process that has little room to
    spare. Adopted map packages stay commented out, since they are managed here
    now and rebuilding them is this script's job.
    """
    ini = os.path.join(SYSTEM, "UT2004.ini")
    with open(ini, "rb") as handle:
        text = handle.read().decode("latin-1")
    if START not in text or END not in text:
        return False
    head, rest = text.split(START, 1)
    _old, tail = (rest.split(END + "\n", 1) if END + "\n" in rest
                  else rest.split(END, 1))
    with open(ini, "wb") as handle:
        handle.write((head + START + "\n" + END + "\n" + tail)
                     .encode("latin-1", "replace"))
    return True


def build(package):
    """Delete the package's .u, run ucc make, and copy the result out.

    The delete is not optional: `ucc make` treats an existing .u as done and
    silently compiles nothing, which is why the hand-written makeit removes it
    first.
    """
    built = os.path.join(SYSTEM, package + ".u")
    if os.path.exists(built):
        os.remove(built)
    result = subprocess.run(["./UCC.exe", "make"], cwd=SYSTEM,
                            capture_output=True, text=True)
    if not os.path.exists(built):
        print("  BUILD FAILED %s" % package)
        for line in (result.stdout or "").splitlines()[-15:]:
            print("      " + line)
        for line in (result.stderr or "").splitlines()[-10:]:
            print("      " + line)
        return None
    destination = os.path.join(EDITOR, "Textures", package + ".utx")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copy2(built, destination)
    return destination


def build_parser():
    """The command line, split out so gui/tools/gen_spec.py can read the flags
    back rather than re-stating them."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="show what would run and stop")
    parser.add_argument("--match", help="only maps whose name contains this")
    parser.add_argument("--variants", action="store_true",
                        help="also convert UT3's campaign variants (_SP, _Necris, "
                             "_Leviathan), which are near-duplicates of the "
                             "versal maps and cost a package each")
    parser.add_argument("--build", action="store_true",
                        help="also run ucc make for each map, one package at a time")
    parser.add_argument("--dry-run", action="store_true",
                        help="print each step without doing any of it")
    parser.add_argument("--skip-converted", action="store_true",
                        help="skip maps whose t3d is already in the editor folder")
    parser.add_argument("--flag", action="append", default=[], metavar="ARGS",
                        help="extra ut3conv flags for every map, split on spaces "
                             "and appended after the per-map ones, so they win. "
                             "Repeatable. Example: --flag '--max-texture-size 2048'")
    return parser


def main():
    args = build_parser().parse_args()

    entries = discover(args.variants)
    if args.match:
        entries = [e for e in entries if args.match.lower() in e[1].lower()]
    if args.skip_converted:
        entries = [e for e in entries
                   if not os.path.exists(os.path.join(EDITOR, "Converted", e[2] + ".t3d"))]

    if args.list:
        print("%d map(s)%s\n" % (len(entries), ", building one at a time" if args.build else ""))
        for _path, base, name, package in entries:
            print("    %-28s -> %-24s %-22s %s"
                  % (base, name, package, " ".join(flags_for(base))))
        return 0

    try:
        failed = run(entries, args)
    finally:
        # Even on Ctrl-C: the ini is shared with the user's own builds.
        if args.build and not args.dry_run and clear_edit_package():
            print("EditPackages restored to its own entries")

    print("\ndone: %d map(s), %d failed%s"
          % (len(entries) - len(failed), len(failed),
             (": " + ", ".join(failed)) if failed else ""))
    print("t3d files are in %s/Converted" % EDITOR)
    if not args.build:
        print("nothing was built -- re-run with --build")
    return 1 if failed else 0


def run(entries, args):
    failed = []
    extra = [token for chunk in getattr(args, "flag", []) or [] for token in chunk.split()]
    if extra:
        print("extra flags for every map: %s" % " ".join(extra))
    for number, entry in enumerate(entries, 1):
        _path, base, name, package = entry
        print("[%d/%d] %s -> %s (%s)" % (number, len(entries), base, name, package))
        if args.dry_run:
            convert(entry, True, extra)
            if args.build:
                print("      EditPackages=%s ; rm %s.u ; ./UCC.exe make ; cp -> %s.utx"
                      % (package, package, package))
            continue

        target = convert(entry, extra=extra)
        if not target:
            failed.append(base)
            continue
        copy_t3d(target)

        if args.build:
            adopted = set_edit_package(package)
            if adopted:
                print("      took over EditPackages for: %s" % ", ".join(adopted))
            if build(package) is None:
                failed.append(base)
            else:
                print("      built and copied to %s/Textures/%s.utx" % (EDITOR, package))
    return failed


if __name__ == "__main__":
    sys.exit(main())
