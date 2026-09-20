# SPDX-FileCopyrightText: 2026 PvtBeans
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# GN Evaluated Index Viewer is free software. You may redistribute it
# and/or modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation, either version 2 of the
# License, or (at your option) any later version. See LICENSE.txt.
#
# Always available free of charge from https://www.youtube.com/@Pvtbeansz

"""Shared viewport projection helpers.

``draw`` and ``picker`` both need to turn cached world-space positions into
region pixel coordinates and window-space depths. Keeping one implementation
here means the two agree about what "visible" means.
"""

import gpu
import numpy as np
from mathutils import Vector

from .utils import log

W_EPSILON = 1e-6
DEPTH_TOLERANCE = 2.0 / (2**24 - 1)


def perspective_matrix(rv3d):
    return np.asarray(rv3d.perspective_matrix, dtype=np.float64)


def clip_coords(positions, persp):
    """Multiply (n, 3) world positions by a 4x4 matrix without allocating a
    homogeneous copy of the array."""
    return positions @ persp[:, :3].T + persp[:, 3]


def project(positions, region, rv3d, persp=None):
    """Return ``(sx, sy, ndc_z, valid)`` in region pixel coordinates."""
    if persp is None:
        persp = perspective_matrix(rv3d)

    clip = clip_coords(positions, persp)
    w = clip[:, 3]
    valid = (w > W_EPSILON) & np.isfinite(clip).all(axis=1)

    ndc_z = np.full(len(positions), 1.0, dtype=np.float64)
    sx = np.full(len(positions), -1.0e9, dtype=np.float64)
    sy = np.full(len(positions), -1.0e9, dtype=np.float64)

    if valid.any():
        inv_w = 1.0 / w[valid]
        sx[valid] = (clip[valid, 0] * inv_w * 0.5 + 0.5) * region.width
        sy[valid] = (clip[valid, 1] * inv_w * 0.5 + 0.5) * region.height
        ndc_z[valid] = clip[valid, 2] * inv_w

    valid &= (ndc_z >= -1.0) & (ndc_z <= 1.0)
    return sx, sy, ndc_z, valid


def view_vectors(positions, rv3d):
    """Per-element view direction (not normalised), pointing away from the eye."""
    if rv3d.is_perspective:
        origin = np.asarray(rv3d.view_matrix.inverted().translation, dtype=np.float64)
        return positions - origin

    direction = rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))
    return np.tile(np.asarray(direction, dtype=np.float64), (len(positions), 1))


def window_depths(positions, region, rv3d, persp=None, view_vecs=None):
    """Window-space depth (0..1), without moving geometry toward the camera.

    A percentage-of-view-distance bias can pull back-side points through thin
    objects. Use a small depth-buffer tolerance at comparison time instead.
    """
    if persp is None:
        persp = perspective_matrix(rv3d)
    clip = clip_coords(positions, persp)
    w = clip[:, 3]
    depth = np.ones(len(positions), dtype=np.float64)
    safe = w > W_EPSILON
    if safe.any():
        depth[safe] = (clip[safe, 2] / w[safe]) * 0.5 + 0.5
    return depth


def screen_rays(sx, sy, region, rv3d):
    """World rays through the centres of the pixels sampled by sample_depth.

    Unproject the near plane and an intermediate plane. This works for both
    orthographic and perspective views without a far-clip-sized ray origin.
    """
    n = len(sx)
    empty = np.zeros((n, 3), dtype=np.float64)
    try:
        inverse = np.linalg.inv(perspective_matrix(rv3d))
    except np.linalg.LinAlgError:
        return empty, empty.copy(), np.zeros(n, dtype=bool)
    clip = np.column_stack((
        (np.floor(sx) + 0.5) * 2.0 / region.width - 1.0,
        (np.floor(sy) + 0.5) * 2.0 / region.height - 1.0,
        np.full(n, -1.0), np.ones(n),
    ))
    near = clip @ inverse.T
    clip[:, 2] = 0.0
    middle = clip @ inverse.T
    valid = ((np.abs(near[:, 3]) > 1e-15) & (np.abs(middle[:, 3]) > 1e-15)
             & np.isfinite(near).all(axis=1) & np.isfinite(middle).all(axis=1))
    origins, directions = empty.copy(), empty.copy()
    origins[valid] = near[valid, :3] / near[valid, 3, None]
    directions[valid] = middle[valid, :3] / middle[valid, 3, None] - origins[valid]
    lengths = np.linalg.norm(directions, axis=1)
    valid &= lengths > 1e-15
    directions[valid] /= lengths[valid, None]
    return origins, directions, valid


def rect_from_points(sx, sy, region, margin=8.0):
    """Clamped integer bounding box (x0, y0, w, h) around screen points."""
    if len(sx) == 0:
        return None

    x0 = int(max(0, np.floor(np.min(sx) - margin)))
    y0 = int(max(0, np.floor(np.min(sy) - margin)))
    x1 = int(min(region.width - 1, np.ceil(np.max(sx) + margin)))
    y1 = int(min(region.height - 1, np.ceil(np.max(sy) + margin)))

    width = x1 - x0 + 1
    height = y1 - y0 + 1
    if width <= 0 or height <= 0:
        return None
    return x0, y0, width, height


def read_depth_rect(x0, y0, width, height):
    """Read a rectangle of the active framebuffer's depth buffer.

    Only valid from inside a draw callback. Returns an ``(height, width)``
    float32 array, or ``None`` when the driver refuses the read.
    """
    try:
        # read_depth uses framebuffer coordinates, not region coordinates.
        vx, vy, _vw, _vh = gpu.state.viewport_get()
        buf = gpu.state.active_framebuffer_get().read_depth(vx + x0, vy + y0, width, height)
    except (RuntimeError, SystemError, ValueError):
        log.debug("Depth readback unavailable on this driver", exc_info=True)
        return None

    try:
        depth = np.asarray(buf, dtype=np.float32)
    except (TypeError, ValueError):
        try:
            depth = np.array(buf.to_list(), dtype=np.float32)
        except (AttributeError, TypeError, ValueError):
            log.debug("Could not convert depth buffer to an array", exc_info=True)
            return None

    try:
        return depth.reshape(height, width)
    except ValueError:
        log.debug("Unexpected depth buffer shape %r", depth.shape)
        return None


def sample_depth(depth, x0, y0, sx, sy):
    """Nearest-pixel depth samples; NaN where the point falls outside the rect."""
    height, width = depth.shape
    px = np.floor(sx).astype(np.int64) - x0
    py = np.floor(sy).astype(np.int64) - y0

    inside = (px >= 0) & (px < width) & (py >= 0) & (py < height)
    sampled = np.full(len(sx), np.nan, dtype=np.float64)
    if inside.any():
        sampled[inside] = depth[py[inside], px[inside]]
    return sampled


def visible_against_depth(depth, x0, y0, sx, sy, win_z, tolerance=DEPTH_TOLERANCE):
    """True where nothing opaque is drawn in front of the element.

    Missing samples are not evidence of visibility. The picker requests a
    fresh snapshot when coverage is incomplete rather than picking through.
    """
    sampled = sample_depth(depth, x0, y0, sx, sy)
    unknown = ~np.isfinite(sampled)
    background = np.where(unknown, False, sampled >= 1.0)
    in_front = np.where(unknown, False, win_z <= sampled + tolerance)
    return (~unknown) & (background | in_front)
