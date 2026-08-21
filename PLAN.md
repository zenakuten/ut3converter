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

Static meshes do not convert, and in a UDK map that is most of it: the references
point into TOXIKK's own `.upk` content packages, and both `StaticMesh` and
`Texture2D` are different binary formats at 868. Left as a proof that the reader
generalises, not as a feature.

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
- **Cooked-package quirks were mild.** Everything needed survived cooking. The
  one real gap is UE3 material graphs, which no amount of parsing turns into a
  UE2 Shader or FinalBlend from a `ucc make`.
- **Per-poly mesh collision has no build-time fix.** `UseSimpleKarmaCollision`
  defaults on, and forcing it off at import broke every mesh. It is set by hand
  in the editor where a mesh needs it.

## 4. Where this stands

Diminishing returns. The pipeline is complete and the general faults are fixed;
what is left is per-map polish, found by looking.

**Not planned:** particles and emitters, Kismet, UE3 shader materials, SpeedTree,
PostProcessVolume, skeletal meshes, destructibles, UDK static meshes.

**Known and accepted:**

- Meshes needing per-poly collision want `UseSimpleKarmaCollision=False` set by
  hand.
- A few BSP faces resolve to the checkerboard placeholder where the material is
  a UE3 shader with no texture in it (CTF-Coret's `Brush_491`).
- Vehicles fall through some meshes that players walk on — a collision flag UE2
  does not derive from an ASE.
- A follower attached to a rotating mover turns on its own pivot instead of
  orbiting the leader.

**If picking this up again:** the editor pass is the work. Open a converted map,
walk it, and compare against the same map in the UT3 editor; every entry in
Phase 12 started that way. `batch.py --match <name> --build` turns a fix into a
rebuilt map, and the 15 test scripts in `tests/` read the stock UT3 maps
directly, so a regression shows up against real data rather than a fixture.
