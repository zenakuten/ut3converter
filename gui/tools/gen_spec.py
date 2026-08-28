#!/usr/bin/env python3
"""Turn ut3conv.py's and batch.py's argparse definitions into a C++ table.

    ./tools/gen_spec.py            # rewrites src/spec_generated.h

Run it after adding or changing a flag in either script; the header it writes
is committed, so building the GUI needs no Python. Everything the interface
shows about an option -- its flag, type, default, choices and the tooltip text
-- comes from here, which is the only way fifty-odd flags stay honest.

SECTIONS below is the one thing kept by hand, and only decides which box a
known flag is drawn in. A flag missing from it lands in "Other" and the script
says so, rather than the option quietly disappearing.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONVERTER = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, CONVERTER)

import ut3conv                                                     # noqa: E402

OUTPUT = os.path.join(os.path.dirname(HERE), "src", "spec_generated.h")

# Which section each t3d flag is drawn in, in order. Grouping only -- the
# widget, default and tooltip all come from argparse.
SECTIONS = [
    ("Package and scale", ["texture_package", "textures", "max_texture_size",
                           "scale", "surface_scale", "no_package",
                           "no_materials", "all_textures"]),
    ("Leave out", ["no_meshes", "no_terrain", "no_movers", "no_sounds",
                   "no_lights", "no_pickups", "no_paths", "no_player_starts",
                   "no_objectives", "no_teleporters", "no_teleporter_effect",
                   "no_onslaught", "no_volumes", "no_skybox", "no_minimap",
                   "no_zone_info", "keep_effect_meshes"]),
    ("Lighting", ["light_gain", "ambient", "ambient_gain",
                  "light_radius_scale"]),
    ("Sky", ["sky_mode", "sky_dome_margin", "sky_merge_distance",
             "shrink_backdrop", "keep_backdrop"]),
    ("World brush", ["world_margin", "world_cell", "no_world_brush"]),
    ("Terrain", ["deco_density", "terrain_layer_scale"]),
    ("Movers and sound", ["mover_keys", "sound_gain"]),
    ("Warfare / Onslaught", ["onslaught_specials", "countdown_time",
                             "countdown_damage", "node_rise", "vehicle_rise"]),
    ("Minimap", ["minimap_size"]),
]

# The two fields that open a file or folder picker, and what they pick.
BROWSE = {
    ("t3d", "package"): "OpenPackage",
    ("t3d", "output"): "SavePath",
    ("t3d", "textures"): "Folder",
    ("textures", "package"): "OpenPackage",
    ("textures", "output"): "Folder",
    ("info", "package"): "OpenPackage",
    ("classes", "package"): "OpenPackage",
    ("list", "package"): "OpenPackage",
    ("props", "package"): "OpenPackage",
    ("imports", "package"): "OpenPackage",
}

INSPECT = ["info", "classes", "list", "props", "imports"]


def subparsers(parser):
    for action in parser._actions:
        if isinstance(action.choices, dict):
            return action.choices
    raise SystemExit("ut3conv.py has no subcommands any more")


def real_actions(parser):
    out = []
    for action in parser._actions:
        if "-h" in action.option_strings or action.dest in ("help", "func", "command"):
            continue
        out.append(action)
    return out


def kind_of(action):
    if action.__class__.__name__ in ("_StoreTrueAction", "_StoreFalseAction"):
        return "Flag"
    if action.choices:
        return "Choice"
    if action.type is int:
        return "Int"
    if action.type is float:
        return "Float"
    return "Text"


def flag_of(action):
    if not action.option_strings:
        return ""
    return max(action.option_strings, key=len)


def label_of(action):
    if action.option_strings:
        return flag_of(action).lstrip("-")
    return action.dest


# This machine's install root must not end up in a committed header, so the
# defaults that are a local absolute path go out as a token the app resolves
# against wherever it finds the converter.
INSTALL_ROOT_TOKEN = "<install-root>"

# The options whose argparse default is install_root(). Named rather than
# detected by value: install_root() returns None outside a UT2004 tree, so a
# standalone checkout would otherwise generate an empty default here and
# quietly lose the token.
INSTALL_ROOT_DEFAULTS = {("t3d", "textures"), ("textures", "output")}


def default_of(command, action, kind):
    if kind == "Flag":
        return cstring("true" if action.default else "false")
    if (command, action.dest) in INSTALL_ROOT_DEFAULTS:
        return cstring(INSTALL_ROOT_TOKEN)
    if action.default is None:
        return '""'
    return cstring(str(action.default))


def cstring(text):
    out = ['"']
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif 32 <= ord(ch) < 127:
            out.append(ch)
        else:
            # The help text is ASCII today; anything else goes out as an
            # escape rather than risking the compiler's source encoding.
            out.append("\\%03o" % ord(ch))
    out.append('"')
    return "".join(out)


# argparse gives the positionals no help, and a required field with no
# tooltip is the one place the interface most needs to explain itself.
HELP_FALLBACK = {
    "package": "the UT3 package to read, normally a .ut3 under "
               "UTGame/CookedPC/Maps",
    "object": "which export to dump: an index, a name like "
              "StaticMeshActor_347, or a dotted path",
}


def tidy(action):
    text = " ".join((action.help or "").split())
    return text or HELP_FALLBACK.get(action.dest, "")


def emit_option(command, action, out):
    kind = kind_of(action)
    choices = []
    if kind == "Choice":
        choices = [str(c) for c in action.choices]
    out.append("    { %s, %s, %s, Kind::%s, %s, %s, %s, %s, { %s } }," % (
        cstring(action.dest),
        cstring(flag_of(action)),
        cstring(label_of(action)),
        kind,
        default_of(command, action, kind),
        cstring(tidy(action)),
        "true" if action.required else "false",
        "Browse::" + BROWSE.get((command, action.dest), "None"),
        ", ".join(cstring(c) for c in choices),
    ))


def batch_actions():
    import batch
    return real_actions(batch.build_parser())


def main():
    parser = ut3conv.build_parser()
    commands = subparsers(parser)

    lines = [
        "// Generated by tools/gen_spec.py -- do not edit.",
        "//",
        "// Every flag, default and tooltip below is read straight out of",
        "// ut3conv.py's and batch.py's argparse definitions. Re-run the",
        "// generator after changing a flag in either script.",
        "#pragma once",
        "",
        '#include "spec.h"',
        "",
        "namespace spec {",
        "",
    ]

    # t3d
    t3d = {a.dest: a for a in real_actions(commands["t3d"])}
    lines.append("inline const Opt kT3dOptions[] = {")
    for dest in ("package", "output"):
        emit_option("t3d", t3d[dest], lines)
    placed = {"package", "output"}
    for _title, dests in SECTIONS:
        for dest in dests:
            if dest not in t3d:
                raise SystemExit("SECTIONS names a flag ut3conv.py no longer has: " + dest)
            emit_option("t3d", t3d[dest], lines)
            placed.add(dest)
    leftover = [d for d in t3d if d not in placed]
    for dest in leftover:
        emit_option("t3d", t3d[dest], lines)
    lines.append("};")
    lines.append("")

    # Section boundaries, as index ranges into the table above.
    lines.append("inline const Section kT3dSections[] = {")
    lines.append('    { "Required", 0, 2 },')
    start = 2
    for title, dests in SECTIONS:
        lines.append('    { %s, %d, %d },' % (cstring(title), start, start + len(dests)))
        start += len(dests)
    if leftover:
        print("note: %d flag(s) not in SECTIONS, drawn under Other: %s"
              % (len(leftover), ", ".join(sorted(leftover))))
        lines.append('    { "Other", %d, %d },' % (start, start + len(leftover)))
    lines.append("};")
    lines.append("")

    # The inspect subcommands, each its own small table.
    for command in INSPECT:
        actions = real_actions(commands[command])
        lines.append("inline const Opt k%sOptions[] = {" % command.capitalize())
        for action in actions:
            emit_option(command, action, lines)
        lines.append("};")
        lines.append("")

    lines.append("inline const Opt kBatchOptions[] = {")
    for action in batch_actions():
        emit_option("batch", action, lines)
    lines.append("};")
    lines.append("")

    lines.append("inline const Command kInspectCommands[] = {")
    for command in INSPECT:
        actions = real_actions(commands[command])
        lines.append('    { %s, k%sOptions, %d },'
                     % (cstring(command), command.capitalize(), len(actions)))
    lines.append("};")
    lines.append("")

    lines.append("inline constexpr int kT3dOptionCount = %d;" % len(t3d))
    lines.append("inline constexpr int kT3dSectionCount = %d;"
                 % (1 + len(SECTIONS) + (1 if leftover else 0)))
    lines.append("inline constexpr int kBatchOptionCount = %d;" % len(batch_actions()))
    lines.append("inline constexpr int kInspectCommandCount = %d;" % len(INSPECT))
    lines.append("")
    lines.append("}  // namespace spec")
    lines.append("")

    with open(OUTPUT, "w", encoding="ascii", newline="\n") as handle:
        handle.write("\n".join(lines))
    print("wrote %s: %d t3d options, %d batch options, %d inspect commands"
          % (os.path.relpath(OUTPUT, os.path.dirname(HERE)), len(t3d),
             len(batch_actions()), len(INSPECT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
