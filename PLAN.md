# UT3 → UT2004 Map Converter — Plan

Convert Unreal Tournament 3 maps (`.ut3`, UE3 package v512) into UT2004 maps
(`.ut2`, UE2). Phase 1 scope was **BSP, textures, lights, PlayerStarts**; that
and everything after it is done.

**Status.** All 55 stock maps convert and build, across DM, CTF, VCTF and
Warfare. `batch.py` runs the lot, one package per build. What remains is a long
tail of per-map defects found by opening each in UnrealEd — see "Where this
stands" at the end, which is the section to read first when picking this up
cold.

## 0. Findings from the recon spike (already done)

Probed `DM-HeatRay.ut3` directly with Python:

| Fact | Value | Consequence |
|---|---|---|
| Package version | UE3 v512, licensee 0, cooker 57 | Well-documented format |
| Compression | LZO1X, 70 chunks | `liblzo2.so.2` present → ctypes, no pip needed |
| Header layout | export entry has a `ComponentMap` (count + n×12 bytes) between `SerialOffset` and `ExportFlags` | Solved; full 28400-entry table parses cleanly |
| `Polys` exports | **611** (alongside 313 `Brush`, 610 `BrushComponent`) | **Brush source geometry survived cooking** — the #1 risk is cleared, we can emit real CSG brushes, not a BSP-node reconstruction |
| `StaticMeshActor` | **2403** (205 unique `StaticMesh`) | See "reality check" below |
| Lights | 381 PointLight, 79 SpotLight, 4 DirectionalLight, 1 SkyLight | Straightforward |
| `Texture2D` | 507 in-map + more via imports | No `.tfc` files anywhere → all mips inline, extraction is self-contained |
| Materials | 207 `Material`, 57 `MaterialInstanceConstant` | Graph-based; needs a "find the diffuse" heuristic |
| `PlayerStart` | 17 | Trivial to carry over |
| Coordinate system | identical between UE2/UE3 (X fwd, Y right, Z up, 65536 rotator units) | No axis conversion; scale defaults to 1.0 |

### Reality check on Phase 1 scope

UT3 levels are built almost entirely from **static meshes**; BSP is used for a
handful of shapes plus 259 `BlockingVolume`s. Converting "BSP + textures + lights"
on HeatRay will therefore produce a recognizably-lit, correctly-scaled but **mostly
empty shell** — the walls and floors people actually see are static meshes.

That is fine as a Phase 1 milestone (it proves the whole pipeline end to end), but
static meshes should be Phase 2, not "someday". They are also very tractable:
UT2004 has real static-mesh support, and 205 unique meshes is a manageable set.

## 1. Architecture

Pure-Python reader as the primary path; the UT3 editor (Wine/Steam) used as a
**ground-truth oracle**, not as the pipeline. Export one brush, one mesh, one
texture from UnrealEd by hand and diff against our parser output — that catches
format mistakes far faster than eyeballing geometry in-game.

```
ut3converter/
  ut3/
    package.py     # header, LZO chunk decompression, name/import/export tables
    props.py       # UE3 tagged-property reader (Int/Float/Bool/Name/Object/Struct/Array)
    objects/
      model.py     # UModel + UPolys -> FPoly list
      texture.py   # UTexture2D -> mips (DXT1/3/5, A8R8G8B8, G8)
      material.py  # Material graph -> "best guess diffuse texture"
      light.py     # Light actors + their components
      mesh.py      # (phase 2) UStaticMesh -> vertices/tris/UVs/materials
  ut2/
    t3d.py         # UE2 .t3d writer (brushes, actors, polygons)
    tga.py / dds.py# texture output
    ase.py         # (phase 2) ASE static mesh writer
    ucpkg.py       # generated .uc with #exec directives -> ucc make
  convert/
    geometry.py  lights.py  textures.py  actors.py  scale.py
  cli.py           # python -m ut3converter DM-HeatRay.ut3 -o out/
  tests/
```

### Output artifacts and how they get into UT2004

The generated package goes to the **UT2004 install root**, not into the
converter's own folder, and that is not a preference: `ucc make` compiles
`<root>/<Package>/Classes/<Package>.uc` and resolves its `#exec ... FILE=`
paths relative to that folder, so anywhere else means hand-copying before every
build. `ut3conv.install_root()` derives it as the converter's parent directory,
confirmed by `System/` being there. `--textures DIR` overrides, `--no-package`
skips it.

Each run clears the package's `Textures/`, `Meshes/`, `Sounds/` and `Terrain/`
of its own file types first. Without that, a mesh that stops being exported (or
gets renamed, as the whole set did when the variant keying was fixed) lingers
for good -- the .uc stops referencing it so it never reaches the build, but it
piles up on disk and hides what the current conversion actually produced. The
first cleaning run on DM-HeatRay removed 690 such files.

1. `out/DM-HeatRay.t3d` — brushes + lights + PlayerStarts (+ static mesh actors in phase 2).
2. `<root>/HeatRayTex/Textures/*.dds|tga` plus a generated `HeatRayTex.uc` full of
   `#exec TEXTURE IMPORT NAME=... FILE=...` directives.
3. Phase 2: `<root>/HeatRayTex/Meshes/*.ase` plus `#exec STATICMESH IMPORT ...`.
4. `ucc make` builds the texture/mesh packages (same Wine workflow as `System/makeit`).
5. UnrealEd: import the `.t3d`, **Build All**, save `.ut2`.

Using `#exec` directives means asset packages are built by the compiler rather than
clicked together in the editor GUI — the only unavoidable manual step is the final
t3d import + geometry/lighting rebuild. (Verify early that `#exec TEXTURE IMPORT`
accepts DXT `.dds`; fall back to TGA + `LODSET`/compression flags if not.)

## 2. Phases

**Phase 0 — spike (mostly done).** Remaining: verify `#exec TEXTURE IMPORT` with a
DDS; hand-import a trivial `.t3d` into UT2004 UnrealEd under Wine to confirm the
round-trip; open `DM-HeatRay.ut3` in the UT3 editor and export one brush to `.t3d`
as a reference file.

**Phase 1a — package reader. DONE.** `ut3/package.py` + `ut3/props.py` + the
`ut3conv.py` dump CLI, with `tests/test_package.py` green: header, LZO chunks,
name/import/export tables, and tagged properties for 27937 of 28400 exports on
DM-HeatRay (the remainder are `Package`/`Class` stubs with no property list).
Verified on CTF-FacingWorlds and DM-Deck too. Format specifics -- including two
places where UT3 departs from the published UE3 layout -- are written up in
`FORMAT.md`.

**Phase 1b — BSP. DONE.** `ut3/objects/model.py` reads UPolys/FPoly natively,
`ut2/t3d.py` writes UT2004 t3d, `convert/geometry.py` maps between them, and
`ut3conv.py t3d` drives it. All 313 brush actors in DM-HeatRay convert (1918
polygons), as do CTF-FacingWorlds, DM-Deck and DM-Sanctuary; `--volumes` also
brings across BlockingVolumes. `tests/test_geometry.py` is green.

Two engine differences had to be handled rather than passed through -- an
off-plane `FPoly.Base` (which UE2's CSG would miscut) and UE3's reuse of the
0x200/0x400/0x800 PolyFlags bits. Both are written up in `FORMAT.md`.

Verified in UnrealEd: the output imports and works.

The converter also emits an enclosing `CSG_Subtract` world brush first, because
UT2004 is subtractive and UT3 is additive (`--no-world-brush` to suppress,
`--world-margin` to size the padding). Without it the imported geometry sits
buried in solid rock and has to be freed by hand.

**Phase 1c — textures. DONE.** `ut3/resolve.py` (package index),
`ut3/objects/texture.py` (Texture2D + bulk mips), `ut3/objects/material.py`
(diffuse resolution), `ut2/images.py` (DDS/TGA writers) and
`convert/textures.py` (orchestration), driven by `ut3conv.py textures` or
`t3d --textures DIR`. `tests/test_textures.py` is green.

All 507 Texture2Ds in DM-HeatRay yield pixel data; 29 of its 30 BSP materials
resolve to a diffuse texture, and the extracted images are visually correct.
Output is a buildable package tree (`<Pkg>/Classes/<Pkg>.uc` with
`#exec TEXTURE IMPORT` lines, `<Pkg>/Textures/*.dds`), and the t3d's texture
references are generated from the same name table so the two always agree --
a texture that fails to export is dropped from the t3d rather than left dangling.

The find that made this work: streaming mip payloads live in the *content
package that owns the texture*, not in the map (see `FORMAT.md`).

Every polygon gets a material, including those whose UE3 material resolved to
nothing (in DM-HeatRay that is 1132 of 1918 polys, all pointing at
`EngineMaterials` defaults) and the world brush's own faces. They fall back to a
generated neutral grey grid, `DefaultBSP` -- UnrealEd reports a poly with no
material as a null material reference on every single build otherwise.

**Phase 1d — lights. DONE.** `convert/lights.py`. PointLight -> Light,
SpotLight -> Spotlight, DirectionalLight -> Sunlight, PointLightToggleable ->
TriggerLight; SkyLight has no UE2 equivalent and is reported, not converted.
461 of DM-HeatRay's 468 lights convert (2 disabled, 5 at zero brightness).

Three unit differences, each checked against the UT2004 source rather than
guessed: `WorldLightRadius() = 25 * (LightRadius + 1)` (Engine/Inc/AActor.h),
half-angle `= acos(1 - LightCone/256)` (Engine/Src/UnRenderVisibility.cpp:400),
and UE2's inverted `LightSaturation` where 255 means white (Engine/Light.uc).

Brightness is the one value with no principled mapping, so `--light-gain`
controls it. The default of 32 is empirical: DM-HeatRay's median UE3 Brightness
is 2.0, which a gain of 32 maps onto 64 -- UE2's own default LightBrightness --
while keeping clamping down to 7% of lights (a gain of 64 clamped 30% of them,
flattening the range).

The SkyLight is the map's real fill light and has no UT2004 equivalent -- zone
ambient lives on LevelInfo, which a t3d import cannot create. The converter
reports the values to set by hand instead (View > Level Properties > ZoneLight).
Without it a converted map is close to black: on DM-HeatRay every point of the
map volume is inside some light's radius, so darkness is not a coverage problem,
it is the missing ambient. Ambient gets its own `--ambient-gain` (default 128)
because UE3's SkyLight brightness feeds a tone-mapped HDR pipeline and does not
scale linearly onto a UE2 ambient byte; 128 puts DM-HeatRay at
`AmbientBrightness=32`. `--light-radius-scale` widens every light if the pools
still read too small.

**Phase 1e — PlayerStarts. DONE.** `convert/actors.py`. All 17 convert, with
Location, Rotation, and TeamNumber where UT3 supplied a TeamIndex.

**Phase 1 is complete.** `tests/test_package.py`, `test_geometry.py`,
`test_textures.py` and `test_lights.py` are all green.

**Phase 2 — static meshes. BUILT, NOT YET VERIFIED IN THE EDITOR.**
`ut3/objects/staticmesh.py` (UStaticMesh reader), `ut2/ase.py` (ASE writer) and
`convert/meshes.py`, wired into `t3d --textures` (`--no-meshes` to skip).
`tests/test_meshes.py` is green.

All 205 meshes in DM-HeatRay parse and validate; 179 are actually referenced,
and 2416 actors place them. Meshes and textures go into one package because the
ASE importer resolves `*BITMAP` against *already-loaded* textures by object
name, so the texture `#exec` lines must come first in the generated `.uc`.

`RawTriangles` -- the editor's source geometry -- is stripped by the cooker, so
geometry is rebuilt from the render buffers (positions, half-float UVs, index
buffer). Two importer conventions had to be cancelled out in the ASE, both read
from Editor/Src/UnStaticMesh.cpp: positions are multiplied by `FVector(-1,1,1)`
on import (so X is written negated) and V is flipped (so V is written flipped).
A material is only committed when `*UVW_V_TILING` is parsed, so every
`*MAP_DIFFUSE` block must carry BITMAP + both tiling lines.

**`#exec STATICMESH IMPORT` cannot load ASE.** That handler only accepts
LightWave `.lwo` and rejects everything else outright
(`Editor/Src/UnEdSrvExecImporters.cpp:426`, "Could not import non .lwo format").
ASE is handled by `UStaticMeshFactory`, which registers the format at
`Editor/Src/UnStaticMesh.cpp:415`, so meshes are imported through the generic
factory exec (`UnEdSrv.cpp:436`) instead:

    #exec NEW STANDALONE StaticMeshFactory FILE=Meshes\X.ase NAME=X PACKAGE=Pkg

**Open risk: 1,187,125 triangles placed.** That is a lot for UE2 even before
lighting. Triage options, in rough order of preference: drop small decorative
meshes, prefer a lower LOD where the mesh has one, cull meshes below a screen-size
threshold, or decimate. Needs a real in-editor measurement first.

**Phase 3 — terrain. DONE.** `ut3/objects/terrain.py`, `ut2/bmp.py` and
`convert/terrain.py`, driven by the same `t3d --textures` run (`--no-terrain` to
skip, `--terrain-layer-scale` for tiling). `tests/test_terrain.py` is green.

DM-HeatRay's single Terrain (129x129 vertices, 3 layers, 6554uu square) converts
to a UT2004 `TerrainInfo` with a G16 heightmap, one alpha map per layer, and the
layer materials resolved through the existing texture pipeline.

The two scale conventions reconcile exactly:

    UE3   Z = Location.Z + (h - 32768) / 128 * DrawScale * DrawScale3D.Z
    UE2   Z = Location.Z + h * TerrainScale.Z / 256   (UnTerrain.cpp:1464)
    =>    TerrainScale.Z = 2 * DrawScale * DrawScale3D.Z
          Location.Z     = UE3 Location.Z - 32768 * DrawScale * DrawScale3D.Z / 128

so the raw heights pass through untouched and world heights match to 0.000000uu.
The heightmap goes out as a 16-bit BMP, the only import path that yields
TEXF_G16 (UnEdFact.cpp:2191); alpha maps as 32-bit TGAs, since TEXF_RGBA8 alpha
maps are read from the alpha channel (UnTerrain.cpp:1516). UE3's 129x129 grid is
trimmed to 128x128 for power-of-two textures, costing one patch of extent.

Four things had to be right before UT2004 would draw any of it, none of them
obvious from the data:

* **`ZoneInfo.bTerrainZone`** must be True. Nothing in the engine sets it, and it
  gates rendering (UnRenderVisibility.cpp:2008), collision and traces
  (UnLevTic.cpp:902) alike -- a correct terrain is simply skipped without it.
* **A ZoneInfo must exist at all.** Terrains register into
  `Region.Zone->Terrains` and `UpdateTerrainArrays` then calls
  `L->Terrains.Empty()` (UnLevel.cpp:845), so a terrain in the LevelInfo's zone
  -- i.e. any map with no ZoneInfo -- is registered and immediately wiped.
* **The mapping is centred**: `ToWorld /= (HeightmapX/2, HeightmapY/2, 32767)`
  (UnTerrain.cpp:1466), so Location is the heightmap centre at height 32767, and
  heights stay centred rather than biased to zero.
* **References must be fully qualified** `Package.Group.Object`. Actor properties
  resolve by exact path; only the t3d polygon importer has an ANY_PACKAGE
  fallback, so a wrong path leaves TerrainMap silently None.

Still to verify in the editor: layer tiling (UT2004 layer UVs run 0..1 across the
whole terrain and UE3's mapping lives in a matrix that does not translate
directly, so `--terrain-layer-scale` defaults to an assumed 16).

**Phase 3 — sky. DONE.** `convert/skybox.py` and `convert/skybox_move.py`.

UT3 has no skybox at all. The horizon is a genuinely huge dome mesh sitting in
the level (DM-HeatRay's `S_UN_Sky_SM_Dome01` at DrawScale 300, a 323,520uu
radius) with the distant city blocks as ordinary actors parked outside the play
area, reaching X -39604. UT2004's own idiom is a separate little room drawn
through PF_FakeBackdrop surfaces with the viewer's rotation but not translation.

**The governing limit is `FAR_CLIPPING_PLANE = 65536` (Core/Src/Core.cpp:197).**
It is not a cull distance that can be tuned: it is the far plane of the
perspective projection matrix itself (Engine/Src/UnRender.cpp:1510), so the
hardware depth-clips anything further from the camera, and nothing in the engine
ever reassigns it. The zone's `bDistanceFog`/`DistanceFogEnd` only move the
frustum *culling* plane (UnRender.cpp:1066) and cannot push geometry past it.

UT3's dome is 323,520uu -- five times that. This single constant is why UE2 has
the skybox idiom at all: a sky big enough to look distant cannot be drawn in the
level, so UE2 draws a small one with the viewer's translation removed instead.

Two modes, selected by `--sky-mode`:

- **`inline` — keeps UT3's model, as far as the engine allows.** The
  dome stays level geometry and the backdrop meshes stay where UT3 put them.
  `fit_inline_dome` takes UT3's authored DrawScale as the target -- the dome is
  deliberately far bigger than what it covers, and shrink-wrapping it around the
  geometry brings the sky far too close -- and clamps it only where an engine
  limit bites. On DM-HeatRay that is DrawScale 49.6, radius 53,512uu, with the
  furthest dome surface 61,862uu from the furthest corner of the play area,
  inside the 65,536uu far plane. Push past that and the sky visibly stops
  partway up, which is what a too-large dome looks like in the editor.

  When the far plane binds, the dome is also re-centred on the play area rather
  than left at UT3's 50,728uu offset, since that offset costs radius one-for-one
  and a centred dome is the largest one that can be drawn. When only
  HALF_WORLD_MAX binds, the offset is scaled by the same factor as the radius
  instead, which preserves the view exactly: a dome at offset d with radius R
  subtends the same angles from the play area as one at s·d with radius s·R.

  Cost: the dome ends up ~1.3x the extent it covers against UT3's ~4.6x, so the
  sky reads as close. That gap cannot be closed inline at any scale.

- **`skybox` (default) — the UT2004 idiom, and the only way to get UT3's
  proportions.**
  A subtractive room, a SkyZoneInfo and the dome scaled to fit; the world brush
  is flagged PF_FakeBackdrop. Because the room is drawn with rotation but not
  translation, the dome reads as infinitely distant and the far plane never
  applies to it.

  UT3's distant city stays **in the level at its true positions and true size**,
  with the world subtract brush grown to enclose it
  (74,698 x 55,892 x 46,154uu on DM-HeatRay) -- an actor outside the void sits
  in solid space and renders nowhere, which is what made it look "missing"
  before. Everything out there is 46,836uu from the play area at worst, well
  inside the far plane, so unlike the dome it needs no scaling at all. This is
  the most faithful combination available: real city at real distance, sky at
  infinity.

  `--shrink-backdrop` instead pulls those 122 meshes into the sky room, scaled
  by the dome's own factor (1.4243/300 = 1:211). A skybox is
  translation-free, so the uniform scale preserves every apparent angle exactly
  -- the horizon is reproduced rather than faked -- but the geometry is no
  longer where UT3 had it. The room must overlap neither the map (voids are
  independent in UE2; ONS-Adara's skybox sits at -40608, 37252, -22516) nor any
  actor, so it is placed clear of the world brush as actually built.

**Phase 4 — pickups and paths. DONE.** `convert/pickups.py`.

The map looked right but did not play: no weapons, no armour, no bot paths.

*Pickups.* UT3 places every item as a `UTPickupFactory` subclass, weapons naming
their weapon in `WeaponPickupClass`. UT2004 does the opposite -- checked against
DM-Rankin, DM-Antalus and DM-1on1-Albatross, it places a *base* that spawns the
item: `xWeaponBase` with a `WeaponType`, or an `xPickUpBase` subclass whose
`PowerUp` names the pickup (`HealthCharger` -> HealthPack, `ShieldCharger` ->
ShieldPack, `SuperShieldCharger` -> SuperShieldPack, `UDamageCharger` ->
UDamagePack). Only small items go down bare, which is what the 17 health vials
do as `MiniHealthPack`.

37 of DM-HeatRay's items convert: 10 weapon bases and 27 pickups. Two
substitutions are not equivalent and are reported rather than hidden -- UT3's
20-point helmet becomes a 50-point ShieldPack, and invisibility becomes UDamage,
UT2004 having no invisibility. Note UT2004's `XWeapons.SniperRifle` *is* the
Lightning Gun (see its `ItemName`), so UT3's sniper maps straight onto it.

*Paths.* PathNode and jump pads convert directly, but the jump velocity does
not: UT2004 computes it during Build Paths from the pad's first forced path
(`AJumpPad::addReachSpecs`, Engine/Src/UnNavigationPoint.cpp:1281). So UT3's
1069 ReachSpecs are thrown away -- the editor rebuilds those -- while each pad's
`JumpTarget` is preserved as a `ForcedPaths` entry, which matches on the
target's *object name* (`Nav->GetFName()`, UnNavigationPoint.cpp:544) and so
must agree with the `Name=` the PathNode was emitted under. 144 nodes, 4 pads,
all 4 links intact.

*Placement.* Both engines put an actor's Location at the *centre* of its
collision cylinder, so a placed actor rests CollisionHeight above the floor --
and the two engines disagree on those heights, which leaves a straight copy
hanging in the air. UnrealEd says so for the bases: `AxPickUpBase::CheckForErrors`
traces a mere 8uu down from Location and warns "xPickUpBase is floating" if it
misses (Engine/Src/UnErrorChecking.cpp:204), so a base sits *on* the floor
rather than a collision height above it.

    actor                UT3 height   UT2004 height   drop
    weapon factory       44           3 (xWeaponBase)   41
    health/armour/power  44           on the floor      42
    health vial          20           23 (MiniHealth)   -3
    PathNode, jump pad   50           43 (NavPoint)      7
    PlayerStart          80           43                37

The UT3 numbers come from the class defaults in `CookedPC/UTGame.u` and
`Engine.u`; `UTPickupFactory`'s own `BaseMeshComp` -- the part that sits on the
floor -- is translated by -44, confirming the convention. PlayerStarts had the
same 37uu error from Phase 1 and were fixed with it.

*Redundant brushes must be material-aware.* Dropping a brush that lies wholly
inside another is safe by volume, but not by surface: `Brush_470` is
geometrically identical to `Brush_125` while being the one that carries the real
floor texture -- 125's faces are all `EngineMaterials.DefaultMaterial`. Dropping
470 left the floor grey. A brush now only counts as redundant when it brings no
material the keeper lacks, which takes the count from 3 to 2.

*The trap.* UT3 cooks class default objects into the map --
`Default__UTArmorPickup_ShieldBelt`, `Default__UTPickupFactory_Invisibility` --
which carry an actor class but no Location, so converting one drops a phantom
pickup at the world origin. `is_placed_actor` (ut3/objects/level.py) requires
the export to live under `TheWorld.PersistentLevel`, and lights and PlayerStarts
now go through the same filter.

**Phase 4 — brush plane hygiene. DONE.** `convert/align.py`.

Players died at random spots with "left a small crater", on foot and in mid-air,
with KillZ nowhere near. That message is `class'Fell'`, which `FellOutOfWorld`
also uses, and the log line `fell out of the world!` identifies the real path:

    if ( bCollideWorld && (Region.ZoneNumber == 0) && !bIgnoreOutOfWorld )
        -- Engine/Src/UnPhysic.cpp:336

Zone 0 is solid space. Open air was being classified solid.

Two defects, both from UT3 brush vertices carrying sub-unit float drift against
UE2's `THRESH_POINT_ON_PLANE` of 0.10:

1. Faces authored flush ended up on planes a few thousandths apart -- inside the
   tolerance, so UE2 calls them one plane, but not equal, so the arithmetic
   disagrees with itself. 375 such pairs across DM-HeatRay.
2. Worse, and initially missed: where two solids *abut*, the faces that meet
   point at each other. Matching only same-facing normals left every seam in the
   map untouched. The spawn block and the ramp beside it met at X=1344.000 and
   X=1344.001 with opposed normals -- a thousandth of a unit of void trapped
   between two solids, exactly where players died.

The fix aligns planes and then rebuilds each vertex as the intersection of the
faces meeting there, so polygons stay exactly planar and brushes keep their
topology. Snapping vertices to the grid was tried first and rejected: it clears
the near-coplanar pairs but tilts sloped faces 0.59uu off their own plane, which
breaks the same tolerance from the other side.

Getting there took a minimal repro -- one subtract box plus the brushes around
one death point -- bisected down to the block and the two ramps. Worth keeping
in mind: subsets must preserve the original CSG order, and the void box must
contain every brush, or the repro grows artefacts of its own.

**Phase 5 — the invisible-face bug. DONE.** `convert/geometry.py`.

Players died at random spots, on foot and in mid-air, anywhere in the map. The
cause was one flag. UT3 marks faces it does not draw with
`RemoveSurfaceMaterial`, and this converter translated that to UE2's
`PF_Invisible` -- 391 faces on DM-HeatRay. UE2 turns that flag into
`NF_NotVisBlocking` (Editor/Src/UnBsp.cpp:242), and zone assignment then stops
treating the face as the boundary between inside and outside
(Editor/Src/UnVisi.cpp:1170), so the open space beyond it inherits "inside" and
is written out as zone 0 -- solid. Anything entering solid space is killed on
the spot (Engine/Src/UnPhysic.cpp:336). Full write-up in `FORMAT.md`.

Measured by reading the built BSP back with `tools/ut2bsp.py`:

    2 brushes     5.77% of space wrongly solid, 1 malformed node -> 0.00%, 0
    304 brushes  10.51%, 299 malformed nodes                     -> 0.00%, 0

On the shipped map: 299 malformed nodes to 0, and every one of the remaining
sample disagreements sits exactly on a brush face, which is the sampler
straddling a surface rather than a defect.

*How it was found, because the route matters.* Guessing at the source geometry
got nowhere across many rebuild cycles. What worked was reading the *built* map:
`tools/ut2bsp.py` runs the engine's own `UModel::PointRegion` over a saved .ut2,
and `tools/verify_solidity.py` compares that against the CSG the .t3d asked for.
That turned "I die sometimes" into a number, and the number could be bisected --
304 brushes down to 2, at which point the map was 20 nodes and exactly one was
malformed: the `PF_Invisible` face.

Two false trails worth remembering. Hand-built UT3 test maps (overlapping,
abutting, carved, rotated, dense) all converted and built perfectly, which
looked like it exonerated the brushwork -- in fact they were clean because none
of them carried a hidden face. And the numbers that first looked like evidence,
dropped-weapon coordinates, were weapons sinking into floors.

*Build setting.* These maps want **BSP balance 1**, not the editor default of
15. `Balance` scores splitter choice as `(100-Balance)*Splits +
Balance*|Front-Back|` (Editor/Src/UnBsp.cpp:461), so a low value minimises
splits: 2206 nodes instead of 2599 on DM-HeatRay. At the default the lighting
occlusion pass recurses deep enough to overflow the stack and the build crashes.

**Phase 5 — the two reported bugs. DONE.**

*Missing StaticMeshActors* (1274, 1263, 99, 133, 462 and others) were never a
conversion fault. All five convert with the right mesh, position and DrawScale;
they were invisible because UE2 does not render actors whose region is solid,
and the `PF_Invisible` bug had 299 malformed nodes making whole clusters vanish.
Fixing the flag fixed them.

*The untextured I-beam* was real, and bigger than it looked. A Texture2D's mips
can be stored inline but LZO-compressed (bulk flag 0x10), and the reader was
slicing those bytes raw; the length then failed its sanity check and the mip was
discarded. `T_HU_Supports_SM_IbeamA_D` has every mip compressed, so it lost all
of them and was dropped, leaving 167 beam actors untextured. Details in
`FORMAT.md`.

The same bug was quietly costing resolution everywhere: of DM-HeatRay's 122
textures, 2 were dropped outright and **119 were exporting at a lower resolution
than the source had available**, because only the uncompressed mips were usable.

**Phase 5b — component material overrides. DONE.** `convert/meshes.py`.

A UE3 `StaticMeshComponent` can override the mesh's materials per actor, and 382
of DM-HeatRay's 2,432 mesh components do; the converter was ignoring all of
them. The reported case, `StaticMeshActor_630`, is a window frame whose own
material resolves to a cubemap falloff texture -- hence the odd look -- while its
override is an animated advertising material whose diffuse is the sign artwork.
Animated materials have no UE2 equivalent, so the diffuse is what comes across,
which is what was wanted here anyway.

Meshes are now keyed by reference *and* material set, so a mesh used two ways is
exported twice: 177 meshes to 208, and 125 textures to 134. See `FORMAT.md` for
the keying trap that first produced 509.

**Phase 5c — ambient sound. DONE.** `ut3/objects/sound.py`, `convert/sounds.py`.

69 of the map's 71 ambient actors convert to UT2004 `AmbientSound`, drawing on
36 of the package's 107 `SoundNodeWave` assets (5.2 MB of WAV). The two left out
are the ones UT3 itself starts silent: `AmbientSoundSimpleToggleable` defaults
to `bAutoPlay=False` and is switched on from Kismet, which does not convert, so
the map's own flag decides — its five machine hums set it, the Cicada engine and
the alarm horn do not.

Each wave's Ogg Vorbis payload is inline in the package; ffmpeg decodes it to a
**mono** WAV (ALAudio will not play a stereo `USound` at all) and
`#exec AUDIO IMPORT` pulls it into the same generated package as the textures
and meshes. `--no-sounds` skips the whole step; `--sound-gain` scales volume.

Three things needed engine reading rather than a straight copy, all written up
in `FORMAT.md`: the radius mapping (`sqrt(Min*Max)/2`, matching the half-volume
distance across two different falloff curves), the archetype chain that holds
every value the mapper left alone, and the mandatory downmix.

The five `AmbientSoundNonLoop` actors land on UT2004's own random-interval
`SoundEmitters` array, which is a closer fit than expected — UE3 draws one of 13
traffic flybys every 1..5s, and UT2004 runs each emitter on an independent
clock, so stretching every interval by the slot count reproduces the same rate.

**Phase 5d — Matinee movers. DONE.** `convert/movers.py`, `convert/curve.py`.

Six of DM-HeatRay's Matinee move tracks bind to a placed actor. Four of those
actors are the map's bullet train and cinematic ship and become eight UT2004
`Mover`s (five carriages, three ship pieces -- the extra six are hard-attached
followers given the lead's key list, which moves the train rigidly without
needing parenting). The other two tracks drive the paired light beams, which
are unlit translucent effect meshes the converter skips either way.

The train loops without a trigger: UT3 runs it on a `Completed -> Delay -> Play`
Kismet loop, which reads as `ConstantLoop`. The ship fires 120s after a scripted
death, so it lands as a dormant `TriggerToggle` with its path intact for a
mapper to hook up. Each carriage's engine hum moves onto its Mover instead of
staying parked at the start of the line.

`FORMAT.md` has the details: resampling for UT2004's single `MoveTime`, why
every key is a delta from t=0, how `IMF_RelativeToInitial` gets rotated, and why
the world void has to grow to cover a 70,021uu path.

**Phase 5e — separate opacity masks. DONE.** `ut2/dxt.py`, `resolve_opacity`
in `ut3/objects/material.py`, `bake_opacity` in `convert/textures.py`.

Reported as `StaticMeshActor_1942` (an ivy mesh) showing black where it should
be see-through. The cause is general, not one mesh: UE3 keeps opacity in its own
`..._M` texture, and 12 of DM-HeatRay's 17 masked materials do -- fences,
crosswalks, manholes, trash, plants, pillar decals. UE2 masks by the drawn
texture's own alpha, so the mask is now composited in and DXT1 diffuses are
repacked as DXT5 (colour blocks copied through untouched). Three materials
declare BLEND_Masked with nothing driving the mask; those stay opaque and are
reported rather than guessed at. See `FORMAT.md` for DXT1's three-colour trap,
which is what made the background black in the first place.

**Phase 6 — DM-Deck, and lifts. DONE.** Second map converted end to end, and
the first with real lifts (2 of them, plus 8 lift nav points).

It settled the move-frame rule DM-HeatRay could only half answer. Deck's lifts
are relative-frame tracks on actors at -90 and +270 degrees of yaw, and both
come out as a clean 2-key 608uu rise -- matching their LiftExits at z=-604
against a bottom of z=-1228 -- only under the delta-from-t0 reading. The
composed reading throws them thousands of units sideways.

It also found a real bug: Matinee plays a track over `[0, InterpLength]`, but an
author may leave keys outside that. `InterpActor_16` carries one at t=-2.196 --
the descent it makes *before* the sequence -- and sampling the curve's own
extent converted the lift as dropping 605uu through the floor and climbing back.
`convert/curve.py: played_range` clips to the window Matinee runs.

Three things are new for lifts:

- `LiftCenter.MyLift` names the lift outright, so no guessing; UE3's
  `SeqEvent_Mover` agrees on both. A lift becomes `StandOpenTimed`, which
  `LiftCenter.SpecialHandling` tests for by name (Engine/LiftCenter.uc:38), and
  keeps the engine's own encroach behaviour since it has to carry people.
- UT3 `LiftCenter`/`LiftExit`/`UTJumpLiftExit` map onto the UT2004 classes of
  the same name, bound by `LiftTag` against the mover's `Tag`. UT3 leaves
  `MyLift` unset on exits, so each is assigned to its nearest centre.
- Followers are `bSlave` movers sharing the leader's `Tag` rather than copies
  carrying a duplicate key list. `Mover::PostBeginPlay` attaches every same-tag
  slave to the leader (Engine/Mover.uc:454), which is UE2's own idiom for a
  multi-part mover and carries rotation properly.
- **Except on a lift**, where that idiom collides with the lift one.
  `ALiftCenter::FindBase` (UnNavigationPoint.cpp:1377) scans for actors whose
  Tag matches its LiftTag and errors "Lift has same tag as another lift" on the
  second Mover it finds -- then returns with MyLift unset, so bots never use the
  lift at all. DM-Deck's two lifts each carry five parts (corner panels and a
  rail, hard-attached in UT3), so every one of them tripped it. A lift's parts
  therefore take their own tag and their own copy of the path, and the lift
  drives them by event: `DoOpen` fires `OpeningEvent` as it starts interpolating
  (Engine/Mover.uc:372) and `TriggerOpenTimed` runs the identical
  open/StayOpenTime/close cycle, so they stay in step. Both ends state
  StayOpenTime explicitly, since each times its own cycle.

`tests/test_lifts.py` covers DM-Deck permanently.

Two things worth watching on Deck: 4,533 mesh actors placing **2.24M triangles**
(HeatRay is 1.16M), and 17 `ReverbVolume`s / 3 `UTTeleporter`s still unconverted.

**Phase 6b — diffuse resolution. DONE.** `ut3/objects/material.py`.

Reported as `StaticMeshActor_1981` and the BSP under it both looking wrong in
DM-Deck -- one cause, since both use `M_LT_Floors_BSP_Master`. The graph walk
returned the *first* texture it reached, and a UE3 diffuse chain multiplies a
reflection term in before adding the base colour, so it was picking up
`T_UN_CubeMaps_Robot_Paint01`. It now collects every reachable sample and scores
the names, with the scoring fixed so single-letter markers only count as
suffixes (matching `_c` anywhere is what let a cubemap outscore a real diffuse).

5 wrong textures out and 9 correct ones in on DM-HeatRay, 2 swapped on DM-Deck.

**Phase 6c — BSP surface UVs. DONE.** `convert/textures.py`, `convert/geometry.py`.

Reported as Brush_209's sides tiling too coarsely while its top looked right.
The cause was the UV rule itself, which had been fitted to two measurements one
of which was wrong. UE3 states BSP surface UVs against a fixed 128 whatever the
texture size; UE2 states them against the texture size. One rule covers it:

    |TextureU_UE2| = |TextureU_UE3| * exported_size / 128

The old "flat 4x plus a size correction" was that same rule evaluated at 512, so
it held for 512 textures and drifted everywhere else -- Brush_209's sides (a
2048 brick reduced to 1024) were 4x too coarse, its 1024 top only 2x, which is
why the top passed. All 1912 of DM-HeatRay's BSP surfaces now reproduce UT3's
repeat distance exactly. `--surface-scale` survives as a by-eye multiplier and
now defaults to 1.0.

**Phase 6d — hazard volumes and the goo you can see. DONE.**
`convert/geometry.py`, `convert/shaders.py`, `convert/meshes.py`.

Reported as DM-Deck's green goo pits neither killing nor showing. Two separate
causes:

*The damage.* Only `BlockingVolume` and `UTKillZVolume` were converted, so the
3 `UTSlimeVolume`s were dropped. UT2004 has no slime volume class, so it is
built the way `XGame.LavaVolume` is -- a PhysicsVolume with pain -- using UT3's
own numbers (DamagePerSec 7, FluidFriction 5, TerminalVelocity 1500), the bio
rifle's damage type, and a ViewFog taken from `XEffects.GoopSmoke`. `UTLavaVolume`
and `UTWaterVolume` need none of that: UT2004 ships `LavaVolume` and
`WaterVolume` already carrying the right damage, drag and entry effects, so
they map straight across. Across the 13 stock UT3 maps that covers 6 slime, 5
water and 2 lava volumes.

*The look.* The pit is drawn by `S_EV_FogSheet_01` meshes wearing
`M_HU_Deck_Goo_Translucent`, unlit translucent and so skipped as effects.
`shaders.py` explains at length why such materials cannot be *built* from a ucc
make -- but UT2004 already ships one that says the same thing, and `Actor.Skins`
overrides a static mesh's material per actor, so the actor is kept and wears
`FinalBlend'XEffectMat.goop.GoopFB'` instead. The material comes from the
component override, not the mesh, which is where the first attempt looked and
found nothing.

Only the liquid *surface* gets it, which took a second pass to get right. UT3
builds a goo pit from one horizontal sheet plus several vertical ones filling
the shaft -- all the same `S_EV_FogSheet_01` plane. The vertical ones are haze,
faded by depth-biased alpha, and they genuinely reach far above the goo: one of
Deck's spans z -2168..-120 against a surface at -1292. Our transforms reproduce
that exactly, but a solid FinalBlend turns them into bright green slabs stabbing
up through the floor. So `sheet_is_horizontal` limits the substitution to
sheets that are flat and level once rotated, which on DM-Deck is the 2 surface
sheets at z -1288 and -888, sitting on goo at -1292 and -892. The other 20 go
back to being skipped, for the reasons at the top of `shaders.py`.

**Phase 6e — collision flags. DONE.** `convert/meshes.py`.

Found because the goo surface was solid enough to stand on, which made the pit
untestable. The cause is general: UE3 states "you walk through this" as
`bCollideActors` on the actor or `CollideActors` on the mesh component, and the
converter read neither, while UT2004's StaticMeshActor defaults to solid on all
three of bCollideActors, bBlockActors and bBlockKarma. So every piece of
decoration UT3 lets you walk through became a wall: 728 actors on DM-Deck and
236 on DM-HeatRay now carry the flags UT3 gives them.

**Phase 6f — teleporters. DONE.** `convert/teleporters.py`.

The pairing needed no invention: both engines say it identically. A sender
names its destination in `URL`, the destination answers to that name in `Tag`.
DM-Deck's UT3 pair is `URL=RedeemME` against `Tag=RedeemME`; DM-Deck17, the
stock UT2004 version of the same map, does exactly that with `upstairsred`. UE3's
default `Tag` is the class name and is dropped rather than carried across as a
destination that does not exist.

The look does not survive: UT3 hangs a `PortalEffect` particle system and a
`TeleporterBaseMesh` off the actor as components, and UT2004's `Teleporter`
draws nothing at all on its own, so a converted teleporter is invisible. Rather
than invent something, this copies what DM-Deck17 does with the same map's
teleporters -- `teleporter-proc` wearing `FinalBlend'XEffectMat.Shield.RedShell'`
and `TelePorterbase` under it, both from packages UT2004 ships.

The offsets are measured **from the floor**, not from the teleporter, and that
distinction is the whole difference between working and not. DM-Deck17 stands
its teleporter 56.25 above the floor (its own CollisionHeight, corroborated by
the weapon base beside it at -305.63); a UT3 one stands 34 (the Translation on
`Default__UTTeleporter`'s base mesh, corroborated by the path nodes level with
DM-Deck's two, at 34 and 36). Both meshes hang below their pivot --
`TelePorterbase` spans -81..-56 and `teleporter-proc` -59..+71, read out of
`XGame_StaticMeshes.usx` -- so copying the actor-relative offsets buried all
25uu of the base underground. Floor-relative, the converted meshes land at
exactly DM-Deck17's heights: the portal +6..+136 above the floor, the base
-4..+21.

Only a teleporter that *sends* is drawn. A destination is somewhere you arrive,
and UT3 says which is which itself: the sender has a URL, the destination only a
Tag. The portal is emitted non-blocking, which DM-Deck17's is not: you walk
through it to use the teleporter, and a map where it landed in the doorway would
be unusable.

Reading those values needed a UE2 tagged-property reader, `tools/ut2props.py`
-- a research tool for looking at stock maps, not part of the pipeline. Note
that a UE2 export saved mid-state writes its execution stack before its
properties (RF_HasStack), which has to be skipped or every name comes out wrong.

**Phase 7 — CTF-FacingWorlds, and CTF. DONE.** `convert/objectives.py`.

Third map converted end to end, first team map. Flag bases map 1:1 --
`UTCTFRedFlagBase`/`UTCTFBlueFlagBase` onto `xRedFlagBase`/`xBlueFlagBase`,
which already carry their flag type, objective name and team shader in their
defaults. The only number needed is the height: both engines centre an actor on
its collision cylinder and the cylinders differ, UT3's navigation points being
50 high against `xRealCTFBase`'s 80 (XGame/xRealCTFBase.uc:36), so a base copied
straight over sinks 30uu into the floor. Team player starts already worked --
`UTTeamPlayerStart` was in the table and TeamIndex maps to TeamNumber, with UT3
eliding index 0, which is red in both engines.

Two things the map found:

- **A crash.** One of its opacity masks is stored as DXT5, which sent
  `bake_opacity` down the alpha decoder for the first time, and that decoder's
  interpolation weights were wrong: the endpoint pairs have to sum to the
  divisor -- 6:1..1:6 over 7 for the eight-alpha mode, 4:1..1:4 over 5 for the
  six-alpha one -- and it wrote (7-i) and (5-i), producing values past 255.
  Encoder and decoder now both come from `dxt.alpha_ramp` so they cannot drift.
- **The world limit.** FacingWorlds hangs its scenery 336,707uu out, needing
  666,372uu of void to enclose -- well past UE2's +/-262144, so the map would
  have been clamped and broken. `--shrink-backdrop` already handled this, but it
  is not a preference when the alternative does not work, so it now happens
  automatically: 463 backdrop meshes move into the skybox at 1:176 and the world
  brush drops to 15,680uu. `--keep-backdrop` forces the old behaviour for
  diagnosing.

**Phase 7b — the vanishing skybox. DONE.** `ut3conv.py`, `ut2/t3d.py`.

CTF-FacingWorlds built with no sky and no background at all, and nothing in the
conversion output said so: it reported a dome, a SkyZoneInfo room and 463
backdrop meshes moved into it, all correctly.

The room was at x=-335058, past UE2's 262144 limit, so the entire skybox was
clamped out of existence. The cause was ordering: the room is placed *clear of
every actor that stays in the level*, but which scenery was leaving had not been
decided yet, so it was placed clear of the very meshes about to be moved into
it. Settling the distant set first puts the room at x=-12960.

Three of the map's lights then remained stranded at 299,312uu -- they lit
backdrop that had just moved into the skybox. `T3DMap.out_of_world` now catches
anything past the limit before the t3d is written and reports what it dropped.
That is a backstop rather than a fix: whatever put an actor out there is the
real bug, but this map showed that shipping one produces a symptom (no sky)
with no trace back to a cause.

**A build note, from testing all three:** the BSP balance that works is
per-map. DM-HeatRay stack-overflows during lighting occlusion at the default 15
and needs 1; CTF-FacingWorlds glitches at 1 and needs the default 15. It is
worth trying both before concluding the geometry is wrong.

**Phase 7c — the purple backdrop. DONE.** `ut3/objects/material.py`,
`convert/textures.py`.

FacingWorlds' cliffs and ocean rendered in iridescent blue and magenta -- normal
maps drawn as diffuse. Two causes, both general:

- A `MaterialInstanceConstant` overriding only some parameters was short-
  circuiting its parent, so an instance that overrides `Normal` alone named a
  normal map and nothing else. The parent is now resolved too and the
  better-scoring texture wins. Fixes the cliffs, and gained Deck a texture as
  well; HeatRay unchanged.
- Where no parent has a diffuse either -- FacingWorlds' ocean -- the normal map
  is now refused rather than drawn, and the material takes the neutral
  placeholder. Grey beats psychedelic.

No normal map or cubemap now reaches any of the three packages.

**Phase 7d — geometry UT3 never draws. DONE.** `convert/meshes.py`.

Brown slabs hanging in FacingWorlds' sky, reported as translucent sky meshes
losing their transparency. They are not translucent -- they are not drawn at
all. `HiddenGame` on a UE3 PrimitiveComponent means the component is never
rendered in play, and this map keeps 21 such meshes purely to cast shadows, in a
group its author named "necris cloud shadowcasters". Because nothing draws them
their material is arbitrary: `Nebula_01` wears a stone floor texture, which is
precisely what turned up in the sky.

Components with `HiddenGame` are now skipped. Only FacingWorlds has any; Deck
and HeatRay have none.

Worth recording what the same check turned up about the rest of that skybox,
since the concern was general: its 442 actors carry 300 opaque and 161 masked
mesh elements and **no translucent ones at all**. The masked ones are foliage
clusters, which the separate-opacity-mask baking already handles as cutouts. So
there is no translucent-sky-mesh problem in this map to solve.

**Phase 7e — level-wide settings. DONE.** `ut3/objects/level.py`,
`convert/terrain.py`, `convert/lights.py`.

Two reports with one root: a converted map had no ZoneInfo unless it had
terrain, so every level-wide setting stayed on the LevelInfo at UT2004's own
defaults and had to be applied by hand.

- **KillZ.** UE3 keeps it on `WorldInfo` -- -3000 on CTF-FacingWorlds, -2000 on
  DM-Deck, -1554 on DM-HeatRay -- against UT2004's ZoneInfo default of -10000.
  Walking off the edge meant a very long fall onto the bottom of the world
  instead of a death. `KILLZ_None` already gives `Died(class'Fell')`, so no kill
  type is needed.
- **Ambient light.** It was only ever applied on terrain maps; elsewhere the
  converter printed it and left it to be typed in. Now every map gets a
  ZoneInfo, with `bTerrainZone` set only when there really is terrain.

Converting the ambient also turned up a real loss: **multiple SkyLights were
being reduced to the largest**. UT3 lights a scene with all of them, and
CTF-FacingWorlds has two contributing 12 and 20 -- so the map was converting at
20 where it should be 32, which is exactly the "a little dark" that was
reported. They now sum, with hue and saturation following the largest
contributor since hue does not average meaningfully. DM-HeatRay and DM-Deck have
one SkyLight each and are unaffected.

**Phase 8 — Warfare -> Onslaught. DONE (WAR-PowerSurge).**
`convert/onslaught.py`.

UT3 renamed Onslaught to Warfare and kept the classes, so cores and nodes
convert one for one and the power link graph converts as data.

- **The link graph** lives in `UTOnslaughtMapInfo.LinkSetups` as directed pairs
  of object references. UT2004's `ONSPowerLinkOfficialSetup` holds the same
  graph as `PowerLinkSetup { name BaseNode; array<name> LinkedNodes; }`,
  resolved against the actor's **Name** (`PowerCores[z].Name == ...BaseNode`,
  ONSOnslaughtGame.uc:238), so nodes are named for their objective -- WestTank,
  EastTank, Prime, MineNode -- and the setup is written in those terms. UT3's
  pairs are grouped by source. Every core and node gets an entry even with no
  links, which is what ONS-Tyrant's own setup does: the game only assigns where
  a name matches, so a node left out keeps the previous setup's links.
- **The countdown node.** WAR-PowerSurge's "Mine Node" is a
  `UTOnslaughtCountdownNode`, and the map already lists it in
  `StandaloneNodes` -- so "the mine node should be a countdown node" is stated
  by the map, not inferred. OnslaughtSpecials2 cannot express it as a placed
  class: `ONSCountdownNode` is a stub whose comment says the behaviour "moved to
  ONSPowerNodeSpecial and the link setups", and `CountdownTime` is a plain `var`.
  So it is an ordinary `ONSPowerNodeSpecial` plus an
  `ONSPowerlinkOfficialSetupSupplement` entry naming it.
- **Which core is red** UT3 does not say -- neither core carries
  `DefenderTeamIndex`. Placement order decides it, and the choice is reported.
- **Vehicles.** Manta/Scorpion/Hellbender/Goliath are the same vehicles under
  UT2004's internal names (ONSHoverBike/ONSRV/ONSPRV/ONSHoverTank) so those are
  exact; the Paladin has no UE2 counterpart and the shielded rocket turret
  becomes UT2004's plasma `ONSManualGunPawn`. Both are reported.
- `UTWarfarePlayerStart` was being dropped entirely -- 10 of PowerSurge's 90.

Syntax was checked against a real paste from ONS-Tyrant rather than guessed at.

**Phase 8b — node teleporters and the mine. DONE.**

- **Node teleporters.** `UTOnslaughtNodeTeleporter_Content` -> `ONSTeleportPad`,
  7 of them. No binding is needed: both engines place these near a node and let
  the node claim the nearest (`ONSPowerCore.uc:228`). UT3 stands its teleporter
  34uu above the floor (its FloorMesh translates down by that) while
  ONSTeleportPad's mesh has its pivot at the base, so the pad drops by 34.
- **The Tarydium mine, as decoration.** There is no UT2004 counterpart for the
  mechanic -- the only `ONSMine*` classes are the Mine Layer weapon, and
  OnslaughtSpecials2 has nothing either -- so only the geometry converts: the
  mine's `S_UN_Cave_SM_Crystal` and the processor's `SM_Processing_Plant`. The
  countdown node works on its own through the supplement regardless. Orb spawns
  are skipped, having no counterpart at all.

Getting those two meshes out found two general faults:

- **Component meshes inherited from the class were invisible.** A gameplay
  actor keeps its mesh on the archetype rather than the instance, so reading
  only the instance found nothing. Following the archetype chain recovered 90
  further actors on WAR-PowerSurge alone.
- **`mesh_is_effect` asked whether *any* element was an effect material**, so a
  mesh with one glowing part was dropped whole. The processing plant is eight
  elements of beams, pipes and doors plus one translucent shield panel. It now
  requires *every* element to be an effect, which is what the test was for --
  a beam or fog sheet is a single quad with a single procedural material.
  Skipped counts fall from 273 to 127 on PowerSurge, 208 to 201 on DM-Deck and
  175 to 167 on CTF-FacingWorlds; DM-HeatRay's light beams are unaffected.

**Phase 8c — Onslaught placement. DONE.** Nodes rest on what is under them
rather than at UT3's stated height (`PrePivot` is applied *before* `DrawScale3D`,
which is what made three earlier corrections all measure against a number half
what it should have been), vehicle factories rise 32uu so a spawning vehicle does
not drop through the mesh it sits on, and `--ambient-gain` lifts the SkyLight
into something UE2 can see.

**Phase 9 — reverb. DONE.** `convert/reverb.py`. A UE3 `ReverbVolume` becomes a
UT2004 `PhysicsVolume` carrying an I3DL2 room effect, matched against the 30
presets in `DirectX9/Include/dsound.h`. `UnAudio.cpp:139` is why it works: a
PhysicsVolume's `VolumeEffect` overrides its zone's reverb. `Priority=-1` keeps
it under anything the map places itself. Cheap, and audible.

**PostProcessVolume: not attempted, and not planned.** UE2 has no equivalent
stage, and nothing in a script build can create one.

**Phase 9b — terrain layers. DONE.** UT3 states a layer's tiling as
`MappingScale`; UE2 repeats every `UScale` *quads* (`UnTerrain.cpp:1874`), so
each layer derives its own per axis. Rotation is quantised to quarter turns,
because `ATerrainInfo::CalcCoords` builds its transform from Location and
TerrainScale alone — there is no rotation in it (`UnTerrain.cpp:1464`). UT3's
foliage becomes decoration layers at `--deco-density`, since UT3 says where
ground cover goes but not how much.

**Phase 9c — the radar map. DONE.** `convert/minimap.py` draws a hillshaded
top-down image from the terrain, which is what an Onslaught map wants for
`RadarMapImage`. Avoids the chicken-and-egg of needing a built map to screenshot.

**Phase 10 — VCTF. DONE.** `XGame.xVehicleCTFGame`, which is CTF plus the
vehicle factories already converted for Warfare. Worked first time.

**Phase 11 — batch. DONE.** `batch.py`. Converts, rewrites `EditPackages` to
name one map package, deletes its `.u`, runs `ucc make`, copies the result out,
then the next map. One package per build is forced by `UCC.exe` being PE32: a
build has ~2GB of address space and that has to cover every package
`EditPackages` names, because UCC still *loads* an already-built package to
resolve references. Seven map packages come to 434MB; all 62 would be ~3.7GB.

**Phase 12 — the editor pass.** Opening each converted map in UnrealEd, which is
where the real defects live. Everything below came out of that, and each one is
general rather than per-map:

- **Cross-package texture collisions.** ASE `*BITMAP` binds by leaf object name
  through `TObjectIterator<UMaterial>`, first match wins
  (`UnStaticMesh.cpp:680`) — no package qualification is possible. Two maps
  loaded together fought over 47 names, and a rock in one wore a texture from
  the other. Every generated texture now carries a 4-hex CRC32 of its package.
- **Single-mip DXT textures.** `UTexture::CreateMips` returns immediately for
  DXT (`UnTex.cpp:492`) and `Compress` bails unless RGBA8/P8
  (`UnTex.cpp:1000`), so the `.dds` has to carry UT3's own mip chain or the
  texture has exactly one level.
- **Brushes lost to T-junctions.** Closedness was tested by pairing edges, and a
  brush with a T-junction fails that while being perfectly closed. CTF-Coret
  lost two subtractive brushes and the recess they carve stayed solid rock.
  `surface_closes` sums face area vectors instead — zero on any closed surface,
  and indifferent to where the vertices sit.
- **`MainScale` was emitted and ignored.** Brush scale is baked into the
  vertices now, so a scaled BlockingVolume is the size it says it is. Coret had
  one blocking a doorway that could not even be selected in the editor.
- **Normal maps drawn as diffuse.** A definitive marker (`normal`, `_n`,
  `cubemap`) disqualifies a texture outright rather than scoring against it:
  `T_Base_Tile_DetailNormal` scored +100 for "normal" and −20 for "base", and 80
  slipped under the threshold that exists to refuse it.
- **Water.** UT3 draws it with a Phong translucent shader whose colour is all
  parameters, so it converts to nothing. A flat, level sheet whose material names
  a liquid and resolves to no colour map — or to nothing but a mask — gets
  `FinalBlend'UCGeneric.Glass.glass06_finalblend'`. 37 actors across 21 maps.
  All three conditions are load-bearing: CTF-Coret hangs
  `M_LT_Base_BSP_Glass_Water_01` on 56 window frames.
- **Relative Matinee tracks were not turned into world space.** A track anchored
  at the origin states its motion in the actor's own frame, so it has to be
  rotated by the placed rotation. Unturned, CTF-Vertebrae's lift descends,
  DM-RisingSun's travels sideways, and DM-Defiance's trains leave their rails by
  6718uu. 68 movers across 22 maps. DM-HeatRay is the exception — world
  coordinates in a track flagged relative — so the rule is confined to tracks
  whose first key is the origin.
- **UT3's builder brush was being built as solid.** It is the brush with no
  `CsgOper`: `ABrush` defaults it to `CSG_Active`, so a real brush always states
  `CSG_Add` or `CSG_Subtract` and the template states nothing. Exactly one per
  map, 66 of 66. The old test looked at the model's name, which caught 40.
- **Component transforms were dropped.** A UE3 `StaticMeshComponent` carries a
  transform relative to its actor; UT2004 has one per actor, so they compose.
  The actor's scale ends up on the wrong side of the component's rotation, and
  112 of the 202 affected actors carry a non-uniform `DrawScale3D` — exact only
  because every component rotation in the stock maps is a whole quarter turn,
  which permutes scale factors rather than shearing them. 218 actors, 9 maps.
- **A MaterialInstanceConstant's explicit diffuse override now wins.** The
  parent only carries the default left in the slot, and those defaults are
  engine placeholders — DM-HeatRay's rubble resolved
  `Engine_MI_Shaders.T_Diffuse`, a 32×32 flat grey that won the name contest
  *because* it is called "T_Diffuse". 84 materials across 27 maps, including
  cliffs that had been painted with grass.

**Phase 13 — UDK, as a spike.** `ut3/package.py`, `ut3/props.py` and
`ut3/objects/model.py` read TOXIKK's UDK maps (package version 868) as well as
UT3's 512, behind version gates. Five format differences: four extra header
offsets, no `ComponentMap` in the export table, `FLightmassPrimitiveSettings`
plus a ruleset `FName` on every `FPoly`, an enum name in every `ByteProperty`
tag, and a one-byte `BoolProperty`. `BL-Dekk.udk` converts to 149 brushes, 197
volumes, 67 lights, 110 path nodes and 9 reverb volumes.

Static meshes did not convert, and in a UDK map that is most of it, so this was
left as a proof that the reader generalises rather than a feature. Phase 13b
took it further.

**Phase 13b — UDK assets. DONE (geometry and textures).**
`ut3/objects/staticmesh.py`.

TOXIKK ships the UDK its maps were built with
(`TOXIKK/Binaries/Win64/UDK.exe`), which reopened this. Two things came out of
it, and the useful one was not the editor.

*What the editor is worth, measured.* `UDK.com` runs commandlets headlessly
under system Wine, needing none of what the GUI needs, so `UnrealEd.BatchExport`
is available. (The GUI editor runs too -- see "Running the UDK editor under
Wine" below -- but nothing in this pipeline needs it.) It is not enough to build a pipeline
on. `StaticMesh OBJ` exports geometry and UVs but writes no `usemtl` or `g`
line, so a multi-element mesh arrives as one undifferentiated blob with its
material assignment gone; `StaticMesh T3D` exports tagged properties only, no
geometry; the FBX exporter faults and leaves 0-byte files; and `Texture2D BMP`
and `TGA` report "Exported ... to <path>" for every texture and then write
nothing at all, the UE3 texture exporters refusing compressed formats and
UnrealEd deleting the failed output. DDS and PNG have no exporter registered.
Its lasting use is as the oracle Section 1 describes: the OBJ export is what
the native reader was checked against below.

*Reading the packages natively, which is what actually worked.*

- **Texture2D at 868 needed no change.** All 29 textures in `te_Lab.upk` read
  with every mip present at full resolution, through the existing reader
  including its inline-LZO-mip handling. The pixels were checked by writing
  DDS and looking at it. The claim above that Texture2D is a different format
  at 868 was simply wrong.
- **StaticMesh at 868 needed four fixes**, each a localized insertion rather
  than a new format, and all gated on the package version so the UT3 path is
  untouched: a 24-byte box holding the kDOP tree's root bound between
  `BodySetup` and the kDOP arrays -- Min and Max only, without the validity
  byte a tagged `FBox` carries, which is why 24 and not 25; 16 bytes between
  `Version` and `LODCount`;
  a `TArray<FFragmentRange>` plus a trailing byte on every element, which
  leaves every buffer after it **unaligned**; and a colour vertex buffer that
  omits its array header entirely when the mesh has no vertex colours.
- **The UV block starts at offset 8, not 12** -- UDK writes two packed normals
  where UT3 writes three. This one does not fail, it lies: on a two-channel
  mesh, reading at 12 returns the *lightmap* UVs instead of the diffuse ones.

Verified against UDK's own OBJ export as ground truth. All four `te_Lab` meshes
match exactly on vertex and triangle count (494/368, 444/380, 81/118, 52/34),
UVs agree (U identical, V flipped, which is the OBJ exporter's convention) and
so do positions (the OBJ writes X, Z, Y). Each element also resolves a real
material reference, which the OBJ route cannot supply at all.

On BL-Dekk that is **4,745 of 4,781 mesh placements parsed, none failed**, for
1,990,658 triangles placed -- DM-Deck scale. The 36 that do not resolve name
editor-only packages (`EditorMeshes`, `MapTemplates`, `EngineVolumetrics`).
`tests/test_meshes.py` still passes on the UT3 maps, UV-set checks included.

*A correction worth keeping, because it cost a detour.* Content `.upk` files
were briefly thought to have a broken tagged-property layout, on the strength
of 457 of 462 exports failing to parse. They do not: that was `read_properties`
being called at offset 0 instead of `read_object_properties`, which probes for
where the list starts. Through the real entry point 436 of 462 parse, the rest
being `Package` exports that carry no property list.

**Phase 13c — materials that name their own channels. DONE (resolution).**
`ut3/objects/material.py`.

Every `MaterialInstanceConstant` in `te_Lab` resolved to the same unrelated
texture. The cause is not a parsing fault but a different way of authoring:
**TOXIKK materials mostly ship no diffuse map at all.** They pack albedo,
specular and gloss into one texture, name the parameter after its own channel
layout -- `Mask 1 (R=Diffuse, G= Specular, B=Gloss)` -- and tint the result
with a `Base Color` vector parameter. Decoding the red channel of one confirms
it: `MF_T_Wall_01_M` is a clean greyscale wall panel, lines, bolts and grime.

The scoring table was throwing exactly that texture away -- "mask", "spec" and
"gloss" all count against it and the name ends `_M` -- so the search fell
through to the parent and took whatever default it found. A parameter name that
declares its own channels is far better evidence than any heuristic, so
`declared_diffuse_channel` now settles the choice outright, ahead of both the
scoring and the parent. `resolve_base_color` walks the instance chain outwards
for the tint, leaf first, since the usual shape is a leaf that restates only
the colour over a parent that holds the texture (`MF_M_Wall_01_Dark_INST` at
0.146 grey over `MF_M_Wall_01_INST`). `material_albedo` returns the two
together with the texture.

Scope, measured rather than assumed: this is an asset-authoring style, not a
map-wide one. Of BL-Dekk's 63 distinct materials **59 resolve to an ordinary
diffuse map, 2 use the declared-mask style and 2 fail** -- it is the te_Lab set,
used by other TOXIKK maps, that is mask-heavy. `tests/test_textures.py` and
`tests/test_meshes.py` still pass on the UT3 maps.

**Phase 13d — baking the tint. DONE.** `ut2/dxt.py`, `convert/textures.py`.

What resolution yields -- texture, channel, tint -- has to become one ordinary
UT2004 texture, so the channel is decoded and multiplied by the Base Color on
the way out. Three parts:

- `encode_dxt1_tinted` is the first thing here that writes colour blocks rather
  than copying them, and it can be short because the hard part of DXT1 --
  fitting a line through a cloud of colours -- is already given: every texel in
  a block is one colour at a different brightness, so the line *is* the tint.
  Endpoints are the block's darkest and brightest texel scaled by it, and a
  texel takes whichever of the four ramp entries its brightness is nearest.
  Hue survives exactly, give or take the five bits DXT1 stores blue in.
- **The tint is part of a texture's identity, not a property of it.**
  `TextureSet` keyed by texture alone, so one mask drawn at two tints collapsed
  into one; it now keys on (texture, channel, tint). `MF_T_Wall_01_M` exports
  twice on te_Lab, at 0.146 and 0.798 -- the same wall panel dark and light.
  `convert/meshes.py` solved the same shape in Phase 5b by keying a mesh on its
  material set.
- A texture the material names as its own albedo channel is exempt from the
  name-based refusal. These are called `..._M` *because* they are masks, and
  `NOT_DIFFUSE` would throw away the one texture the material actually draws.

Baking runs per mip level, ahead of the opacity bake, and hands it DXT1 -- which
is what that pass already expects, so a masked cutout still composites on top.

**Phase 13e — two faults a multi-package map exposes. DONE.**
`convert/textures.py`, `convert/meshes.py`, `ut3/objects/material.py`.

Reported as `StaticMeshActor_2116` wearing a brown texture, and "a lot of
meshes have that wrong texture" -- 111 of BL-Dekk's 296 meshes were painted
with `MF_T_Mud_01_D`. Two independent causes, neither of them UDK-specific:

- **Cache keys held an index without the table it came from.** `TextureSet`
  keyed materials by `(is_import, index)` and `MeshSet` by the same plus its
  overrides. A `PackageIndex` only means something relative to the package
  holding the table, so index -880 in one content package and -880 in another
  are unrelated objects -- and the first material to claim an index answered
  for every other package's. A cooked UT3 map resolves everything through its
  own table and the collision cannot arise; a UDK map draws on dozens of
  `.upk` files. `ObjRef` already carries its package, so the key is now
  `(pkg.path, is_import, index)` and no call site had to change. The same edit
  fixed a latent mismatch in `MeshSet.name_for`, which keyed on the raw
  overrides tuple where `add` keyed on a stringified one, so any lookup with
  overrides missed.
- **A pixel heuristic was overruling a material's own statement.** The
  relief-bake test refuses a near-white, desaturated `_D` as a per-mesh bake,
  and its own docstring says it is only for candidates that names cannot
  separate. It ran first regardless. TOXIKK's panels are bright desaturated
  sci-fi metal: `T_HighTechPanels_D` measures 0.739 brightness at 0.008
  saturation -- further into the bake region than WAR-PowerSurge's genuine bake
  -- while the parameter holding it is called `DiffuseMap`. An override that
  names the diffuse slot now settles it before the pixels are read.

  The UT3 case it exists for is untouched *by construction*: WAR-Serenity's
  `M_UN_Rock_SM_Cliffs01_MI_SideA_05` names `Normal`, `DetailNormal` and
  `ShadeMap`, none of which is a diffuse slot, so it still falls through to the
  pixel test and still lands on the tiling rock rather than the bake. Checked
  against the map rather than assumed.

Meshes wearing the mud texture: **111 -> 1**, and that one really is mud.

**Phase 13f — a reference resolved against the wrong package. DONE.**
`ut3/resolve.py`.

Found converting BL-Foundation, which reported 327 actors with no mesh against
BL-Dekk's 23. Two thirds were real:

- **`PackageIndex.resolve` paired a ref's export with the caller's package.**
  `if ref.is_export: return pkg, ref.export` -- and `ObjRef.export` correctly
  reads `ref.pkg`, so the *export* was right and the *package* returned with it
  was not. A reference does not have to come from the package passed in:
  following an archetype chain reads properties out of whatever package defines
  the archetype. Resolving a `UA_Lights_01` export index against BL-Foundation
  handed back that map's export 463 -- an `ExponentialHeightFogComponent` where
  a lamp mesh should have been, 48 actors' worth. `ref.pkg` is the authority
  now, with `pkg` as a fallback. UT3 maps are unaffected: everything there
  resolves through the one cooked map package, so the two were always equal.

  That makes three bugs in this family, all invisible until a map drew on more
  than one package: the cache keys and the override package in Phase 13e, and
  this. The rule they all break is the same one -- **a PackageIndex means
  nothing without the table it came from.**

- **Content the game does not ship.** UDK's own `Engine/Content` --
  `EditorMeshes`, `EngineVolumetrics`, `EngineMeshes` -- lives in the UDK
  installation, and BL-Foundation places 118 `EditorMeshes.TexPropPlane`, 9
  `EngineMeshes.Sphere` and 116 `EngineVolumetrics` light beams. Unpacking the
  UDK zip somewhere is enough; installing it over the game stops TOXIKK
  launching. `UT3CONV_EXTRA_CONTENT` (PATH-separated) adds trees to the index,
  and `batch.py` fills it in from `~/TOXIKK_UDK/Engine/Content` when that
  exists. The light beams and fog sheets then convert far enough to be
  recognised as the unlit translucent effects they are and skipped on purpose,
  rather than counted as missing.

Actors with no mesh: **327 -> 39**, and those 39 name `UA_Buildings_B_01`,
which ships in neither the game nor the UDK zip.

**Phase 14i — static parameters, and the tint nobody was applying.** Two
findings from one reported surface on BL-Dekk,
`M_TechPanel_simple_FloorPanels_RUNTIME_MainHallLow_INST`, checked against what
the UDK browser shows for it.

*Static switch parameters are readable after all.* Phase 14e recorded as a
limitation that these instances say `bHasStaticPermutationResource=True` and
carry no `StaticParameters` tagged property, so every static branch was
evaluated at the master's default. UE3 writes an `FStaticParameterSet` into the
native data *after* the tagged properties, and it can be found: the switch
array is `count` then `count` of

    FName (8) | Value (4) | bOverride (4) | FGuid (16)

with the component-mask array (stride 44) immediately after it, which is what
makes locating it safe -- a run of bytes that merely looks like one array
almost never has a second, differently shaped one right behind it. The layout
is confirmed field by field against the browser: 33 switches and 4 masks on
this instance, `bUseBoxCorrectedCubemap` and `bUseCubemapCorrection` the only
two it overrides, `bUseDiffuseMap` and `bUseDetailSpecularMap` true but
inherited, `bUseSnow` and `bUseDirt` false. All 50 values agree once the parent
chain is walked.

It changes no texture resolution on any of the three maps -- the masters'
defaults happened to agree everywhere it mattered. Worth having anyway: the
branches are now evaluated correctly rather than correctly by luck, and a
static switch decides which half of a material UE3 compiles at all.

*The tint was the real fault.* `resolve_base_color` matches "Base Color", which
is the packed-mask idiom Phase 13c found. This material multiplies its diffuse
map by a `DiffuseColor` parameter instead, and nothing was applying it. **14 of
BL-Dekk's 31 material instances carry a non-white one**, and two are not
subtle: its landing pools are (0.04, 0.073, 0.243), a deep blue drawn from a
grey texture, and a corrugated floor is (0.154, 0.178, 0.204).

`diffuse_tint` walks the instance chain leaf-first for a vector parameter named
like a tint -- and requires it to be one the graph actually *reads*, since a
chain of instances accumulates parameters later revisions stopped using.
`reachable_parameters` shares the live-branch walk `_reachable_textures`
already does for exactly that. The result becomes a `ColorModifier`, which is
what Phase 14b built the machinery for.

**The two colour paths must not both fire, and the data said so before the
code did.** `constant_colour` answers a material whose colour input folds whole
-- which needs no texture in it -- and `diffuse_tint` answers one that draws a
texture and multiplies it. On DM-Deck *every* material with a tint also folds,
because the fog sheets and light beams state their colour as the same `Color`
parameter both routines would find; applying both would square it. So the tint
is only consulted when the fold declines. Pure black is refused as well, for
the reason 14h refuses it: it erases the surface, and a material that tints
something black does it through a mask this cannot follow.

BL-Dekk goes from 6 opaque materials with something to say to 67, and 31
ColorModifiers. DM-Deck and DM-HeatRay are untouched, which is the check that
matters.

*And a third normal map the names miss.* With the tint pass exercising more
materials, `_is_normal_map` refused `SF_T_ConcreteDetail_D_N` too -- another
name ending `_D_N` rather than `_n`.

**Phase 14j — a heightmap drawn as light.** Reported as `StaticMeshActor_4839`
on BL-Dekk having "a rainbow colour", and read correctly as an effect layer
rather than the diffuse. It was:

    Shader T_HighTechPanels_D_f1d0SH
        Diffuse              = T_HighTechPanels_D           (the panel, neutral grey)
        SelfIllumination     = SF_T_GroundHeightmaps_Glow   <- a heightmap
        SelfIlluminationMask = SF_T_GroundHeightmaps_Glow

Phase 14f gave lit opaque materials a glow, and `resolve_emissive` will walk
the EmissiveColor chain to whatever texture it reaches. For
`M_HighTechPanel_EdenParticles_INST` that is a ground heightmap, and a
heightmap blended over a wall as self-illumination is exactly the iridescent
sheen Phase 7c refuses normal maps for.

Two fixes, and it is worth noting they independently catch the same five
materials -- which is the confirmation that neither is a guess:

* **The material says so.** `bUseEmissive` is *False* on all five. Phase 14i is
  what makes that readable, and it is a statement rather than a heuristic, so
  it outranks the graph walk -- which reaches the texture through a path the
  switch does not gate. `resolve_emissive` now returns nothing when a material
  declares it does not glow.
* **The texture says so too.** `_unusable_glow` already refused a featureless
  image and an unbakeable format; it now also refuses one whose name
  disqualifies it as colour, one that reads as a tangent-space normal by pixels,
  and one whose name says "height". `score_texture_name` has no opinion on
  heightmaps because a heightmap never turns up as a *diffuse* candidate -- as
  a glow it does.

BL-Dekk: 5 bad glows gone, and `SF_T_GroundHeightmaps` no longer reaches the
package at all. DM-Deck and DM-HeatRay: 0 materials affected either way, which
is the check that matters -- 40 and 78 glows respectively, none of them
declaring bUseEmissive False and none resolving a non-colour map.

**Phase 14k — a tint that turned walls translucent.** Reported as BL-Dekk's
new materials looking right but "a lot of them have transparency", with layers
popping past each other as the camera moved. Not an engine limitation: every
`ColorModifier` this converter emits was doing it.

`ColorModifier` defaults `AlphaBlend=True` and `RenderTwoSided=True`
(Engine/ColorModifier.uc), and nothing was overriding them. The render
interface ORs both into the pass, and `AlphaBlend` rewrites a surface still at
ONE/ZERO into SRCALPHA/INVSRCALPHA (D3D9MaterialState.cpp:1735) -- so applying
a *tint* to an opaque wall makes it alpha-blended. UE2 sorts translucent
surfaces per actor, which is exactly the popping: two overlapping walls swap
order as the camera moves.

Phase 14i is what made this visible, by giving 67 of BL-Dekk's opaque materials
a tint and therefore a ColorModifier. Both flags are now stated False on every
one; where a surface really is blended or two-sided, the FinalBlend above it
says so. The effects are unaffected either way -- `ApplyFinalBlend` runs first
and leaves the blend at something other than ONE/ZERO, so the override could
never fire there.

28 ColorModifiers on BL-Dekk, 14 on DM-Deck, 2 on DM-HeatRay, all three
rebuilt and every one now carrying `(AlphaBlend, RenderTwoSided) = (False,
False)`.

**Phase 14l — two colours chosen per pixel.** Reported as
`StaticMeshActor_2410` wearing `M_HighTechPanel_Pipes_Red_INST`, "a fancy
material", appearing in UT2004 as a flat texture. It was flat: the pipe was
getting its red tint and nothing else.

TOXIKK paints a panel with *two* colours picked per pixel by one channel of the
diffuse map -- `DiffuseColor` for the body, `DiffuseColor2` for the trim,
`DiffuseColorMaskChannel` naming the channel that chooses, `bUseDiffuseColor2`
saying whether it applies at all. **32 of BL-Dekk's materials do this**, a
third of the map, and none of DM-Deck's -- an asset-authoring style, like the
packed masks of Phase 13c. Multiplying by `DiffuseColor` alone turns the pipe's
pale trim red and loses it; on the floor panels it is starker, warm white
against near-black.

Reading the mask channel is Phase 14i's parser again: the
StaticComponentMaskParameter array immediately after the switches, already
parsed to confirm the switches were where they looked. The pipes select **A**
(their DXT5 alpha is the mask) and the floor panels **G**.

*Baked, not built.* UE2's `Combiner` can blend two materials by a mask, but its
mask is an alpha channel and this one is arbitrary, and the pieces would come
to five objects and three texture stages for one surface. Instead the result is
computed per pixel -- `rgb * lerp(colour one, colour two, mask)`, in linear
space with one conversion at the end -- and written as an ordinary texture. 12
distinct (texture, colour pair) combinations across the map collapse to 11
files, and because it lands in the mesh's own material through the ASE the
actors need no `Skins` at all: `StaticMeshActor_2410` now carries none.

That needed a general DXT1 encoder. `encode_dxt1_tinted` from Phase 13d can be
four lines of maths because its block colours are collinear by construction --
one hue at varying brightness. These are not: a block straddling the trim holds
red and near-white at once, so `encode_dxt1_rgb` finds each block's line from
its darkest and brightest texel. Alpha is deliberately dropped, which is right
twice over: every material doing this is opaque, and on the pipes the alpha *is*
the mask, so keeping it would leave the shape of the trim in a channel nothing
reads.

The pipes come out red-bodied at a mean of (155, 46, 9) with the trim intact,
their blue siblings at (8, 72, 163). `diffuse_tint` stands down wherever a
blend was baked, or the body colour would be applied twice. DM-Deck and
DM-HeatRay: zero textures recoloured, and identical output.

**Phase 14m — UDK ambient sound levels.** Reported as TOXIKK's ambient sounds
being too loud against UT3's. They were, and not by a little: every one of
BL-Dekk's 112 came out at `SoundVolume` 255 with a radius of 707, against a
real 0.275 and 261 for the first of them.

UDK's `AmbientSoundSimple` states its levels under names of its own, as plain
floats, where UT3 states each as one `RawDistributionFloat`:

    UT3 512   MinRadius / MaxRadius   VolumeModulation   PitchModulation
    UDK 868   RadiusMin / RadiusMax   VolumeMin/Max      PitchMin/Max

Reading only UT3's names meant every value fell back to its default -- volume
1.0, hence 255 for all 112, and `sqrt(400*5000)/2` for the radius, hence 707
for all of them too. Loud, and audible from nearly three times too far. The
mean of a min/max pair is the same reading `distribution_value` already takes
of a uniform distribution, so both paths agree about what a range means.

*And the slots scale it again.* Each `SoundSlot` carries a `VolumeScale`, a
`PitchScale` and a `Weight` giving its share of the random draw, multiplying
whatever the actor states. Only 9 of BL-Dekk's 112 are not 1.0, so it moves the
median not at all -- but the one that made this visible goes from 0.55 to
0.275, which is the difference between wrong and right for that sound.

    volume min/median/max        radius median
    DM-Deck        51/115/217        150      (unchanged)
    DM-HeatRay    122/166/255        150      (unchanged)
    BL-Dekk        64/210/242         74      (was 255/255/255 at 707)
    BL-Foundation  64/229/229         61      (was 255/255/255 at 707)

The UT3 maps are untouched, which is the check that matters.

*What is left is not a bug.* TOXIKK authors its ambients louder than UT3 does
-- 0.825 is the commonest value in BL-Dekk against UT3's 0.45..0.65 -- and
neither engine's master mix transfers. `--sound-gain` is the dial for that, and
`batch.py`'s per-map `EXTRA_FLAGS` is where a standing value would go.

**Phase 14n — a false property list beating the real one.** Reported as
`StaticMeshActor_509` on BL-Foundation being too big or in the wrong place. It
was in the wrong place: at the world origin, an 11,260-unit city block dropped
on the middle of the map. Its real Location is (16258, 14805, 3408).

An export's tagged property list starts after whatever its class serializes
natively, so `read_object_properties` finds the offset by trying each one until
a list parses cleanly to its None terminator. It swept **all aligned offsets
first and the unaligned ones after**, on the stated grounds that UT3 always
aligns. That order is the bug: a false positive later in the export beats the
true start earlier in it. TOXIKK's StaticMeshActors carry a 26-byte native
prefix, and `StaticMeshActor_509` also parses at 236 -- so the aligned 236 won,
and with it went the Location, Rotation and DrawScale3D that live in the real
list at 26.

Earliest wins now, aligned or not. Measured across five maps:

    DM-Deck        27557 exports,   0 change
    DM-HeatRay     28400 exports,   0 change
    BL-Dekk        18232 exports, 368 change   (363 recover a Location)
    BL-Cube         1841 exports,  47 change   ( 46 recover a Location)
    BL-Foundation  32434 exports, 505 change   (504 recover a Location)

**The UT3 maps do not move at all**, which is the check that matters -- their
lists really are aligned, so scanning from zero finds the same offset.

A handful of exports read *fewer* properties afterwards, and every one is a
correction rather than a loss. `CameraActor` is the example: the aligned scan
landed at 108, inside the `CamOverridePostProcess` struct, and returned its
`bOverride_*` flags as though they were the actor's own, ending at 766 of a
906-byte export. The new scan starts at 26, returns
`CamOverridePostProcess, DrawFrustum, MeshComp, Location, Tag`, and ends at 906
-- exactly the export size.

Every StaticMeshActor in all three TOXIKK maps now carries a Location: 2772 on
BL-Foundation, 4732 on BL-Dekk, 191 on BL-Cube, none missing.

**Phase 14o — BL-Artifact, and a mesh with no LOD stride.** The map converts
whole: 0 brushes (it is built entirely from static meshes, 105 volumes aside),
168 meshes, 1,738 actors placing 2.27M triangles, 84 lights, 89 backdrop meshes
moved into the skybox at 1:224.

One mesh would not read. `terrain_sheets_polySurface12` is 12MB and came back
`None`, which cost the map a terrain sheet. UDK normally writes sixteen bytes
between `Version` and `LODCount` -- every mesh in four other maps does, and
Phase 13b added the skip for exactly that -- and this one writes none. Nothing
in the class says which, and there is no version to key on: it is the same
package as the meshes that do.

So the LOD table is found rather than strided to. It announces itself far more
strongly than any offset could: a small LOD count, followed by a whole LOD that
parses into elements whose indices all address its own vertices. The expected
position is still tried first, so nothing that already worked changes; the
search starts back at the kDOP tree because the difference can be negative --
here it is *minus* sixteen. `terrain_sheets_polySurface12` reads as 7,597
vertices and 14,378 triangles, and its triangle count matching the kDOP
collision count exactly is the confirmation that the offset is right.

That is the fourth stride in this reader replaced by something self-validating,
after the header GUID, the export ComponentMap and the FPoly tail. Same lesson
each time: a stride learned from the packages to hand is a guess about the ones
that are not.

    DM-Deck        125 meshes, all read and valid   (unchanged)
    DM-HeatRay     205 meshes, all read and valid   (unchanged)
    BL-Artifact      7 meshes, all read and valid   (was 6, one unreadable)

*Two actors legitimately have no Location*, which is worth recording because
Phase 14n was the opposite case. `StaticMeshActor_834` and `_835` parse cleanly
from 26 to exactly their export end with no Location in the list, and their
meshes are authored in world space -- `terrain_sheets_polySurface16` spans
(2717, 296, 558) to (3917, 1700, 772) rather than sitting around its own
origin. Placing them at the origin is correct.

*Worth watching in the editor:* 2.27M triangles is the heaviest map converted
so far, and the map has no SkyLight, so it gets no ambient at all unless
`--ambient` is given. `batch.py` has no flag for it yet.

**Phase 14p — CC-Citadel, and TOXIKK's second prefix.** The largest map
converted from any game so far: 412 brushes, 311 meshes, **5,735 actors placing
3.0M triangles**, 1,297 lights, 299 ambient sounds, 32 player starts of which 16
are team-assigned. 16,964 package references, none unresolved.

*Its KillZ is positive, and that is correct.* `WorldInfo.KillZ` reads 6025 with
a `StallZ` of 15300, where every other map converted so far is negative. It is
not a misparse: the map is authored high in Z -- its PlayerStarts sit at
10027..10603 and its path nodes at 6712..14251, so nothing playable is below the
line. Maps get built up in the air in the editor and this is what that looks
like. Worth writing down because a positive KillZ looks exactly like the kind of
sign error that has bitten this converter before.

*`CC-` now maps to `DM-`.* TOXIKK's prefixes are its game modes -- BL is
Bloodlust, a deathmatch, and CC is Cell Capture, a team objective mode. Neither
mode's objectives convert, and a map left under a prefix UT2004 does not know
appears in no gametype's list at all, so both land on `DM-`. For CC that is more
than a fallback: its team-assigned PlayerStarts are exactly what UT2004's Team
Deathmatch wants. **Assault is the better target for CC maps** -- the mode
matches, and `UT2k4Assault` is installed -- but it needs objectives that nothing
currently converts, so it is left for later rather than half-done.

*Ambient 32*, matching BL-Foundation: with 1,297 placed lights, more than any
other converted map, most of its own lighting survives and it needs no more fill.

**Phase 14q — BL-Ganesha, and three maps with no sky.** A jungle temple map:
7 brushes (it is built from meshes), 133 meshes, 1,693 actors placing 1.5M
triangles, and only 12 lights -- **nine of which the mapper set to zero
brightness**, which is stated in the map and not a misreading. Three live lights
and a Sunlight for a daylight map, everything else baked, so it takes
`--ambient 96` like BL-Cube.

*It had no sky at all, and neither did two others.* `SKY_NAME_HINTS` was
`("_sky_", "skydome", "_dome", "skybox")`, which catches UT3's
`S_UN_Sky_SM_SkyDome05` and TOXIKK's `sm_skybox` -- but three of TOXIKK's six
maps use a **sphere**: `SM_SkySphere` on BL-Ganesha and BL-Cube, and
`SM_SkySphere_4UVChannels` on BL-Foundation. None matched, so no SkyZoneInfo was
built and the sphere stayed in the level as ordinary geometry: 4,096uu of mesh
at DrawScale 40 is 163,840uu across inside a 72,835uu world, so most of it fell
outside the void and drew nowhere, and what was inside sat past the far plane.

The hint added is `"skysphere"`, not `"sky"`, and the difference matters.
Measured across eight maps, plain `"sky"` changes nothing on any UT3 map -- but
it also matches `SM_ShaneSky_01`, which BL-Artifact and CC-Citadel each place
*alongside* the `sm_skybox` they already use successfully. `find_sky_meshes`
returns the largest first, so ShaneSky's 106,516uu radius would take the dome
away from a skybox that works today, and nothing in the data says which of the
two the author meant. The narrow hint fixes the three maps that are broken and
leaves the two that are not alone.

All three rebuilt and now carry a SkyZoneInfo; DM-Deck and DM-HeatRay still pick
`S_UN_Sky_SM_SkyDome03` and `S_UN_Sky_SM_Dome01` exactly as before.

**Phase 15 — a third UE3 build: Gears of War Reloaded. READS, DOES NOT YET
CONVERT.** `WarGame/CookedPC/Maps/MP_Maps/MP_Courtyard.war`, package version
835, **licensee 76** -- the first licensee-modified build this reader has seen,
against UT3's 512 and UDK's 868, both licensee 0.

The format work is done and the package reads completely: 6,438 names, 1,041
imports, **30,936 exports**, 262 Polys, 852 StaticMeshes, 1,165 Texture2Ds.
Four things had to change, and three of them were version thresholds that had
been guesses all along.

* **LZ4.** Compression flags `0x20`, where stock UE3 has COMPRESS_BiasSpeed and
  this reader knew only ZLIB and LZO. Everything around the codec -- the chunk
  table, the block framing -- is unchanged, so the codec is the whole
  difference. `liblz4` is already on the system and goes in through ctypes
  exactly as `liblzo2` does.
* **The header's extra offsets.** UDK carries four more offsets after
  `depends_offset` and UT3 does not; the gate was `version >= 584`, a guess
  from having only those two builds. 835 does **not** carry them, so the GUID
  was read 16 bytes late, no chunk table was found, and `read_range` handed
  back raw compressed bytes as if they were the name table. Replaced with a
  probe: the first generation restates the export and name counts the header
  has just given, which lines up only when the GUID starts where it is being
  read from. The header checks itself, at any version and any licensee.
* **The export ComponentMap.** UT3 keeps one, UDK does not, and the gate was
  `version < 639` -- another guess. 835 *does* keep one, so the map's first
  class default object had its component name (`ParticleSystemComponent0`)
  read as a net-object count and the table fell apart 17 entries in. The table
  has a known length, so it can check itself: exactly one reading consumes
  `export_offset .. depends_offset` to the byte. On MP_Courtyard that is
  2,271,328 bytes for 30,936 exports, and only the ComponentMap reading lands
  on it.
* **The FPoly tail.** UT3 has nothing after LightingChannels, UDK has a
  nine-field FLightmassPrimitiveSettings plus a ruleset FName, and 835 has a
  *seven*-field version and no name -- 28 bytes. A threshold cannot separate
  three shapes when the odd one out is in the middle, so each candidate is
  tried and the one that consumes the export exactly wins. Verified on all
  three builds: 503/503, 358/358 and 262/262 Polys exports read.

The pattern is worth stating on its own: **every version threshold in this
reader was a guess made from two data points, and the third build broke all
three of them.** Each is now a probe against a length or a restated count the
format already carries. That is strictly better than a threshold even for the
builds already working, because it cannot be wrong about a build nobody has
tried.

*What the map converts to today, which is not much.* 492 polygons and 82
volumes, 0 brushes (86 brush actors resolve no model), 0 lights, 0 player
starts, no static meshes at all. The reason is that Gears cooks its actors
differently:

* **`StaticMeshCollectionActor`** -- 34 of them holding 3,381
  StaticMeshComponents between them, which is where the whole map is.
  `convert_actors` looks for StaticMeshActor and finds 16. The transforms are
  readable: the actor's tagged properties end at 500 bytes and the remaining
  6,400 are exactly 100 FMatrices, one per component, the first decoding to a
  clean rotation and a translation of (-517.001, -2895.721, 491.111). What is
  not yet cracked is where a *cooked* component's StaticMesh reference lives --
  `read_object_properties` finds no property list on one at all.
* **`StaticLightCollectionActor`** does the same for lights.
* **`WarTeamPlayerStart`** and `WarTeamPlayerStart_Wingman` (46 between them)
  are not in the PlayerStart table.

So this is a working reader for a third UE3 dialect, not yet a converted map.

**Phase 17 -- Gears converts. Four more thresholds fall.** MP_Courtyard now
comes out with 669 static meshes placing 2,513,028 triangles, 93 lights and 44
player starts, where Phase 15 left it at 12 actors, no lights and no starts.

*The collection actors.* Gears cooks scenery in bundles: 34
StaticMeshCollectionActors holding 3,362 StaticMeshComponents, and one
StaticLightCollectionActor holding all 118 lights. Both are laid out the same
way -- an array of component references in the tagged properties, then one
FMatrix per entry filling the rest of the export -- so `convert/collections.py`
walks them once and hands each component to the existing emitters shaped like
an actor. Nothing in meshes.py or lights.py had to learn about collections
beyond where its input comes from.

Each matrix decomposes exactly: row lengths are the scale, normalised rows the
rotation, fourth row the translation. Round-tripping the recovered rotator back
through `rotation_matrix` on all 3,362 components reproduces the original basis
to 5e-07. The matrix is the *whole* transform, so the component's own
Translation/Rotation/Scale are stripped before the emitter folds a component
into its actor -- otherwise they apply twice.

*Four more version thresholds, all wrong for 835.* Phase 15 said every
threshold in this reader was a guess from two data points. That was still true
of four more:

* **The property-tag dialect.** How wide a BoolProperty's value is, and whether
  a ByteProperty names its enum in the tag. UT3 writes four bytes and no name,
  UDK one byte and the name; Gears sits between them by version and follows UT3
  on both. Reading one-byte bools desynchronised the stream at the first bool,
  and `StaticMesh` came out of 304 of 3,381 components instead of 3,377. Now
  measured per package by scoring the four combinations over a sample of
  exports -- the wrong dialect dies at the first tag it misreads, so the count
  separates them eleven to one. It also lifted this map's BSP from 492 polygons
  to 1,018 and its volumes from 82 to 168.
* **Three StaticMesh layout flags**, which were one version flag. Gears writes
  the kDOP root bound like UDK, but no per-element Fragments array and no
  vertex-colour elision -- like UT3. Neither pure path read a single one of its
  852 meshes. They are now three independent traits probed as a combination and
  cached once a mesh reads.
* **The UV offset** is no longer guessed at all. The vertex stride and the UV
  count are both in the buffer's own header, so what precedes the UVs is
  arithmetic: `elem_size - num_texcoords * uv_stride`. Gears' 20-byte vertices
  with two 4-byte sets give 12, UT3's number.

*An OOM, not a parse failure.* `_array` read a bulk-array header and trusted
it. While *searching* for the LOD table those two numbers are arbitrary, and a
count in the hundreds of millions became a list comprehension that took the
process out -- exit 137, no traceback, on a machine with 83GB free. The header
now has to fit the export, and a zero element size is rejected explicitly since
`0 * count` fits anything.

*What is not done.* Gears splits a map across several .war files -- audio, VFX
and lighting sub-levels beside the base -- and nothing merges them.
MP_Courtyard keeps everything in the base file so it loses nothing, but
MP_Depot and MP_Escalation keep their lighting in a `_Lighting` sub-level and
would come out unlit. `batch.py` lists the 29 base maps and skips the
sub-levels rather than converting them into empty shells.

**Phase 16 -- packaging a built map for a server.** `tools/package_maps.py`.
Converting and building a map leaves it in two places the editor uses; shipping
it needs four files in one folder -- the `.ut2`, its `.utx`, and a `.uz2` of
each, which is the form a server sends to a joining client. The tool copies and
compresses, taking a map by its *source* name the way `batch.py --match` does
and asking `batch` for the converted and package names rather than restating
the rules.

It checks two things that doing it by hand does not:

* **Staleness.** The `.ut2` is made by hand in UnrealEd, so nothing links it to
  the `.t3d` it came from and a converter fix that was never re-imported is
  invisible -- the map looks built and is a version behind. DM-Dekk was packaged
  that way: its `.ut2` predated the skysphere fix, so its dome was still
  `SM_skybox_up_high` at 1:224 rather than `SM_SkySphere` at 1:667. A `.ut2`
  older than its `.t3d` is now refused, with `--force` to override.
* **The compression ratio.** UCC reports one through a signed 32-bit percentage
  that overflows on large files: BLGaneshaTex.utx packs to 43% of its size and
  UCC calls it -28%. The ratio printed here is measured from the two files.

The six TOXIKK maps are packaged: Dekk, Cube, Foundation, Artifact, Citadel and
Ganesha -- 24 files, maps packing to 5-20% and texture packages to 28-46%.

**Phase 18 -- a fourth UE3 game, and maps that arrive in pieces.** Angels Fall
First (UDK 872, LZO). The reader needed nothing: the tag-dialect probe from
Phase 17 measured it as UDK-style on its own, which is the first time a new
build has cost no format work at all.

What it needed was *sub-levels*. An AFF map is a nearly empty persistent level
that streams the rest in. AFF-Errah.udk holds 12 mesh actors and 9 lights in a
world 48,000 x 85,000 uu across; the map is three packages beside it, named
only from `WorldInfo.StreamingLevels`:

    loc-errah-terrain      3,506 mesh actors, 50 lights
    loc-errah-camplewis    5,491 mesh actors, 132 lights
    brf-generic-assets     20 InterpActors

`convert/sublevels.py` reads those names and opens each package; ut3conv.py
runs the actor converters over the list instead of over one package. Nothing
merges *packages* -- each sub-level is converted as itself into the shared mesh
and texture sets, so object references stay inside the package that made them,
which is the only way they resolve. Only `LevelStreamingAlwaysLoaded` is taken:
`LevelStreamingAuto` is conditional, and AFF-Errah's four are briefing rooms
that would land on top of the map. `--no-sublevels` turns it off.

*The play area is not always the BSP.* Every map until now had brushes covering
the space it is played in, so the world bounds came from them. AFF does not:
measured against six brushes and 42 polygons, the entire streamed-in level read
as distant backdrop. 8,242 of 8,945 actors were being shrunk into the skybox
and 5,300 more dropped for falling outside a world brush sized to the shell.

`play_area_for` now takes the bounds from the placed meshes when the BSP is
much smaller than they are, trimming 2% off each axis so the horizon scenery --
the thing the bound exists to identify -- does not define it. That brought the
move down to 437 and the drops to 413. Seven call sites were reading
`stats.world_bounds` (the brushes) where they meant "how big is this map"; they
read the corrected bounds now. Verified no change on DM-Deck, BL-Dekk or
MP_Courtyard, none of which trigger it.

    AFF-Errah   750 static meshes, 8,945 actors placing 3,761,216 triangles,
                191 lights, 12 player starts, 177 textures

*Known and not fixed.* AFF's sky dome is smaller than the room's, so the
backdrop move *enlarges* rather than shrinks (the ratio print said "1:0" until
it was taught to read both ways). 412 of the 437 moved meshes then fall outside
UE2's world and are dropped. The map is unaffected -- these are horizon props
-- but a map that leans on its backdrop would notice.

### Running the UDK editor under Wine

Not needed to convert anything -- the pipeline is pure Python and never invokes
UDK.exe -- but it is what lets a converted map be compared against the original,
which is the editor pass Section 4 asks for. It does work. Four separate causes,
each fixed in turn, on a copy of the Steam install with the UDK zip installed
over it (installing over the *Steam* copy stops TOXIKK launching):

1. **Managed assemblies are not on the probing path.** `UDK.exe.config` declares
   `<probing privatePath="Editor/Release"/>`, and TOXIKK ships the editor's C#
   assemblies flat in `Binaries/` instead. Copy the `Binaries/*.dll|exe` files
   into `Binaries/Win64/Editor/Release/`.
2. **UDK's installer replaces the script packages.** `cruzade.u` is compiled
   against TOXIKK's `Engine.u`, and UDK's stock one cannot supply
   `Engine.PrimitiveComponent:MaxDrawDistance`, so every CRZ class fails to
   load, `LoadObject` returns null and the asset database dereferences it.
   Restore `UDKGame/Script/*.u` from the Steam copy. (This is the same fault
   that breaks the game itself when UDK is installed over it -- identical
   address, reading 0x8C.) Only the script packages and stock UDK sample content
   are overwritten; `Content/Toxikk` and `Content/Maps` are untouched.
3. **Multi-threaded shader compilation crashes** in MSVCR100. Set
   `bAllowMultiThreadedShaderCompile=False` under `[DevOptions.Shaders]` in
   `UDKGame/Config/UDKEngine.ini` (and `Engine/Config/BaseEngine.ini`, so a
   config regeneration keeps it).
4. **Wine's `d3dx9_43` stubs `D3DXDisassembleShader`,** which UDK calls after
   every shader compile (`D3D9ShaderCompiler.cpp:467`, error `FFFFFFFF`). The
   genuine Microsoft DLL may already be in `system32` -- Wine loads its own
   builtin regardless -- so what is needed is the override:
   `wine reg add 'HKCU\Software\Wine\DllOverrides' /v d3dx9_43 /t REG_SZ /d native /f`.
   `D3DCompiler_43.dll` (64-bit) is wanted too; both come out of
   `Binaries/Redist/UE3Redist.exe`, whose `DXRedistCutdown/Jun2010_*_x64.cab`
   files 7z will open.

A handful of TOXIKK materials still fail to compile for PC-D3D-SM3 and draw as
the default material in viewports.

**Not attempted: material functions.** TOXIKK ships
`MF_MaterialFunctions.upk` and `SF_MaterialFunctions.upk`. Material functions
are a UDK feature with no UT3 equivalent, and the graph walk knows nothing
about them.

**Phase 14 — generated materials. DONE.**

Everything above assumes a converted material is one flat `Texture`, because
`convert/shaders.py` said a `ucc make` build cannot create a `Shader` or a
`FinalBlend`. It can. The probe is `ShaderLab/` at the install root — not part
of the pipeline; build it by adding `EditPackages=ShaderLab` and running
`ucc make` from `System/`.

A `Begin Object` block in a class's defaultproperties is constructed with
`Outer = InParent` and `RF_Public`, and during `ImportPropertiesScripts`
InParent is `Class->GetOuter()` — the class's own **package**, not the class
default object (Editor/Src/UnEditor.cpp:824; `GEditor->Bootstrapping` is zero
there, being raised only around `#exec`). So the object lands in the package
root exactly like an `#exec TEXTURE IMPORT`ed Texture. `UObject::ResolveName`
(Core/Src/UnObj.cpp:3648) walks `.` to arbitrary depth and even documents the
`ClassName.SubObjectName` form, so nothing about the path was ever a barrier.
UT2004 already ships objects made this way: `XGame.BulletSplash.SpriteEmitter29`
and its siblings are `Begin Object` subobjects sitting in `XGame.u`.

`editinlinenew` turned out to be a red herring — ImportProperties never reads
the flag. `ColorModifier` and `OpacityModifier`, both `noteditinlinenew`, build
exactly the same way.

What the probe built, read back out of the saved `.u` with `tools/ut2props.py`,
every value intact:

    TexPanner  LabDustPan     Material=Texture'ShaderLab.BSP.LabDust'
                              PanDirection=(Yaw=16384) PanRate=0.05
    Shader     LabFenceShader Diffuse/Opacity=Texture'ShaderLab.BSP.LabFence'
                              OutputBlending=OB_Masked TwoSided=True
    FinalBlend LabDustFB      Material=TexPanner'ShaderLab.LabDustPan'
                              FrameBufferBlending=FB_Brighten ZWrite=False

— over textures imported by `#exec` into the same package, in a graph several
levels deep. A *second* package built afterwards resolved
`Shader'ShaderLab.LabFenceShader'` from the saved file into its own import
table, which is the same shape a converted map's import table has.

Two conditions, both learned the hard way:

* **Something must reference the material or it is not saved.** SavePackage
  writes only tagged objects. The first probe defined five materials,
  referenced none of them, and produced a package containing none of them —
  no error, no warning, just an empty package. `MaterialSet.emit` therefore
  always writes a `GeneratedMaterials` array alongside the blocks.
* **Order matters twice.** A package is resolvable by a later one only if it
  appears earlier in EditPackages; and within one defaultproperties block,
  ImportProperties reads line by line, so a block referring to an object
  defined below it resolves to nothing. Registration order is dependency order
  because a graph is built from the texture outwards.

*What was built.* `ut2/materials.py` collects the definitions and emits the
`Begin Object` blocks plus the keep-alive array into the generated `.uc`;
`surface_style` and `build_material` in `convert/shaders.py` do the translation;
`TextureSet` owns a `MaterialSet` and answers `material_for` (a bare path, for a
t3d polygon) and `material_class_for` (`Class'Pkg.Name'`, for an actor
property). `--no-materials` turns the whole thing off and reproduces the old
output exactly, which is how each change below was checked.

The mapping is Epic's, read out of `XEffectMat.utx` rather than invented:

    UE3                          UE2
    BLEND_Additive               FinalBlend FB_Brighten,    ZWrite=False
    BLEND_Translucent            FinalBlend FB_Translucent, ZWrite=False
    BLEND_Modulate               FinalBlend FB_Modulate,    ZWrite=False
    BLEND_Masked                 (nothing — the texture's MASKED=1 and the
                                  Phase 5e opacity bake already say it)
    MLM_Unlit                    Shader with Diffuse = SelfIllumination
    TwoSided                     TwoSided on whichever object is outermost

`Link.LinkBeamBlueFB` is FB_Brighten with ZWrite off over a bare Texture;
`goop.GoopFB` is FB_Translucent over `GoopShader`, which sets Diffuse and
SelfIllumination to the same TexOscillator and nothing else — that is how UE2
spells "unlit", and it is what the converter now emits.

*Two consumers.* A BSP surface names its material directly in the t3d, which
needs nothing new: `StaticLoadObject(UMaterial::StaticClass(), ...)` at
`Editor/Src/UnEdFact.cpp:1600` takes any UMaterial. A static mesh cannot —
UT2004 keeps materials on the mesh and the ASE is the only way to put them
there, but an ASE binds `*BITMAP` while the package is being *parsed*, and a
`Begin Object` material does not exist until defaultproperties are imported at
the end of the build. So the mesh keeps its flat texture and the actor overrides
it through `Skins(n)`, which UT2004 reads per material index
(`AActor::GetSkin`, Engine/Src/UnActor.cpp:1275) — the mechanism Phase 6d
already used for the goo.

*Effect meshes are the payoff.* The objection at the top of `shaders.py` — that
`M_EV_Lightbeam_Master_01` resolves to a texture off a disabled branch and draws
as "a solid grey slab where UT3 draws a soft glow" — was an objection to the
*blend mode*, not to the texture. Black contributes nothing under FB_Brighten,
so the same texture drawn additively is a glow. `effect_is_drawable` keeps an
effect actor whenever a non-opaque blend mode and a real texture are both there.
Both conditions are load-bearing: a material that resolves nothing takes the
grey placeholder, and a grey placeholder drawn additively is a bright haze over
everything behind it.

*DM-Deck, converted end to end and built.*

    21 UT3 materials a flat texture cannot express (3 additive, 18 translucent,
       all unlit) -> 17 Shader/FinalBlend objects in DMDeckTex.utx
    201 effect actors that used to be dropped: 199 now drawn, 2 dropped
    242 Skins references across 208 actors
    0 BSP surfaces — DM-Deck's brushwork is genuinely all opaque

The 84 that resolve `T_ASC_Base_BSP_Plaster_S` are the map's window glass
(`S_HU_Deck_SM_FWindow_Glass` and its two broken variants), invisible before;
107 are the light beams. BSP does exercise the path on other maps — CTF-Coret
takes 4 of its 60 brush materials, DM-Sanctuary and CTF-Strident one each, a
modulated `T_FX_RoilingFlame`.

*One thing this broke, and the fix.* Drawing the effect meshes put actors in the
list that had never been there, and one of DM-Deck's fog sheets sits 114,621uu
from the play area. That answered "is there scenery too far to draw?" with yes
and moved the map's entire distant city into the skybox — 62 meshes at 1:167,
real level geometry among them. `drop_distant_effects`
(`convert/skybox_move.py`) removes effect actors past the far plane before the
question is asked: they cannot be drawn there whatever happens, and a map should
not be restructured around geometry that contributes nothing. DM-Deck's backdrop
stays in the level exactly as it did before.

*Phase 14b — the whole graph, not just the blend mode.* Reported as
`StaticMeshActor_4656` on DM-Deck looking wrong: one of the goo pit's fog
sheets, drawn as a grey cloud where UT3 has green haze. The first pass carried
a texture and a blend mode and threw away everything else the material said,
and for UT3's volumetric effects everything else is most of it.

`M_UN_Volumetrics_TexturedFogSheet_01_Goo` is a `MaterialInstanceConstant` over
`M_EV_FogSheet_Master_01` that overrides six scalars and one vector, and the
master states its EmissiveColor as `Color.rgb * Color.a` and nothing else.
There is no texture in the colour path at all. So the entire appearance of a
goo pit is one parameter — and resolving it as "a texture" lands on whatever
turns up in the opacity chain, drawn white. Worse, every instance of a master
resolves to the *same* texture, so DM-Deck's seven light beam variants (warm
sunbeam, cool window, grey machine) were one white beam seven times.

`ut3/objects/graph.py` folds an expression graph to a constant.
`collect_parameters` walks the instance chain leaf-first for the overrides;
`fold` evaluates Multiply/Add/Subtract/Divide/Abs/Min/Max/OneMinus/Clamp/Power/
LinearInterpolate/Desaturation/ComponentMask over Constants, ScalarParameters,
VectorParameters and StaticSwitchParameters. Anything else — a texture sample,
a Fresnel, a CameraVector — returns None and the caller falls back to what it
did before. It never guesses: it evaluates the graph exactly or declines.

Three things it took to get right:

* **A masked input pads opaque, not with zero.** `Color.rgb * Color.a` masks
  its left side to RGB, and padding that with alpha 0 multiplied the alpha out
  of the result.
* **sRGB, not linear.** UE3 authors colour parameters linearly and gamma-
  corrects on output; UE2 stores what it displays. 0.5 linear is 188, not 128,
  and without the conversion every tint comes out muddy.
* **An opacity chain does not fold, and that is the right answer.** UT3 drives
  fog opacity from `PixelDepth` through a `DotProduct` and a `Divide` — a
  per-pixel depth fade UE2 has nothing for. Standing those nodes at their
  strongest and folding anyway was tried and gave 0.0002, because the depth
  arithmetic is in world units and there is no sensible depth to supply. The
  machinery was written and then removed: a plausible-looking wrong number is
  worse than declining. The texture supplies the shape instead.

*Two more things now convert.* A `Panner` node becomes a `TexPanner` — UE2
states the same idea as a rotator and a rate, and `UTexPanner::GetMatrix`
(UnMaterial.cpp:500) offsets UV by `PanRate * PanDirection.Vector()` per
second, so the two speeds are a magnitude and an angle. The angle is negated
because the converter flips V everywhere else. DM-Deck's waterfalls flow, its
fog drifts and its city traffic moves. And a folded colour becomes a
`ColorModifier`, which multiplies the material under it by a constant in both
colour and alpha (`HandleTFactor_SP`, D3D9MaterialState.cpp:223).

*The blend mapping was wrong, and the render code says so.* Read out of
`D3D9MaterialState.cpp:299`: `FB_Translucent` is `ONE/INVSRCCOLOR` — UE1's
brightness-keyed translucency, which ignores alpha entirely — while
`FB_AlphaBlend` is `SRCALPHA/INVSRCALPHA`, which is what UE3's
BLEND_Translucent actually means. The first pass used FB_Translucent for all of
them. It is now FB_AlphaBlend, falling back to FB_Translucent only where the
exported texture has no alpha channel to blend on, since alpha 1 everywhere
would draw a fog sheet solid. That fallback is why `build_materials` runs at
the *end* of `export_textures` rather than during `add_material`: only the
export knows which textures ended up with alpha.

*The shape that comes out*, all of it Epic's own idiom from `XEffectMat.utx`:

    Texture
      -> TexPanner        if the graph has a Panner
      -> Shader           if MLM_Unlit; Diffuse = SelfIllumination, which is
                          how goop.GoopShader spells "ignores lighting"
      -> ColorModifier    if the colour folds to a constant
      -> FinalBlend       FB_Brighten / FB_AlphaBlend / FB_Translucent

Every one of those is a `UModifier` except the Shader, and the render interface
accumulates the whole chain into one state (D3D9RenderInterface.h:395), so they
compose. A `Shader` that will *not* be wrapped in a FinalBlend gets
`OutputBlending=OB_Masked` where UT3 says BLEND_Masked: OB_Normal with no
Opacity turns AlphaTest off outright (D3D9MaterialState.cpp:1521), which would
draw a cutout solid.

*DM-Deck, rebuilt.* 21 UT3 materials become 44 objects — 19 FinalBlend, 14
ColorModifier, 8 Shader, 3 TexPanner — of which 14 carry a folded tint and 7
pan. The seven light beam variants come out at seven different colours
(147,124,82 for the sunbeams, 102,119,118 for the windows, 130,136,142 for the
machine room) and seven different strengths; the goo is (174,228,91) at 65%.
`StaticMeshActor_4656` wears `T_EV_FogSheet_CloudTex_02_c305FB_4`, that green
over a panning cloud texture at FB_AlphaBlend; `StaticMeshActor_4611` wears
`..._FB_7`, the window beam at alpha 13. All 3,407 material references in the
t3d resolve against exports in the built package.

*Phase 14c — the level, not just the colour.* Reported as
`StaticMeshActor_4611`, a light beam wearing
`M_UN_Volumetrics_Lightbeam_Cheap_02_Windows`. The colour was right by then;
the strength was not. That material's opacity is

    clamp(0.0025/Distance * PixelDepth) * FalloffA * FalloffB * ScalarParameter"Opacity"

and the scalar is 0.05. Phase 14b established that an opacity chain does not
fold, which is true of the *chain* — but the **level** it is scaled to almost
always does, stated as one parameter multiplied in at the top. Dropping it drew
every beam at full strength, twenty times what UT3 shows.

`graph.constant_scale` walks down the multiply chain, folding whichever side of
each `Multiply` is constant and following the side that is not, so what it
returns is the exact product of the factors that genuinely fold. Three wrappers
are transparent to it: a `StaticSwitchParameter` (picking the live branch is
exact — the other is not compiled into the shader at all), a `DepthBiasedAlpha`
(UE2 has no depth bias, so the alpha under it is all that survives) and a
`Clamp` (pulling a factor out from under one is exact wherever the clamp is not
biting, and these clamp to 0..1 around a value that reaches 1 at most). It never
descends into a Multiply where neither side folds, so the depth term and the
falloff textures are left alone as the shape.

DM-Deck's beams come out at 0.035, 0.05, 0.10, 0.125, 0.15 and 0.25 — the range
a mapper would tune by hand — its window glass at 0.8, the goo at 0.65 and the
background flare at 0.5. Everything with no `Opacity` parameter reads 1.0.

Where the level goes depends on the blend, and the render code decides it:
`FB_AlphaBlend` and `FB_Brighten` are both driven by source alpha, so it becomes
the `ColorModifier`'s alpha; `FB_Translucent` is `ONE/INVSRCCOLOR` and ignores
alpha entirely, so there the level *is* the brightness and it scales the colour
instead.

**A material at zero opacity is not built at all.** DM-HeatRay's
`M_EFX_Particles_Distortion01` states `Opacity = Constant 0`: it is pure screen
distortion, and what UT3 shows through it is the scene behind. Recording it
would have been worse than useless — `will_build` is what decides whether an
effect mesh is drawn, so the actor would have been kept, given no Skins, and
drawn wearing its flat texture solid.

*Phase 14d — textures in a branch UT3 never compiles.* Reported as
`StaticMeshActor_4656` still not right after 14c: a goo sheet drawn with a
cloud texture where UT3 draws a soft falloff.

`M_UN_Volumetrics_TexturedFogSheet_01_Goo` is named after an overlay it does
not use. Its master gates that overlay behind a `UseTextureOverlay`
**StaticSwitchParameter**, the default is off, and the instance overrides no
static parameters at all. A static switch is not a runtime branch: UE3 compiles
one side into the shader and discards the other, so a texture on the dead side
is never sampled. `convert/shaders.py` has said exactly this since Phase 1 --
"the texture a naive resolver lands on (`T_EV_DustPanner_01`) comes from a
disabled `UseTextureOverlay` branch" -- and it was never acted on because the
effect meshes were being dropped anyway. Phase 14 made them visible.

Two places now follow only the live branch: `_collect`, which the diffuse walk
uses, and `_subobject_textures`, which is the last resort and the one that
mattered here -- a fog sheet has no texture in its colour path at all, so it
always falls through to "every texture this material owns", where the cloud
beat the falloff because `score_texture_name` penalises "falloff" by 60 as a
rule aimed at cubemap falloffs.

**Reachability needed its own traversal.** `_collect` follows a fixed list of
inputs chosen for hunting a diffuse, and a fog sheet's only texture hangs off a
`DepthBiasedAlpha.Alpha` -- not on the list, so the walk stopped one node short
of everything that mattered and the reachable set came back empty. Widening the
diffuse walk instead would change which texture every material in every map
resolves to, in order to answer a question about reachability. So
`_reachable_textures` follows *every* property that resolves to a
MaterialExpression and answers only that.

Measured across six maps, 32 of 1,381 materials change and every one is an
effect material:

    fog sheets                     cloud overlay -> T_EV_FogSheet_Falloff_01
    every M_EV_Lightbeam_Master_01 T_EV_DustPanner_01 -> T_EV_LightBeam_Falloff_02
    WAR-Torlan's distortion river  T_UN_Rock2_BSP_Rock08 -> T_Liquid_SM_NanoBlack_Mask_01

None loses its texture. The river is the one worth noting for being nothing to
do with fog: it was painted with a rock texture.

*The bio goo substitution stays, and now there is a reason on file.* With
materials buildable it was worth asking whether `EFFECT_SUBSTITUTES` still
earns its place. For the goo *surface* it does. `M_HU_Deck_Goo_Translucent`
computes its colour as

    Constant3Vector(2.0, 4.0, 0.4) * GoldCube1.rgb        (a cubemap, by ReflectionVector)
      + T_EV_DustPanner_01 * Constant3Vector(0.025, 0.025, 0.01)

-- the green is a cubemap reflection tint. Cubemaps are refused as colour maps
(Phase 7c) and this pipeline exports none, so what a generated material would
draw is the fallback, `T_EV_DustPanner_01`, which measures mean 126 over a
106..164 range: near-uniform grey. A flat grey slab where UT3 has moving green
goo, against `XEffectMat.goop.GoopFB`, which is an actual animated goop.

The split `sheet_is_horizontal` already draws turns out to be the right one and
is unchanged: the flat, level *surface* sheets take the stock material, and the
vertical haze around them converts with its own falloff and tint.

*Phase 14e — DM-HeatRay, and a panner running backwards.* Reported as
`StaticMeshActor_1276`, a light cone whose animated material scrolled the wrong
way. It did. `material_panner` negated UE3's `SpeedY` on the grounds that the
converter flips V — and it does not. `ut2/ase.py` writes `1.0 - v` and the ASE
importer computes `1.0 - ST.Y` back (Editor/Src/UnStaticMesh.cpp:1048), so the
two cancel and a converted mesh carries UT3's own UVs; the BSP surface writer
never flips at all. Both engines also offset the *coordinates* by speed times
time (`UTexPanner::GetMatrix`, Engine/Src/UnMaterial.cpp:500), so
`PanRate * PanDirection.Vector()` is `(SpeedX, SpeedY)` with nothing negated.

Every panning material along V was reversed; DM-Deck's waterfalls were running
uphill. The control that makes it unambiguous is `M_UT_SM_movingcars`, whose
panner is pure U: yaw 0 under either reading, and unchanged by the fix.

*A second panner fault, found alongside it.* `material_panner` returned the
first Panner anywhere in the graph. `M_EV_FogSheet_Master_01` carries two, one
per texture sample, so a sheet could scroll at a speed belonging to a texture it
was not drawing. `resolve_diffuse` already leaves the sample it landed on in
`_LAST_SAMPLE`; that sample's own Coordinates chain is searched first now, and
the material at large only as a fallback.

*And two from building DM-HeatRay, before anyone looked at it.*

* **A scalar nested one level inside the product was missed.**
  `M_EV_Lightbeam_Master_01_INST` sets `Opacity` to 0.1, and `constant_scale`
  returned 1.0 -- ten times too strong, the same fault as 14c and not caught by
  it. The walk gave up when *neither* side of a `Multiply` folded, and this
  master puts the scalar under a `DepthBiasedAlpha` one level down. Descending
  both sides is correct for a product, but doing it naively brought back the
  0.0002 problem: `M_UN_Volumetrics_Lightbeam_Cheap_02` clamps
  `PixelDepth * 0.0025` *inside* its product, and that 0.0025 is a saturating
  depth ramp rather than a level. So the walk has two phases -- the spine
  (`_SPINE`: static switches, DepthBiasedAlpha, the outermost Clamp) is
  followed down to the product, and inside the product nothing but `Multiply`
  is descended. Checked against 640 non-opaque materials across six maps: only
  two land under 0.02, `M_EFX_Particles_Distortion01` at 0.0 and `M_Invis_01`
  at 0.01, both honest.
* **No-op ColorModifiers.** A folded colour of pure white at full alpha
  multiplies by one. UCC drops the property (it equals the class default) but
  the object is still built and still costs a texture stage at render time,
  and stages run out -- "No stages left for constant color modifier" is a real
  failure path (D3D9MaterialState.cpp:1751). Skipped now.

*A limitation worth stating.* These instances carry
`bHasStaticPermutationResource=True` but no `StaticParameters` tagged property:
UE3 keeps static-switch overrides in native data after the property list, which
this reader did not read, so a switch's value came from the master's default.
**Phase 14i lifted this** -- the set is now parsed out of the native data and
checked against UDK's own browser.

*Phase 14f — opaque materials that still say something.* Reported as
`StaticMeshActor_876`, a city sign converting as two flat textures where UT3
draws two materials. Both of its elements are **BLEND_Opaque**, and everything
above only built for non-opaque surfaces, so neither was even considered.

Two kinds of opaque material are expressible and were being thrown away:

* **Unlit.** `M_HU_Deco_SM_CitySignStores` is MLM_Unlit with no diffuse at all
  -- an animated LED panel that is pure emissive. A `Shader` whose Diffuse and
  SelfIllumination are the same material is UE2's way of saying that.
* **Lit, with a glow.** `M_HU_Deco_SM_CitySignsTexts` paints
  `T_HU_Deco_SM_CitySign01b_D` and adds `T_HU_Deco_SM_CitySignsTexts_E` at
  fifteen times brightness on top. UE2's Shader has exactly those two slots.

The old code skipped both, on the stated grounds that "UT3 marks a great deal
of ordinary geometry unlit ... and self-illuminating it all would flatten the
lighting". Measured instead of asserted, that is wrong: of 487 opaque materials
across four maps only **42 are unlit**, and every one is a city sign, an ad
board, a holo screen or a sky. **128 more carry a separate emissive texture** --
signs, street lights, lit windows.

**The trap, and it is a total one.** `Shader.SelfIllumination` without a
`SelfIlluminationMask` does not add a glow. It *replaces* the diffuse and
unlits the whole surface:

    if( InShader->SelfIllumination && !InShader->SelfIlluminationMask )
        { InDiffuse = InShader->SelfIllumination; Unlit = 1; ... }
        -- D3D9Drv/Src/D3D9MaterialState.cpp:972

So the obvious `Shader{Diffuse=D, SelfIllumination=E}` would have drawn the
sign as its `..._E` texture alone -- which measures mean 17 out of 255, nearly
black. Far worse than the flat diffuse it replaced. With the mask set, the
engine takes its alpha and blends the glow over the lit diffuse instead
(D3D9MaterialState.cpp:1096).

That alpha is what a UE3 emissive does not have: it is read as *colour*, and
its brightness is the mask. So `bake_self_alpha` writes each glow texture's own
Rec. 601 luma into its alpha, and the Shader names it as both SelfIllumination
and SelfIlluminationMask. A glow is therefore a *separate copy* of the texture
-- `glow` is part of `add_texture`'s key, because the same image drawn as an
ordinary diffuse must not acquire an alpha channel.

Half of UT3's emissives are DXT5, whose alpha is overwritten by this and whose
colour half needed a decoder of its own -- a DXT5 block is eight bytes of alpha
followed by a DXT1 colour block exactly, so `decode_dxt5_channel` is the same
decode at an offset. Overwriting is the right call: the material never samples
that alpha.

`_unusable_glow` refuses two things, and refusing is what keeps a texture
nothing will reference from being exported: an image with no variation at all
(the engine's placeholder, `UN_Shaders.T_Diffuse`, is 32x32 at mean 128 with a
spread of zero -- an unfilled slot, and drawn as a glow it washes the surface
out), and any format whose luminance cannot be measured, which is by
construction the same set `bake_self_alpha` cannot bake.

DM-HeatRay: 25 Shaders (15 glowing, 10 unlit), 14 glow textures, 599 actors
wearing a generated material against 22 before. `StaticMeshActor_876` gets both
of its elements -- `Skins(0)` the lit sign glowing its text, `Skins(1)` the
unlit panning store panel.

*Phase 14g — a Panner animates its own sample and no other.* Reported
immediately after 14f: the same sign now carried its materials but scrolled,
and does not in UT3.

`material_panner` searched the drawn sample's Coordinates first and fell back
to the material at large. `M_HU_Deco_SM_CitySignStores` is exactly the shape
that breaks: a scrolling LED underlay (`T_UN_Team_SM_LED_Base_Pan`, through a
Rotator) with static artwork over it. The drawn sample is the artwork, whose
Coordinates are a `BumpOffset` with no Panner at all -- so the fallback ran and
put the underlay's Panner on the artwork.

The rule is simply that a Panner belongs to the sample it is wired to. Where
the drawn sample is known its Coordinates are the authority **including when
they say no**, and the fallback survives only for the case where there is no
sample to ask: `resolve_diffuse` lands on one only when the graph walk reached
a texture, and a fog sheet or a light beam has no texture in its colour path at
all, so its texture comes from the last-resort "every texture this material
owns" scan. That is where the fog and the beams live, and their material's own
Panner remains the only information there is.

Of 717 materials across four maps whose drawn sample is known, **679 now
correctly do not pan** -- and the list of what had been sliding is stairs,
rubble, ferns, grass, hair and concrete. 14f is what would have made that
visible, by giving those materials a Shader to carry the panner on. The
waterfalls, the moving cars and the light cones keep theirs, all three having
their Panner on the sample actually drawn.

DM-HeatRay's TexPanners fall from 11 to 3, DM-Deck's from 8 to 3.

*Phase 14h — BL-Dekk, and the same machinery on UDK.* The material work above
was written against UT3's cooked packages; TOXIKK's BL-Dekk exercises it on UDK
(package version 868) where materials are mostly `MaterialInstanceConstant`
chains over shared masters. It converts: 38 UT3 materials become 29 objects --
12 Shaders, 10 FinalBlends, 6 TexPanners, a ColorModifier -- with 328 actors
wearing one and all 8,050 package references resolving. Four faults came out of
it, three of them general.

* **A normal map the name rules miss.** `SF_T_TilingBubbles_N_H` ends `_N_H`
  rather than `_n`, scores a clean 0, and painted BL-Dekk's pools. Phase 7c
  refuses normal maps by name because one drawn as diffuse renders in
  iridescent blue and magenta; names run out, and the pixels do not. A
  tangent-space normal is a unit vector packed around (0, 0, 1), so a flat one
  is (128, 128, 255) and any real one stays near it. `_is_normal_map` refuses
  blue >= 200 with red and green both inside 100..160. Over three maps it flags
  exactly two textures: the one the name already catches, and this one.
* **Only the first *connected* colour input counts.** `constant_colour` tried
  DiffuseColor, and on failing to fold it fell through to EmissiveColor.
  TOXIKK's `SF_M_SnowBarrier` has a textured diffuse -- so it declined, quite
  correctly -- and an unused EmissiveColor that folds to zero. The result was a
  `ColorModifier` multiplying the barrier by **black**. The first connected
  input is the one that defines the surface, and if it does not fold then the
  colour is in a texture and there is no constant tint. A folded pure black is
  refused outright as well: nobody writes black as a tint, it is an input left
  at zero, and multiplying by it erases the surface.
* **Names longer than an FName.** `MaterialSet` appends a tag and a two-letter
  kind to the texture's name, and UT3 has textures named after the material
  that flattened them:
  `M_UN_Volumetrics_Lightbeam_Cheap_02_FloodlightsCold_Flattened_c305CM` is 68
  characters against `NAME_SIZE = 64` (Core/Inc/UnName.h:16). Past that the
  name is truncated on import while the reference keeps its length, and for a
  material `ucc make` fails outright. Both `MaterialSet._unique` and
  `TextureSet._unique` now trim the base to fit, counter included -- the
  texture side was over the limit too, silently.
* **Two maps wanting one package.** `~/TOXIKK` is the working copy (the Steam
  install with the UDK zip over it, see "Running the UDK editor under Wine"),
  and it is not interchangeable with the Steam one: `BL-Dekk.udk` differs
  between them, so `batch.py` now prefers it. But UDK's installer also drops
  its own sample maps into `Maps/UT3`, including a `DM-Deck.udk` that lands on
  the same output name and the same package as UT3's DM-Deck -- the second run
  clobbering the first's source tree and then failing to build against the
  other's t3d. `SKIP_MAP_DIRS` leaves UDK's sample tree alone, and a package
  claimed twice is now reported rather than silently overwritten.

*One bug this found in Phase 14a.* `drop_distant_effects` was calling
`furthest_from` without the "is it outside the play area" guard the backdrop
measurement uses. That function answers "how far can a player get from this",
so for anything standing *in* the level it returns the map's own diagonal —
and DM-HeatRay is 131,761uu across, so all 22 of its light beams were being
thrown away as too distant to draw. 19 of them are in the map and now convert.

*Verified in the editor on DM-Deck*, over four rounds: 14a drew the effects at
all, 14b gave them their colours, 14c their strengths, 14d the right textures.
Overdraw and sorting were the things to watch and neither has been a problem on
this map. DM-HeatRay and TOXIKK's BL-Dekk are built too, and between them found
everything in 14e to 14h. None of the three has been walked since 14h, which
changes all of them. No other map has been looked at, and the regression batch
has not been run since 14b -- which now matters more than it did, since 14d and
14h both moved texture resolution. Not yet done: masked materials with a
separate `..._M` opacity texture could become a `Shader` with Diffuse and
Opacity instead of the DXT5 repack Phase 5e does; a `TextureCoordinate`'s
UTiling/VTiling could become a `TexScaler` (it is baked into mesh UVs today and
ignored on BSP); and `EFFECT_SUBSTITUTES`/`MATERIAL_SUBSTITUTES` could hand goo
and water back to UT3's own materials now that those convert.

## 3. What turned out hard

Revised against what actually happened, rather than what was expected.

- **Materials were the hardest part, and the heuristic held.** "First texture
  sample reaching Diffuse" needed four rounds of correction — relief bakes,
  instances that override only a normal map, definitive non-colour markers, and
  explicit slot overrides — but the per-map `materials.ini` escape hatch was
  never needed. Every fix generalised.
- **Lighting needed global re-tuning, as expected.** `--light-gain` (default 32)
  and `--ambient-gain` (default 16) are the dials; UT3 maps convert dark
  otherwise, and a map with any SkyLight gets a floor of 15 ambient.
- **Scale was a non-issue.** 1.0 throughout. UT2004's dodge and higher jump make
  UT3 geometry play fine.
- **UT2004's limits were not the problem; `UCC.exe`'s address space was.** See
  Phase 11. No map needed splitting.
- **Cooked-package quirks were mild.** Everything needed survived cooking. UE3
  material graphs were called the one real gap here, on the grounds that a
  `ucc make` cannot produce a UE2 Shader or FinalBlend. That was wrong — see
  Phase 14.
- **Per-poly mesh collision has no build-time fix.** `UseSimpleKarmaCollision`
  defaults on, and forcing it off at import broke every mesh. It is set by hand
  in the editor where a mesh needs it.

## 4. Where this stands

The pipeline is complete and the general faults are fixed. What is left is
per-map polish found by looking — and Phase 14's generated materials, which
build and resolve correctly but have not been walked through in the editor.

**Not planned:** particles and emitters, Kismet, SpeedTree, PostProcessVolume,
skeletal meshes, destructibles. (UE3 shader materials were on this list until
Phase 14 showed they can be built.)

**Wanted, not done:** TOXIKK's CC maps convert as `DM-` because Cell Capture's
objectives do not convert. Assault is the mode that matches and the mod is
installed; what is missing is the objective conversion, not the game type.

**Known and accepted:**

- Meshes needing per-poly collision want `UseSimpleKarmaCollision=False` set by
  hand.
- A few BSP faces resolve to the checkerboard placeholder where the material is
  a UE3 shader with no texture in it (CTF-Coret's `Brush_491`).
- Vehicles fall through some meshes that players walk on — a collision flag UE2
  does not derive from an ASE.
- A follower attached to a rotating mover turns on its own pivot instead of
  orbiting the leader.

**If picking this up again:** run `batch.py` first — Phase 14 changed texture
resolution for 32 materials across the stock maps and only DM-Deck has been
looked at. Then the editor pass, which is the work. Open a converted map,
walk it, and compare against the same map in the UT3 editor; every entry in
Phase 12 started that way. `batch.py --match <name> --build` turns a fix into a
rebuilt map, and the 15 UT3 test scripts in `tests/` read the stock maps
directly, so a regression shows up against real data rather than a fixture.
`tests/test_udk.py` does the same against TOXIKK's packages, checking the mesh
reader against UDK's own OBJ export, and skips cleanly when TOXIKK is absent.
