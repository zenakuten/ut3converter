# ut3conv

Converts a UT3 map (`.ut3`, UE3 package version 512) into a UT2004 `.t3d` plus a
buildable asset package. Worked example throughout: `DM-Deck.ut3`.

Paths below assume UT2004 is installed at `C:\UT2004` (Windows) or `~/UT2004`
(Linux), with this converter in `ut3converter` inside it, and UT3 in its usual
Steam location. Substitute your own if they differ. The converter is plain
Python 3 and runs natively on either; only `UCC.exe` needs Wine on Linux.

## Converting DM-Deck

**Windows**

```
cd C:\UT2004\ut3converter
python ut3conv.py t3d "C:\Program Files (x86)\Steam\steamapps\common\Unreal Tournament 3\UTGame\CookedPC\Maps\DM-Deck.ut3" -o out-dm-deck\DM-Deck.t3d
```

**Linux**

```bash
cd ~/UT2004/ut3converter
./ut3conv.py t3d ~/".steam/steam/steamapps/common/Unreal Tournament 3/UTGame/CookedPC/Maps/DM-Deck.ut3" -o out-dm-deck/DM-Deck.t3d
```

Two things come out of that one command:

| | |
|---|---|
| `out-dm-deck/DM-Deck.t3d` | the map — brushes, actors, lights, movers, terrain |
| `<UT2004>/DMDeckTex/` | the asset package source: textures, meshes, sounds, and a generated `DMDeckTex.uc` of `#exec` imports |

The package name defaults to the map's name stripped of punctuation with `Tex`
appended, so `DM-Deck.ut3` gives `DMDeckTex`. Override it with
`--texture-package` if you want something else.

The package goes to the UT2004 install root by default, and that is not
incidental: `ucc make` compiles `<root>/<Package>/Classes/<Package>.uc` and
resolves the `FILE=` paths in it relative to that folder, so anywhere else means
copying it back by hand before every build. `--textures DIR` moves it if you are
only inspecting the output.

Expect a summary like this — it is worth reading, since it is where the
conversion says what it could not do:

```
  package: C:\UT2004\DMDeckTex
  textures: 62 written, 2 materials unresolved -> DMDeckTex.uc
  skybox: S_UN_Sky_SM_SkyDome03 in a SkyZoneInfo room; world brush set to FakeBackdrop
DM-Deck.ut3 -> out-dm-deck/DM-Deck.t3d
  302 brushes (71 subtractive), 3150 polygons, 12484 vertices; 194 volumes; ...
  97 static meshes (94360 triangles), 4542 actors placing 2245001 triangles; ...
  28 movers from 10 move track(s) (20 attached, moving with a leader); 2 lift(s) ...
  72 ambient sounds from 39 waves (4.6 MB); 56 one-shot emitters
  638 lights (615 Light, 21 Spotlight, 2 Sunlight); skipped 1 SkyLight
  38 pickups (13 weapon bases, 25 items); 138 path nodes, 0 jump pads
```

## Building the package

The `.t3d` references textures and meshes by package name, so the package has to
be compiled before the map will open with anything on it. First name it in
`EditPackages` in `System/UT2004.ini`, and nothing else map-related:

```ini
EditPackages=DMDeckTex
```

**Windows**

```
cd C:\UT2004\System
del DMDeckTex.u
ucc make
copy DMDeckTex.u ..\Textures\DMDeckTex.utx
```

**Linux**

```bash
cd ~/UT2004/System
rm -f DMDeckTex.u
wine UCC.exe make
cp DMDeckTex.u ../Textures/DMDeckTex.utx
```

Deleting the `.u` first is not optional: `ucc make` skips a package whose `.u`
already exists, so without it nothing is rebuilt.

**One map package per build.** `UCC.exe` is a 32-bit binary, so a build has
about 2GB of address space, and that has to cover every package `EditPackages`
names — not just the ones being compiled, because UCC still *loads* an
already-built package to resolve references. Seven map packages come to 434MB;
all 62 would be roughly 3.7GB. Leave unrelated entries (OnslaughtSpecials2, your
own mods) alone — they cost nothing, since their `.u` files already exist.

Copying the result to `Textures/DMDeckTex.utx` rather than leaving it in
`System` is deliberate: these are map assets, not code, and `.utx` is where the
editor looks for them.

## Importing into UnrealEd

`File > Import` the `.t3d` (**not** `File > Open` — it is a t3d, not a `.ut2`),
then:

1. **Build > Geometry**, then **Build > Paths** — UT2004 derives its own
   reachspecs and jump velocities, so bots have no paths until you do.
2. Save as `<UT2004>/Maps/DM-Deck.ut2`.

## Options worth knowing

Everything below is a flag on the `t3d` subcommand; `ut3conv.py t3d --help`
lists all of them.

**Scale and scope**

| | |
|---|---|
| `--scale F` | world scale factor (default 1.0 — UT3 and UT2004 units agree) |
| `--texture-package NAME` | package name instead of the derived `<Map>Tex` |
| `--textures DIR` | where to write the package (default: the install root) |
| `--no-package` | emit only the `.t3d` |
| `--max-texture-size N` | largest mip exported (default 1024) |

**Leaving things out** — each of these has a `--no-` form: `--no-meshes`,
`--no-terrain`, `--no-movers`, `--no-sounds`, `--no-lights`, `--no-pickups`,
`--no-paths`, `--no-player-starts`, `--no-objectives`, `--no-teleporters`,
`--no-onslaught`, `--no-volumes`, `--no-skybox`, `--no-minimap`. Useful for
bisecting a map that will not build.

**Lighting** — UT3's units do not map onto UT2004's, so these are the dials you
will actually reach for:

| | |
|---|---|
| `--light-gain F` | UE3 brightness 1.0 becomes this UE2 `LightBrightness` (default 32) |
| `--ambient-gain F` | scales the UT3 SkyLight into `AmbientBrightness` (default 16) |
| `--light-radius-scale F` | widen every light's radius |

**Sky** — `--sky-mode skybox` (default) puts UT3's dome in a `SkyZoneInfo` room
so it reads as infinitely distant; `inline` keeps it as level geometry, where
UE2's hard 65536uu far plane will clip it. `--shrink-backdrop` pulls distant
backdrop meshes into the sky room, which happens automatically anyway when
reaching them would cross UE2's 262144uu world limit.

**Warfare maps** — `--onslaught-specials` places the OnslaughtSpecials2 core and
node classes instead of stock Onslaught, which is what keeps countdown nodes and
standalone flags. Without that mod installed the editor drops those actors
silently and the map ends up with no power cores. `--countdown-time` and
`--countdown-damage` tune a countdown node, and `--node-rise` lifts a node you
still have to jump on. `--vehicle-rise` (default 32) keeps a spawning vehicle
from dropping through the mesh it rests on.

**Terrain** — `--deco-density F` sets how thickly UT3's foliage becomes UT2004
decoration layers (0 omits them; UT3 states where ground cover goes but not how
much). `--terrain-layer-scale` forces one tiling on every layer instead of
deriving each from UT3's `MappingScale`.

## Inspecting a package

The same tool reads UT3 packages directly, which is how most conversion bugs get
found (`python ut3conv.py ...` on Windows):

```bash
./ut3conv.py info    DM-Deck.ut3                     # header summary
./ut3conv.py classes DM-Deck.ut3                     # histogram of export classes
./ut3conv.py list    DM-Deck.ut3 -c StaticMeshActor  # exports, filtered
./ut3conv.py props   DM-Deck.ut3 StaticMeshActor_347 --components
```

## What does not convert

Particles and emitters, Kismet scripting, UE3 shader materials (nothing in a
`ucc make` can build a Shader or FinalBlend), SpeedTree foliage,
PostProcessVolumes, skeletal meshes and destructibles. Meshes needing per-poly
collision still want `UseSimpleKarmaCollision=False` set by hand in the editor.

## Tests

```bash
python3 tests/test_geometry.py      # one of 15, tests/test_*.py
```

They read the stock UT3 maps from the Steam install, so they are regression
tests against real data rather than fixtures. `FORMAT.md` documents the UE3 structures that were reversed.
