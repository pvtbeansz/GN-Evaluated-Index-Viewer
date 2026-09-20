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

"""Evaluated-geometry cache.

Everything expensive happens here, never inside a draw callback: the
evaluated geometry is read once per relevant depsgraph update into numpy
arrays, and the draw code only projects those arrays to screen space.

The supported Blender 5.2+ path uses ``Object.evaluated_geometry()``, which
exposes every component of a Geometry Nodes result — mesh, point cloud,
curves and instances — rather than just the mesh. A legacy ``to_mesh()``
fallback is retained defensively, but versions below 5.2 are not part of the
official support target for this release.
"""

import bpy
import numpy as np

from . import utils
from . import visibility
from .utils import log

# Ceiling on retained position/normal rows. Bulk RNA extraction can temporarily
# allocate the full domain before slicing; this is not a peak-memory bound.
# gniv_max_labels separately caps the labels drawn.
BUILD_LIMIT = 200000

# Which component each domain lives on.
DOMAIN_COMPONENT = {
    'POINT': 'MESH',
    'EDGE': 'MESH',
    'FACE': 'MESH',
    'CLOUD': 'POINTCLOUD',
    'CURVE_POINT': 'CURVES',
    'CURVE': 'CURVES',
    'INSTANCE': 'INSTANCES',
}

COMPONENT_LABELS = {
    'MESH': "mesh",
    'POINTCLOUD': "point cloud",
    'CURVES': "curves",
    'INSTANCES': "instances",
}

_timer_pending = False

cache = {
    "object": None,
    "domain": None,
    "positions": None,    # (n, 3) float64, world space
    "normals": None,      # (n, 3) float64, world space, or None
    "indices": None,      # (n,) int64 — real element indices
    "start": 0,           # index of the first cached element
    "total": 0,           # element count in the evaluated geometry
    "truncated": False,
    "dirty": True,
    "status": "",
    "components": {},     # {'MESH': 812, 'INSTANCES': 40, ...}
    "legacy_api": False,  # True when running on the to_mesh() fallback
    "instance_surfaces": {},  # GN index -> copied local BVH and world matrices
    "revision": 0,
}


def max_index():
    return max(cache["total"] - 1, 0)


def count():
    positions = cache["positions"]
    return 0 if positions is None else len(positions)


# Short forms for the sidebar, where Blender truncates rather than wraps.
COMPONENT_SHORT = {
    'MESH': "mesh",
    'POINTCLOUD': "cloud",
    'CURVES': "curves",
    'INSTANCES': "inst",
}


def available_components():
    """Compact list of components the evaluated geometry actually has."""
    names = [COMPONENT_SHORT.get(key, key.lower()) for key in cache["components"]]
    return " · ".join(sorted(names))


def reset():
    cache["revision"] += 1
    cache["object"] = None
    cache["domain"] = None
    cache["positions"] = None
    cache["normals"] = None
    cache["indices"] = None
    cache["start"] = 0
    cache["total"] = 0
    cache["truncated"] = False
    cache["dirty"] = True
    cache["status"] = ""
    cache["components"] = {}
    cache["legacy_api"] = False
    cache["instance_surfaces"] = {}


# ---------------------------------------------------------------------------
# Row selection
# ---------------------------------------------------------------------------


def rows_for_range(lo, hi):
    """Cached rows covering indices ``lo..hi`` as a zero-copy slice.

    ``indices`` is always a contiguous arange starting at ``start``, so an
    index range maps directly onto a slice — no boolean mask, no copy.
    """
    n = count()
    if n == 0:
        return slice(0, 0)
    start = cache["start"]
    first = max(int(lo) - start, 0)
    last = min(int(hi) - start + 1, n)
    if last <= first:
        return slice(0, 0)
    return slice(first, last)


def rows_for_values(values):
    """Cached rows for an arbitrary set of element indices."""
    n = count()
    if n == 0 or not values:
        return np.empty(0, dtype=np.int64)
    start = cache["start"]
    rows = np.fromiter((int(v) - start for v in values), dtype=np.int64, count=len(values))
    rows = rows[(rows >= 0) & (rows < n)]
    rows.sort()
    return rows


# ---------------------------------------------------------------------------
# Component discovery
# ---------------------------------------------------------------------------


def _non_empty(data, attr):
    """A component only counts if it actually holds elements."""
    if data is None:
        return None
    try:
        if len(getattr(data, attr)) == 0:
            return None
    except (AttributeError, ReferenceError, TypeError):
        return None
    return data


def _collect_components(obj_eval):
    """Return ``(components, owned_mesh, geometry_owner)``.

    ``owned_mesh`` is non-None only on the legacy path, where the caller must
    call ``to_mesh_clear()``. ``geometry_owner`` is the GeometrySet returned
    by ``evaluated_geometry()`` on Blender 4.5+.  It *must* stay alive for as
    long as any component datablock from that GeometrySet is accessed; letting
    it fall out of scope turns those component StructRNA wrappers into dangling
    references.
    """
    getter = getattr(obj_eval, "evaluated_geometry", None)

    if getter is None:
        cache["legacy_api"] = True
        try:
            mesh = obj_eval.to_mesh()
        except (RuntimeError, ReferenceError):
            log.debug("to_mesh failed", exc_info=True)
            return {}, None, None
        components = {}
        if _non_empty(mesh, "vertices") is not None:
            components['MESH'] = mesh
        return components, mesh, None

    cache["legacy_api"] = False
    try:
        geometry = getter()
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        log.warning("evaluated_geometry() failed", exc_info=True)
        return {}, None, None

    components = {}
    try:
        mesh = _non_empty(getattr(geometry, "mesh", None), "vertices")
        if mesh is not None:
            components['MESH'] = mesh

        cloud = _non_empty(getattr(geometry, "pointcloud", None), "points")
        if cloud is not None:
            components['POINTCLOUD'] = cloud

        curves = _non_empty(getattr(geometry, "curves", None), "points")
        if curves is not None:
            components['CURVES'] = curves
    except ReferenceError:
        # If this happens, Blender invalidated the GeometrySet earlier than
        # expected.  Treat it as an unavailable evaluated result rather than
        # letting a dangling RNA pointer escape into the rest of the add-on.
        log.warning("GeometrySet component became invalid during discovery", exc_info=True)
        return {}, None, geometry

    try:
        instances = geometry.instances_pointcloud()
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        log.debug("instances_pointcloud() unavailable", exc_info=True)
        instances = None
    instances = _non_empty(instances, "points")
    if instances is not None:
        components['INSTANCES'] = instances

    # Keep the GeometrySet itself alive in the caller while its component
    # datablocks are counted and copied into NumPy arrays.  Returning only the
    # components is not enough: Blender invalidates them when the owner dies.
    return components, None, geometry


def _component_counts(components):
    counts = {}
    for key, data in components.items():
        try:
            if key == 'MESH':
                counts[key] = len(data.vertices)
            else:
                counts[key] = len(data.points)
        except (AttributeError, ReferenceError, TypeError):
            counts[key] = 0
    return counts


# ---------------------------------------------------------------------------
# Position extraction
# ---------------------------------------------------------------------------


def _attribute_positions(data):
    """Read the ``position`` attribute. Works for point clouds and curves."""
    try:
        attribute = data.attributes.get("position")
    except (AttributeError, ReferenceError, TypeError):
        log.debug("No attribute collection on %r", data, exc_info=True)
        return None
    if attribute is None:
        log.debug("No position attribute on %r", data)
        return None

    try:
        n = len(attribute.data)
        buf = np.empty(n * 3, dtype=np.float32)
        attribute.data.foreach_get("vector", buf)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        log.debug("Could not read position attribute", exc_info=True)
        return None

    buf.shape = (n, 3)
    return buf


def _vertex_arrays(mesh):
    n = len(mesh.vertices)
    co = np.empty(n * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    co.shape = (n, 3)

    try:
        no = np.empty(n * 3, dtype=np.float32)
        mesh.vertices.foreach_get("normal", no)
        no.shape = (n, 3)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        log.debug("Vertex normals unavailable; backface culling disabled", exc_info=True)
        no = None

    return co, no


def _edge_arrays(mesh, vco, vno):
    n = len(mesh.edges)
    if n == 0:
        return np.empty((0, 3), dtype=np.float32), None

    ev = np.empty(n * 2, dtype=np.int32)
    mesh.edges.foreach_get("vertices", ev)
    ev.shape = (n, 2)

    centers = (vco[ev[:, 0]] + vco[ev[:, 1]]) * 0.5
    normals = None
    if vno is not None:
        normals = vno[ev[:, 0]] + vno[ev[:, 1]]
    return centers.astype(np.float32), normals


def _face_arrays(mesh, vco, vno):
    n = len(mesh.polygons)
    if n == 0:
        return np.empty((0, 3), dtype=np.float32), None

    try:
        centers = np.empty(n * 3, dtype=np.float32)
        mesh.polygons.foreach_get("center", centers)
        centers.shape = (n, 3)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        log.debug("polygons.center unavailable; averaging loop vertices", exc_info=True)
        loop_start = np.empty(n, dtype=np.int32)
        loop_total = np.empty(n, dtype=np.int32)
        mesh.polygons.foreach_get("loop_start", loop_start)
        mesh.polygons.foreach_get("loop_total", loop_total)
        nl = len(mesh.loops)
        lv = np.empty(nl, dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", lv)
        sums = np.add.reduceat(vco[lv], loop_start, axis=0)
        centers = (sums / loop_total[:, None]).astype(np.float32)

    try:
        normals = np.empty(n * 3, dtype=np.float32)
        mesh.polygons.foreach_get("normal", normals)
        normals.shape = (n, 3)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        log.debug("Face normals unavailable; backface culling disabled", exc_info=True)
        normals = None

    return centers, normals


def _curve_offsets(curves):
    """Offsets into the point array, one per curve plus a trailing total."""
    n = len(curves.curves)
    if n == 0:
        return None

    try:
        offsets = np.empty(n + 1, dtype=np.int32)
        curves.curve_offset_data.foreach_get("value", offsets)
        return offsets
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        log.debug("curve_offset_data unavailable; walking curve slices", exc_info=True)

    try:
        offsets = np.zeros(n + 1, dtype=np.int64)
        running = 0
        for i, curve in enumerate(curves.curves):
            offsets[i] = running
            running += len(curve.points)
        offsets[n] = running
        return offsets
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        log.warning("Could not determine curve offsets", exc_info=True)
        return None


def _curve_center_arrays(curves, point_positions):
    """One position per curve: the mean of its control points."""
    offsets = _curve_offsets(curves)
    if offsets is None or point_positions is None:
        return None

    starts = offsets[:-1].astype(np.int64)
    lengths = (offsets[1:] - offsets[:-1]).astype(np.float64)
    if len(starts) == 0:
        return np.empty((0, 3), dtype=np.float32)

    # Empty curves would break reduceat and produce a divide-by-zero.
    keep = lengths > 0
    if not keep.all():
        log.debug("Skipping %d empty curves", int((~keep).sum()))

    centers = np.zeros((len(starts), 3), dtype=np.float64)
    if keep.any():
        sums = np.add.reduceat(point_positions.astype(np.float64), starts[keep], axis=0)
        centers[keep] = sums / lengths[keep][:, None]
    return centers.astype(np.float32)


def _instance_transforms(instances):
    """Read affine instance matrices, normalising Blender's bulk layout."""
    try:
        attribute = instances.attributes.get("instance_transform")
    except (AttributeError, ReferenceError, TypeError):
        attribute = None
    if attribute is None:
        log.debug("No instance_transform attribute; falling back to position")
        return None

    n = len(attribute.data)
    buf = np.empty(n * 16, dtype=np.float32)
    for field in ("value", "matrix"):
        try:
            attribute.data.foreach_get(field, buf)
            break
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            log.debug("instance_transform has no %r field", field)
    else:
        log.warning("Could not read instance_transform", exc_info=True)
        return None

    matrices = buf.reshape(n, 4, 4)

    transposed = matrices.transpose(0, 2, 1)
    different = np.flatnonzero(np.any(matrices != transposed, axis=(1, 2)))
    if len(different):
        # A pure rotation at the origin also needs correct layout detection;
        # looking only for nonzero translation cannot distinguish it.
        row = int(different[0])
        try:
            direct = np.asarray(getattr(attribute.data[row], field), dtype=np.float32)
            if direct.shape == (4, 4):
                if np.allclose(direct, matrices[row]):
                    return matrices.copy()
                if np.allclose(direct, transposed[row]):
                    return transposed.copy()
        except (AttributeError, ReferenceError, TypeError, ValueError):
            pass
    if np.any(np.abs(matrices[:, 3, :3]) > 1e-6):
        return transposed.copy()
    # Symmetric matrices are unchanged by transposition. Translation in the
    # final column identifies row-major affine matrices on fallback builds.
    if np.any(np.abs(matrices[:, :3, 3]) > 1e-6):
        return matrices.copy()
    return transposed.copy()


def _instance_positions(instances):
    matrices = _instance_transforms(instances)
    return _attribute_positions(instances) if matrices is None else matrices[:, :3, 3]


def _domain_arrays(components, domain):
    """``(positions, normals, unavailable_reason)`` for one domain."""
    component = DOMAIN_COMPONENT.get(domain, 'MESH')
    data = components.get(component)

    if data is None:
        label = COMPONENT_LABELS.get(component, component.lower())
        if cache["legacy_api"] and component != 'MESH':
            return None, None, (
                "This Blender build does not expose evaluated %s components." % label)
        return None, None, "Evaluated geometry has no %s component." % label

    if component == 'MESH':
        vco, vno = _vertex_arrays(data)
        if domain == 'POINT':
            return vco, vno, None
        if domain == 'EDGE':
            local, normals = _edge_arrays(data, vco, vno)
            return local, normals, None
        local, normals = _face_arrays(data, vco, vno)
        return local, normals, None

    if component == 'POINTCLOUD':
        return _attribute_positions(data), None, None

    if component == 'CURVES':
        points = _attribute_positions(data)
        if domain == 'CURVE_POINT':
            return points, None, None
        return _curve_center_arrays(data, points), None, None

    if component == 'INSTANCES':
        return _instance_positions(data), None, None

    return None, None, "Unknown geometry component."


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------


def _slice_start(scene, total):
    """Where to begin caching when the geometry is larger than BUILD_LIMIT."""
    if total <= BUILD_LIMIT:
        return 0
    if not getattr(scene, "gniv_use_range", False):
        return 0
    lo = min(scene.gniv_range_min, scene.gniv_range_max)
    return int(min(max(lo, 0), total - BUILD_LIMIT))


def rebuild(scene, depsgraph, obj=None):
    reset()
    cache["dirty"] = False

    if obj is None:
        obj = getattr(bpy.context, "active_object", None)
    if obj is None:
        cache["status"] = "No active object."
        return

    cache["object"] = obj.name
    cache["domain"] = scene.gniv_domain

    try:
        obj_eval = obj.evaluated_get(depsgraph)
    except (ReferenceError, RuntimeError) as exc:
        log.debug("evaluated_get failed for %r: %s", obj.name, exc)
        cache["status"] = "Object is not in the current depsgraph."
        return

    components, owned_mesh, geometry_owner = _collect_components(obj_eval)
    # Keep ``geometry_owner`` referenced until after every component has been
    # copied.  See _collect_components(): its child datablocks are invalid once
    # the GeometrySet is garbage-collected.
    cache["components"] = _component_counts(components)

    if not components:
        cache["status"] = "Evaluated geometry is empty."
        if owned_mesh is not None:
            _release(obj_eval)
        return

    try:
        local, normals, reason = _domain_arrays(components, scene.gniv_domain)
        if reason is not None:
            cache["status"] = reason
            return
        if local is None:
            cache["status"] = "Could not read positions for this domain."
            return

        total = len(local)
        cache["total"] = total

        start = _slice_start(scene, total)
        if total > BUILD_LIMIT:
            local = local[start:start + BUILD_LIMIT]
            if normals is not None:
                normals = normals[start:start + BUILD_LIMIT]
            cache["truncated"] = True
        cache["start"] = start

        matrix = np.asarray(obj_eval.matrix_world, dtype=np.float64)
        world = local.astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3]

        if normals is not None:
            try:
                normal_matrix = np.asarray(
                    obj_eval.matrix_world.to_3x3().inverted_safe().transposed(),
                    dtype=np.float64,
                )
                normals = normals.astype(np.float64) @ normal_matrix.T
                lengths = np.linalg.norm(normals, axis=1, keepdims=True)
                lengths[lengths == 0.0] = 1.0
                normals = normals / lengths
            except (ValueError, np.linalg.LinAlgError) as exc:
                log.debug("Could not transform normals: %s", exc)
                normals = None

        cache["positions"] = np.ascontiguousarray(world, dtype=np.float64)
        cache["normals"] = None if normals is None else np.ascontiguousarray(normals, dtype=np.float64)
        cache["indices"] = np.arange(start, start + len(world), dtype=np.int64)

        if scene.gniv_domain == 'INSTANCE':
            transforms = _instance_transforms(components['INSTANCES'])
            if transforms is not None:
                world_transforms = matrix @ transforms[start:start + len(world)].astype(np.float64)
                try:
                    cache["instance_surfaces"] = visibility.build_instance_surfaces(
                        depsgraph, obj, world_transforms, start)
                except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
                    log.warning("Instance surface cache unavailable; using origin depth", exc_info=True)

        if total == 0:
            cache["status"] = "Evaluated geometry is empty for this domain."

        log.debug("Rebuilt cache: %s / %s, %d elements, components=%s",
                  obj.name, scene.gniv_domain, total, cache["components"])

    except (AttributeError, IndexError, MemoryError, ReferenceError, RuntimeError, TypeError, ValueError) as exc:
        log.warning("Could not read evaluated geometry for %r", obj.name, exc_info=True)
        reset()
        cache["dirty"] = False
        cache["status"] = "Could not read evaluated geometry: %s" % exc
    finally:
        if owned_mesh is not None:
            _release(obj_eval)
        # Explicit assignment documents the lifetime boundary and prevents an
        # over-eager refactor from dropping the owner before extraction ends.
        geometry_owner = None


def _release(obj_eval):
    """Only the legacy to_mesh() path owns its mesh and has to free it."""
    try:
        obj_eval.to_mesh_clear()
    except (ReferenceError, RuntimeError):
        log.debug("to_mesh_clear failed", exc_info=True)


# ---------------------------------------------------------------------------
# Deferred rebuild
# ---------------------------------------------------------------------------


def _rebuild_timer():
    global _timer_pending
    _timer_pending = False

    log.debug("Deferred rebuild timer fired")
    context = bpy.context
    scene = getattr(context, "scene", None)
    if scene is None or not getattr(scene, "gniv_show_indices", False):
        return None

    try:
        depsgraph = context.evaluated_depsgraph_get()
    except (AttributeError, RuntimeError):
        log.debug("No depsgraph available for deferred rebuild", exc_info=True)
        return None

    rebuild(scene, depsgraph)
    utils.tag_redraw_all_view3d()
    return None


def request_rebuild():
    """Mark the cache dirty and rebuild on the next timer tick.

    This defers work out of depsgraph handlers and draw callbacks (where
    evaluating geometry is unsafe) and coalesces bursts of updates. It still
    runs on Blender's main thread.
    """
    global _timer_pending
    cache["dirty"] = True
    if _timer_pending:
        log.debug("Rebuild request coalesced; timer already pending")
        return

    _timer_pending = True
    log.debug("Rebuild requested; registering deferred timer")
    try:
        bpy.app.timers.register(_rebuild_timer, first_interval=0.0)
    except (RuntimeError, ValueError):
        log.debug("Could not register rebuild timer", exc_info=True)
        _timer_pending = False


def cancel_pending():
    global _timer_pending
    _timer_pending = False
    try:
        if bpy.app.timers.is_registered(_rebuild_timer):
            bpy.app.timers.unregister(_rebuild_timer)
    except (RuntimeError, ValueError):
        log.debug("Could not unregister rebuild timer", exc_info=True)
