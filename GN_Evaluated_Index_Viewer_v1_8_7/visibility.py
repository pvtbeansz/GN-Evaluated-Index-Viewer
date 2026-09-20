# SPDX-FileCopyrightText: 2026 PvtBeans
# SPDX-License-Identifier: GPL-2.0-or-later

"""Instance surface snapshots, without retaining evaluated Blender objects.

Instance origins can be inside their own geometry. Visibility must compare
the front surface with viewport depth, not compare the origin with that depth.
Match full transforms to depsgraph instances; never assume depsgraph iteration
order is the Geometry Nodes Index field. Unmatched/nested references retain
the conservative origin test instead of gaining through-selection.
"""

import numpy as np
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

from .utils import log
from . import project

# Bound extra work independently of the label/position cache. Exceeding a
# budget leaves an origin-only test, never an occlusion bypass.
MAX_SURFACE_INSTANCES = 20000
MAX_SURFACE_TRIANGLES = 1000000


def build_instance_surfaces(depsgraph, owner, transforms, start=0):
    """Map actual GN indices to (local BVH, inverse world transform).

    Only unambiguous, directly matching mesh instances are mapped. In
    particular a source object name or a nearby origin alone is insufficient:
    separate instances of the same object must occlude each other normally.
    """
    count = len(transforms)
    if not count or count > MAX_SURFACE_INSTANCES:
        return {}
    tree = KDTree(count)
    for row, matrix in enumerate(transforms):
        tree.insert(tuple(matrix[:3, 3]), row)
    tree.balance()

    matches = {}
    meshes = {}
    triangles_left = MAX_SURFACE_TRIANGLES
    owner_original = owner.original
    for instance in depsgraph.object_instances:
        if not instance.is_instance:
            continue
        parent = instance.parent
        if parent is None or parent.original != owner_original:
            continue
        obj = instance.object
        if obj is None or obj.type != 'MESH':
            continue
        matrix = np.asarray(instance.matrix_world, dtype=np.float64).copy()
        if not np.isfinite(matrix).all():
            continue
        # Allow float32 depsgraph rounding, including at large world offsets.
        tolerance = max(1e-6, float(np.max(np.abs(matrix[:3, 3]))) * 2e-7)
        candidates = [row for _co, row, _distance in
                      tree.find_range(tuple(matrix[:3, 3]), tolerance)
                      if np.allclose(transforms[row], matrix, rtol=2e-7, atol=1e-6)]
        if len(candidates) != 1:
            continue
        row = candidates[0]
        if row in matches:
            # Several leaves of a nested reference at the same transform do
            # not identify one top-level reference reliably.
            matches[row] = None
            continue
        matches[row] = None
        try:
            inverse = np.linalg.inv(matrix)
        except np.linalg.LinAlgError:
            continue

        # RNA pointers are used only as keys during this evaluation. No RNA
        # object escapes this function; BVHs own copies of all geometry.
        key = obj.as_pointer()
        if key not in meshes:
            mesh = None
            meshes[key] = None
            try:
                mesh = obj.to_mesh()
                if mesh is None:
                    continue
                triangle_count = sum(max(0, len(p.vertices) - 2) for p in mesh.polygons)
                if triangle_count == 0 or triangle_count > triangles_left:
                    continue
                mesh.calc_loop_triangles()
                vertices = [tuple(v.co) for v in mesh.vertices]
                triangles = [tuple(t.vertices) for t in mesh.loop_triangles]
                meshes[key] = BVHTree.FromPolygons(vertices, triangles, all_triangles=True)
                triangles_left -= len(triangles)
            except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
                log.debug("Instance surface unavailable", exc_info=True)
            finally:
                if mesh is not None:
                    obj.to_mesh_clear()
        if meshes[key] is not None:
            matches[row] = (meshes[key], inverse, matrix)

    surfaces = {start + row: surface for row, surface in matches.items() if surface is not None}
    log.debug("Instance surface cache: %d/%d direct mesh references matched", len(surfaces), count)
    return surfaces


def surface_positions(surfaces, indices, positions, sx, sy, region, rv3d):
    """Return front-surface positions at the sampled pixels, plus a hit mask.

    A miss retains the origin, useful for an empty or off-centre instance.
    Occlusion is still tested at that origin; a miss never forces visibility.
    """
    result = np.asarray(positions, dtype=np.float64).copy()
    hits = np.zeros(len(indices), dtype=bool)
    if not surfaces:
        return result, hits
    origins, directions, valid = project.screen_rays(sx, sy, region, rv3d)
    for row, index in enumerate(indices):
        surface = surfaces.get(int(index))
        if surface is None or not valid[row]:
            continue
        bvh, inverse, matrix = surface
        origin = inverse[:3, :3] @ origins[row] + inverse[:3, 3]
        direction = inverse[:3, :3] @ directions[row]
        length = np.linalg.norm(direction)
        if not np.isfinite(length) or length <= 1e-15:
            continue
        location, _normal, _face, _distance = bvh.ray_cast(tuple(origin), tuple(direction / length))
        if location is not None:
            result[row] = matrix[:3, :3] @ np.asarray(location) + matrix[:3, 3]
            hits[row] = True
    return result, hits
