"""Rotator and rotation-matrix arithmetic shared by the converters.

UE stores an orientation as three 16-bit angles -- pitch about Y, yaw about Z,
roll about X, 65536 units to the turn -- and builds a matrix from them in that
order. Vectors are rows, so a point is transformed as `v * M` and a chain of
rotations composes left to right: the first one applied is the leftmost factor.
"""

import math

# Rotator units in a full turn, and the quarter turn that marks an axis swap.
TURN = 65536
QUARTER = TURN // 4


def rotation_matrix(rotator):
    """UE's FRotationMatrix rows, from a (pitch, yaw, roll) in rotator units."""
    pitch, yaw, roll = (float(c) * math.tau / TURN for c in rotator)
    sp, cp = math.sin(pitch), math.cos(pitch)
    sy, cy = math.sin(yaw), math.cos(yaw)
    sr, cr = math.sin(roll), math.cos(roll)
    return (
        (cp * cy, cp * sy, sp),
        (sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp),
        (-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp),
    )


def rotate(v, matrix):
    return tuple(sum(v[axis] * matrix[axis][i] for axis in range(3)) for i in range(3))


def multiply(a, b):
    """The rotation that applies `a` and then `b`."""
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
                 for i in range(3))


def to_rotator(matrix):
    """The (pitch, yaw, roll) that rotation_matrix would build from this matrix.

    Straight inversion of the construction above, except at the poles. With the
    pitch at a quarter turn the matrix depends only on roll minus yaw, so the
    two are not separately recoverable; roll is handed to yaw, which describes
    the same orientation and is what UnrealEd itself shows.
    """
    sp = max(-1.0, min(1.0, matrix[0][2]))
    pitch = math.asin(sp)
    if abs(sp) > 1.0 - 1e-9:
        yaw = math.atan2(-matrix[1][0], matrix[1][1])
        roll = 0.0
    else:
        yaw = math.atan2(matrix[0][1], matrix[0][0])
        roll = math.atan2(-matrix[1][2], matrix[2][2])
    return tuple(int(round(a * TURN / math.tau)) % TURN for a in (pitch, yaw, roll))


def axis_images(matrix, tolerance=1e-6):
    """For each local axis, the world axis it lands on -- or None if it is not
    a rotation by whole quarter turns.

    Vectors being rows, row `i` of the matrix is the image of local axis `i`, so
    an axis-aligned rotation has exactly one unit entry per row. Only such a
    rotation carries a non-uniform scale through it exactly: `R S R^T` stays
    diagonal, its entries merely permuted.
    """
    images = []
    for local in range(3):
        found = [world for world in range(3)
                 if abs(abs(matrix[local][world]) - 1.0) <= tolerance]
        if len(found) != 1:
            return None
        images.append(found[0])
    if sorted(images) != [0, 1, 2]:
        return None
    return images
