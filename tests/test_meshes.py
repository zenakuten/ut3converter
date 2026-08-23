#!/usr/bin/env python3
"""Regression tests for static mesh conversion (Phase 2).

    python3 tests/test_meshes.py [path/to/DM-HeatRay.ut3]
"""

import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from convert.meshes import MeshSet, _component_of, convert_actors, export_meshes
from convert.textures import TextureSet
from ut2.ase import write_ase
from ut3.objects.staticmesh import read_static_mesh, validate
from ut3.package import Package
from ut3.props import read_object_properties
from ut3.resolve import PackageIndex

DEFAULT_MAP = (
    "/home/josh/.steam/steam/steamapps/common/Unreal Tournament 3/"
    "UTGame/CookedPC/Maps/DM-HeatRay.ut3"
)

_failures = []


def check(label, got, want):
    if got == want:
        print("  ok    %s = %r" % (label, got))
    else:
        print("  FAIL  %s = %r (expected %r)" % (label, got, want))
        _failures.append(label)


def check_that(label, cond, detail=""):
    if cond:
        print("  ok    %s %s" % (label, detail))
    else:
        print("  FAIL  %s %s" % (label, detail))
        _failures.append(label)


def _find_map(reference, name):
    """Locate another UT3 map relative to the one under test.

    UT3 keeps maps in three directories -- Maps, Private/Maps and UT3G/Maps
    (the Titan Pack) -- so search from CookedPC rather than assuming a sibling.
    """
    root = os.path.dirname(os.path.abspath(reference))
    while root != os.path.dirname(root) and os.path.basename(root) != "CookedPC":
        root = os.path.dirname(root)
    for dirpath, _dirs, files in os.walk(root):
        if name + ".ut3" in files:
            return os.path.join(dirpath, name + ".ut3")
    return None


def main(path):
    p = Package(path)
    index = PackageIndex.for_map(path)

    print("mesh parsing")
    meshes = p.exports_of_class("StaticMesh")
    check("static meshes in package", len(meshes), 205)
    parsed = valid = 0
    problems = []
    for e in meshes:
        m = read_static_mesh(p, e)
        if m is None:
            problems.append((e.name, "parse failed"))
            continue
        parsed += 1
        ok, why = validate(m)
        if ok:
            valid += 1
        else:
            problems.append((e.name, why))
    check("meshes parsed", parsed, len(meshes))
    # validate() checks index range, triangle counts against the elements, and
    # that every vertex lies inside the mesh's own serialized bounds.
    check("meshes passing validation", valid, len(meshes))
    for name, why in problems[:5]:
        print("        %s: %s" % (name, why))

    print("a known mesh")
    mesh = read_static_mesh(p, p.find("S_ASC_Floor_SM_StairsSid01")[0])
    lod = mesh.lod0
    check("elements", len(lod.elements), 1)
    check("triangles", len(lod.indices) // 3, 196)
    check("triangles match the element", lod.elements[0].num_triangles, 196)
    check("vertices", len(lod.positions), 268)
    check("one UV per vertex", len(lod.uvs), len(lod.positions))
    check_that("UVs are in a sane range",
               all(-16 <= u <= 16 and -16 <= v <= 16 for u, v in lod.uvs))

    print("ase round-trip")
    tmp = tempfile.mkdtemp(prefix="ut3conv-ase-")
    try:
        positions = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0)]
        uvs = [(0.25, 0.75), (0.5, 0.5), (0.0, 1.0)]
        faces = [(0, 1, 2, 0)]
        ase = write_ase(os.path.join(tmp, "T.ase"), "T", positions, uvs, faces, ["Tex"])
        text = open(ase, encoding="latin-1").read()

        verts = re.findall(r"\*MESH_VERTEX\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)", text)
        check("vertices written", len(verts), 3)
        # The importer multiplies positions by FVector(-1,1,1), so X is written
        # negated and the two cancel out.
        check("X is pre-negated for the importer",
              [float(v[1]) for v in verts], [-1.0, -4.0, -7.0])
        check("Y and Z pass through",
              [(float(v[2]), float(v[3])) for v in verts], [(2.0, 3.0), (5.0, 6.0), (8.0, 9.0)])

        tverts = re.findall(r"\*MESH_TVERT\s+(\d+)\s+(\S+)\s+(\S+)", text)
        # The importer computes V = 1.0 - V, so V is written flipped.
        check("V is pre-flipped for the importer",
              [round(float(t[2]), 4) for t in tverts], [0.25, 0.5, 0.0])
        check("U passes through", [round(float(t[1]), 4) for t in tverts], [0.25, 0.5, 0.0])

        faces_out = re.findall(r"\*MESH_FACE (\d+):\s+A:\s*(\d+) B:\s*(\d+) C:\s*(\d+)", text)
        check("faces written", len(faces_out), 1)
        check("winding order is preserved", faces_out[0][1:], ("0", "1", "2"))
        check_that("face carries a material id", "*MESH_MTLID 0" in text)
        # A material is only committed when *UVW_V_TILING is parsed.
        check_that("material block is complete",
                   "*BITMAP" in text and "*UVW_U_TILING" in text and "*UVW_V_TILING" in text)
        # *BITMAP has its last 5 characters stripped, then is matched by name.
        bitmap = re.search(r'\*BITMAP "([^"]+)"', text).group(1)
        # The importer strips the path, then the trailing 5 chars (.dds plus the
        # closing quote), and matches what is left against a loaded texture name.
        check("bitmap stem matches the texture name",
              (bitmap.split("\\")[-1] + '"')[:-5], "Tex")
        check_that("list sections close with two tabs", "\t\t}" in text)
        check_that("ase is CRLF", "\r\n" in open(ase, "rb").read().decode("latin-1"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("effect meshes")
    from convert.shaders import material_is_effect, mesh_is_effect
    # Unlit + non-opaque + no DiffuseColor = a procedural glow with no UE2
    # equivalent; anything with real surface colour must keep converting.
    # Phase 14: an effect mesh is kept when a UE2 material can be built for it,
    # which is the whole point of being able to build one -- the objection was
    # to drawing a glow as an opaque quad, not to the texture. With materials
    # off (--no-materials) the old behaviour has to come back exactly.
    flat = TextureSet("K", materials=False)
    kept, _k = convert_actors(p, index, MeshSet("K"), flat, skip_effects=True)
    allm, _a = convert_actors(p, index, MeshSet("A"), TextureSet("A", materials=False),
                              skip_effects=False)
    check("effect actors skipped when no material can be built", len(allm) - len(kept), 26)
    check_that("--keep-effect-meshes converts them", len(allm) > len(kept))
    rich = TextureSet("M")
    drawn, drawn_stats = convert_actors(p, index, MeshSet("M"), rich, skip_effects=True)
    check_that("with materials on they are drawn instead of skipped",
               len(drawn) > len(kept))
    check("and every one that came back has a material to wear",
          len(drawn) - len(kept), drawn_stats.drawn_effects)
    # Nothing is built until the textures are settled -- see
    # TextureSet.build_materials -- so at this point they are still pending.
    check_that("which needed non-opaque materials held for building",
               len(rich.pending) > 0, "%d pending" % len(rich.pending))
    for texture_name in list(rich.textures):
        rich.alpha_channel[texture_name] = False
    rich.build_materials(index)
    check_that("and those become real UE2 objects", len(rich.materials) > 0,
               "%d objects" % len(rich.materials))
    check_that("none of which is over the placeholder",
               all(rich.FALLBACK_NAME not in value
                   for _kind, props in rich.materials.definitions.values()
                   for _key, value in props))
    # Some effects the material test cannot reach at all:
    # S_UN_Volumetrics_FogVolume_Mesh_01 has an element whose material does not
    # resolve, so there is no BlendMode to judge and a light shaft converts as a
    # solid cone of geometry. UE2 draws no fog volume, so the name decides.
    from convert.shaders import mesh_name_is_effect

    check_that("a fog volume mesh is an effect by name",
               mesh_name_is_effect("S_UN_Volumetrics_FogVolume_Mesh_01"))
    # Not the same thing as a fog *sheet*: that is DM-Deck's goo surface, which
    # resolves properly and gets a stock UT2004 FinalBlend instead of dropping.
    check_that("a fog sheet is not caught by it",
               not mesh_name_is_effect("S_EV_FogSheet_01"))
    check_that("nor is ordinary geometry",
               not mesh_name_is_effect("S_ASC_Floor_SM_StairsSid01"))

    print("component transforms")
    # A UE3 StaticMeshComponent carries a transform relative to its actor, and
    # UT2004 has nowhere to put a second one, so it is composed into the actor's.
    # DM-Diesel is the map that showed it: 40 of its meshes hang on a component
    # pitched a quarter turn, and its pipes converted lying down.
    from convert.rotation import axis_images, multiply, rotation_matrix, to_rotator
    from convert.meshes import _effective_transform

    diesel_path = _find_map(path, "DM-Diesel")
    if diesel_path is None:
        print("  skip   DM-Diesel not found")
    else:
        dp = Package(diesel_path)
        didx = PackageIndex.for_map(diesel_path)
        pipe = [e for e in dp.exports if e.name == "StaticMeshActor_347"][0]
        pprops, _s, _e = read_object_properties(dp, pipe)
        pcomp = _component_of(dp, pipe, pprops)
        check("the actor is placed with a quarter turn of yaw",
              tuple(pprops.get("Rotation").value), (0, -16384, 0))
        check("and its component adds a quarter turn of pitch",
              tuple(pcomp.get("Rotation").value), (16384, 0, 0))
        placed, scale3d, offset = _effective_transform(pprops, pcomp)
        check("which compose into one rotator", placed, (16384, 49152, 0))
        # The point of the exercise: the mesh has to end up standing on end.
        composed = multiply(rotation_matrix((16384, 0, 0)), rotation_matrix((0, -16384, 0)))
        ours = rotation_matrix(placed)
        check_that("reproducing UE3's own transform",
                   max(abs(composed[i][j] - ours[i][j])
                       for i in range(3) for j in range(3)) < 1e-9)
        check("with nothing left to scale or offset", (scale3d, offset),
              ((1.0, 1.0, 1.0), (0.0, 0.0, 0.0)))
        _actors, dstats = convert_actors(dp, didx, MeshSet("D"), TextureSet("D"))
        check("DM-Diesel has 40 of them", dstats.component_transforms, 40)

    # Rotation and non-uniform scale do not commute, and 112 of the 202 such
    # actors in the stock maps carry both -- but every component rotation in
    # them is a whole quarter turn, which swaps scale factors between axes
    # rather than shearing them.
    images = axis_images(rotation_matrix((16384, 0, 0)))
    check("a quarter turn of pitch swaps X and Z", images, [2, 1, 0])
    check("a half turn leaves every axis where it was",
          axis_images(rotation_matrix((0, 32768, 0))), [0, 1, 2])
    check("and an eighth turn is not an axis swap at all",
          axis_images(rotation_matrix((0, 8192, 0))), None)
    for rotator in ((0, 0, 0), (16384, 0, 0), (0, 32768, 0), (0, 0, 16384),
                    (1000, -20000, 5000), (16384, 12345, 0)):
        m = rotation_matrix(rotator)
        back = rotation_matrix(to_rotator(m))
        check_that("a rotator survives the round trip through a matrix %s" % (rotator,),
                   max(abs(m[i][j] - back[i][j]) for i in range(3) for j in range(3)) < 1e-9)

    print("water surfaces")
    # UT3 water is procedural -- tint, refraction and fresnel are all shader
    # parameters and the only texture in the graph is a detail normal map, so
    # CTF-LostCause's pool arrives flat grey. It gets a stock UT2004 material.
    # The interesting half of this test is CTF-Coret, where the naive rule is a
    # disaster: 56 window frames wear M_LT_Base_BSP_Glass_Water_01 and eight of
    # them are level. They resolve a real texture, so none is touched.
    #
    # CTF-Nanoblack is the third case: its pools do resolve a texture, but
    # T_NEC_Nanoblack_WaterMask is an opacity channel -- solid red at a mean
    # alpha of 14/255 -- so the sheet renders as a 95% transparent red film and
    # reads as missing. A mask counts as no colour here, but only here: the
    # rain-puddle decals and glass panes that resolve masks elsewhere are not
    # water and keep them.
    WATER = "FinalBlend'UCGeneric.Glass.glass06_finalblend'"
    for map_name, want, mesh_name in (("CTF-LostCause", 1, "WaterPlane"),
                                      ("CTF-Nanoblack", 8, None),
                                      ("CTF-Coret", 0, None)):
        water_map = _find_map(path, map_name)
        if water_map is None:
            print("  skip   %s not found" % map_name)
            continue
        wp = Package(water_map)
        widx = PackageIndex.for_map(water_map)
        wactors, wstats = convert_actors(wp, widx, MeshSet("W"), TextureSet("W"))
        subbed = [a for a in wactors
                  if any(k == "Skins(0)" and v == WATER for k, v in a.properties)]
        check("%s water surfaces substituted" % map_name, len(subbed), want)
        check("and the stats agree", wstats.substituted_water, want)
        if map_name == "CTF-Nanoblack":
            check_that("the sheet reported missing is one of them",
                       any(a.name == "StaticMeshActor_72" for a in subbed),
                       str(sorted(a.name for a in subbed)))
        if mesh_name is not None:
            check_that("the substituted actor is the water plane",
                       all(mesh_name in dict(a.properties)["StaticMesh"] for a in subbed),
                       str([dict(a.properties)["StaticMesh"] for a in subbed]))

    print("actor conversion")
    texture_set = TextureSet("TestTex")
    mesh_set = MeshSet("TestTex")
    actors, stats = convert_actors(p, index, mesh_set, texture_set)
    check_that("actors converted", stats.actors > 2300, str(stats.actors))
    check_that("unique meshes collected", len(mesh_set.meshes) > 150, str(len(mesh_set.meshes)))
    check_that("every actor names a mesh",
               all(any(k == "StaticMesh" for k, _v in a.properties) for a in actors))
    check_that("actor names are unique", len({a.name for a in actors}) == len(actors))

    out_dir = tempfile.mkdtemp(prefix="ut3conv-mesh-")
    try:
        lines, stats = export_meshes(mesh_set, out_dir, index, texture_set, stats=stats)
        written = os.listdir(os.path.join(out_dir, "TestTex", "Meshes"))
        check("an exec line per exported mesh", len(lines), len(written))
        check_that("every exec references a file that exists",
                   all(os.path.exists(os.path.join(out_dir, "TestTex", "Meshes",
                                                   re.search(r"FILE=Meshes\\(\S+)", l).group(1)))
                       for l in lines))
        # "#exec STATICMESH IMPORT" only accepts .lwo; ASE goes through the
        # generic factory exec instead.
        check_that("exec lines use the ASE-capable factory path",
                   all(l.startswith("#exec NEW STANDALONE StaticMeshFactory") for l in lines))
        check_that("no mesh failed to export", not stats.failed, str(stats.failed[:3]))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    print("UV sets")
    # UE3 meshes carry several UV sets and the material says which to sample.
    # UT3's sky dome has a polar map in channel 0 that collapses every apex
    # vertex onto one line of the texture, and the disc projection its material
    # actually reads in channel 1 -- exporting channel 0 pinches the sky into a
    # fan of wedges at the zenith.
    from ut3.objects.material import material_uv_channel

    dome = [e for e in p.exports
            if p.class_name_of(e) == "StaticMesh" and "Sky_SM_Dome" in e.name]
    if dome:
        mesh = read_static_mesh(p, dome[0])
        lod = mesh.lods[0]
        check("the dome has two UV sets", len(lod.uv_sets), 2)
        element = lod.elements[0]
        channel, _u, _v = material_uv_channel(p, index, element.material)
        check("its material samples the second", channel, 1)
        apex_z = max(v[2] for v in lod.positions)
        used = set(lod.indices[element.first_index:
                               element.first_index + element.num_triangles * 3])
        apex = [i for i in used if lod.positions[i][2] > apex_z - 1e-3]
        check_that("every apex vertex the faces use shares one UV in that set",
                   len({lod.uv_sets[channel][i] for i in apex}) == 1,
                   "%d apex vertices -> %s"
                   % (len(apex), sorted({lod.uv_sets[channel][i] for i in apex})))
        check_that("and it is the middle of the texture, not an edge",
                   all(abs(c - 0.5) < 1e-3 for c in lod.uv_sets[channel][apex[0]]),
                   "%s" % (lod.uv_sets[channel][apex[0]],))
        check_that("channel 0 would instead spread them along one edge",
                   len({lod.uv_sets[0][i] for i in apex}) == len(apex),
                   "%d distinct" % len({lod.uv_sets[0][i] for i in apex}))

    print("component material overrides")
    # A UT3 StaticMeshComponent can override the mesh's own materials per actor,
    # and 382 of DM-HeatRay's mesh actors do. UT2004 keeps materials on the mesh
    # with no per-actor override, so a mesh used with two different material
    # sets has to be exported twice -- but only where the sets actually differ,
    # which is 23 of 179 meshes here. Keying variants on the material reference
    # objects instead of their paths splits nearly every actor into its own
    # mesh, which is how this was first got wrong (509 meshes instead of 208).
    from convert.meshes import _material_overrides
    from ut3.objects.level import ordered_exports

    overridden = 0
    for export in ordered_exports(p, ("StaticMeshActor", "InterpActor")):
        props, start, _end = read_object_properties(p, export)
        if start is None:
            continue
        comp = _component_of(p, export, props)
        if comp is not None and _material_overrides(p, comp):
            overridden += 1
    check_that("actors carrying a material override are seen", overridden > 300,
               "%d actors" % overridden)
    check_that("mesh variants stay close to the unique mesh count",
               180 < len(mesh_set.meshes) < 260, "%d meshes" % len(mesh_set.meshes))

    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))
