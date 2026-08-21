"""UModel / UPolys readers.

Neither serializes as tagged properties -- both are native. Layouts here were
reversed against stock UT3 maps; see FORMAT.md.

UPolys, after its (empty) property list:

    i32   Count, i32 Max, i32 Owner      <- TTransArray header
    FPoly[Count]

FPoly:

    FVector Base, Normal, TextureU, TextureV
    i32     VertexCount, FVector[VertexCount]
    u32     PolyFlags
    i32     Actor            (PackageIndex of the owning brush)
    FName   ItemName
    i32     Material         (PackageIndex)
    i32     iLink, iBrushPoly
    f32     ShadowMapScale
    u32     LightingChannels
"""

import struct

from ..props import read_object_properties

# Package version at which an FPoly grew its Lightmass settings and ruleset
# name. UT3 (512) has neither and TOXIKK's UDK (868) has both; the exact
# version in between was not established, so this is the earliest that keeps
# every UT3 map reading the way it did.
UDK_FPOLY_EXTRAS = 584
# bUseTwoSidedLighting, bShadowIndirectOnly, FullyOccludedSamplesFraction,
# bUseEmissiveForStaticLighting, EmissiveLightFalloffExponent,
# EmissiveLightExplicitInfluenceRadius, EmissiveBoost, DiffuseBoost,
# SpecularBoost -- nine 4-byte fields.
LIGHTMASS_SETTINGS_SIZE = 36


class Poly:
    __slots__ = (
        "base",
        "normal",
        "texture_u",
        "texture_v",
        "vertices",
        "flags",
        "actor",
        "item_name",
        "material",
        "link",
        "brush_poly",
        "shadow_map_scale",
        "lighting_channels",
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __repr__(self):
        return "<Poly %d verts normal=%s flags=0x%X>" % (
            len(self.vertices),
            tuple(round(c, 2) for c in self.normal),
            self.flags,
        )


def _vec(r):
    return struct.unpack("<3f", r.bytes(12))


def read_polys(pkg, export):
    """Read a UPolys export into a list of Poly."""
    if pkg.class_name_of(export) != "Polys":
        raise ValueError("%s is a %s, not a Polys" % (export.name, pkg.class_name_of(export)))
    data = pkg.export_data(export)
    _props, start, end = read_object_properties(pkg, export)
    if start is None:
        end = 0
    from ..package import Reader

    r = Reader(data, end)
    count = r.i32()
    r.i32()  # array max
    r.i32()  # TTransArray owner (points back at this Polys object)
    polys = []
    for _ in range(count):
        base = _vec(r)
        normal = _vec(r)
        tu = _vec(r)
        tv = _vec(r)
        n_verts = r.i32()
        if not (0 <= n_verts <= 64):
            raise ValueError("implausible vertex count %d in %s" % (n_verts, export.name))
        verts = [_vec(r) for _ in range(n_verts)]
        flags = r.u32()
        actor = r.i32()
        item_name = pkg.fname(r)
        material = r.i32()
        link = r.i32()
        brush_poly = r.i32()
        shadow_map_scale = r.f32()
        lighting_channels = r.u32()
        if pkg.version >= UDK_FPOLY_EXTRAS:
            # UDK adds FLightmassPrimitiveSettings -- nine fields, two-sided
            # lighting through to the specular boost -- and the FName of the
            # procedural-building ruleset variation. Neither has any meaning in
            # UE2, so both are skipped; what matters is the stride, since
            # reading a poly short walks into the middle of the next one.
            r.p += LIGHTMASS_SETTINGS_SIZE
            pkg.fname(r)
        polys.append(
            Poly(
                base=base,
                normal=normal,
                texture_u=tu,
                texture_v=tv,
                vertices=verts,
                flags=flags,
                actor=pkg.ref(actor),
                item_name=item_name,
                material=pkg.ref(material),
                link=link,
                brush_poly=brush_poly,
                shadow_map_scale=shadow_map_scale,
                lighting_channels=lighting_channels,
            )
        )
    return polys


def read_bounds(pkg, model_export):
    """FBoxSphereBounds (Origin, BoxExtent, SphereRadius) at the head of a UModel."""
    data = pkg.export_data(model_export)
    _props, start, end = read_object_properties(pkg, model_export)
    if start is None:
        end = 0
    if end + 28 > len(data):
        return None
    ox, oy, oz, ex, ey, ez, radius = struct.unpack_from("<7f", data, end)
    return (ox, oy, oz), (ex, ey, ez), radius


def surface_closes(polys, tolerance=1e-3):
    """Does this surface enclose a volume, judged by the divergence theorem?

    For any closed surface the face area vectors sum to zero -- every outward
    face is balanced by the rest of the hull. An open one leaves a residue the
    size of its opening, so comparing that residue against the total area says
    how nearly closed the shape is without ever looking at an edge.

    Which matters because edges lie. Matching them pairwise calls a brush open
    the moment it has a T-junction, where one face spans what two faces split:
    CTF-Coret's Brush_143 has a face running (0,0) to (-320,0) while its
    neighbours meet at (-224,0), and six edges of forty go unmatched for that
    reason alone. Both it and Brush_144 are CSG_Subtract, so dropping them left
    the recess they carve as solid rock with a placeholder texture on it.
    """
    total = [0.0, 0.0, 0.0]
    magnitude = 0.0
    for poly in polys:
        verts = poly.vertices
        if len(verts) < 3:
            continue
        # Newell's method: twice the area vector, robust on non-planar faces.
        area = [0.0, 0.0, 0.0]
        for i in range(len(verts)):
            a, b = verts[i], verts[(i + 1) % len(verts)]
            area[0] += (a[1] - b[1]) * (a[2] + b[2])
            area[1] += (a[2] - b[2]) * (a[0] + b[0])
            area[2] += (a[0] - b[0]) * (a[1] + b[1])
        for i in range(3):
            total[i] += area[i]
        magnitude += sum(c * c for c in area) ** 0.5
    if magnitude <= 0.0:
        return False
    return (sum(c * c for c in total) ** 0.5) / magnitude <= tolerance


def is_closed_solid(polys):
    """Do these FPolys enclose a volume?

    UE2's CSG has no way to reject an open one: a brush that is a single flat
    sheet is added as an entire *half-space* of solid, which shows up in game as
    an invisible plane that kills anything crossing it (the pawn lands in zone 0
    and the engine calls FellOutOfWorld, UnPhysic.cpp:336).

    Two tests, and either will do. Every edge of a closed hull is shared by
    exactly two faces, once in each direction -- exact, but blind to T-junctions
    (see surface_closes). The area-vector residue catches those, at the cost of
    accepting a shape whose openings happen to cancel out, which no brush
    authored in an editor does.
    """
    if len(polys) < 4:
        return False           # nothing with fewer than four faces encloses anything
    edges = {}
    for poly in polys:
        verts = poly.vertices
        if len(verts) < 3:
            return False
        for i in range(len(verts)):
            a = tuple(round(c, 2) for c in verts[i])
            b = tuple(round(c, 2) for c in verts[(i + 1) % len(verts)])
            if a == b:
                continue
            edges[(a, b)] = edges.get((a, b), 0) + 1
    for (a, b), count in edges.items():
        if count != 1 or edges.get((b, a), 0) != 1:
            return surface_closes(polys)
    return True


def find_polys(pkg, model_export):
    """Find the UPolys belonging to a UModel.

    The UModel layout after its property list is Bounds followed by several
    bulk-serialized arrays whose sizes vary, so rather than parse all of it we
    locate the PackageIndex in the object's data that resolves to a Polys
    export.

    Two thirds of the models own their Polys as a sub-object, which settles it
    outright. For the rest the search has to guess, and a wrong guess is worse
    than no answer: an unrelated Polys yields a stray face or two, and UE2 turns
    that into a half-space of solid. So candidates are checked for actually
    enclosing a volume before being accepted.
    """
    polys_indices = getattr(pkg, "_polys_index_cache", None)
    if polys_indices is None:
        polys_indices = {e.index for e in pkg.exports_of_class("Polys")}
        pkg._polys_index_cache = polys_indices

    # Deterministic when the model owns its Polys, which covers 446 of
    # DM-HeatRay's 611 models.
    owned = getattr(pkg, "_polys_owner_cache", None)
    if owned is None:
        owned = {}
        for e in pkg.exports_of_class("Polys"):
            owned.setdefault(e.outer, e)
        pkg._polys_owner_cache = owned
    if model_export.index in owned:
        return owned[model_export.index]

    data = pkg.export_data(model_export)
    _props, start, end = read_object_properties(pkg, model_export)
    if start is None:
        end = 0

    candidates = []
    # Brush models have empty Vectors/Points/Nodes arrays, which puts the
    # reference at a fixed +76 from the property list -- try that first.
    if end + 80 <= len(data):
        v = struct.unpack_from("<i", data, end + 76)[0]
        if v in polys_indices:
            candidates.append(v)
    for off in range(end, len(data) - 3, 4):
        v = struct.unpack_from("<i", data, off)[0]
        if v in polys_indices and v not in candidates:
            candidates.append(v)
    if not candidates:
        return None
    for v in candidates:
        export = pkg.exports[v - 1]
        try:
            polys = read_polys(pkg, export)
        except (ValueError, struct.error, IndexError):
            continue
        if is_closed_solid(polys or []):
            return export
    # Nothing encloses a volume. Returning the first candidate anyway would
    # hand the caller a sheet, so let it decide with the geometry in hand.
    return pkg.exports[candidates[0] - 1]
