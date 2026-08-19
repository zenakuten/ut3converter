"""Make near-coplanar brush faces exactly coplanar.

UE2 decides which side of a plane a point is on with a fixed tolerance --
`THRESH_POINT_ON_PLANE` is 0.10 (Core/Inc/UnMath.h) -- and everything in CSG,
zoning and collision rests on that answer being consistent. UT3 brush vertices
carry sub-unit float drift from the UT3 editor, so faces that were authored
flush end up on planes a few thousandths of a unit apart. DM-HeatRay has 375
such pairs: near enough that UE2 calls them the same plane, far enough that the
arithmetic disagrees with itself.

The result is slivers and mis-classified space. In game that space is solid, so
the pawn's Location lands in zone 0 and the engine kills it outright as having
fallen out of the world (Engine/Src/UnPhysic.cpp:336) -- no fall, no KillZ, just
death on crossing an invisible boundary in mid-air.

Snapping vertices to the grid does remove the near-coplanar pairs, but it also
tilts sloped faces off their own plane (0.59uu on DM-HeatRay), which is a
different way to break the same tolerance. So this works the other way round:
the *planes* are clustered and aligned first, then each vertex is rebuilt as the
intersection of the faces that meet there. Every polygon then lies exactly on
its plane by construction, the brush keeps its topology, and nothing moves more
than a fraction of a unit.
"""

# Planes closer than this are treated as intended to be the same plane. It sits
# just past UE2's own THRESH_POINT_ON_PLANE of 0.10 so that no pair the engine
# would call coplanar survives as two distinct planes -- which is the whole
# point. Nothing legitimate is lost: the thinnest brush in DM-HeatRay is 4uu.
DEFAULT_PLANE_TOLERANCE = 0.11

# Normals this close count as one direction. UE2 compares normals component-wise
# against THRESH_NORMALS_ARE_SAME (0.00002, Core/Inc/UnMath.h); the dot-product
# equivalent is 1 - 6e-10, which is far tighter than UT3's float drift, so this
# is deliberately looser -- about a quarter of a degree -- to catch faces the
# UT3 editor left fractionally askew of each other.
NORMAL_TOLERANCE = 0.9999995

# A vertex that would have to move further than this to satisfy its planes says
# the brush is not what we think it is, so it is left alone.
MAX_VERTEX_SHIFT = 1.0


def _key(v):
    return (round(v[0], 3), round(v[1], 3), round(v[2], 3))


def _newell(verts):
    """Plane normal from vertex winding, which is how UE2 derives it."""
    nx = ny = nz = 0.0
    for i in range(len(verts)):
        a, b = verts[i], verts[(i + 1) % len(verts)]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length < 1e-12:
        return None
    return (nx / length, ny / length, nz / length)


def _solve3(rows):
    """Intersect three planes: rows are (nx, ny, nz, d). None if near-parallel."""
    (a1, b1, c1, d1), (a2, b2, c2, d2), (a3, b3, c3, d3) = rows
    det = (a1 * (b2 * c3 - b3 * c2)
           - b1 * (a2 * c3 - a3 * c2)
           + c1 * (a2 * b3 - a3 * b2))
    if abs(det) < 1e-6:
        return None
    x = (d1 * (b2 * c3 - b3 * c2) - b1 * (d2 * c3 - d3 * c2)
         + c1 * (d2 * b3 - d3 * b2)) / det
    y = (a1 * (d2 * c3 - d3 * c2) - d1 * (a2 * c3 - a3 * c2)
         + c1 * (a2 * d3 - a3 * d2)) / det
    z = (a1 * (b2 * d3 - b3 * d2) - b1 * (a2 * d3 - a3 * d2)
         + d1 * (a2 * b3 - a3 * b2)) / det
    return (x, y, z)


class PlaneSet:
    """Clusters face planes so faces UE2 cannot tell apart share one exactly.

    Two passes, because the answer must not depend on the order faces arrive
    in: first every plane is filed under a direction, then the offsets along
    each direction are sorted and any run of neighbours closer than the
    tolerance collapses onto its mean. A chain of faces each a hundredth from
    the next therefore ends up on one plane rather than several.
    """

    def __init__(self, tolerance=DEFAULT_PLANE_TOLERANCE):
        self.tolerance = tolerance
        self.directions = []        # [normal, [offsets]]
        self.resolved = {}          # direction index -> sorted [(offset, merged)]

    def _direction(self, normal):
        """(index, sign) of the axis this normal lies on, either way along it.

        Orientation is deliberately ignored. Where two solids abut, the faces
        that meet point *at each other*, so matching only same-facing normals
        leaves exactly the pairs that matter uncompared -- and a block whose
        face sits 0.001uu from the ramp's face traps a sliver of void between
        two solids, which is what UE2 then mis-classifies.
        """
        for index, (n, _offsets) in enumerate(self.directions):
            dot = n[0] * normal[0] + n[1] * normal[1] + n[2] * normal[2]
            if dot >= NORMAL_TOLERANCE:
                return index, 1.0
            if dot <= -NORMAL_TOLERANCE:
                return index, -1.0
        self.directions.append([tuple(normal), []])
        return len(self.directions) - 1, 1.0

    def add(self, normal, offset):
        index, sign = self._direction(normal)
        self.directions[index][1].append(offset * sign)

    def build(self):
        self.resolved = {}
        for index, (_n, offsets) in enumerate(self.directions):
            merged = []
            run = []
            for offset in sorted(offsets):
                if run and offset - run[-1] > self.tolerance:
                    mean = sum(run) / len(run)
                    merged.extend((o, mean) for o in run)
                    run = []
                run.append(offset)
            if run:
                mean = sum(run) / len(run)
                merged.extend((o, mean) for o in run)
            self.resolved[index] = merged

    def resolve(self, normal, offset):
        index = sign = None
        for i, (n, _offsets) in enumerate(self.directions):
            dot = n[0] * normal[0] + n[1] * normal[1] + n[2] * normal[2]
            if dot >= NORMAL_TOLERANCE:
                index, sign = i, 1.0
                break
            if dot <= -NORMAL_TOLERANCE:
                index, sign = i, -1.0
                break
        if index is None:
            return tuple(normal), offset
        signed = offset * sign
        best = None
        for original, mean in self.resolved.get(index, ()):
            gap = abs(original - signed)
            if best is None or gap < best[0]:
                best = (gap, mean)
        if best is None or best[0] > 1e-9:
            return tuple(normal), offset
        axis = self.directions[index][0]
        # Hand back the face's own orientation, on the shared plane.
        return tuple(c * sign for c in axis), best[1] * sign


def align_brushes(brushes, tolerance=DEFAULT_PLANE_TOLERANCE, passes=4):
    """Align near-coplanar faces across `brushes`. Returns (moved, skipped).

    Repeated until it settles: moving a vertex shifts the planes of every face
    that meets there, which can bring a further pair inside the tolerance, so
    one sweep does not finish the job.

    Brushes are expected to carry world position in a Location property and
    vertices in brush space, with no rotation or scale -- which is what the
    converter emits.
    """
    moved_total = 0
    skipped_total = 0
    for _ in range(passes):
        moved, skipped = _align_once(brushes, tolerance)
        moved_total = max(moved_total, moved)
        skipped_total = max(skipped_total, skipped)
        if not moved:
            break
    return moved_total, skipped_total


def _align_once(brushes, tolerance):
    prepared = []
    planes = PlaneSet(tolerance)
    for brush in brushes:
        origin = _origin_of(brush)
        if origin is None:
            continue
        faces = []
        for poly in brush.polygons:
            world = [tuple(v[i] + origin[i] for i in range(3)) for v in poly.vertices]
            normal = _newell(world)
            if normal is None:
                faces = None
                break
            offset = sum(normal[i] * world[0][i] for i in range(3))
            planes.add(normal, offset)
            faces.append((poly, world, normal, offset))
        if faces:
            prepared.append((brush, origin, faces))

    planes.build()
    moved = skipped = 0
    for brush, origin, faces in prepared:
        resolved = [planes.resolve(normal, offset) for _p, _w, normal, offset in faces]
        # Where each vertex is used, so it can be rebuilt from its own faces.
        incident = {}
        for index, (_poly, world, _n, _o) in enumerate(faces):
            for v in world:
                incident.setdefault(_key(v), (v, set()))[1].add(index)
        replacement = {}
        ok = True
        for key, (original, face_indices) in incident.items():
            rows = [(resolved[i][0][0], resolved[i][0][1], resolved[i][0][2], resolved[i][1])
                    for i in sorted(face_indices)]
            point = None
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    for k in range(j + 1, len(rows)):
                        point = _solve3((rows[i], rows[j], rows[k]))
                        if point is not None:
                            break
                    if point is not None:
                        break
                if point is not None:
                    break
            if point is None:
                ok = False
                break
            if max(abs(point[i] - original[i]) for i in range(3)) > MAX_VERTEX_SHIFT:
                ok = False
                break
            replacement[key] = point
        if not ok:
            skipped += 1
            continue
        changed = False
        for index, (poly, world, _n, _o) in enumerate(faces):
            new_verts = []
            for v in world:
                p = replacement[_key(v)]
                new_verts.append(tuple(p[i] - origin[i] for i in range(3)))
                if max(abs(p[i] - v[i]) for i in range(3)) > 1e-9:
                    changed = True
            poly.vertices = new_verts
            poly.normal = resolved[index][0]
            # Origin is the texture base, not a vertex: UE2 takes the plane from
            # Vertex[0] and uses Base only to anchor the texture, so leaving it
            # where UT3 put it keeps the surface alignment we spent so long
            # matching. It is at most a hair off the adjusted plane.
        if changed:
            moved += 1
    return moved, skipped


def _origin_of(brush):
    for key, value in brush.properties:
        if key == "Location":
            nums = value.strip("()").replace("X=", "").replace("Y=", "").replace("Z=", "")
            try:
                return [float(c) for c in nums.split(",")]
            except ValueError:
                return None
    return [0.0, 0.0, 0.0]
