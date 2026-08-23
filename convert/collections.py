"""Cooked component collections: StaticMeshCollectionActor.

Gears of War Reloaded cooks its scenery differently from UT3 and UDK. Where
those write one StaticMeshActor per placed mesh, Gears bundles them: 34
StaticMeshCollectionActors hold 3,362 StaticMeshComponents between them, and
`convert_actors` -- which looks for actors -- found 16 meshes in a map that
places three thousand. MP_Courtyard converted to an empty shell for that
reason.

The bundle is simple once read. A collection actor's tagged properties hold a
`StaticMeshComponents` array of object references, and its serial data ends
with one FMatrix per entry, in the same order:

    +0            tagged properties, ending with StaticMeshComponents(N)
    size - N*64   N x FMatrix, the components' local-to-world transforms

The matrices are the whole transform, so the component's own Translation,
Rotation and Scale must not be applied on top -- that is what `_transform_free`
strips before the emitter folds a component into its actor.

Each matrix decomposes exactly: the row lengths are the scale, the normalised
rows are the rotation, and the fourth row is the translation. Round-tripping
the recovered rotator back through `rotation_matrix` on all 3,362 of
MP_Courtyard's components reproduces the original basis to 5e-07, so nothing
here is approximated.
"""

import math
import struct

from convert.rotation import to_rotator
from ut3.props import Properties, Struct, read_object_properties

COLLECTION_CLASS = "StaticMeshCollectionActor"
LIGHT_COLLECTION_CLASS = "StaticLightCollectionActor"

MATRIX_SIZE = 64

# The component transform lives in the collection actor's matrix, so these are
# dropped from the component to stop _effective_transform applying them twice.
_TRANSFORM_KEYS = ("Translation", "Rotation", "Scale", "Scale3D")


class _PseudoExport:
    """Just enough of an Export for the emitter: a name and a component map."""

    __slots__ = ("name", "components", "size")

    def __init__(self, name):
        self.name = name
        self.components = {}
        self.size = 0


def _decompose(m):
    """(location, rotator, scale3d) from an FMatrix's 16 floats."""
    rows = (m[0:3], m[4:7], m[8:11])
    scale = tuple(math.sqrt(sum(c * c for c in row)) for row in rows)
    basis = tuple(
        tuple(c / s if s > 1e-9 else 0.0 for c in row)
        for row, s in zip(rows, scale)
    )
    return (m[12], m[13], m[14]), to_rotator(basis), scale


def _transform_free(comp):
    """A copy of the component's properties with its own transform removed."""
    out = Properties()
    for name, idx, type_name, value in comp:
        if name not in _TRANSFORM_KEYS:
            out.add(name, idx, type_name, value)
    # Kept so the mesh lookup can still follow the archetype chain.
    out.export = getattr(comp, "export", None)
    return out


def expand(pkg):
    """Yield (export, source_class, props, comp) for every collected component.

    Shaped to match what `convert_actors` reads from a real actor, so a
    collected mesh goes through exactly the same emission path -- materials,
    effect substitution, collision flags and all.
    """
    for actor in pkg.exports_of_class(COLLECTION_CLASS):
        props, start, _end = read_object_properties(pkg, actor)
        if start is None:
            continue
        array = props.get("StaticMeshComponents")
        if array is None or not array.count:
            continue
        data = pkg.export_data(actor)
        table = actor.size - array.count * MATRIX_SIZE
        if table < 0 or table + array.count * MATRIX_SIZE > len(data):
            continue
        for i, ref in enumerate(array.as_objects()):
            if not ref.is_export:
                continue
            comp, comp_start, _e = read_object_properties(pkg, ref.export)
            if comp_start is None:
                continue
            comp.export = ref.export
            matrix = struct.unpack_from("<16f", data, table + i * MATRIX_SIZE)
            location, rotator, scale3d = _decompose(matrix)

            placed = Properties()
            placed.add("Location", 0, "StructProperty", Struct("Vector", location))
            placed.add("Rotation", 0, "StructProperty", Struct("Rotator", rotator))
            placed.add("DrawScale3D", 0, "StructProperty", Struct("Vector", scale3d))
            # Named for the component, which is unique across the package; the
            # collection actor's own name is shared by a hundred of these.
            yield (_PseudoExport(ref.export.name), "StaticMeshActor",
                   placed, _transform_free(comp))


def _collected(pkg, actor_class, array_name):
    """Walk one kind of collection actor, yielding (component ref, matrix).

    Both collections are laid out the same way -- an array of component
    references in the tagged properties, and one FMatrix per entry filling the
    rest of the export -- so the walk is shared.
    """
    for actor in pkg.exports_of_class(actor_class):
        props, start, _end = read_object_properties(pkg, actor)
        if start is None:
            continue
        array = props.get(array_name)
        if array is None or not array.count:
            continue
        data = pkg.export_data(actor)
        table = actor.size - array.count * MATRIX_SIZE
        if table < 0 or table + array.count * MATRIX_SIZE > len(data):
            continue
        for i, ref in enumerate(array.as_objects()):
            if not ref.is_export:
                continue
            yield ref, struct.unpack_from("<16f", data, table + i * MATRIX_SIZE)


def expand_lights(pkg):
    """Yield (export, light class, props, component) for every collected light.

    The component's class names the light: a PointLightComponent is what a
    PointLight actor would have held, so dropping "Component" recovers the
    class `convert_lights` already knows how to place. MP_Courtyard keeps all
    118 of its lights in one StaticLightCollectionActor, which is why the map
    converted with no lighting whatsoever.
    """
    for ref, matrix in _collected(pkg, LIGHT_COLLECTION_CLASS, "LightComponents"):
        comp, comp_start, _e = read_object_properties(pkg, ref.export)
        if comp_start is None:
            continue
        comp.export = ref.export
        component_class = pkg.class_name_of(ref.export)
        if not component_class.endswith("Component"):
            continue
        light_class = component_class[: -len("Component")]
        location, rotator, _scale = _decompose(matrix)

        placed = Properties()
        placed.add("Location", 0, "StructProperty", Struct("Vector", location))
        placed.add("Rotation", 0, "StructProperty", Struct("Rotator", rotator))
        yield _PseudoExport(ref.export.name), light_class, placed, comp
