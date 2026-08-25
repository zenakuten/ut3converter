"""Static mesh conversion: UE3 StaticMesh/StaticMeshActor -> UT2004.

Meshes become ASE files imported by `#exec STATICMESH IMPORT` into the same
package as the textures (the ASE importer resolves `*BITMAP` names against
already-loaded textures, so they must share a package and the textures must be
imported first). Actors become t3d StaticMeshActors.
"""

import os
import re
import struct

from ut2.ase import write_ase
from ut2.t3d import Actor, rot, vec
from convert import collections
from convert.rotation import axis_images, multiply, rotate, rotation_matrix, to_rotator
from convert.shaders import (effect_is_drawable, effect_substitute, mesh_is_effect,
                             sheet_is_horizontal, water_substitute)
from ut3.objects.level import ordered_exports
from convert.textures import _material_key
from ut3.objects.material import material_uv_channel
from ut3.objects.staticmesh import read_static_mesh, validate
from ut3.props import read_object_properties

_SANITIZE = re.compile(r"[^A-Za-z0-9_]")

ACTOR_CLASSES = ("StaticMeshActor", "InterpActor", "UTDeployableNodeLocker",
                 # Warfare scenery whose mechanic does not convert but whose
                 # geometry should still be there -- see convert/onslaught.py.
                 "UTOnslaughtTarydiumMine_Content",
                 "UTOnslaughtTarydiumProcessor_Content")


def sanitize(name):
    out = _SANITIZE.sub("_", name or "")
    if out and out[0].isdigit():
        out = "_" + out
    return out or "Mesh"


class MeshStats:
    def __init__(self):
        self.meshes = 0
        self.actors = 0
        self.triangles = 0          # unique mesh triangles
        self.scene_triangles = 0    # triangles once placed
        self.skipped_unreadable = 0
        self.skipped_no_mesh = 0
        self.failed = []
        self.actor_mesh_names = []
        self.mesh_triangle_counts = {}
        self.skipped_effects = 0
        self.drawn_effects = 0      # effect meshes kept because a material converts
        self.jump_pad_meshes = 0
        self.substituted_effects = 0
        self.substituted_water = 0
        self.component_transforms = 0
        self.non_colliding = 0
        self.non_blocking = 0
        self.skipped_hidden = 0
        self.uv_channels = {}       # mesh -> the UV set its material asks for
        self.skinned = 0            # actors given a generated UE2 material
        self.culled = 0             # actors carrying UT3's own draw distance

    def __str__(self):
        out = ("%d static meshes (%d triangles), %d actors placing %d triangles"
               % (self.meshes, self.triangles, self.actors, self.scene_triangles))
        if self.skipped_no_mesh:
            out += "; %d actors without a mesh" % self.skipped_no_mesh
        if self.skipped_hidden:
            out += "; %d never drawn in UT3 either" % self.skipped_hidden
        if self.skipped_effects:
            out += "; %d effect actors skipped" % self.skipped_effects
        if self.drawn_effects:
            out += ("; %d effect actors drawn with a generated UE2 material"
                    % self.drawn_effects)
        if self.skinned:
            out += "; %d actor(s) wearing one through Skins" % self.skinned
        if self.jump_pad_meshes:
            out += ("; %d jump pads given the marker UT2004's own draws nothing for"
                    % self.jump_pad_meshes)
        if self.substituted_effects:
            out += "; %d given a stock UT2004 effect material" % self.substituted_effects
        if self.substituted_water:
            out += "; %d water surface(s) given a stock UT2004 material" % self.substituted_water
        if self.component_transforms:
            out += ("; %d actor(s) carrying a component transform of their own"
                    % self.component_transforms)
        if self.non_colliding or self.non_blocking:
            out += "; %d walk-through, %d non-blocking (as UT3 has them)" % (
                self.non_colliding, self.non_blocking)
        if self.culled:
            out += "; %d drawing only within UT3's own cull distance" % self.culled
        if self.skipped_unreadable:
            out += "; %d meshes unreadable" % self.skipped_unreadable
        if self.uv_channels:
            out += "; %d using a second UV set (%s)" % (
                len(self.uv_channels),
                ", ".join(sorted(self.uv_channels)[:3])
                + (", ..." if len(self.uv_channels) > 3 else ""))
        return out


class MeshSet:
    """Unique meshes referenced by the level, keyed by reference and materials.

    A UT3 StaticMeshComponent may override the mesh's own materials per actor,
    and 382 of DM-HeatRay's mesh actors do. UT2004 keeps materials on the mesh
    itself with no per-actor override, so a mesh used with two different
    material sets has to be exported twice. Only 23 of 179 meshes here need
    that; the rest are consistent and cost nothing.
    """

    def __init__(self, package_name):
        self.package_name = package_name
        self.by_ref = {}     # (is_import, index, materials) -> mesh name
        # mesh name -> (Package, export, material overrides, override package).
        # The overrides come from the actor's component and so belong to the
        # *map*, while the mesh belongs to whatever content package defines it.
        # In a cooked UT3 map those are the same file and the distinction never
        # shows; a UDK map keeps its meshes in separate .upk files, and
        # resolving a map's override index against a content package reads some
        # unrelated object or runs off the import table entirely.
        self.meshes = {}
        # mesh name -> [UE2 material path per element, None where the element
        # needs none]. Filled by export_meshes, which is the only place the
        # elements are read; `apply_skins` then puts them on the actors.
        self.skins = {}

    def name_for(self, ref, overrides=()):
        if ref is None or ref.is_null:
            return None
        name = self.by_ref.get(
            (ref.pkg.path, ref.is_import, ref.index,
             tuple(None if r is None else str(r) for r in overrides)))
        if not name:
            return None
        return "%s.%s" % (self.package_name, name)

    def _unique(self, base):
        name = sanitize(base)
        if name not in self.meshes:
            return name
        n = 2
        while "%s_%d" % (name, n) in self.meshes:
            n += 1
        return "%s_%d" % (name, n)

    def add(self, pkg, index, ref, overrides=()):
        # Key on the material *paths*: object references are distinct instances
        # per actor, so keying on them would split every actor into its own mesh.
        # The package is part of the key for the same reason it is in
        # TextureSet: an index means nothing without the table it came from.
        key = (ref.pkg.path, ref.is_import, ref.index,
               tuple(None if r is None else str(r) for r in overrides))
        if key in self.by_ref:
            return self.by_ref[key]
        owner, export = index.resolve(pkg, ref)
        if export is None or owner.class_name_of(export) != "StaticMesh":
            self.by_ref[key] = None
            return None
        signature = key[2]
        for name, (existing_pkg, existing, existing_over, _over_pkg) in self.meshes.items():
            if (existing_pkg is owner and existing.index == export.index
                    and tuple(None if r is None else str(r)
                              for r in existing_over) == signature):
                self.by_ref[key] = name
                return name
        name = self._unique(export.name)
        self.meshes[name] = (owner, export, overrides, pkg)
        self.by_ref[key] = name
        return name


def _hidden_in_game(comp):
    """Does UE3 draw this component at all?

    `HiddenGame` on a PrimitiveComponent means it is never rendered in play --
    UE3's way of keeping geometry that exists only to cast a shadow or block an
    occlusion query. CTF-FacingWorlds has 21 of them, a group the author named
    "necris cloud shadowcasters", and because nothing draws them their material
    is arbitrary: one wears a stone floor texture. Converted, they appear as
    solid brown slabs hanging in the sky.
    """
    return comp is not None and comp.get("HiddenGame") is True


def _collision_off(props, comp):
    """Does UT3 mark this actor as something you walk through?

    UE3 states it in two places and either is enough: `bCollideActors` on the
    actor, or `CollideActors` on the component that carries the mesh. UT2004's
    StaticMeshActor defaults to solid on both counts (bCollideActors,
    bBlockActors and bBlockKarma all True), so ignoring this makes every piece
    of decoration a wall -- 930 actors on DM-Deck, 269 on DM-HeatRay, including
    the sheet drawn over a goo pit you are supposed to fall into.
    """
    if props.get("bCollideActors") is False:
        return True
    return comp is not None and comp.get("CollideActors") is False


def _cull_distance(props, comp):
    """How far UT3 draws this actor, or None for "always".

    UE3 states it on the component as `CachedCullDistance` -- the value the cook
    settled on -- with `CullDistance` as the authored one behind it. UT3 sets it
    on almost everything: 3,974 of WAR-PowerSurge's 4,106 mesh components, and
    86,020 across the map set. UE2 has the same idea on the actor, `CullDistance`
    (Engine/Actor.uc:133, "0 == no distance cull").

    The two engines measure it from different places, and the difference is in
    our favour. `CheckCullDistance` compares against `BoxDistanceSqr`
    (UnRenderVisibility.cpp:23, :46) -- the distance to the nearest point of the
    actor's bounding box -- where UE3 measures to the bounds centre. Box
    distance is never the larger of the two, so a number carried across culls no
    earlier here than it did in UT3, which is the safe direction for a big mesh
    with a distant origin.

    UE2 also scales the test by `CullDistanceFOVBias`, `tan(FOV/2)`
    (UnRenderVisibility.cpp:1526). That is exactly 1.0 at UT2004's default FOV of
    90, so the numbers mean what they say; a player on a wider FOV culls sooner
    and one zoomed in culls later, which is the behaviour UT2004's own maps get.
    """
    for source in (comp, props):
        if source is None:
            continue
        for key in ("CachedCullDistance", "CullDistance"):
            value = source.get(key)
            if value is None:
                continue
            try:
                distance = float(value)
            except (TypeError, ValueError):
                continue
            if distance > 0.0:
                return distance
    return None


def _material_overrides(pkg, comp):
    """The component's Materials array, as a hashable tuple of refs."""
    materials = comp.get("Materials")
    if materials is None or not len(materials):
        return ()
    try:
        refs = materials.as_objects()
    except (ValueError, struct.error):
        return ()
    return tuple(None if r is None or r.is_null else r for r in refs)


def _mesh_ref(comp, owner=None, index=None):
    """The component's StaticMesh, following the archetype chain if inherited.

    A gameplay actor keeps its mesh on the class rather than the instance, so
    the cooked component says nothing: WAR-PowerSurge's Tarydium mine and
    processor both come out meshless if only the instance is read.
    """
    ref = comp.get("StaticMesh")
    if ref is not None and not ref.is_null:
        return ref
    if owner is None or index is None or getattr(comp, "export", None) is None:
        return None
    from ut3.objects.sound import _archetype_props

    ref = _archetype_props(owner, index, comp.export).get("StaticMesh")
    if ref is not None and hasattr(ref, "is_null") and not ref.is_null:
        return ref
    return None


def _component_of(pkg, export, props):
    """The actor's StaticMeshComponent properties."""
    # CollisionComponent comes last: on an actor that has both, it is often the
    # skeletal mesh, and WAR-PowerSurge's Tarydium processor keeps its static
    # mesh under a property simply called "StaticMesh".
    ref = (props.get("StaticMeshComponent") or props.get("StaticMesh")
           or props.get("CollisionComponent"))
    if ref is not None and ref.is_export:
        comp_props, start, _end = read_object_properties(pkg, ref.export)
        if start is not None:
            # Carried so the mesh lookup can follow the archetype chain.
            comp_props.export = ref.export
            return comp_props
    for name, idx in export.components.items():
        if "StaticMesh" in name:
            comp = pkg.ref(idx)
            if comp.is_export:
                comp_props, start, _end = read_object_properties(pkg, comp.export)
                if start is not None:
                    comp_props.export = comp.export
                    return comp_props
    return None


def _vector_of(props, name, default=(1.0, 1.0, 1.0)):
    value = props.get(name)
    if value is None or not value.value:
        return default
    return tuple(value.value)


def _effective_transform(props, comp):
    """(rotation, DrawScale3D, world offset) with the component's own folded in.

    A UE3 StaticMeshComponent carries a transform of its own, relative to the
    actor holding it, and UT3 maps use it: DM-Diesel hangs 40 meshes on a
    component pitched a quarter turn, which is why its pipes converted lying
    down instead of standing up. UT2004 has one transform per actor and no
    component to put a second on, so the two are composed into it.

    The composition has to be read in the order UE3 applies it,

        v * Scale_c * Rot_c * Trans_c * Scale_a * Rot_a * Trans_a

    against the one UT2004 offers,

        v * Scale * Rot * Trans

    so the rotations simply multiply, but the actor's scale ends up on the wrong
    side of the component's rotation. Non-uniform scale and rotation do not
    commute in general -- which would matter, since 112 of the 202 such actors
    in the stock maps do carry a non-uniform DrawScale3D. They commute exactly
    when the rotation sends axes to axes: `Rot_c * Scale_a` is then
    `Scale_permuted * Rot_c`, the scale factors merely swapped between axes.
    Every component rotation in the stock maps is a whole quarter turn (yaw 180
    on 98 of them, pitch 90 on 94, roll 90 on the rest), so the swap is exact
    and nothing is approximated.
    """
    rotation = tuple(int(c) for c in _vector_of(props, "Rotation", (0, 0, 0)))
    scale3d = _vector_of(props, "DrawScale3D")
    offset = (0.0, 0.0, 0.0)
    if comp is None:
        return rotation, scale3d, offset

    comp_rot = tuple(int(c) for c in _vector_of(comp, "Rotation", (0, 0, 0)))
    comp_scale = comp.get("Scale", 1.0)
    comp_scale3d = _vector_of(comp, "Scale3D")
    comp_offset = _vector_of(comp, "Translation", (0.0, 0.0, 0.0))

    if any(comp_rot):
        turn = rotation_matrix(comp_rot)
        rotation = to_rotator(multiply(turn, rotation_matrix(rotation)))
        images = axis_images(turn)
        if images is not None:
            scale3d = tuple(scale3d[images[i]] for i in range(3))
    scale3d = tuple(scale3d[i] * comp_scale3d[i] * comp_scale for i in range(3))
    if any(comp_offset):
        # Stated before the actor's own scale and rotation, so it picks both up
        # on the way out. No stock map has one; this is here so a map that does
        # is not silently misplaced.
        actor_scale = _vector_of(props, "DrawScale3D")
        drawscale = props.get("DrawScale", 1.0)
        scaled = tuple(comp_offset[i] * actor_scale[i] * drawscale for i in range(3))
        offset = rotate(scaled, rotation_matrix(
            tuple(int(c) for c in _vector_of(props, "Rotation", (0, 0, 0)))))
    return rotation, scale3d, offset


def _actor_sources(pkg):
    """Every placeable mesh, as (export, source class, properties, component).

    Two shapes reach the same emitter. UT3 and UDK write one actor per mesh, so
    the actor carries the transform and points at a component. Gears bundles
    them into StaticMeshCollectionActors, where the transform is a matrix in
    the collection and the component is the only thing named -- see
    convert/collections.py. Both arrive here looking alike.
    """
    for export in ordered_exports(pkg, ACTOR_CLASSES):
        props, start, _end = read_object_properties(pkg, export)
        if start is None:
            continue
        yield (export, pkg.class_name_of(export), props,
               _component_of(pkg, export, props))
    for entry in collections.expand(pkg):
        yield entry


def convert_actors(pkg, index, mesh_set, texture_set=None, scale=1.0, stats=None,
                   skip_effects=True, skip=(), no_collision=()):
    """Collect static mesh actors and emit t3d StaticMeshActors.

    `skip` names actors handled elsewhere -- an InterpActor that became a Mover
    would otherwise be placed twice, once moving and once parked. `no_collision`
    names ones that must stay visible but stop blocking, which is how a power
    node's pad scenery makes room for the node's own touch cylinder.
    """
    stats = stats or MeshStats()
    out = []
    names = set()
    effect_cache = {}
    skip = set(skip)
    for export, source_class, props, comp in _actor_sources(pkg):
        if export.name in skip:
            continue
        if comp is None:
            stats.skipped_no_mesh += 1
            continue
        mesh_ref = _mesh_ref(comp, pkg, index)
        if mesh_ref is None or mesh_ref.is_null:
            stats.skipped_no_mesh += 1
            continue
        if _hidden_in_game(comp):
            stats.skipped_hidden += 1
            continue
        # Unlit translucent effects (light beams, fog sheets) have no textured
        # equivalent in UE2 and cost fill rate for nothing -- see shaders.py.
        # Unless UT2004 already ships a material that says the same thing, in
        # which case the actor is kept and wears that instead.
        substitute = None
        is_effect = bool(skip_effects) and mesh_is_effect(pkg, index, mesh_ref, effect_cache)
        if is_effect:
            rotation = props.get("Rotation")
            substitute = effect_substitute(pkg, index, mesh_ref,
                                           _material_overrides(pkg, comp), effect_cache)
            if substitute is not None and not sheet_is_horizontal(
                    pkg, index, mesh_ref,
                    tuple(rotation.value) if rotation is not None and rotation.value
                    else (0, 0, 0), effect_cache):
                substitute = None
            if substitute is None:
                # A stock UT2004 material was the only way to draw one of these
                # until the converter could build its own. Now it can, so the
                # mesh is kept whenever UT3's own material converts -- see
                # effect_is_drawable for why the objection at the top of
                # shaders.py was to the blend mode rather than to the texture.
                if effect_is_drawable(pkg, index, mesh_ref,
                                      _material_overrides(pkg, comp), texture_set,
                                      effect_cache):
                    stats.drawn_effects += 1
                else:
                    stats.skipped_effects += 1
                    continue
            else:
                stats.substituted_effects += 1
        elif texture_set is not None:
            # Not an effect, but water is procedural in UT3 too -- tint,
            # refraction and fresnel are all shader parameters, and the only
            # texture in the graph is a detail normal map. CTF-LostCause's pool
            # is the case: refuse the normal map and the sheet is flat grey.
            rotation = props.get("Rotation")
            substitute = water_substitute(
                pkg, index, mesh_ref, _material_overrides(pkg, comp),
                tuple(rotation.value) if rotation is not None and rotation.value
                else (0, 0, 0), texture_set, effect_cache)
            if substitute is not None:
                stats.substituted_water += 1
        mesh_name = mesh_set.add(pkg, index, mesh_ref, _material_overrides(pkg, comp))
        if mesh_name is None:
            stats.skipped_no_mesh += 1
            continue

        properties = [("StaticMesh", "StaticMesh'%s.%s'" % (mesh_set.package_name, mesh_name))]
        placed_rot, placed_scale3d, comp_offset = _effective_transform(props, comp)
        if any(comp_offset) or tuple(props.get("Rotation").value if props.get("Rotation") is not None
                                     and props.get("Rotation").value else (0, 0, 0)) != placed_rot:
            stats.component_transforms += 1
        location = props.get("Location")
        if location is not None and location.value:
            world = [location.value[i] + comp_offset[i] for i in range(3)]
            properties.append(("Location", vec([c * scale for c in world])))
        elif any(comp_offset):
            properties.append(("Location", vec([c * scale for c in comp_offset])))
        if any(placed_rot):
            properties.append(("Rotation", rot(placed_rot)))
        draw_scale = props.get("DrawScale", 1.0)
        if draw_scale != 1.0:
            properties.append(("DrawScale", "%f" % draw_scale))
        if placed_scale3d != (1.0, 1.0, 1.0):
            properties.append(("DrawScale3D", vec(placed_scale3d)))
        cull = _cull_distance(props, comp)
        if cull is not None:
            properties.append(("CullDistance", "%f" % (cull * scale)))
            stats.culled += 1
        pre_pivot = props.get("PrePivot")
        if pre_pivot is not None and pre_pivot.value and any(pre_pivot.value):
            properties.append(("PrePivot", vec([c * scale for c in pre_pivot.value])))
        if substitute is not None:
            # Overrides whatever the exported mesh carries, so the ASE's own
            # texture is irrelevant here.
            properties.append(("Skins(0)", substitute))
        if _collision_off(props, comp) or export.name in no_collision:
            properties.extend([("bCollideActors", "False"), ("bBlockActors", "False"),
                               ("bBlockKarma", "False")])
            stats.non_colliding += 1
        elif props.get("bBlockActors") is False:
            properties.extend([("bBlockActors", "False"), ("bBlockKarma", "False")])
            stats.non_blocking += 1

        name = sanitize(export.name)
        if name in names:
            n = 2
            while "%s_%d" % (name, n) in names:
                n += 1
            name = "%s_%d" % (name, n)
        names.add(name)
        emitted = Actor("StaticMeshActor", name, properties)
        # Remember what it was in UT3: only plain scenery may be relocated into
        # the skybox, never a mover staged off-map.
        emitted.source_class = source_class
        # And whether it is one of UT3's volumetric effects, which are drawn on
        # sufferance: see drop_distant_effects.
        emitted.is_effect = is_effect
        out.append(emitted)
        stats.actor_mesh_names.append(mesh_name)
        stats.actors += 1
    return out, stats


def export_meshes(mesh_set, out_dir, index, texture_set=None, scale=1.0,
                  group="Meshes", stats=None):
    """Write an ASE per mesh; returns the #exec lines to add to the package."""
    stats = stats or MeshStats()
    package = mesh_set.package_name
    meshes_dir = os.path.join(out_dir, package, "Meshes")
    os.makedirs(meshes_dir, exist_ok=True)

    lines = []
    counts = {}
    for name in sorted(mesh_set.meshes):
        owner, export, overrides, override_pkg = mesh_set.meshes[name]
        mesh = read_static_mesh(owner, export)
        if mesh is None:
            stats.skipped_unreadable += 1
            stats.failed.append((name, "unreadable"))
            continue
        ok, why = validate(mesh)
        if not ok:
            stats.skipped_unreadable += 1
            stats.failed.append((name, why))
            continue
        lod = mesh.lod0

        # Element -> material index, and the texture each one resolves to.
        textures = []
        skins = []
        face_material = {}
        for material_index, element in enumerate(lod.elements):
            texture_name = None
            # The component's override wins: UT3 uses it to dress one mesh
            # differently per actor, and the mesh's own material is often a
            # placeholder that resolves to something meaningless like a cubemap.
            material = element.material
            material_pkg = owner
            if material_index < len(overrides) and overrides[material_index] is not None:
                material = overrides[material_index]
                material_pkg = override_pkg
            if texture_set is not None:
                resolved = texture_set.add_material(material_pkg, index, material)
                texture_name = resolved
                # A UE2 material cannot go in the ASE: `*BITMAP` is resolved at
                # `#exec` time, during class parsing, and the materials do not
                # exist until defaultproperties are imported at the end of the
                # build. So the mesh keeps its flat texture and the actor
                # overrides it through Skins, which UT2004 reads per material
                # index (AActor::GetSkin, Engine/Src/UnActor.cpp:1275).
                #
                # The reference is kept rather than resolved: the materials are
                # not built until the textures have been written, which is
                # after this runs. apply_skins settles it.
                pending = (material is not None and not material.is_null
                           and _material_key(material) in texture_set.pending)
                skins.append(material if pending else None)
            textures.append(texture_name or texture_set.FALLBACK_NAME if texture_set else None)
            for t in range(element.num_triangles):
                face_material[element.first_index // 3 + t] = material_index

        faces = []
        for face_index, (a, b, c) in enumerate(lod.triangles):
            faces.append((a, b, c, face_material.get(face_index, 0)))

        # UE3 meshes can carry several UV sets and the material says which one
        # to sample; UT2004 stores only one, so bake the chosen set per element.
        # UT3's sky dome is the case that matters: channel 0 is a polar map that
        # collapses every apex vertex onto one line of the texture, channel 1 is
        # the disc projection its material actually reads.
        uvs = list(lod.uvs) if len(lod.uvs) == len(lod.positions) else []
        if uvs:
            for element in lod.elements:
                channel, u_tiling, v_tiling = material_uv_channel(
                    owner, index, element.material)
                if channel == 0 and u_tiling == 1.0 and v_tiling == 1.0:
                    continue
                source = lod.uv_sets[channel] if channel < len(lod.uv_sets) else lod.uvs
                if len(source) != len(uvs):
                    continue
                # Which vertices the element covers has to come from its own
                # triangles: the declared MaxVertexIndex is not dependable (the
                # sky dome reports 295 for a 592-vertex mesh).
                first = element.first_index
                for v in lod.indices[first:first + element.num_triangles * 3]:
                    if v < len(uvs):
                        u, w = source[v]
                        uvs[v] = (u * u_tiling, w * v_tiling)
                if channel:
                    stats.uv_channels[name] = channel
        if any(skins):
            mesh_set.skins[name] = skins
        path = write_ase(os.path.join(meshes_dir, "%s.ase" % name), name,
                         lod.positions, uvs, faces, textures, scale=scale)
        # NOT "#exec STATICMESH IMPORT": that handler only accepts LightWave
        # .lwo (Editor/Src/UnEdSrvExecImporters.cpp:426). ASE is handled by
        # UStaticMeshFactory, which registers the "ase" format
        # (Editor/Src/UnStaticMesh.cpp:415), reached through the generic
        # factory exec (UnEdSrv.cpp:436).
        lines.append("#exec NEW STANDALONE StaticMeshFactory FILE=Meshes\\%s NAME=%s PACKAGE=%s"
                     % (os.path.basename(path), name, package))
        stats.meshes += 1
        stats.triangles += len(faces)
        counts[name] = len(faces)
    stats.mesh_triangle_counts = counts
    stats.scene_triangles = sum(counts.get(n, 0) for n in stats.actor_mesh_names)
    return lines, stats


def apply_skins(actors, mesh_set, texture_set, stats=None):
    """Put each mesh's UE2 materials on the actors that place it.

    UT2004 keeps materials on the static mesh, and the ASE is the only way to
    put them there -- but an ASE binds `*BITMAP` while the package is being
    parsed, long before a `Begin Object` material exists (see export_meshes).
    `Skins(n)` is the way round it: it overrides material index n per actor
    (AActor::GetSkin, Engine/Src/UnActor.cpp:1275), and it is what
    convert/shaders.py already uses for the goo and the teleporter portals.

    Per actor rather than per mesh is not the waste it looks: a mesh is keyed
    by its material set, so every actor placing one wants exactly the same
    Skins, and the t3d is text UnrealEd reads once.

    An actor that already carries a Skins(0) keeps it. That is the stock-material
    substitution -- UT3's goo wearing XEffectMat's -- which was chosen against
    the actor rather than derived from the mesh, and is the more specific answer.
    """
    applied = 0
    for actor in actors:
        mesh = None
        for key, value in actor.properties:
            if key == "StaticMesh":
                mesh = value.split(".")[-1].rstrip("'")
            elif key.startswith("Skins("):
                mesh = None
                break
        refs = mesh_set.skins.get(mesh) if mesh else None
        if not refs:
            continue
        wearing = False
        for i, ref in enumerate(refs):
            if ref is None:
                continue
            path = texture_set.material_class_for(ref)
            # None where the material was pending but its texture was dropped.
            if path and not path.startswith("Texture'"):
                actor.properties.append(("Skins(%d)" % i, path))
                wearing = True
        applied += 1 if wearing else 0
    if stats is not None:
        stats.skinned = applied
    return applied
