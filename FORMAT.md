# UT3 package format notes (version 512, cooker 57)

Details verified empirically against stock UT3 maps. Where UT3 differs from the
UE3 layouts described in most public references, it is called out.

## File header

```
u32   Tag = 0x9E2A83C1
i32   Version         (low 16 bits = 512, high 16 = licensee 0)
i32   TotalHeaderSize
FStr  FolderName      ("None")
u32   PackageFlags
i32   NameCount, NameOffset
i32   ExportCount, ExportOffset
i32   ImportCount, ImportOffset
i32   DependsOffset
byte  Guid[16]
i32   GenerationCount, then 12 bytes each (exports, names, net objects)
u32   EngineVersion (3487), CookerVersion (57)
u32   CompressionFlags   (2 = LZO)
i32   ChunkCount, then per chunk: uncompressed offset/size, compressed offset/size
```

FString: `i32` length; positive = Latin-1, negative = UTF-16LE of `-len` chars,
both NUL-terminated.

## Compression

Retail maps are LZO1X in ~70 chunks. Each chunk begins with its own header:
`magic, blockSize (0x20000), compressedSize, uncompressedSize`, then one
`(compressedSize, uncompressedSize)` pair per block, then the blocks.

All table/export offsets are in **uncompressed** file coordinates. Bytes before
the first chunk's uncompressed offset (the first 105 bytes) sit uncompressed at
the head of the physical file.

`liblzo2.so.2` via ctypes handles decompression -- `lzo1x_decompress_safe`, no
`lzo_init` needed. No `.tfc` texture caches exist in UT3, so all mip data is
inline in the packages.

## Name / import tables

- Name entry: FString + `u64` flags.
- FName reference: `i32 index`, `i32 number`; number 0 means the bare name,
  otherwise the name is rendered `Name_<number-1>`.
- Import entry: FName PackageName, FName ClassName, `i32` OuterIndex, FName ObjectName.

## Export table -- **UT3 has a ComponentMap**

This is the part that differs from the commonly published UE3 export layout:

```
i32   ClassIndex, SuperIndex, OuterIndex
FName ObjectName
i32   ArchetypeIndex
u64   ObjectFlags
i32   SerialSize, SerialOffset
i32   ComponentCount, then per component: FName + i32     <-- UT3-specific
u32   ExportFlags
i32   NetObjectCount, then that many i32
byte  PackageGuid[16]
u32   PackageFlags
```

The ComponentMap's `i32` values are **0-based export indices**, not
PackageIndexes -- add 1 before resolving. (Verified against the matching
`LightComponent` / `CollisionComponent` object properties on three actors.)

A correct parse ends exactly on `DependsOffset`; that is the cheapest check that
the layout is right.

## Tagged properties

```
FName Name          ("None" terminates the list)
FName Type
i32   Size
i32   ArrayIndex
FName StructName    (StructProperty only)
<Size bytes of value>
```

**BoolProperty in UT3 is 4 bytes, not 1.** `Size` is 0 and the value follows the
tag as an `i32`. Later UE3 builds moved this to a single byte in the tag, and
that is what most references describe; using a byte here desynchronises the whole
list. There is no `EnumName` in the tag at this version (added around v633), so a
ByteProperty is an FName when `Size == 8` and a raw byte otherwise.

Unknown (non-native) struct types serialize as nested tagged-property lists.

Object serial data may be prefixed before the property list: 8 bytes for
components, 32 bytes for actors carrying a script state frame (RF_HasStack).
Rather than model each case, `props.find_property_start()` probes 4-byte offsets
and keeps the first that parses through to the terminating `None`.

Only properties differing from the archetype are serialized, so anything absent
must be read from the class default (e.g. `PointLightComponent.LightColor` is
usually missing and means white).

## UPolys and FPoly (native)

`UPolys` serializes its property list (empty), then a TTransArray header --
`i32 Count, i32 Max, i32 Owner` (Owner points back at the UPolys itself) --
followed by `Count` FPoly records:

```
FVector Base, Normal, TextureU, TextureV
i32     VertexCount, then FVector[VertexCount]
u32     PolyFlags
i32     Actor            (PackageIndex of the owning brush)
FName   ItemName
i32     Material         (PackageIndex)
i32     iLink, iBrushPoly
f32     ShadowMapScale
u32     LightingChannels
```

Verified across four retail maps: every polygon has a unit normal, vertices
planar to within 0.0004uu, and the winding matches
`normalize((v1-v0) x (v2-v0))` -- so UE2 and UE3 share a winding convention and
vertex order can pass through untouched.

### Base is a texture origin in both engines -- pass it through

`FPoly.Base` drifts off the polygon plane routinely in UE3 (by up to 31500uu on
DM-HeatRay, affecting 1737 of 1912 brush polys). That is fine: **UE2 never uses
Base as a plane point.** Every CSG plane comes from `Vertex[0]` --
`FPlane(EdPoly->Vertex[0], EdPoly->Normal)` at `Editor/Src/UnBsp.cpp:231` and
`:292`, and the splitters at `:436` and `:548` likewise. `FPoly::Split(Normal,
Base, ...)` takes its plane as a *parameter*; it is easy to misread that as the
polygon's own Base field, which is what led this converter to "correct" Base
onto the plane for a while. Doing so shifts the texture origin on every face
whose TextureU/V are not perpendicular to the normal (350 polys on DM-HeatRay),
which shows up as misaligned textures on angled faces.

Pass Base through exactly as UT3 has it. Verified against the UT3 editor's own
t3d export of a brush.

### PolyFlags do not survive the engine gap

Only `PF_Invisible` (0x1), `PF_NotSolid` (0x8), `PF_Semisolid` (0x20),
`PF_TwoSided` (0x100) and `PF_Portal` (0x4000000) mean the same thing in both
engines. UE3 reuses the 0x200/0x400/0x800 bits (they appear on ~80% of polys)
for something else, and in UE2 those are `PF_AutoUPan`/`PF_AutoVPan`/
`PF_NoSmooth` -- passing them through would set panning on most surfaces in the
map. Everything outside the shared set is dropped.

## UModel (native, partially decoded)

After the property list: `FBoxSphereBounds` (Origin, BoxExtent, SphereRadius,
28 bytes), then several bulk-serialized arrays as `i32 ElementSize, i32 Count,
data` (Vectors 12, Points 12, Nodes 64, ... Verts 24), then the `Polys`
PackageIndex, then more arrays.

The array block's exact composition is not fully reversed, so `find_polys()`
locates the Polys pointer instead: for brush models (all arrays empty) it sits
at a fixed +76 from the end of the property list; otherwise the object's data is
scanned for PackageIndexes resolving to a Polys export. Where that is ambiguous
(29 volume models in DM-Sanctuary), the correct one is the candidate outered to
the model. This resolves 100% of brush and volume models across the four maps
tested.

The cached `Bounds` are stale in about two thirds of brush models -- they
disagree with the actual vertex extents by 16-32uu on a single axis. Recompute
from vertices rather than trusting them.

## UTexture2D and bulk data

After the property list (which carries SizeX, SizeY, Format, MipTailBaseIdx,
SRGB, LODGroup):

```
FByteBulkData SourceArt        (16 bytes: flags, count, sizeOnDisk, offsetInFile)
i32           MipCount
per mip:      FByteBulkData, [inline payload], i32 SizeX, i32 SizeY
```

Bulk flags seen in UT3 maps:

| Flags | Meaning | Payload |
|---|---|---|
| `0x00` | inline | follows the header, `count == sizeOnDisk` |
| `0x11` | stored elsewhere, compressed | see below |
| `0x21` | stripped by the cooker | `count 0`, size/offset `-1` |

**Where the `0x11` payloads live.** Not in the map. The offsets are physical
offsets into the *content package that owns the texture* -- the root of the
object's path. `ASC_Base.BSP.Materials.T_ASC_Base_BSP_Concrete01_N` in
DM-HeatRay resolves to `CookedPC/Environments/ASC_Base.upk`, and its mips sit at
exactly the recorded offsets there. (There are no `.tfc` caches in UT3, and the
offsets are not valid in either the map's uncompressed or physical space -- both
were checked.)

Each payload is a standard compressed chunk: `magic 0x9E2A83C1, blockSize,
compressedSize, uncompressedSize`, then one `(compressed, uncompressed)` pair per
block, then LZO blocks. `sizeOnDisk` counts the compressed blocks only, so
consecutive payloads are spaced `sizeOnDisk + 24` apart for single-block mips.

The cooker strips the top mip of large textures (2048x2048 arrives with its
first mip gone, largest available 1024x1024). That costs resolution only --
UT2004 wants 1024 or smaller anyway -- and **does not affect surface mapping**,
which was verified rather than assumed (see the surface scale note below).

### BSP surface UVs: UE3 normalises by 128, UE2 by the texture size

This is the whole difference, and it subsumes what looked like two separate
effects. UE3 states a BSP surface's `TextureU`/`TextureV` against a fixed
constant regardless of what texture the surface wears -- swap a 512 for a 2048
in UE3 and the tiling does not move. UE2 divides by the texture size instead. So
a surface has to be restated in terms of the size **actually exported**:

    |TextureU_UE2| = |TextureU_UE3| * exported_size / 128

The 128 is derived rather than guessed. `T_LT_Floors_BSP_Organic05b_D` is 512
declared and 512 exported, so no size reduction confuses it; UT3 gives its
surfaces `|TextureU|` 0.5, and 2.0 was measured in the editor as the value that
matches. `0.5 * 512 / 2.0 = 128`. Every one of DM-HeatRay's 1912 BSP surfaces
then lands on UT3's own repeat distance to the unit.

The evidence that UE3 is size-independent is in the data: across the map,
`|TextureU|` clusters on 1, 1/2, 1/3, 1/4, 1/5 -- the reciprocals of UnrealEd's
surface scale -- and the *same* values appear on 512, 1024 and 2048 textures
alike. If UE3 divided by texture size, artists would have needed different
values per texture size to get consistent tiling.

**The rule this replaced was fitted to a bad measurement**, and it is worth
recording how it survived. It read `|TextureU_UE2| = |TextureU_UE3| * 4 *
exported/declared`: a "flat 4x between the engines" plus a correction for
reduced textures. The flat 4 was really `exported/128` evaluated at 512, so it
was right for every 512 texture and wrong everywhere else -- and the second data
point used to confirm it, a 2048/1024 brick, had been mis-measured as 0.5 when
it should have been 2.0. Two points, one of them wrong, and a two-term formula
fits both. The symptom was brick siding tiling four times too coarsely while a
1024 sidewalk beside it was only two times off and read as fine.

Static meshes need none of this -- their UVs are normalised.

### Alpha channels are usually not transparency

UE3 routinely packs specular, gloss or detail masks into a diffuse texture's
alpha, so a DXT5 format says nothing about whether a surface is see-through.
`T_LT_Floors_BSP_Organic05b_D` has alpha ranging 88-165 and is a solid floor.
Only the *material* knows: it is transparent if something drives Opacity or
OpacityMask, or if BlendMode is not opaque. Setting `ALPHA=1` on import from the
texture format alone renders solid walls half-transparent. Across DM-HeatRay
exactly one BSP texture is genuinely transparent -- a fence.

## The CSG paradigm flip

UT2004 levels are **subtractive** -- the world begins as solid rock and rooms are
carved from it. UT3 levels are **additive** -- the world begins empty and
geometry is added. Importing UT3 brushes into UT2004 unchanged leaves them
entombed in solid space, so the converter emits one large `CSG_Subtract` box
around the whole map as the first actor, before any converted brush. UE2 applies
CSG in actor order, so ordering matters.

### The first Brush in a t3d is eaten as the builder brush

UT2004's level importer takes the **first actor of exact class `Brush`** and
makes it the level's active (builder) brush: it is moved into `Actors(1)` and
never built into the BSP (`Editor/Src/UnEdFact.cpp:647`; the slot convention
`Actors(0)=LevelInfo`, `Actors(1)=builder` is asserted at `:533`). UnrealEd's own
map exports satisfy this by writing the builder brush first.

A converted map must therefore emit a throwaway builder brush before any real
geometry. Without one the world subtract brush -- the first brush in the file --
is silently consumed, no subtraction happens, and every additive brush stays
entombed in solid space. The symptom is a map that imports and builds without
errors but shows almost nothing, and a builder brush sitting where the world box
should be.

Note this bites only exact-class `Brush` actors; volumes (BlockingVolume and the
rest) are subclasses and never take the slot.

UE2 also clamps the world to +/-262144uu (`HALF_WORLD_MAX`, Engine/Inc/Engine.h);
the converter warns if converted geometry would reach past that.

Brush polygons are **outward-facing for both operations** in both engines -- the
editor reverses them itself when filtering a subtraction into the BSP
(`SubtractBrushFromWorldFunc`, Editor/Src/UnBsp.cpp:1146). Nothing needs
flipping on the way across.

### CSG order comes from Level.Actors, not the export table

Both engines apply brush CSG in level-actor order, and DM-HeatRay interleaves
its 312 brushes across 62 add/subtract runs -- so the order is not a detail. The
export table is sorted by name (`Brush_0, Brush_10, Brush_102...`), which is
*not* the authoring order (`Brush_0, Brush_259, Brush_440...`). `Level.Actors`
is natively serialized as a TTransArray (count, max, owner, then one
PackageIndex each) right after the Level's property list; `ut3/objects/level.py`
reads it.

### The first texture in a diffuse chain is usually the wrong one

A UE3 diffuse input is an expression graph, and the graph does not put the base
colour first. It routinely multiplies a reflection or detail term in before
adding the flat texture the surface is actually painted with, so walking to the
first texture sample down the "A" input lands on a cubemap or a specular map.
DM-Deck's `M_LT_Floors_BSP_Master` reaches `T_UN_CubeMaps_Robot_Paint01` two
levels before `T_LT_Floors_BSP_Organic11_D`; because that material is shared by
the floor brushes and the meshes standing on them, one bad pick mistextures both
at once.

Collect every reachable sample instead and score the names. Two rules matter:

- **Single-letter channel markers only count at the end of a name.** Matching
  `_c` anywhere is what made the cubemap win in the first place --
  `T_UN_CubeMaps_Robot_Paint01` contains one and so scored as a colour map,
  tying the real `_D` diffuse and winning on graph order.
- **"cubemap" and "falloff" are strong negatives.** A reflection probe is never
  the diffuse, however the graph multiplies it in.

Measured effect: 5 wrong textures out and 9 real `_D` diffuses in on DM-HeatRay
(building facades, shop fronts, windows, cement blocks), 2 swapped on DM-Deck.

### A material instance does not replace its parent, it edits it

`MaterialInstanceConstant` overrides named parameters and inherits the rest, so
the textures it *names* are only the ones it changed. Resolving an instance by
picking the best of its own `TextureParameterValues` and stopping there reads a
partial override as the whole material.

CTF-FacingWorlds' cliffs are the case that shows it. The chain is three
instances deep, and the leaf overrides `Normal` alone:

    M_UN_Rock_SM_Cliffs01_CaveWall_01_INST     Normal -> T_UN_Rock_SM_Blackspire01_N
      M_UN_Rock_SM_Cliffs01_CaveWall_01        DiffuseTexture -> T_UN_Cave_Rock_Big_Wall_D
        ..._MI_Master_SubMaster_05             DiffuseTexture -> T_UN_Terrain_FloorStone_Rock01

So the only texture the leaf names is a normal map, and the diffuse is one level
up. Resolve the parent too and keep whichever scores better *as a texture* --
the parameter name helps choose among one instance's own overrides, but what
gets drawn is the texture.

**A normal map drawn as diffuse is unmistakable**: iridescent blue, cyan and
magenta, because that is what tangent-space normals look like as colour. Worth
recognising on sight, and worth refusing outright -- a name scoring at or past
the normal-map penalty is not a colour map at all, and the neutral grey
placeholder is the better answer. FacingWorlds' ocean has no diffuse anywhere in
its chain and takes the placeholder.

### Some materials are instructions, not textures

`EngineMaterials.RemoveSurfaceMaterial` is how a UE3 mapper hides a BSP face:
the surface still blocks, it just is not drawn. It is not a texture and must
become `PF_Invisible` in UE2. On DM-HeatRay it covers **393 polygons and
116 million uu2 -- more surface area than any real material in the map** -- so
treating it as a missing texture fills the level with large blank walls.

`EngineMaterials.DefaultMaterial` (733 more polys) is the genuine "no material
assigned" case and does render in UE3, so those keep the placeholder texture.

### The builder brush must be skipped

The level's first Brush actor is UnrealEd's builder brush -- the shape template,
not level geometry. Its signature: no explicit `CsgOper`, group "Cube", and its
model is named exactly `Brush` where every real brush's model is `Model_<n>`.
Converting it drops a stray cube into the middle of the map.

## Coverage on DM-HeatRay.ut3

27937 of 28400 exports yield a property list. The rest are `Package` (390) and
`Class` (25) stubs plus ~48 objects that serialize no properties at all.


## UE2's 65536uu far clipping plane

`FAR_CLIPPING_PLANE = 65536.f` (Core/Src/Core.cpp:197) is never reassigned
anywhere in the engine, and it is passed straight to `FPerspectiveMatrix` when
the camera's projection is built (Engine/Src/UnRender.cpp:1510). It is therefore
a hardware depth clip, not a cull distance: geometry further from the camera
than 65,536uu is not drawn, whatever its actor settings say.

The zone properties that look like the knob for this are not. `bDistanceFog` and
`DistanceFogEnd` only widen the frustum *culling* plane:

    FarClip = (ViewZoneInfo->bDistanceFog && ...) ?
        Max(ViewZoneInfo->DistanceFogEnd, ...) : FAR_CLIPPING_PLANE;
    ViewFrustum.BoundingPlanes[4] = FPlane(ViewOrigin + Z * FarClip, Z);
        -- Engine/Src/UnRender.cpp:1066

Raising them lets the engine consider distant geometry for rendering, but the
projection still clips it. Wireframe viewports skip the far plane entirely
(`if(!Viewport->IsWire())` at UnRender.cpp:1061), which is why a too-large sky
dome looks fine in the editor's ortho views and is cut off in the 3D view.

Consequence for conversion: UT3 geometry more than ~65,000uu from the play area
cannot simply be carried across. A sky dome has to be either shrunk under the
limit or moved into a SkyZoneInfo room, which is drawn with the viewer's
rotation but not translation and so is never subject to it.


## UE3 meshes carry several UV sets; the material picks one

`FStaticMeshVertexBuffer` writes `NumTexCoords` before the vertex array, and
each vertex holds that many UV pairs interleaved after its three packed
normals. Reading only the first set is wrong whenever the material asks for
another: a `MaterialExpressionTextureSample` has a `Coordinates` input, and when
it is wired to a `MaterialExpressionTextureCoordinate` that node's
`CoordinateIndex` (plus `UTiling`/`VTiling`) names the set to sample.

UT3's sky dome is the case that shows it. `S_UN_Sky_SM_Dome01` has two sets:

    channel 0   U 0..1 azimuth, V -1..0 latitude   (polar map)
    channel 1   apex -> (0.5, 0.5), rim -> circle  (disc projection)

and `M_UN_Sky_SM_Invasion2` samples channel 1. Exporting channel 0 puts all 32
apex vertices on one line of the texture, so the sky converges into a fan of
wedges at the zenith. Channel 1 puts them all at the texture centre.

UT2004 stores one UV set per mesh -- and not even per vertex: the ASE importer
derives a per-triangle planar mapping with `FTexCoordsToVectors`
(Core/Inc/UnMath.h:2912) -- so the chosen set has to be baked in at export.

Note also that `FStaticMeshElement`'s `MaxVertexIndex` cannot be trusted for
this: the dome reports 295 for a 592-vertex mesh. Take the vertices an element
covers from its own slice of the index buffer instead.


## Brushes that do not enclose a volume

UE3 lets a `UModel` hold any set of `FPoly`s, and DM-HeatRay ships five brushes
that are not closed hulls -- four are a single flat face, one is an 18-face
open shell. UT3 does not care. UE2 very much does: `CSG_Add` on an open brush
solidifies an entire **half-space**, because the CSG has only the polygon planes
to work from and nothing closes the volume.

In game that is an invisible plane. Cross it -- on foot or in mid-air -- and the
pawn's Location resolves into zone 0, which is solid space, and the engine kills
it outright:

    if ( bCollideWorld && (Region.ZoneNumber == 0) && !bIgnoreOutOfWorld )
    {
        debugf( TEXT("%s fell out of the world!"), GetName());
        eventFellOutOfWorld(KILLZ_None);
        -- Engine/Src/UnPhysic.cpp:336

`FellOutOfWorld` dies with `class'Fell'` (Pawn.uc:1162), so the message is the
same "left a small crater" that a fatal fall gives -- but there is no fall, and
KillZ is never involved. The giveaway is the `fell out of the world!` line in
the log.

So `is_closed_solid` (ut3/objects/model.py) checks that every edge is shared by
exactly two faces, once in each direction, and the converter drops any brush
that fails. It also decides between candidates when a model's Polys has to be
located by search: an unrelated Polys yields a stray face or two, and accepting
it would plant one of these killing planes.

Note `find_polys` only has to guess for models that do not own their Polys as a
sub-object -- 446 of DM-HeatRay's 611 models do, and there the outer settles it.


## UT3 float drift vs UE2's 0.1 plane tolerance

UE2 answers "which side of this plane is the point on" with a fixed thickness --
`THRESH_POINT_ON_PLANE` is 0.10 (Core/Inc/UnMath.h:2101) -- and CSG, zoning and
collision all rest on that answer being self-consistent.

UT3 brush vertices are not on the grid. DM-HeatRay's are full of values like
`-571.598633` and `-333.80957`, so faces that were authored flush come out on
planes a few thousandths of a unit apart. The stair brushes at 1242, 3943 share
a slope whose four faces sat at offsets 0.007 to 0.018 apart. UT3 does not care.
UE2 calls them the same plane while the arithmetic says otherwise, and the CSG
produces slivers and mis-classified space.

The symptom is a pawn dying instantly in mid-air, because space that should be
empty is solid:

    if ( bCollideWorld && (Region.ZoneNumber == 0) && !bIgnoreOutOfWorld )
    {
        debugf( TEXT("%s fell out of the world!"), GetName());
        eventFellOutOfWorld(KILLZ_None);
        -- Engine/Src/UnPhysic.cpp:336

Zone 0 is solid space. There is no fall and KillZ is never consulted, but the
death message is the same "left a small crater" a fatal fall gives, because
`FellOutOfWorld` dies with `class'Fell'` (Pawn.uc:1162). The `fell out of the
world!` line in the log is what tells the two apart.

Orientation must be ignored when matching planes. Where two solids abut, the
faces that meet point *at each other*, so comparing only same-facing normals
leaves exactly the pairs that matter untouched. DM-HeatRay's spawn block and the
ramp beside it met at X=1344.000 and X=1344.001 with opposed normals -- a
thousandth of a unit of void trapped between two solids, well inside the 0.1
threshold, and the region around it rendered differently depending on which side
the player approached from.

`convert/align.py` fixes it the only way that keeps the geometry valid. Snapping
vertices to the grid removes the near-coplanar pairs but tilts sloped faces off
their own plane by up to 0.59uu, which breaks the same tolerance from the other
side. Instead the *planes* are clustered first -- grouped by direction, then
runs of offsets closer than the tolerance collapsed onto their mean -- and each
vertex is rebuilt as the intersection of the faces meeting there. Every polygon
then lies exactly on its plane, the brush keeps its topology, and no vertex
moves more than a fraction of a unit. On DM-HeatRay it takes the count of
near-coplanar pairs inside UE2's threshold from 375 to 26, and the ramp cluster
that was killing players to identical planes within a single float ulp.


## Reading solidity back out of a built .ut2

UE2 kills anything whose Location resolves into a solid BSP leaf -- zone 0 --
and reports it as falling out of the world (Engine/Src/UnPhysic.cpp:336). Such a
region is invisible and lethal, so guessing at it from the source geometry is
hopeless; the answer is in the built map.

`tools/ut2bsp.py` parses a .ut2's UModel far enough to run the engine's own
`UModel::PointRegion` (Engine/Src/UnTrace.cpp:760), which is a plain walk from
node 0 taking `iChild[IsFront]` and reading `iZone[IsFront]` off the last node.
`tools/verify_solidity.py` then replays the .t3d's CSG over a grid and reports
every point where the two disagree.

Two traps in the UModel layout, both of which produce a parse that looks nearly
right (correct node count) while the fields are nonsense:

* `FBspNode::Projectors` is only serialised when the archive is neither saving
  nor loading (Engine/Src/UnModel.cpp:143), so a file on disk never carries it.
* `iChild[0]` is iBack and `iChild[1]` is iFront (Engine/Inc/UnObj.h:119).

Sanity checks worth running on any parse: every plane normal should be unit
length, zone numbers should be small (UE2 allows 64), a point inside a known
brush must come back solid, and one outside the world must come back zone 0.


## PF_Invisible turns open space solid

UT3 marks faces it does not draw with `RemoveSurfaceMaterial`, and the obvious
translation is UE2's `PF_Invisible`. It is the single most damaging thing this
converter did.

    // Editor/Src/UnBsp.cpp:242
    if( Surf->PolyFlags & (PF_Invisible|PF_Portal) ) NodeFlags |= NF_NotVisBlocking;

    // Editor/Src/UnVisi.cpp:1170 -- zone assignment
    AssignAllZones( iFront, Outside ||  Node.IsCsg(NF_NotVisBlocking) );
    AssignAllZones( iBack,  Outside && !Node.IsCsg(NF_NotVisBlocking) );

`IsCsg(NF_NotVisBlocking)` is false for such a node, so zone assignment stops
treating the face as the boundary between inside and outside. The open space
beyond it inherits "inside" and is written out as zone 0 -- solid. Anything
whose Location lands there is killed instantly as having fallen out of the world
(Engine/Src/UnPhysic.cpp:336), with no fall and no KillZ involved.

The flag exists for invisible *collision hulls*, where the far side genuinely is
inside the same solid. On an ordinary brush face it inverts the meaning of the
surface.

Measured by rebuilding DM-HeatRay with only that flag cleared, and reading the
built BSP back with `tools/ut2bsp.py`:

    brushes   with PF_Invisible          without
    2         5.77% of space wrongly     0.00%, 0 malformed nodes
              solid, 1 malformed node
    304       10.51%, 299 malformed      0.00%, 0 malformed nodes

So the faces are drawn instead. For geometry UT3 chose to hide, that is almost
always buried inside other geometry where nobody sees it.

Worth keeping in mind for any other UE3 flag that looks like a rendering hint:
in UE2 several of them carry structural meaning for CSG and zoning. `PF_Portal`
sits behind the same line, and `PF_NotSolid`/`PF_Semisolid` remove a face from
CSG entirely.


## Inline bulk data can be compressed

A Texture2D's mips carry an `FByteBulkData` header of
`(flags, count, sizeOnDisk, offsetInFile)`. The obvious reading is that flag
0x01 (`BULKDATA_StoreInSeparateFile`) means "fetch it from the content package"
and its absence means "the bytes follow inline, verbatim". The second half is
wrong. An inline payload can also be LZO-compressed, flagged 0x10
(`BULKDATA_SerializeCompressedLZO`), in exactly the chunk format used for the
external ones -- magic, block size, sizes, then LZO blocks.

Slicing those bytes raw yields a payload whose length does not match the format's
expected size, so it gets discarded as corrupt. The failure is quiet and
uneven: a texture keeps whichever mips happen to be stored uncompressed, so it
still exports, just at a lower resolution. A texture whose mips are *all*
compressed loses every one and is dropped, and the meshes using it render
untextured -- which is how this was noticed, via one I-beam in DM-HeatRay.

On DM-HeatRay, of 122 textures: 2 were being dropped entirely, and 119 were
exporting at a lower resolution than the source had available.

The decompressor was already there for the external path; the inline path simply
never called it.


## Per-actor material overrides on StaticMeshComponent

A UE3 `StaticMeshComponent` carries a `Materials` array that overrides the
mesh's own materials for that actor alone. It is not an edge case: 382 of
DM-HeatRay's 2,432 mesh components use it, and the mesh's built-in material is
often a placeholder that resolves to something meaningless -- the window frame
`S_LT_Walls_SM_WinFrame01a` resolves to a cubemap falloff texture, while the
override on one of its actors is the animated advertising material whose diffuse
is the sign artwork.

UT2004 keeps materials on the mesh with no per-actor override, so a mesh used
with more than one material set has to be exported once per set. Only 23 of
DM-HeatRay's 179 meshes need that, taking the exported count from 177 to 208.

The trap when keying those variants: object references are distinct instances
per actor and do not compare by value, so keying on the reference objects splits
nearly every actor into its own mesh -- 509 meshes instead of 208, tripling the
triangle count. Key on the resolved path string instead.

Animated UE3 materials have no UE2 equivalent, so what comes across is the
diffuse the graph walk finds, which for these signs is the static artwork behind
the animation.

## SoundNodeWave: Ogg in the package, WAV in UT2004

A cooked `SoundNodeWave` serializes four bulk blocks after its property list --
`RawData` (editor PCM, stripped), `CompressedPCData`, `CompressedXbox360Data`,
`CompressedPS3Data` -- with the same `(flags, count, sizeOnDisk, offsetInFile)`
headers a texture mip uses. All 107 of DM-HeatRay's waves carry the PC payload
inline, so nothing has to be chased into a content package: the first block
whose payload starts `OggS` is the Ogg Vorbis stream.

UT2004 stores a `USound` as a whole file and the only importable format is WAV
(`USoundFactory` registers just `wav`, Editor/Src/UnEdFact.cpp:1741), so the
Ogg has to be decoded out and back in through `#exec AUDIO IMPORT`. That costs
size -- DM-HeatRay's 36 ambient waves are 5.2 MB as 16-bit PCM.

**Mono is mandatory, not preferred.** ALAudio refuses to build a buffer for a
stereo `USound` at all -- `debugf("Shouldn't use stereo sound")` then returns
NULL, ALAudio/Src/ALAudioSubsystem.cpp:1892 -- so a stereo import is simply
silent. Every wave is downmixed on the way through.

`NumChannels` cannot be trusted to catch those: like every UE3 property it is
elided when it matches the archetype, so DM-HeatRay's stereo wind bed claims to
be mono. The Ogg's own Vorbis identification header is authoritative.

## Distributions hide their values in the archetype chain

The ambient sound nodes state radii, volume and pitch as `RawDistributionFloat`,
pointing at a `DistributionFloatUniform` whose `Min`/`Max` are written only when
the mapper changed them. Reading the instance alone therefore yields 0 for
everything left alone -- a pitch of 0 rather than 1, a `MaxRadius` of 0 rather
than 5000 -- which silently mutes or de-tunes about a third of the map's
ambients.

The defaults live at the end of an archetype chain that runs out of the map and
into `Engine.u`: `SoundNodeAmbient_2` -> `Default__AmbientSoundSimple.
SoundNodeAmbient0.DistributionPitch` (empty) -> `Default__SoundNodeAmbient.
DistributionPitch` (`Min=1, Max=1`). `Export.archetype` is a plain package
index, so `PackageIndex.resolve(pkg, pkg.ref(export.archetype))` walks it like
any other reference. The values that matter: MinRadius 400, MaxRadius 5000,
Volume 1.0, Pitch 1.0, DelayTime 1.0.

Elision is per property, not per object -- `AmbientSoundNonLoop_0`'s DelayTime
writes `Max=5` and inherits `Min=1` -- so merge with the archetype key by key.

## The two engines do not share a falloff curve

UE3 `ATTENUATION_Logarithmic` is full volume inside `MinRadius`, silent at
`MaxRadius`, and logarithmic between, so half volume falls at the geometric
mean of the two.

UE2 hands the radius to OpenAL as `AL_REFERENCE_DISTANCE` under
`AL_INVERSE_DISTANCE_CLAMPED` with rolloff 1 (ALAudioSubsystem.cpp:383 and
:609): full volume inside `SoundRadius`, then `SoundRadius/distance`, so half
volume falls at `2*SoundRadius`. It never reaches zero -- the engine stops the
sound at `100*SoundRadius` instead (`GAudioMaxRadiusMultiplier`,
Core/Src/Core.cpp:179, used in UnAudio.cpp:189).

Matching the half-volume distance is what keeps a sound covering the same part
of the map: `SoundRadius = sqrt(MinRadius*MaxRadius)/2`. Half of DM-HeatRay's
ambients declare `MinRadius` 0 or 1, where UE3's log curve is degenerate and the
geometric mean collapses, so cap the ratio (at 20) before taking the root.

Two limits have no UE2 answer. A UE3 ambient can set `bSpatialize=False` for a
2D bed; UT2004 has no such flag for a level actor, so a global wind ends up
panned towards wherever UT3 parked it. And on the `SoundEmitters` path
`SoundVolume` is scaled by 4 rather than divided by 255 (UnActor.cpp:96 against
:138), so anything above 2 is already full volume after the 0..1 gain clamp --
one-shot emitters cannot hold a relative volume.

## Matinee stores movement where UT2004 does, near enough

UT3 animates scenery from Kismet: a `SeqAct_Interp` names an `InterpData`, whose
`InterpGroup`s each own an `InterpTrackMove` holding a `PosTrack` and an
`EulerTrack`; the group binds to an actor through the action's *variable links*,
matching `LinkDesc` against `InterpGroup.GroupName`. There is no persistent
`InterpGroupInst` in the file -- those are built at runtime -- so the variable
links are the only route from a track to an actor.

UT2004's `Mover` keeps the same data on the actor: `KeyPos[24]`/`KeyRot[24]`
offsets from where the mover is placed (`Location = BasePos + KeyPos[KeyNum]`,
UnMover.cpp:63), one `MoveTime` per leg. Note 24, not the 8 UT99 had, and both
arrays are plain `var` rather than `var()` -- not editable in UnrealEd's
property window, but a t3d sets them like anything else.

Three things do not carry across:

**Timing.** UE3 keys sit at arbitrary times; UT2004 spends the same `MoveTime`
on every leg. Using the UE3 keys directly would smear a 0.7s leg and a 3.9s leg
into the same duration, so the curve is resampled at even intervals instead
(`FInterpCurve::Eval` scales tangents by the segment duration, so it can be
sampled anywhere). A two-point track is left as two keys, which reproduces it
exactly.

**Frame.** `IMF_World` states world transforms, so its keys are the track value
minus where the actor is placed. `IMF_RelativeToInitial` states motion about the
actor's initial transform, so its keys are the track value minus its own value
at t=0.

Both leave `KeyPos[0]` at (0,0,0), and that is load-bearing rather than tidy.
UnrealEd draws a mover at `BasePos + KeyPos[KeyNum]`, so a non-zero key 0 shifts
the actor off its mark **in the editor viewport as well as in game** -- which is
the visible symptom if you get this wrong. It also means the pivot never has to
be reasoned about: whatever offset the mesh has from its origin, the mover
inherits the placed transform untouched and only the motion comes from the track.

The relative rule is measured rather than read out of UE3. UE3 composes such a
track *through* the initial transform, i.e. `RelTM * BaseTM`, which would rotate
the motion by the placed rotation too, and DM-HeatRay's train sits at 180
degrees of yaw -- so that reading sends it along -X, and composing the full
transform additionally lifts it 244uu off its deck and 3284uu sideways into open
air. Both were tried in the editor and neither is what UT3 does. The train's own
viaduct settles it: the deck runs along y=3284 at z=436, its support pillars
(`S_HU_Pillars_SM_Pillar02_Beam`) carry it from x=-6560 through the play area,
and the five carriages are queued along it towards -X. The train runs +X from
where it is parked, which is the unrotated delta.

Note this is *not* in tension with attachment, which does rotate: the same
train's followers state a `RelativeLocation` of +6788 X and land at -6788 X in
the world. Hard attachment and the Matinee move frame are separate mechanisms.

**Triggering.** Kismet does not convert, so a mover needs a UT2004 state. The
map itself says which: `bLooping` on the action, or a `Completed` output that
leads back to the same action within a couple of hops (UT3's idiom for repeating
scenery is `Completed -> Delay -> Play`), means it runs forever and becomes
`ConstantLoop`. DM-HeatRay's cinematic ship instead fires 120 seconds after a
scripted death, so it becomes a dormant `TriggerToggle` that keeps its path.

Two more traps in the surrounding data:

- **Attachment.** UE3 hard-attaches the four trailing carriages to the lead one.
  UT2004 movers cannot usefully be parented from a t3d, but they do not need to
  be -- keys are relative to each mover's own placed position, so handing every
  follower the same key list moves the train rigidly. Exact for translation;
  approximate once the leader rotates, since a follower then turns on its own
  pivot instead of orbiting.
- **The void has to cover the path.** An actor outside the subtract brush is in
  solid space and renders nowhere, so a mover whose path leaves the void
  vanishes mid-run. DM-HeatRay's train travels 70,021uu and nearly doubles the
  world brush. That costs no BSP nodes -- the void is one convex box either way.

`AmbientSound` rides along: the engine hum on each carriage is a plain Actor
property, so it moves onto the Mover rather than staying parked as its own
actor at the start of the line.

## UE3 keeps opacity in its own texture; UE2 cannot follow

A UE3 material graph samples opacity from whatever texture it likes, and the
house style is a `..._M` mask beside the `..._D` diffuse, read from one channel.
UE2 has no such indirection: a surface is cut out or blended by the alpha of the
one texture it draws. 12 of DM-HeatRay's 17 masked materials keep their mask
elsewhere, so leaving it there means every fence, crosswalk, manhole, trash pile
and plant renders solid.

The mask therefore has to be composited into the exported texture's alpha. DXT1
has no alpha to composite into, which is what these diffuse maps are, so they
are repacked as **DXT5** -- much cheaper than it sounds, because a DXT5 block is
an 8-byte alpha block followed by a colour block in *exactly* DXT1's layout. The
colour half copies through byte for byte and only the alpha half is built, from
two fixed endpoints (255 and 0) and 3-bit indices.

The trap is DXT1's three-colour mode. A block whose first endpoint is not
greater than the second encodes three colours plus transparent, and *most* of
these textures are mostly that: 62% of the ivy atlas's blocks, 88% of the
chainlink's. UE3 never samples that alpha -- the material reads RGB and gets the
artwork's black background, which is exactly the "black where transparency
should be" symptom -- but a DXT5 block has no such mode, so reinterpreting one
unchanged keeps the three-colour palette and turns those texels black for good.
Such blocks get their endpoints swapped to force the four-colour ramp and their
indices remapped; the two endpoint colours stay exact, the midpoint shifts by a
sixth of the endpoint spread, and the transparent index takes a neighbouring
colour rather than black (which is what texture filtering wants at a cutout
edge anyway).

One rule falls out of this: **never set `MASKED=1` or `ALPHA=1` on a DXT1
export**. There is no alpha channel to test, so at best it does nothing, and at
worst UE2 reads the three-colour blocks' fourth index as transparent and punches
holes UT3 never had. Three of DM-HeatRay's materials declare `BLEND_Masked` with
no expression driving `OpacityMask` at all; those stay opaque and are reported.

## Lifts state themselves, and both engines use the same word for it

UT3 marks a lift with `LiftCenter.MyLift`, pointing straight at the actor, and
its bots path over `LiftCenter`/`LiftExit` nav points exactly as UT2004's do.
That makes lifts the one kind of mover that needs no inference at all: the map
names them. `SeqEvent_Mover`, UE3's "a pawn used this mover" event, is a second
signal and agrees with `MyLift` on both of DM-Deck's lifts.

UT2004 wants such a mover in `StandOpenTimed` specifically -- `LiftCenter.
SpecialHandling` tests for that state by name (Engine/LiftCenter.uc:38) before
deciding whether a bot needs to trigger the lift -- so the state is not
interchangeable with the other open-timed ones. The nav points bind by
`LiftTag` against the mover's `Tag` rather than by a direct reference, and UT3
leaves `MyLift` unset on its exits (it pairs them at runtime), so each exit goes
to its nearest centre. `UTJumpLiftExit` is UT2004's `LiftExit` with
`bLiftJumpExit=True`.

Collision comes free: `AActor::GetPrimitive` returns the `StaticMesh` when one
is set (UnActor.cpp:2269) and `AMover` does not override it, so a static-mesh
mover collides on its real shape rather than Mover's 160x160 default cylinder.
A lift also keeps the engine's `ME_ReturnWhenEncroach` rather than the
ignore-everything setting background scenery gets, because it has to carry
players rather than pass through them.

## Multi-part movers: bSlave, not copied keys

`Mover::PostBeginPlay` walks every `Mover` sharing its `Tag` and calls
`SetBase(Self)` on the ones with `bSlave` (Engine/Mover.uc:454). That is UE2's
own answer to UE3 hard attachment, and it is better than giving each follower a
copy of the leader's key list: attachment carries rotation, whereas duplicated
keys only translate, so a rotating leader would leave its followers spinning on
their own pivots. Slaves need a matching `Tag` and nothing else -- no keys, no
state, no `MoveTime`.

Two traps. `Mover`'s default `Tag` is the class name, so *every* converted mover
shares `Tag='Mover'` unless one is set explicitly -- fine until the first
`bSlave` appears, at which point every slave in the level attaches to whichever
leader gets there first. And a slave writes no `KeyPos`, so anything reasoning
about how far a mover travels (the enclosing void, here) has to take the reach
from its leader rather than from the emitted properties.

## Matinee only plays [0, InterpLength]

A track's keys are free to sit outside the window the sequence actually plays,
and authors do use that: DM-Deck's `InterpActor_16` carries a key at t=-2.196
describing the descent the lift makes *before* its Matinee starts. Sampling a
track over its own key extent therefore converts that lift as dropping 605uu
through the floor and climbing back, instead of the 608uu rise it performs.
Clip to `[0, InterpLength]` -- intersected with the keys' own range, since the
length may also run past the last key.
