#!/usr/bin/env python3
"""Compare a built .ut2's BSP against the CSG the .t3d asked for.

    python3 tools/verify_solidity.py map.ut2 map.t3d [x0 x1 y0 y1 z0 z1 step]

UE2 kills anything whose Location lands in a solid BSP leaf -- zone 0 -- and
calls it falling out of the world (Engine/Src/UnPhysic.cpp:336), so a region the
CSG meant to be empty but the engine built as solid is lethal and invisible.
This walks UModel::PointRegion (Engine/Src/UnTrace.cpp:760) over a grid and
reports where the engine disagrees with the brushes it was given.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ut2bsp import load_bsp, point_region


def load_brushes(t3d_path):
    """Every CSG brush from a .t3d as (name, op, planes, bounds), in CSG order."""
    text = open(t3d_path, encoding="latin-1").read()
    out = []
    for match in re.finditer(r"   Begin Actor Class=Brush Name=(\S+).*?\n   End Actor\n",
                             text, re.S):
        block, name = match.group(0), match.group(1)
        if name == "Brush":
            continue                      # the throwaway builder brush
        op = re.search(r"CsgOper=(\S+)", block)
        op = (op.group(1) if op else "?").replace("CSG_", "")
        match = re.search(r"Location=\(X=(\S+?),Y=(\S+?),Z=(\S+?)\)", block)
        loc = [float(v) for v in match.groups()] if match else [0.0, 0.0, 0.0]
        planes, verts = [], []
        for poly in re.finditer(r"Begin Polygon(.*?)End Polygon", block, re.S):
            w = [tuple(float(c) + loc[i] for i, c in enumerate(v))
                 for v in re.findall(r"Vertex\s+(\S+),(\S+),(\S+)", poly.group(1))]
            if len(w) < 3:
                continue
            nx = ny = nz = 0.0
            for i in range(len(w)):
                a, b = w[i], w[(i + 1) % len(w)]
                nx += (a[1] - b[1]) * (a[2] + b[2])
                ny += (a[2] - b[2]) * (a[0] + b[0])
                nz += (a[0] - b[0]) * (a[1] + b[1])
            length = (nx * nx + ny * ny + nz * nz) ** 0.5
            if length < 1e-9:
                continue
            n = (nx / length, ny / length, nz / length)
            planes.append((n, sum(n[i] * w[0][i] for i in range(3))))
            verts += w
        if planes:
            lo = [min(v[i] for v in verts) for i in range(3)]
            hi = [max(v[i] for v in verts) for i in range(3)]
            out.append((name, op, planes, lo, hi))
    return out


def intended_solid(brushes, p):
    """Replay the CSG: the world starts solid, each brush containing p wins."""
    state = True
    for _name, op, planes, lo, hi in brushes:
        if any(p[i] < lo[i] - 0.1 or p[i] > hi[i] + 0.1 for i in range(3)):
            continue
        if all(sum(n[i] * p[i] for i in range(3)) - d <= 0.1 for n, d in planes):
            state = (op == "Add")
    return state


def main(argv):
    ut2, t3d = argv[1], argv[2]
    box = [float(v) for v in argv[3:9]] if len(argv) >= 9 else None
    step = int(argv[9]) if len(argv) >= 10 else 64
    nodes = load_bsp(ut2)
    brushes = load_brushes(t3d)
    if box is None:
        lo = [min(b[3][i] for b in brushes) for i in range(3)]
        hi = [max(b[4][i] for b in brushes) for i in range(3)]
        box = [lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]]
    print("%d nodes, %d brushes; scanning %s at %duu" % (len(nodes), len(brushes), box, step))
    total = bad = 0
    worst = []
    for x in range(int(box[0]), int(box[1]), step):
        for y in range(int(box[2]), int(box[3]), step):
            for z in range(int(box[4]), int(box[5]), step):
                p = (x, y, z)
                total += 1
                if (point_region(nodes, p)[0] == 0) != intended_solid(brushes, p):
                    bad += 1
                    if len(worst) < 20:
                        worst.append(p)
    print("samples %d, disagreements %d (%.2f%%)" % (total, bad, 100.0 * bad / max(total, 1)))
    for p in worst:
        print("   engine says solid where the CSG says empty: %s" % (p,))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
