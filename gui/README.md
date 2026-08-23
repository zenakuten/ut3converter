# ut3convgui

A window over [`ut3conv.py`](../ut3conv.py) and [`batch.py`](../batch.py), for
driving a UT3-to-UT2004 conversion without assembling a fifty-flag command
line by hand.

Nothing is converted in this process. Run spawns the same command the
converter's README documents — shown in full above the log, and copyable — and
streams its output back. So anything you work out here transfers straight to a
terminal, and vice versa.

SDL3 and Dear ImGui, the same stack as
[utcolor](https://github.com/zenakuten/utcolor): one binary, no runtime
assets, same build on Windows and Linux.

## The tabs

**Convert** is `ut3conv.py t3d` — one map to a `.t3d` plus a buildable asset
package. The two required fields sit at the top; the rest are grouped in
sections that start shut. A section holding a non-default value says so in its
header (`Lighting (1 set)`) and opens itself, so nothing left over from last
time can change the conversion from behind a collapsed header. Hovering any
label or field gives you that flag's `--help` text.

**Batch** is `batch.py` — every UT3 map, one package per `ucc make`. Start with
`list` to see what would run. `editor` and `ut3 root` reach the script through
`UT3CONV_EDITOR` and `UT3CONV_UT3_ROOT`; leaving them empty uses the defaults
at the top of `batch.py`.

**Inspect** is `info`, `classes`, `list`, `props` and `imports` — reading a UT3
package without converting it, which is how most conversion bugs get found.

Only options you actually changed reach the command line, so the previewed
command stays as short as the one you would have typed.

## Setup

The **Setup** header at the top holds the two things this cannot always guess:
which `python` to use, and where `ut3conv.py` lives. The folder is found by
walking up from the executable, so a build in `gui/build` finds it with nothing
set. If it comes up empty and highlighted, point it at the converter folder.

Last-used values are remembered per tab, in `settings.ini` under
`SDL_GetPrefPath` — `~/.local/share/zenakuten/ut3convgui/` on Linux,
`%APPDATA%\zenakuten\ut3convgui\` on Windows.

## Build

### Requirements
- CMake 3.20+
- C++17 compiler (MSVC, GCC, Clang)
- SDL3 3.2 or newer — the process and dialog APIs it leans on arrived there
- Dear ImGui, either already built or fetched during configure

SDL3 is always used as found: this never builds it. Dear ImGui is the only
piece that may need fetching, and it is five source files plus two backends.

### Linux

SDL3 comes from your distribution (`pacman -S sdl3`, `apt install libsdl3-dev`).
Dear ImGui is not usually packaged, so CMake clones it during configure and
builds it against that SDL3. Nothing else is needed — no vcpkg:

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

If you do have Dear ImGui built somewhere already, point CMake at it and the
fetch is skipped:

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=/path/to/imgui/prefix
```

### Windows (MSVC)

There is no system SDL3 to find, so both come from
[vcpkg](https://github.com/microsoft/vcpkg) via `vcpkg.json`. Set `VCPKG_ROOT`
to your vcpkg installation, then:

```
cmake -B build -DCMAKE_TOOLCHAIN_FILE=%VCPKG_ROOT%/scripts/buildsystems/vcpkg.cmake
cmake --build build --config Release
```

Do not use the vcpkg toolchain on Linux. It ignores the SDL3 you already have
and builds its own from source, dragging in util-linux and systemd to do it.

### Cross-compiling the Windows .exe from Linux

Beats booting Windows for a build. On Arch:

```bash
sudo pacman -S mingw-w64-toolchain
yay -S mingw-w64-sdl3
cmake -B build-win -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain-mingw.cmake
cmake --build build-win
```

Dear ImGui is fetched and built by the same cross-compiler, so SDL3 is the only
package that has to exist for Windows.

Four DLLs have to sit beside `ut3convgui.exe`, all from
`/usr/x86_64-w64-mingw32/bin/` (`mingw-w64-sdl3`, `mingw-w64-lzo`,
`mingw-w64-lz4`):

| | |
|---|---|
| `SDL3.dll` | SDL itself |
| `libssp-0.dll` | Arch builds that SDL3.dll with the stack protector, so it imports this. Miss it and the exe dies at load with `status c0000135` and no window |
| `lzo2.dll` | UE3 compresses package chunks with LZO, and every stock UT3 map uses it. The scripts load this through ctypes, not the exe |
| `lz4.dll` | The same for Gears of War Reloaded, which compresses with LZ4 |

The last two are for the Python scripts rather than the interface, which is why
`objdump -p ut3convgui.exe` does not list them. Linux users install `lzo` and
`lz4` from their distribution instead, the same as SDL3.

Nothing else: the toolchain file links the GCC runtime statically, so
`libgcc_s_seh-1.dll`, `libstdc++-6.dll` and `libwinpthread-1.dll` are not
needed. `objdump -p ut3convgui.exe | grep 'DLL Name'` confirms what a build
actually wants.

Then `./build/ut3convgui`.

## What is in a release zip

A release is self-contained: the binary sits beside the converter it drives, so
`ut3conv.py` is found with nothing to configure.

```
ut3convgui-<platform>-v2/
    ut3convgui[.exe]     the interface
    SDL3.dll             Windows only, with libssp-0.dll beside it
    lzo2.dll lz4.dll     Windows only: the package codecs, for the scripts
    ut3conv.py           the converter
    batch.py             every map, one package per build
    convert/ ut3/ ut2/   the modules those two import
    README.md            the converter's own README
    README-gui.md        this file
```

Python 3 is the one thing not in the box, since the scripts are what do the
converting. Linux also needs SDL3, LZO and LZ4 from your distribution (Arch
`sdl3 lzo lz4`, Debian/Ubuntu `libsdl3-0 liblzo2-2 liblz4-1`); the Windows zip
carries its own. Without LZO no stock UT3 map opens, since all of them are
LZO-compressed; without LZ4, no Gears of War Reloaded map.

`tests/` and `tools/` are left out, being neither imported nor needed to
convert a map.

## Keeping up with the scripts

`src/spec_generated.h` holds every flag the interface shows — name, type,
default, choices and the tooltip text — and it is generated from the scripts'
own argparse definitions:

```bash
./tools/gen_spec.py
```

Run that after adding or changing a flag in `ut3conv.py` or `batch.py`. The
header is committed, so building needs no Python; only regenerating does. A
flag the generator does not recognise is drawn under **Other** rather than
going missing, and the generator says so. The one thing kept by hand is
`SECTIONS` at the top of `gen_spec.py`, which decides only which box a flag is
drawn in.
