// The shape of a converter option. The table itself is generated from the
// scripts' argparse definitions -- see tools/gen_spec.py and spec_generated.h.
#pragma once

namespace spec {

// What the option is, which decides both the widget and how it is parsed.
enum class Kind {
    Flag,     // a bare --no-something, on or off
    Text,     // free text: a package name, a folder
    Int,
    Float,
    Choice,   // one of a fixed set, e.g. --sky-mode
};

// Whether the field gets a picker beside it, and what that picker picks.
enum class Browse {
    None,
    OpenPackage,   // an existing .ut3
    SavePath,      // the .t3d to write
    Folder,
};

struct Opt {
    const char* dest;        // argparse dest, used as the settings key
    const char* flag;        // "--light-gain", or "" for a positional
    const char* label;       // what the interface shows
    Kind kind;
    const char* def;         // the default, as text ("true"/"false" for Flag)
    const char* help;        // argparse help, shown on hover
    bool required;
    // A positional taking nargs="*" or "+": one field, split on whitespace, so
    // repoint_package.py can be given a .ut2 and its .utx in one box.
    bool variadic;
    Browse browse;
    const char* choices[8];  // Choice only, null-terminated by zero-init
};

// A titled box of consecutive options: [begin, end) into the option table.
struct Section {
    const char* title;
    int begin;
    int end;
};

// One inspect subcommand and its options.
struct Command {
    const char* name;
    const Opt* options;
    int count;
};

}  // namespace spec
