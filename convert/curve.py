"""Evaluating UE3 InterpCurves, which is how Matinee stores every animated value.

An `InterpCurveVector` is a list of `InterpCurvePointVector`: an input time, an
output vector, an arrive and a leave tangent, and a mode saying how to get from
this point to the next. `FInterpCurve::Eval` scales the tangents by the segment
duration before the cubic, so a curve reads the same whatever the key spacing.
"""


class CurvePoint:
    __slots__ = ("t", "out", "arrive", "leave", "mode")

    def __init__(self, t, out, arrive, leave, mode):
        self.t = t
        self.out = out
        self.arrive = arrive
        self.leave = leave
        self.mode = mode


def _vec(value):
    inner = getattr(value, "value", None)
    if inner is None or len(inner) < 3:
        return (0.0, 0.0, 0.0)
    return tuple(float(c) for c in inner[:3])


def read_vector_curve(raw):
    """[CurvePoint] from an InterpCurveVector struct; empty if it has no points."""
    value = getattr(raw, "value", None)
    if not hasattr(value, "get"):
        return []
    points = value.get("Points")
    if points is None or not len(points):
        return []
    try:
        entries = points.as_props()
    except (ValueError, IndexError):
        return []
    out = []
    for entry in entries:
        out.append(CurvePoint(
            float(entry.get("InVal", 0.0)),
            _vec(entry.get("OutVal")),
            _vec(entry.get("ArriveTangent")),
            _vec(entry.get("LeaveTangent")),
            str(entry.get("InterpMode", "CIM_CurveAuto")),
        ))
    out.sort(key=lambda p: p.t)
    return out


def _cubic(p0, t0, p1, t1, a):
    a2 = a * a
    a3 = a2 * a
    return ((2 * a3 - 3 * a2 + 1) * p0 + (a3 - 2 * a2 + a) * t0
            + (-2 * a3 + 3 * a2) * p1 + (a3 - a2) * t1)


def eval_curve(points, t):
    """The curve's value at time `t`, clamped to its ends."""
    if not points:
        return (0.0, 0.0, 0.0)
    if t <= points[0].t:
        return points[0].out
    if t >= points[-1].t:
        return points[-1].out
    index = 0
    for i in range(len(points) - 1):
        if points[i].t <= t < points[i + 1].t:
            index = i
            break
    a, b = points[index], points[index + 1]
    span = b.t - a.t
    if span <= 0.0:
        return a.out
    if a.mode == "CIM_Constant":
        return a.out
    alpha = (t - a.t) / span
    if a.mode == "CIM_Linear":
        return tuple(a.out[i] + (b.out[i] - a.out[i]) * alpha for i in range(3))
    return tuple(_cubic(a.out[i], a.leave[i] * span,
                        b.out[i], b.arrive[i] * span, alpha) for i in range(3))


def sample(points, count, start=None, end=None):
    """`count` values evenly spaced in time over [start, end].

    The range matters as much as the count. Matinee plays a track from 0 to its
    `InterpLength` and nothing outside that window is ever seen, but an author
    is free to leave keys out there: DM-Deck's second lift carries one at
    t=-2.196, the shape of the descent it makes *before* the sequence starts.
    Sampling the curve's own extent picks that up and converts the lift as
    dropping 605uu through the floor and coming back, instead of rising 608.
    """
    if not points:
        return []
    if len(points) == 1 or count < 2:
        return [points[0].out]
    lo = points[0].t if start is None else start
    hi = points[-1].t if end is None else end
    if hi <= lo:
        lo, hi = points[0].t, points[-1].t
    step = (hi - lo) / (count - 1)
    return [eval_curve(points, lo + step * i) for i in range(count)]


def played_range(points, length):
    """The slice of a track Matinee actually plays: [0, length], clipped to it."""
    if not points:
        return 0.0, 0.0
    lo, hi = points[0].t, points[-1].t
    if not length or length <= 0:
        return lo, hi
    start, end = max(lo, 0.0), min(hi, length)
    if end <= start:
        return lo, hi
    return start, end
