#!/usr/bin/env python3
"""ut3conv -- convert UT3 maps to UT2004, and inspect UT3 packages.

Conversion:

    ./ut3conv.py t3d      <pkg> -o map.t3d [--textures DIR] [--volumes]
                                [--scale N] [--light-gain N] [--no-lights]
                                [--no-player-starts] [--no-world-brush]
    ./ut3conv.py textures <pkg> -o DIR

Inspection:

    ./ut3conv.py info     <pkg>
    ./ut3conv.py classes  <pkg> [-n 40]
    ./ut3conv.py list     <pkg> [-c ClassName] [-m NamePattern] [-n 50]
    ./ut3conv.py props    <pkg> <ExportNameOrIndex> [--components]
    ./ut3conv.py imports  <pkg> [-m Pattern] [-n 50]

The t3d covers BSP brushes, lights and PlayerStarts; --textures also writes a
buildable UT2004 texture package the t3d references. Import the t3d in
UnrealEd, then Build All.
"""

import argparse
import fnmatch
import os
import re
import sys

from ut3.package import Package
from convert.terrain import DEFAULT_DECO_DENSITY
from convert.textures import DEFAULT_MAX_SIZE

HALF_WORLD_MAX = 262144.0

# Engine/Src/UnRender.cpp:1510 -- the far plane of the projection matrix itself,
# a constant the engine never reassigns. See convert/skybox.py.
FAR_CLIPPING_PLANE = 65536.0


def install_root():
    """The UT2004 install this converter lives inside, or None.

    A generated package is only buildable from the install root: `ucc make`
    compiles `<root>/<Package>/Classes/<Package>.uc` and resolves its
    `#exec ... FILE=` paths relative to that folder, so writing the package
    anywhere else means copying it there by hand before every build. The
    converter sits at `<root>/ut3converter`, so the root is its parent --
    confirmed by System/ being there rather than assumed.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isdir(os.path.join(root, "System")):
        return root
    return None

# A backdrop actor's location is its pivot, so the void has to reach past it by
# roughly the size of the building hanging off that pivot.
BACKDROP_PAD = 8192.0
from ut3.props import Array, Properties, Struct, read_object_properties


def _fmt(value, indent=0):
    pad = "  " * indent
    if isinstance(value, Properties):
        lines = []
        for name, idx, type_name, v in value:
            key = name if idx == 0 else "%s[%d]" % (name, idx)
            lines.append("%s  %-28s %s" % (pad, key, _fmt(v, indent + 1)))
        return "\n" + "\n".join(lines) if lines else "(empty)"
    if isinstance(value, Struct):
        if isinstance(value.value, Properties):
            return "%s {%s\n%s  }" % (value.type, _fmt(value.value, indent + 1), pad)
        if value.value is not None:
            return "%s%s" % (value.type, tuple(round(x, 4) if isinstance(x, float) else x for x in value.value))
        return repr(value)
    if isinstance(value, Array):
        return repr(value)
    if isinstance(value, float):
        return repr(round(value, 6))
    if isinstance(value, bytes):
        return "<%d bytes> %s" % (len(value), value[:16].hex())
    return str(value)


def cmd_info(args):
    p = Package(args.package)
    print("file            %s" % p.path)
    print("version         %d (licensee %d)" % (p.version, p.licensee))
    print("engine/cooker   %d / %d" % (p.engine_version, p.cooker_version))
    print("folder          %r" % p.folder_name)
    print("package flags   0x%08X" % p.package_flags)
    print("header size     %d" % p.header_size)
    print("names           %d @ %d" % (p.name_count, p.name_offset))
    print("imports         %d @ %d" % (p.import_count, p.import_offset))
    print("exports         %d @ %d" % (p.export_count, p.export_offset))
    print("compression     0x%X, %d chunks" % (p.compression_flags, len(p.chunks)))
    if p.chunks:
        total_u = sum(c[1] for c in p.chunks)
        total_c = sum(c[3] for c in p.chunks)
        print("                %.1f MB -> %.1f MB compressed" % (total_u / 1e6, total_c / 1e6))


def cmd_classes(args):
    p = Package(args.package)
    hist = sorted(p.class_histogram().items(), key=lambda kv: -kv[1])
    for name, count in hist[: args.number]:
        print("%6d  %s" % (count, name))
    print("%6d  (total exports)" % p.export_count)


def cmd_list(args):
    p = Package(args.package)
    shown = 0
    for e in p.exports:
        cn = p.class_name_of(e)
        if args.cls and cn.lower() != args.cls.lower():
            continue
        if args.match and not fnmatch.fnmatch(e.name.lower(), args.match.lower()):
            continue
        print("%6d  %-34s %-28s %7d bytes @ %d" % (e.index, cn, e.name, e.size, e.offset))
        shown += 1
        if shown >= args.number:
            print("... (use -n to show more)")
            break


def _resolve(p, ident):
    if ident.isdigit():
        idx = int(ident)
        if 1 <= idx <= p.export_count:
            return [p.exports[idx - 1]]
        return []
    return p.find(ident)


def cmd_props(args):
    p = Package(args.package)
    hits = _resolve(p, args.object)
    if not hits:
        sys.exit("no export matches %r" % args.object)
    for e in hits[: args.number]:
        props, start, end = read_object_properties(p, e)
        print("=" * 72)
        print("%s %s  (export %d, %d bytes @ %d)" % (p.class_name_of(e), p.path_of(e.index), e.index, e.size, e.offset))
        print("  flags 0x%016X  archetype %s" % (e.flags, p.ref(e.archetype)))
        if start is None:
            print("  <no tagged properties found>")
        else:
            print("  property list at +%d, ends at +%d of %d" % (start, end, e.size))
        print(_fmt(props).lstrip("\n"))
        if args.components and e.components:
            print("  components:")
            for name, idx in e.components.items():
                print("    %-28s %s" % (name, p.ref(idx)))


def _texture_package_name(package_path, override=None):
    if override:
        return override
    base = os.path.basename(package_path).rsplit(".", 1)[0]
    return re.sub(r"[^A-Za-z0-9_]", "", base) + "Tex"


# Everything the converter writes into a package folder. Anything else there
# was put there by hand, so cleaning is limited to these.
GENERATED_SUFFIXES = (".dds", ".tga", ".ase", ".bmp", ".wav", ".ogg")
GENERATED_DIRS = ("Textures", "Meshes", "Sounds", "Terrain")


def clean_package(out_dir, package):
    """Delete the previous run's output so nothing stale survives a rebuild.

    Worth doing rather than overwriting in place: a mesh that stops being
    exported (or gets renamed, as they do when the variant keying changes)
    otherwise lingers for good. The generated .uc stops referencing it so it
    never reaches the build, but it accumulates on disk and makes it impossible
    to tell what the current conversion actually produced.
    """
    removed = 0
    for name in GENERATED_DIRS:
        folder = os.path.join(out_dir, package, name)
        if not os.path.isdir(folder):
            continue
        for entry in os.listdir(folder):
            if entry.lower().endswith(GENERATED_SUFFIXES):
                try:
                    os.remove(os.path.join(folder, entry))
                    removed += 1
                except OSError:
                    pass
    return removed


def _build_assets(p, package_path, texture_package, out_dir, max_size, with_meshes, scale,
                  skip_effects, with_terrain, layer_scale, no_skybox, with_sounds=True,
                  sound_gain=1.0, with_movers=True, max_keys=24, no_collision=(),
                  deco_density=None, with_materials=True, with_sublevels=True,
                  all_textures=False):
    """Extract textures (and optionally static meshes) into one buildable package."""
    from convert.textures import TextureSet, collect_brush_materials, export_textures
    from ut3.resolve import PackageIndex

    stale = clean_package(out_dir, texture_package)
    if stale:
        print("  cleared %d file(s) from the previous build" % stale)

    index = PackageIndex.for_map(package_path)

    # A map may be spread over several packages: the persistent level names the
    # ones it always has loaded, and UT2004 has no streaming, so the conversion
    # is their union. See convert/sublevels.py. `levels` is what every actor
    # converter below iterates; a map with no streaming levels gets a list of
    # one and behaves exactly as before.
    levels = [p]
    if with_sublevels:
        from convert.sublevels import open_levels

        found, missing = open_levels(p, index)
        if found:
            print("  %d streaming sub-level(s) merged in: %s"
                  % (len(found), ", ".join(name for name, _sub in found)))
            levels += [sub for _name, sub in found]
        if missing:
            print("  %d streaming sub-level(s) NOT FOUND, so their part of the "
                  "map is missing: %s" % (len(missing), ", ".join(missing)))

    texture_set = TextureSet(texture_package, materials=with_materials,
                             all_textures=all_textures)
    for level in levels:
        collect_brush_materials(level, index, texture_set)

    mesh_actors, mesh_stats, mesh_exec = [], None, []
    mover_actors, mover_stats = [], None
    if with_meshes:
        from convert.meshes import MeshSet, convert_actors, export_meshes

        mesh_set = MeshSet(texture_package)
        moved = set()
        for level in levels:
            if with_movers:
                from convert.movers import convert_movers

                movers, level_moved, mover_stats = convert_movers(
                    level, index, mesh_set, texture_set, scale=scale, max_keys=max_keys,
                    skip_effects=skip_effects, stats=mover_stats
                )
                mover_actors += movers
                moved |= level_moved
            actors, mesh_stats = convert_actors(level, index, mesh_set, texture_set,
                                                scale=scale, stats=mesh_stats,
                                                skip_effects=skip_effects, skip=moved,
                                                no_collision=no_collision)
            mesh_actors += actors
        # Terrain foliage is scattered by the terrain rather than placed as
        # actors, so nothing above has referenced it. Register it here, while
        # the mesh export is still ahead of us -- see convert.terrain.
        if with_terrain:
            from convert.terrain import register_foliage

            foliage = register_foliage(p, index, mesh_set)
            if foliage:
                print("  %d terrain foliage mesh(es) added for the decoration layers"
                      % foliage)
        # Resolving mesh materials adds to the texture set, so meshes must be
        # exported before the textures are written.
        sky_meshes = []
        mesh_exec, mesh_stats = export_meshes(
            mesh_set, out_dir, index, texture_set, scale=scale, stats=mesh_stats
        )

    sky_info = None
    if with_meshes and not no_skybox:
        from convert.skybox import find_sky_meshes, mesh_radius

        names = find_sky_meshes(p, index, mesh_set)
        if names:
            # UT3 puts its dome in the level at map scale (DrawScale 300 here,
            # a 323,526uu radius). The skybox room replaces it, so drop the
            # in-level copies or they swallow the whole map.
            sky_refs = tuple("StaticMesh'%s.%s'" % (texture_package, n) for n in names)
            dome_actors = [a for a in mesh_actors
                           if any(v in sky_refs for k, v in a.properties
                                  if k == "StaticMesh")]
            mesh_actors = [a for a in mesh_actors if a not in dome_actors]
            mesh_stats.skipped_sky = len(dome_actors)
            sky_info = (names[0], mesh_radius(p, index, mesh_set, names[0]),
                        dome_actors[0] if dome_actors else None)

    terrain_actors, terrain_stats = [], None
    if with_terrain:
        from convert.terrain import convert_terrain

        terrain_actors, terrain_exec, terrain_stats = convert_terrain(
            p, index, out_dir, texture_package, texture_set, scale=scale,
            layer_scale=layer_scale,
            mesh_set=mesh_set if with_meshes else None,
            deco_density=(DEFAULT_DECO_DENSITY if deco_density is None
                          else deco_density),
        )
        mesh_exec = list(mesh_exec) + terrain_exec

    sound_actors, sound_stats = [], None
    if with_sounds:
        from convert.sounds import (SoundSet, convert_ambient_sounds, drop_failed,
                                    export_sounds)

        sound_set = SoundSet(texture_package)
        for level in levels:
            sounds, sound_stats = convert_ambient_sounds(
                level, index, sound_set, scale=scale, volume_gain=sound_gain,
                stats=sound_stats
            )
            sound_actors += sounds
        sound_exec, sound_stats = export_sounds(sound_set, out_dir, index,
                                                stats=sound_stats)
        sound_actors = drop_failed(sound_actors, sound_set, sound_stats)
        if mover_actors:
            from convert.movers import attach_sounds

            sound_actors = attach_sounds(mover_actors, sound_actors, sound_stats)
        mesh_exec = list(mesh_exec) + sound_exec

    written, uc_path = export_textures(
        texture_set, out_dir, index, max_size=max_size, extra_exec=mesh_exec
    )
    if with_meshes:
        from convert.meshes import apply_skins

        # Last, because both halves have to be settled first: export_meshes is
        # where a mesh's elements are read at all, and export_textures is where
        # the materials over them are actually built.
        apply_skins(mesh_actors + mover_actors, mesh_set, texture_set, mesh_stats)
    return (texture_set, written, uc_path, mesh_actors, mesh_stats,
            terrain_actors, terrain_stats, sky_info, sound_actors, sound_stats,
            mover_actors, mover_stats, levels)


def cmd_t3d(args):
    from convert.geometry import convert_brushes, make_builder_brush, make_world_brushes
    from ut2.t3d import T3DMap, vec

    p = Package(args.package)
    texture_package = _texture_package_name(args.package, args.texture_package)

    # A UT3 node stands on scenery with a blocking pad over it; both sit inside
    # the touch cylinder UT2004's own node brings, so they are found up front and
    # kept out of the way of the meshes and brushes below.
    pad_meshes, pad_volumes = set(), set()
    if not args.no_onslaught:
        from convert.onslaught import node_pads

        pad_meshes, pad_volumes = node_pads(p)

    texture_set = None
    mesh_actors, mesh_stats = [], None
    terrain_actors, terrain_stats = [], None
    sound_actors, sound_stats = [], None
    mover_actors, mover_stats = [], None
    sky_info = None
    if args.textures and not args.no_package:
        (texture_set, written, uc_path, mesh_actors, mesh_stats,
         terrain_actors, terrain_stats, sky_info,
         sound_actors, sound_stats, mover_actors, mover_stats, levels) = _build_assets(
            p, args.package, texture_package, args.textures, args.max_texture_size,
            with_meshes=not args.no_meshes, scale=args.scale,
            skip_effects=not args.keep_effect_meshes,
            with_terrain=not args.no_terrain, layer_scale=args.terrain_layer_scale,
            deco_density=args.deco_density,
            no_collision=pad_meshes,
            no_skybox=args.no_skybox, with_sounds=not args.no_sounds,
            sound_gain=args.sound_gain, with_movers=not args.no_movers,
            max_keys=args.mover_keys, with_materials=not args.no_materials,
            with_sublevels=not args.no_sublevels,
            all_textures=args.all_textures,
        )
        print("  package: %s" % os.path.dirname(os.path.dirname(uc_path)))
        print("  textures: %d written, %d materials unresolved -> %s"
              % (written, texture_set.unresolved, os.path.basename(uc_path)))
        if texture_set.extra:
            print("      %d imported only because --all-textures asked for them, "
                  "referenced by nothing" % len(texture_set.extra))
        if texture_set.failed:
            # Only --all-textures makes this ordinary: a texture no material
            # draws can be in a format UE2 has no equivalent for, and dropping
            # it silently would look like the flag had missed it.
            print("      %d skipped, in a format UE2 cannot store: %s"
                  % (len(texture_set.failed),
                     ", ".join("%s (%s)" % pair for pair in texture_set.failed[:5])))
        if texture_set.composited:
            print("      %d had a separate UE3 opacity mask baked into their alpha"
                  % len(texture_set.composited))
        if texture_set.blended:
            print("      %d recoloured by a two-tone material's colour pair"
                  % len(texture_set.blended))
        if texture_set.tinted:
            print("      %d drew their colour from one channel of a packed mask, "
                  "tinted by the material's Base Color" % len(texture_set.tinted))
        if texture_set.refused:
            unique_refused = sorted(set(texture_set.refused))
            print("      %d material(s) resolved to something that is not a colour "
                  "map and use the placeholder instead: %s"
                  % (len(texture_set.refused), ", ".join(unique_refused[:3])))
        if texture_set.materials:
            blends = {}
            tinted = panned = 0
            for _name, blend, _unlit, colour, panner in texture_set.built:
                blends[blend] = blends.get(blend, 0) + 1
                tinted += 1 if colour else 0
                panned += 1 if panner else 0
            if blends:
                extra = []
                if tinted:
                    extra.append("%d tinted by a folded colour" % tinted)
                if panned:
                    extra.append("%d panning" % panned)
                print("      %d UT3 material(s) a flat texture cannot express (%s) "
                      "-> %d generated object(s)%s"
                      % (len(texture_set.built),
                         ", ".join("%d %s" % (n, b.replace("BLEND_", "").lower())
                                   for b, n in sorted(blends.items())),
                         len(texture_set.materials),
                         "; " + ", ".join(extra) if extra else ""))
            if texture_set.invisible:
                print("      %d material(s) UT3 itself draws at zero opacity "
                      "(pure screen distortion), so nothing is built and the "
                      "surfaces they dress are dropped" % len(texture_set.invisible))
        if texture_set.no_alpha_channel:
            print("      %d wanted a cutout but UT3 gave no mask to bake, so they "
                  "stay opaque: %s" % (len(texture_set.no_alpha_channel),
                                       ", ".join(sorted(texture_set.no_alpha_channel)[:3])))

    brushes, stats = convert_brushes(
        p,
        texture_package=texture_package,
        scale=args.scale,
        include_volumes=not args.no_volumes,
        texture_set=texture_set,
        surface_scale=args.surface_scale,
        skip=pad_volumes,
    )
    # UE3 has no skybox: the dome is ordinary geometry the player sees at
    # distance. "inline" keeps that model, at UT3's own size unless UE2's
    # 262144uu world forces it down; "skybox" uses UT2004's SkyZoneInfo idiom.
    world_bounds = stats.world_bounds
    if mesh_actors:
        from convert.skybox_move import play_area_for

        world_bounds, from_meshes = play_area_for(world_bounds, mesh_actors)
        if from_meshes:
            size = tuple(world_bounds[1][i] - world_bounds[0][i] for i in range(3))
            print("  the BSP is a shell around a streamed-in map, so the play area "
                  "is taken from the %d placed mesh(es): %.0f x %.0f x %.0f uu"
                  % (len(mesh_actors), size[0], size[1], size[2]))
    # The map's own extent, before the expansions below (movers, an inline dome,
    # kept backdrop) grow `world_bounds`. Everything that asks "how big is this
    # map" wants this. It was stats.world_bounds everywhere until a map turned
    # up whose BSP is not the map -- see play_area_for.
    base_bounds = world_bounds
    inline_dome = None
    dome_clamped = False
    world_margin = args.world_margin + stats.max_brush_radius
    if world_bounds and mesh_actors:
        from convert.skybox_move import drop_distant_effects

        mesh_actors, dropped_effects = drop_distant_effects(
            mesh_actors, (tuple(world_bounds[0]), tuple(world_bounds[1])),
            FAR_CLIPPING_PLANE)
        if dropped_effects:
            mesh_stats.drawn_effects -= dropped_effects
            mesh_stats.actors -= dropped_effects
            print("  %d effect actor(s) dropped: further from the play area than the "
                  "%.0f far clipping plane, so nothing would draw them"
                  % (dropped_effects, FAR_CLIPPING_PLANE))
    if sky_info and args.sky_mode == "inline" and world_bounds:
        from convert.skybox import fit_inline_dome
        from convert.skybox_move import parse_location, parse_scale

        _name, dome_radius, dome_actor = sky_info
        # Read UT3's DrawScale before the actor is rewritten with the new one.
        native_scale = parse_scale(dome_actor) if dome_actor is not None else 1.0
        scale, radius, dome_loc, world_bounds, dome_clamped = fit_inline_dome(
            world_bounds,
            parse_location(dome_actor) if dome_actor is not None else None,
            dome_radius or 1.0,
            [parse_location(a) for a in mesh_actors],
            margin=args.sky_dome_margin,
            native_scale=native_scale,
            world_margin=world_margin,
        )
        inline_dome = (dome_actor, scale, radius, native_scale, dome_loc)

    shrink_backdrop = args.shrink_backdrop
    if args.sky_mode == "skybox" and not shrink_backdrop and world_bounds:
        # The backdrop stays in the level, so the void has to reach it: an actor
        # outside the subtract brush sits in solid space and renders nowhere.
        from convert.skybox_move import furthest_from, is_outside, parse_location

        lo, hi = [list(b) for b in world_bounds]
        play_area = (tuple(world_bounds[0]), tuple(world_bounds[1]))
        # Measured over the backdrop only -- the actors the move would take.
        # Every mesh would instead measure the map's own diagonal, which on a
        # large level exceeds the far plane all by itself: WAR-PowerSurge reads
        # 67,149uu that way with no backdrop actors at all, and would be
        # rebuilt for a problem it does not have.
        outer = (tuple(v - world_margin for v in world_bounds[0]),
                 tuple(v + world_margin for v in world_bounds[1]))
        backdrop_reach = 0.0
        for actor in mesh_actors:
            location = parse_location(actor)
            if location is None:
                continue
            if is_outside(location, outer):
                backdrop_reach = max(backdrop_reach, furthest_from(play_area, location))
            for i, v in enumerate(location):
                lo[i] = min(lo[i], v - BACKDROP_PAD)
                hi[i] = max(hi[i], v + BACKDROP_PAD)
        # UE2 clamps every coordinate to +/-HALF_WORLD_MAX, so a backdrop that
        # cannot be reached without crossing it is not a judgement call: keeping
        # it in the level breaks the map outright. CTF-FacingWorlds hangs its
        # scenery 336,707uu out and needs 666,372uu of void to enclose it.
        reach = max(max(abs(lo[i]), abs(hi[i])) for i in range(3)) + world_margin
        if reach > HALF_WORLD_MAX and not args.keep_backdrop:
            shrink_backdrop = True
            print("  backdrop reaches %.0fuu, past UE2's %.0f world limit -- moving "
                  "it into the skybox instead of enclosing it" % (reach, HALF_WORLD_MAX))
        elif backdrop_reach > FAR_CLIPPING_PLANE and not args.keep_backdrop:
            # Fitting inside the world is not enough: the far plane of the
            # projection matrix is a hard 65536 (Core/Src/Core.cpp:197, used at
            # Engine/Src/UnRender.cpp:1510) and is never reassigned. The zone's
            # DistanceFogEnd only moves the frustum *culling* plane
            # (UnRender.cpp:1066), so nothing in a map can push geometry past
            # it. Scenery further than that from where a player can stand is
            # depth-clipped and the sky shows through it -- WAR-Serenity puts 33
            # meshes up to 139,245uu out. The skybox is the only place UE2 can
            # draw them, which is why the idiom exists at all.
            shrink_backdrop = True
            print("  backdrop stands %.0fuu from the play area, past the %.0f far "
                  "clipping plane -- moving it into the skybox so it is drawn at all"
                  % (backdrop_reach, FAR_CLIPPING_PLANE))
        else:
            world_bounds = (tuple(lo), tuple(hi))

    # A mover needs its whole path enclosed, not just where it starts: an actor
    # outside the subtract brush is in solid space and renders nowhere, so a
    # train that leaves the void vanishes mid-run. Costs nothing in BSP -- the
    # void is one convex box either way -- but it does grow the level bounds.
    if mover_actors and world_bounds:
        from convert.movers import key_extent

        lo, hi = [list(b) for b in world_bounds]
        for actor in mover_actors:
            if actor.cls != "Mover":
                continue
            extent = key_extent(actor)
            if extent is None:
                continue
            for i in range(3):
                lo[i] = min(lo[i], extent[0][i] - BACKDROP_PAD)
                hi[i] = max(hi[i], extent[1][i] + BACKDROP_PAD)
        if (tuple(lo), tuple(hi)) != world_bounds:
            print("  world void widened to cover the mover paths")
        world_bounds = (tuple(lo), tuple(hi))

    t3d = T3DMap()
    # Must come first: UT2004 consumes the first Class=Brush actor as the
    # builder brush (UnEdFact.cpp:647), so give it one to eat.
    t3d.add(make_builder_brush())
    world = None
    if not args.no_world_brush and base_bounds:
        # UT2004 is subtractive, UT3 is additive: carve out the space the UT3
        # geometry occupies before adding it back, or it stays buried in rock.
        margin = world_margin
        cells = make_world_brushes(
            world_bounds,
            margin=margin,
            texture=texture_set.name_for(None) if texture_set else None,
            fake_backdrop=bool(sky_info) and args.sky_mode == "skybox",
            cell=args.world_cell,
        )
        for cell_brush in cells:
            t3d.add(cell_brush)
        world = cells[0]
    for brush in brushes:
        t3d.add(brush)

    light_stats = actor_stats = None
    if not args.no_lights:
        from convert.lights import convert_lights

        lights = []
        for level in levels:
            level_lights, light_stats = convert_lights(
                level, scale=args.scale, gain=args.light_gain,
                radius_scale=args.light_radius_scale,
                ambient_gain=args.ambient_gain, stats=light_stats)
            lights += level_lights
        for light in lights:
            t3d.add(light)

    # An ambient given on the command line wins over anything derived. UT3
    # states a level's fill light as a SkyLight and --ambient-gain scales that,
    # but a UDK map lit by baked lightmaps has no SkyLight at all -- BL-Dekk
    # has none, and 264 LightMapTexture2Ds instead -- so there is nothing for a
    # gain to multiply and only a number given here can light the map.
    if args.ambient is not None:
        hue, saturation = 0, 255  # white: UE2 reads 255 saturation as unsaturated
        if light_stats is not None and light_stats.ambient:
            hue, saturation = light_stats.ambient[1], light_stats.ambient[2]
        if light_stats is None:
            from convert.lights import LightStats

            light_stats = LightStats()
        light_stats.ambient = (max(0, min(255, args.ambient)), hue, saturation)
        light_stats.ambient_parts = [light_stats.ambient[0]]
    for actor in terrain_actors:
        t3d.add(actor)
    for actor in sound_actors:
        t3d.add(actor)
    for actor in mover_actors:
        t3d.add(actor)
    taken_names = set()
    if not args.no_player_starts:
        from convert.actors import convert_player_starts

        starts = []
        for level in levels:
            level_starts, actor_stats = convert_player_starts(
                level, scale=args.scale, stats=actor_stats)
            starts += level_starts
        taken_names.update(a.name for a in starts)
        for actor in starts:
            t3d.add(actor)

    onslaught_stats = None
    if not args.no_onslaught:
        from convert.onslaught import convert_onslaught

        onslaught, onslaught_stats = convert_onslaught(
            p, scale=args.scale, taken=taken_names,
            countdown_time=args.countdown_time,
            specials=args.onslaught_specials,
            countdown_damage=args.countdown_damage,
            vehicle_rise=args.vehicle_rise)
        # A node's plate hangs PrePivot.Z below its Location and its touch
        # cylinder is centred on it, so one sunk into the brush it stands on is
        # both hidden and unreachable -- see rest_on_brushes.
        from convert.onslaught import rest_on_brushes

        rest_on_brushes(onslaught, brushes, onslaught_stats, rise=args.node_rise)
        taken_names.update(a.name for a in onslaught)
        for actor in onslaught:
            t3d.add(actor)

    objective_stats = None
    if not args.no_objectives:
        from convert.objectives import convert_objectives

        objectives, objective_stats = convert_objectives(
            p, scale=args.scale, taken=taken_names)
        taken_names.update(a.name for a in objectives)
        for actor in objectives:
            t3d.add(actor)

    teleporter_stats = None
    if not args.no_teleporters:
        from convert.teleporters import convert_teleporters

        teleporters, teleporter_stats = convert_teleporters(
            p, scale=args.scale, taken=taken_names,
            with_effect=not args.no_teleporter_effect)
        taken_names.update(a.name for a in teleporters)
        for actor in teleporters:
            t3d.add(actor)

    pickup_stats = None
    if not args.no_pickups or not args.no_paths:
        from convert.pickups import (convert_paths, convert_pickups,
                                     convert_weapon_lockers)

        pickup_stats = None
        if not args.no_pickups:
            items, pickup_stats = convert_pickups(p, scale=args.scale,
                                                  taken=taken_names)
            for actor in items:
                t3d.add(actor)
            lockers, pickup_stats = convert_weapon_lockers(
                p, scale=args.scale, stats=pickup_stats, taken=taken_names)
            for actor in lockers:
                t3d.add(actor)
        if not args.no_paths:
            # Paths after pickups so a name clash renames the path node, not the
            # pickup -- jump pads point at path nodes by their emitted name.
            paths, pickup_stats = convert_paths(p, scale=args.scale,
                                                stats=pickup_stats, taken=taken_names)
            for actor in paths:
                t3d.add(actor)
            # UT2004's own jump pad draws nothing, so the marker UT3 keeps on
            # the pad class is rebuilt from stock content.
            from convert.pickups import jump_pad_markers
            from ut3.resolve import PackageIndex

            for actor in jump_pad_markers(p, PackageIndex.for_map(args.package),
                                          scale=args.scale,
                                          stats=pickup_stats):
                t3d.add(actor)

    if inline_dome:
        dome_actor, dome_scale, dome_radius, native, dome_loc = inline_dome
        if dome_actor is not None:
            for i, (k, v) in enumerate(dome_actor.properties):
                if k == "DrawScale":
                    dome_actor.properties[i] = (k, "%f" % dome_scale)
                elif k == "Location":
                    # The offset is scaled with the radius, so the dome moves.
                    dome_actor.properties[i] = (k, vec(dome_loc))
            if not any(k == "DrawScale" for k, _v in dome_actor.properties):
                dome_actor.properties.append(("DrawScale", "%f" % dome_scale))
            if not any(k == "bUnlit" for k, _v in dome_actor.properties):
                dome_actor.properties.append(("bUnlit", "True"))
            t3d.add(dome_actor)
        if dome_clamped:
            print("  sky dome kept in the level at DrawScale %.1f (radius %.0f uu); "
                  "UT3's %.0f is past %s" % (dome_scale, dome_radius, native, dome_clamped))
            print("      ^ a bigger dome would be depth-clipped; --sky-mode skybox "
                  "shows it at UT3's proportions instead")
        else:
            print("  sky dome kept in the level at UT3's own DrawScale %.1f "
                  "(radius %.0f uu)" % (dome_scale, dome_radius))

    if sky_info and args.sky_mode == "skybox":
        from convert.skybox import make_skybox

        from convert.skybox_move import is_outside, parse_location

        sky_mesh, sky_radius, dome_actor = sky_info
        # Which scenery is leaving the level has to be settled *before* the room
        # is placed, because the room is placed clear of whatever stays. Deciding
        # it afterwards puts the room clear of the very meshes about to be moved
        # into it: on CTF-FacingWorlds that pushed the room to x=-335058, past
        # UE2's 262144 world limit, and the whole sky vanished.
        wlo, whi = base_bounds
        # Outside the world *brush*, not merely the geometry bounds: only actors
        # the subtract box does not enclose are unrenderable.
        box = (tuple(v - margin for v in wlo), tuple(v + margin for v in whi))
        distant = []
        if shrink_backdrop:
            distant = [a for a in mesh_actors
                       if getattr(a, "source_class", "StaticMeshActor") == "StaticMeshActor"
                       and parse_location(a) is not None
                       and is_outside(parse_location(a), box)]
        staying = [a for a in mesh_actors if a not in distant]

        # Clear of every actor that stays, not just the brushes: UT3 puts distant
        # backdrop meshes far outside the play area and the room must miss them.
        # Start from the world brush as actually built -- with --keep-backdrop it
        # grew to enclose that scenery, and a sky room inside it would be carved
        # out of the map itself.
        lo = [v - margin for v in world_bounds[0]]
        hi = [v + margin for v in world_bounds[1]]
        for actor in staying + terrain_actors:
            for key, value in actor.properties:
                if key != "Location":
                    continue
                m = re.match(r"\(X=(\S+?),Y=(\S+?),Z=(\S+?)\)", value)
                if not m:
                    continue
                for i, v in enumerate(float(x) for x in m.groups()):
                    lo[i] = min(lo[i], v)
                    hi[i] = max(hi[i], v)
        sky_actors = make_skybox(base_bounds, texture_package, sky_mesh,
                                 sky_radius or 1.0, clear_of=(tuple(lo), tuple(hi)),
                                 ambient=light_stats.ambient if light_stats else None,
                                 texture=texture_set.name_for(None) if texture_set else None)
        for actor in sky_actors:
            t3d.add(actor)

        # Optionally bring UT3's distant backdrop geometry into the sky room.
        # The scale is set by the dome: whatever shrank it to fit shrinks the
        # horizon too, so every apparent angle is preserved. Off by default --
        # that scenery is well inside the far plane where UT3 put it, so it can
        # stay real geometry at a real distance and only the world brush has to
        # grow to reach it.
        if shrink_backdrop:
            from convert.skybox_move import move_to_skybox, parse_scale

            sky_center = parse_location(sky_actors[1])
            dome_scale = parse_scale(sky_actors[-1]) if len(sky_actors) > 2 else 1.0
            ut3_dome_scale = parse_scale(dome_actor) if dome_actor is not None else 1.0
            world_scale = (dome_scale / ut3_dome_scale) if ut3_dome_scale else 1.0
            map_center = [(wlo[i] + whi[i]) / 2.0 for i in range(3)]
            if distant:
                from convert.skybox import DEFAULT_MERGE_DISTANCE
                from convert.skybox_move import merge_close

                mesh_actors = staying
                moved = move_to_skybox(distant, map_center, sky_center, world_scale)
                merged = merge_close(moved, args.sky_merge_distance)
                for actor in merged:
                    t3d.add(actor)
                note = ""
                if len(merged) != len(moved):
                    note = " (%d merged as co-located)" % (len(moved) - len(merged))
                # Printed as a ratio that reads correctly in both directions:
                # the sky room usually shrinks the horizon (1:667 on BL-Dekk),
                # but a map whose dome is smaller than the room's enlarges it,
                # and "1:0" said nothing.
                if world_scale and world_scale <= 1.0:
                    ratio = "1:%.0f" % (1.0 / world_scale)
                elif world_scale:
                    ratio = "%.2f:1" % world_scale
                else:
                    ratio = "unscaled"
                print("          %d distant backdrop mesh(es) moved into the skybox "
                      "at %s scale%s" % (len(merged), ratio, note))
            # Place the dome where UT3 had it relative to the map, same scale.
            if dome_actor is not None:
                dome_loc = parse_location(dome_actor)
                if dome_loc:
                    for i, (k, v) in enumerate(sky_actors[-1].properties):
                        if k == "Location":
                            sky_actors[-1].properties[i] = (k, vec(tuple(
                                sky_center[j] + (dome_loc[j] - map_center[j]) * world_scale
                                for j in range(3))))
        print("  skybox: %s in a SkyZoneInfo room; world brush set to FakeBackdrop"
              % sky_mesh)
        if getattr(mesh_stats, "skipped_sky", 0):
            print("          %d in-level sky mesh actor(s) dropped (UT3 scale would "
                  "dwarf the map)" % mesh_stats.skipped_sky)

    # Emitted last: the skybox block above removes the distant backdrop meshes
    # from this list before they reach the level.
    for actor in mesh_actors:
        t3d.add(actor)

    # The editor would otherwise give the map its own LevelInfo at class
    # defaults, which precaches for deathmatch whatever the map turned out to be.
    from convert.levelinfo import ONSLAUGHT, game_type, make_level_info

    precache = game_type(onslaught_stats, objective_stats)
    # The play area as built, so the Onslaught radar can be sized from it -- and
    # the same range the radar image has to span, so the two agree by
    # construction. See convert/minimap.py.
    radar_image = None
    if (precache == ONSLAUGHT and terrain_stats is not None
            and getattr(terrain_stats, "rendered", None) and uc_path
            and not args.no_minimap):
        from convert.levelinfo import radar_range
        from convert.minimap import insert_exec, render, write_minimap

        reach = radar_range(base_bounds)
        image = render(terrain_stats.rendered, reach, args.minimap_size)
        if image is not None:
            package_dir = os.path.dirname(os.path.dirname(uc_path))
            _name, line, radar_image = write_minimap(
                os.path.join(package_dir, "Terrain"), texture_package, "Terrain",
                "Map", image, args.minimap_size)
            if insert_exec(uc_path, line):
                print("  radar map drawn from the terrain over +/-%.0fuu (%dx%d)"
                      % (reach, args.minimap_size, args.minimap_size))
            else:
                radar_image = None
    t3d.add(make_level_info(precache, bounds=base_bounds,
                            radar_image=radar_image))

    # Every map gets a ZoneInfo, not just terrain ones. A converted map has none
    # of its own, so its zone is the LevelInfo and every level-wide setting --
    # ambient light, KillZ -- would have to be applied there by hand.
    zone = None
    if not args.no_zone_info and base_bounds:
        from convert.terrain import make_zone_info
        from ut3.objects.level import kill_z

        level_kill_z = kill_z(p)
        zone = make_zone_info(base_bounds, args.world_margin,
                              light_stats.ambient if light_stats else None,
                              terrain=bool(terrain_actors),
                              kill_z=(level_kill_z * args.scale
                                      if level_kill_z is not None else None))
        t3d.add(zone)

    # Anything still outside UE2's world would be silently misplaced, and the
    # scenery a stranded light was lighting has usually gone into the skybox.
    stranded = t3d.out_of_world()
    if stranded:
        t3d.drop(stranded)
        kinds = {}
        for actor in stranded:
            kinds[actor.cls] = kinds.get(actor.cls, 0) + 1
        print("  %d actor(s) dropped for falling outside UE2's %.0fuu world: %s"
              % (len(stranded), HALF_WORLD_MAX,
                 ", ".join("%d %s" % (n, c) for c, n in sorted(kinds.items()))))

    t3d.write(args.output)

    print("%s -> %s" % (os.path.basename(args.package), args.output))
    print("  %s" % stats)
    if mesh_stats:
        print("  %s" % mesh_stats)
    if terrain_stats and terrain_stats.terrains:
        print("  %s" % terrain_stats)
    if mover_stats and mover_stats.movers:
        print("  %s" % mover_stats)
    if sound_stats and (sound_stats.actors or sound_stats.failed):
        print("  %s" % sound_stats)
        for name, why in sound_stats.failed[:5]:
            print("      skipped sound %s: %s" % (name, why))
    if zone:
        carried = []
        for key, value in zone.properties:
            if key == "KillZ":
                carried.append("KillZ %.0f" % float(value))
            elif key == "AmbientBrightness":
                carried.append("ambient %s" % value)
            elif key == "bTerrainZone":
                carried.append("terrain")
        print("  ZoneInfo emitted, carrying %s"
              % (", ".join(carried) if carried else "the level's zone"))
    elif light_stats and light_stats.ambient:
        print("      ^ set by hand in View > Level Properties > ZoneLight")
    if light_stats:
        print("  %s" % light_stats)
        if not light_stats.ambient and args.ambient is None:
            print("      no SkyLight, so the zone gets no ambient at all -- if the "
                  "map plays dark, --ambient N is the dial (a UDK map bakes its "
                  "fill light into lightmaps, which do not convert)")
        if light_stats.ambient:
            b, h, sat = light_stats.ambient
            parts = getattr(light_stats, "ambient_parts", [])
            detail = ""
            if len(parts) > 1:
                detail = " (%s summed)" % " + ".join(str(v) for v in parts)
            floored = getattr(light_stats, "ambient_floored", None)
            if floored is not None:
                detail += (" (raised from %d: one dim SkyLight, and a map with "
                           "any SkyLight gets at least this much)" % floored)
            if args.ambient is not None:
                print("  --ambient -> AmbientBrightness=%d AmbientHue=%d "
                      "AmbientSaturation=%d (given, not derived)" % (b, h, sat))
            else:
                print("  UT3 SkyLight -> AmbientBrightness=%d AmbientHue=%d "
                      "AmbientSaturation=%d%s; --ambient-gain to taste"
                      % (b, h, sat, detail))
    if actor_stats:
        print("  %s" % actor_stats)
    if onslaught_stats and (onslaught_stats.cores or onslaught_stats.vehicles):
        print("  %s" % onslaught_stats)
        if onslaught_stats.red:
            print("      ^ UT3 states no team on either core; %s taken as red and %s "
                  "as blue by placement order" % (onslaught_stats.red,
                                                  onslaught_stats.blue))
    if objective_stats and objective_stats.objectives:
        print("  %s" % objective_stats)
    if teleporter_stats and teleporter_stats.teleporters:
        print("  %s" % teleporter_stats)
    if pickup_stats:
        print("  %s" % pickup_stats)
        if pickup_stats.path_nodes or pickup_stats.jump_pads:
            print("      ^ run Build > Paths in UnrealEd: UT2004 derives its own "
                  "reachspecs and jump velocities")
    if world:
        # The expanded bounds, not stats.world_bounds: inline sky mode grows the
        # box to enclose the dome, and reporting the raw geometry bounds here
        # understates the brush by an order of magnitude.
        print("  world void tiled into %d subtract cell(s)" % len(cells))
        lo, hi = world_bounds
        size = [hi[i] - lo[i] + 2 * margin for i in range(3)]
        print("  world subtract brush: %.0f x %.0f x %.0f uu (margin %.0f)"
              % (size[0], size[1], size[2], margin))
        # UE2 clamps the world to +/-262144uu (Engine/Inc/Engine.h HALF_WORLD_MAX).
        reach = max(max(abs(lo[i]), abs(hi[i])) + margin for i in range(3))
        if reach > 262144.0:
            print("  WARNING: geometry reaches %.0fuu, past UE2's HALF_WORLD_MAX of 262144" % reach)
    print("  texture package placeholder: %s" % texture_package)
    if stats.dropped_flag_bits:
        print("  %d polygons had UE3-only flag bits dropped" % stats.dropped_flag_bits)


# What `assets` sweeps out of a content package, and which exporter takes it.
# Everything else in a .upk -- SkeletalMesh, AnimSet, ParticleSystem,
# PhysicsAsset, AnimTree -- has no UE2 equivalent this converter can write, so
# it is counted and reported rather than silently ignored.
ASSET_CLASSES = ("Texture2D", "StaticMesh", "SoundNodeWave")

# Named so the summary can say what it is leaving behind rather than just how
# many exports it skipped. The count is what matters: a .upk whose content is
# mostly skeletal is not worth extracting.
UNCONVERTIBLE = ("SkeletalMesh", "AnimSet", "AnimSequence", "ParticleSystem",
                 "PhysicsAsset", "AnimTree")


def _build_package_assets(p, package_path, package_name, out_dir, max_size,
                          match=None, with_textures=True, with_meshes=True,
                          with_sounds=True, scale=1.0):
    """Extract a content package's assets, with no level to walk.

    `_build_assets` above starts from a level and follows its actors, which is
    the only way to know what a *map* uses. A .upk has no level: nothing
    references anything, so the sweep is the package's own export table and
    every asset of a class we can write is taken.
    """
    from convert.meshes import MeshSet, export_meshes
    from convert.sounds import SoundSet, export_sounds
    from convert.textures import TextureSet, export_textures
    from ut3.resolve import PackageIndex

    stale = clean_package(out_dir, package_name)
    if stale:
        print("  cleared %d file(s) from the previous build" % stale)

    index = PackageIndex.for_map(package_path)
    texture_set = TextureSet(package_name)
    counts = {cls: 0 for cls in ASSET_CLASSES}
    left = {}

    mesh_set = MeshSet(package_name)
    sound_set = SoundSet(package_name)
    for e in p.exports:
        cls = p.class_name_of(e)
        if cls in UNCONVERTIBLE:
            left[cls] = left.get(cls, 0) + 1
            continue
        if cls not in ASSET_CLASSES:
            continue
        if match and not fnmatch.fnmatch(e.name.lower(), match.lower()):
            continue
        if cls == "Texture2D" and with_textures:
            texture_set.add_texture(p, e)
        elif cls == "StaticMesh" and with_meshes:
            mesh_set.meshes[mesh_set._unique(e.name)] = (p, e, (), None)
        elif cls == "SoundNodeWave" and with_sounds:
            sound_set.waves[sound_set._unique(e.name)] = (p, e)
        else:
            continue
        counts[cls] += 1

    # Meshes before textures, for the reason _build_assets gives: exporting a
    # mesh is where its materials are read, and those add to the texture set.
    extra_exec, mesh_stats = [], None
    if with_meshes and mesh_set.meshes:
        extra_exec, mesh_stats = export_meshes(
            mesh_set, out_dir, index, texture_set, scale=scale
        )
        extra_exec = list(extra_exec)
    sound_stats = None
    if with_sounds and sound_set.waves:
        sound_exec, sound_stats = export_sounds(sound_set, out_dir, index)
        extra_exec += list(sound_exec)

    written, uc_path = export_textures(
        texture_set, out_dir, index, max_size=max_size, extra_exec=extra_exec
    )
    if with_meshes and mesh_set.meshes:
        from convert.meshes import apply_skins

        # No actors to skin, but this is also what settles mesh_set.skins, so
        # the summary can say how many elements found a material.
        apply_skins([], mesh_set, texture_set, mesh_stats)
    return (texture_set, written, uc_path, counts, left, mesh_set, sound_set,
            mesh_stats, sound_stats)


def cmd_assets(args):
    p = Package(args.package)
    package_name = _texture_package_name(args.package, args.texture_package)
    (texture_set, written, uc_path, counts, left, mesh_set, sound_set,
     mesh_stats, sound_stats) = _build_package_assets(
        p, args.package, package_name, args.output, args.max_texture_size,
        match=args.match, with_textures=not args.no_textures,
        with_meshes=not args.no_meshes, with_sounds=not args.no_sounds,
        scale=args.scale,
    )
    print("%s -> %s" % (os.path.basename(args.package), args.output))
    print("  package: %s" % os.path.join(args.output, package_name))
    print("  %d texture(s), %d static mesh(es), %d sound(s)"
          % (counts["Texture2D"], counts["StaticMesh"], counts["SoundNodeWave"]))
    print("  %d texture file(s) written -> %s"
          % (written, os.path.basename(uc_path)))
    if sound_stats is not None and getattr(sound_stats, "failed", None):
        print("  %d sound(s) could not be decoded: %s"
              % (len(sound_stats.failed),
                 ", ".join(name for name, _why in sound_stats.failed[:5])))
    for name, why in texture_set.failed[:10]:
        print("  skipped %s: %s" % (name, why))
    if left:
        print("  not converted, no UE2 equivalent: %s"
              % ", ".join("%d %s" % (n, cls) for cls, n in sorted(left.items())))
    print("  build it with: ucc make   (add %s to EditPackages)" % package_name)


def cmd_textures(args):
    """Textures only -- `assets` with the other two sweeps off."""
    args.no_meshes = True
    args.no_sounds = True
    args.no_textures = False
    args.match = None
    args.scale = 1.0
    return cmd_assets(args)


def cmd_imports(args):
    p = Package(args.package)
    shown = 0
    for i, imp in enumerate(p.imports):
        path = p.path_of(-(i + 1))
        if args.match and not fnmatch.fnmatch(path.lower(), args.match.lower()):
            continue
        print("%6d  %-28s %s" % (-(i + 1), imp.class_name, path))
        shown += 1
        if shown >= args.number:
            print("... (use -n to show more)")
            break


def build_parser():
    """The full command line, as its own function so the GUI can read it.

    ut3convgui.py builds its widgets by walking these actions -- names,
    defaults, types and help text all come from here, so a flag added below
    shows up in the GUI without touching it.
    """
    ap = argparse.ArgumentParser(prog="ut3conv", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("info", help="package header summary")
    sp.add_argument("package")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("classes", help="histogram of export classes")
    sp.add_argument("package")
    sp.add_argument("-n", "--number", type=int, default=40)
    sp.set_defaults(func=cmd_classes)

    sp = sub.add_parser("list", help="list exports")
    sp.add_argument("package")
    sp.add_argument("-c", "--cls", help="filter by class name")
    sp.add_argument("-m", "--match", help="glob match on object name")
    sp.add_argument("-n", "--number", type=int, default=50)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("props", help="dump an object's tagged properties")
    sp.add_argument("package")
    sp.add_argument("object", help="export index, name, or dotted path")
    sp.add_argument("-n", "--number", type=int, default=3, help="max matching objects to dump")
    sp.add_argument("--components", action="store_true")
    sp.set_defaults(func=cmd_props)

    sp = sub.add_parser("t3d", help="convert BSP brushes to a UT2004 .t3d")
    sp.add_argument("package")
    sp.add_argument("-o", "--output", required=True, help="output .t3d path")
    sp.add_argument("--scale", type=float, default=1.0, help="world scale factor (default 1.0)")
    sp.add_argument("--no-volumes", action="store_true",
                    help="omit BlockingVolumes (UT3's invisible walls; they keep players "
                         "inside the intended play area)")
    sp.add_argument("--texture-package", help="package name to qualify texture references with")
    sp.add_argument("--world-cell", type=float, default=0.0,
                    help="tile the enclosing void into cells this big (0 = one box, the "
                         "default; tiled cells have been seen to carve only partly)")
    sp.add_argument("--no-world-brush", action="store_true",
                    help="omit the enclosing subtractive brush (UT2004 is subtractive, UT3 additive)")
    sp.add_argument("--world-margin", type=float, default=1024.0,
                    help="padding around the geometry for the world brush (default 1024)")
    sp.add_argument("--textures", metavar="DIR", default=install_root(),
                    help="where to write the generated package (textures, meshes, "
                         "terrain, sounds). Defaults to the UT2004 install root, "
                         "which is the only place ucc make can build it from")
    sp.add_argument("--no-package", action="store_true",
                    help="emit only the .t3d, with no buildable asset package")
    sp.add_argument("--max-texture-size", type=int, default=DEFAULT_MAX_SIZE,
                    help="largest mip to export (default %d)" % DEFAULT_MAX_SIZE)
    sp.add_argument("--no-meshes", action="store_true",
                    help="omit static meshes (requires --textures otherwise)")
    sp.add_argument("--no-terrain", action="store_true", help="omit converted terrain")
    sp.add_argument("--no-movers", action="store_true",
                    help="omit Movers (Matinee-animated InterpActors then stay parked "
                         "where UT3 placed them)")
    sp.add_argument("--mover-keys", type=int, default=24,
                    help="keyframes to resample a Matinee move track into "
                         "(default 24, the size of Mover.KeyPos)")
    sp.add_argument("--no-sounds", action="store_true",
                    help="omit ambient sounds (they need ffmpeg to decode UT3's Ogg)")
    sp.add_argument("--sound-gain", type=float, default=1.0,
                    help="scales every ambient's volume; UE3 1.0 maps to SoundVolume "
                         "255 at the default 1.0")
    sp.add_argument("--sky-merge-distance", type=float, default=1.0,
                    help="merge backdrop meshes landing this close in the sky room "
                         "(invisible at skybox scale; avoids co-located warnings)")
    sp.add_argument("--shrink-backdrop", action="store_true",
                    help="always shrink UT3's distant backdrop meshes into the sky room "
                         "by the dome's scale factor, instead of leaving them in the "
                         "level at their true positions. Happens automatically anyway "
                         "when reaching them would cross UE2's 262144uu world limit")
    sp.add_argument("--keep-backdrop", action="store_true",
                    help="leave the backdrop in the level even when the void needed to "
                         "enclose it runs past UE2's world limit (the map will be "
                         "clamped and broken; for diagnosing that)")
    sp.add_argument("--sky-mode", choices=("skybox", "inline"), default="skybox",
                    help="skybox puts the dome in a SkyZoneInfo room so it reads as "
                         "infinitely distant, at UT3's proportions; inline keeps it as "
                         "ordinary level geometry, capped by UE2's 65536uu far plane")
    sp.add_argument("--sky-dome-margin", type=float, default=1.25,
                    help="how far past the furthest geometry the inline dome sits")
    sp.add_argument("--no-skybox", action="store_true",
                    help="omit the SkyZoneInfo room (the world brush then shows a flat texture)")
    sp.add_argument("--no-zone-info", action="store_true",
                    help="do not emit the ZoneInfo that terrain needs to render")
    sp.add_argument("--deco-density", type=float, default=0.5,
                    help="how thickly UT3's terrain foliage is scattered as UT2004 "
                         "decoration layers; 0 omits them (default 0.5). UT3 states "
                         "where the ground cover goes but not how much")
    sp.add_argument("--terrain-layer-scale", type=float, default=0.0,
                    help="force one texture tiling on every terrain layer; by "
                         "default each layer derives its own from UT3's "
                         "MappingScale, per axis")
    sp.add_argument("--keep-effect-meshes", action="store_true",
                    help="convert unlit translucent effect meshes (light beams, fog sheets) "
                         "instead of skipping them; they import as opaque surfaces")
    sp.add_argument("--all-textures", action="store_true",
                    help="import every texture each material refers to, not just "
                         "the one drawn -- normals, speculars, masks and the "
                         "branches a static switch turns off. Nothing references "
                         "them; they are there for hand-editing the package "
                         "afterwards, and they cost package size")
    sp.add_argument("--no-materials", action="store_true",
                    help="do not build UE2 Shader/FinalBlend objects for translucent, "
                         "additive or unlit surfaces; every surface falls back to a "
                         "flat texture, which is what the converter did before")
    sp.add_argument("--surface-scale", type=float, default=1.0,
                    help="extra multiplier on BSP surface UVs; 1.0 reproduces UT3's "
                         "own tiling (the real conversion is UE3_BSP_UV_SCALE)")
    sp.add_argument("--no-sublevels", action="store_true",
                    help="do not merge the streaming sub-levels the persistent level "
                         "always loads; an Angels Fall First map is nearly empty "
                         "without them, since its content is streamed in")
    sp.add_argument("--no-lights", action="store_true", help="omit converted lights")
    sp.add_argument("--no-player-starts", action="store_true", help="omit PlayerStarts")
    sp.add_argument("--no-onslaught", action="store_true",
                    help="omit Warfare/Onslaught conversion (power cores, nodes, the "
                         "link setup and vehicle factories)")
    sp.add_argument("--no-minimap", action="store_true",
                    help="do not draw a radar background from the terrain")
    sp.add_argument("--minimap-size", type=int, default=256,
                    help="radar background resolution, square (default 256)")
    sp.add_argument("--vehicle-rise", type=float, default=32.0,
                    help="how far above its stated spot a vehicle factory is placed, "
                         "so a spawning vehicle does not drop through the mesh it "
                         "rests on (default 32); 0 keeps UT3's exact position")
    sp.add_argument("--node-rise", type=float, default=0.0,
                    help="extra height for a power node that rests on a brush, on "
                         "top of standing its mesh and touch cylinder clear of the "
                         "floor; raise it if a node still has to be jumped on")
    sp.add_argument("--countdown-time", type=int, default=120,
                    help="seconds a UT3 countdown node must be held (default 120); UT3 "
                         "does not state one in the map")
    sp.add_argument("--countdown-damage", type=int, default=901,
                    help="damage a completed countdown deals to the enemy core "
                         "(default 901, ONS-Tyrant's value, against a core with "
                         "4500 health); needs --onslaught-specials")
    sp.add_argument("--onslaught-specials", action="store_true",
                    help="place the OnslaughtSpecials2 core/node classes instead of "
                         "stock Onslaught, keeping countdown nodes and standalone "
                         "flags. Requires that mod: without it the editor drops the "
                         "actors silently and the map has no power cores")
    sp.add_argument("--no-objectives", action="store_true",
                    help="omit team game objectives (CTF flag bases)")
    sp.add_argument("--no-teleporters", action="store_true",
                    help="omit teleporters")
    sp.add_argument("--no-teleporter-effect", action="store_true",
                    help="emit the Teleporter alone, without the portal meshes "
                         "UT2004's own DM-Deck17 dresses its teleporters with "
                         "(a converted teleporter is otherwise invisible)")
    sp.add_argument("--no-pickups", action="store_true",
                    help="omit weapon bases, health, armour and powerups")
    sp.add_argument("--no-paths", action="store_true",
                    help="omit PathNodes and jump pads (bots then have no paths)")
    sp.add_argument("--ambient", type=int, default=None,
                    help="set the zone's AmbientBrightness (0-255) outright, "
                         "overriding whatever the SkyLights give. A UDK map "
                         "bakes its fill light into lightmaps and often has no "
                         "SkyLight, leaving nothing for --ambient-gain to scale")
    sp.add_argument("--ambient-gain", type=float, default=16.0,
                    help="scales the UT3 SkyLight into a UT2004 AmbientBrightness (default 16)")
    sp.add_argument("--light-radius-scale", type=float, default=1.0,
                    help="widen every light's radius by this factor (default 1.0)")
    sp.add_argument("--light-gain", type=float, default=32.0,
                    help="UE3 brightness 1.0 maps to this UE2 LightBrightness (default 32)")
    sp.set_defaults(func=cmd_t3d)

    sp = sub.add_parser("textures", help="extract textures into a buildable UT2004 package")
    sp.add_argument("package")
    sp.add_argument("-o", "--output", default=install_root(),
                    help="where to write the package (default: the UT2004 install root)")
    sp.add_argument("--texture-package", help="name for the generated package")
    sp.add_argument("--max-texture-size", type=int, default=DEFAULT_MAX_SIZE)
    sp.set_defaults(func=cmd_textures)

    sp = sub.add_parser("assets", help="extract a content package's textures, "
                                       "static meshes and sounds into one "
                                       "buildable UT2004 package")
    sp.add_argument("package")
    sp.add_argument("-o", "--output", default=install_root(),
                    help="where to write the package (default: the UT2004 install root)")
    sp.add_argument("--texture-package", help="name for the generated package")
    sp.add_argument("--max-texture-size", type=int, default=DEFAULT_MAX_SIZE,
                    help="largest mip to export (default %d)" % DEFAULT_MAX_SIZE)
    sp.add_argument("-m", "--match",
                    help="only assets whose name matches this glob")
    sp.add_argument("--scale", type=float, default=1.0,
                    help="scale applied to static mesh geometry (default 1.0)")
    sp.add_argument("--no-textures", action="store_true", help="skip Texture2D")
    sp.add_argument("--no-meshes", action="store_true", help="skip StaticMesh")
    sp.add_argument("--no-sounds", action="store_true", help="skip SoundNodeWave")
    sp.set_defaults(func=cmd_assets)

    sp = sub.add_parser("imports", help="list imports")
    sp.add_argument("package")
    sp.add_argument("-m", "--match")
    sp.add_argument("-n", "--number", type=int, default=50)
    sp.set_defaults(func=cmd_imports)

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    main()
