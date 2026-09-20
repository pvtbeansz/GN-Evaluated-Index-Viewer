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

"""Interactive evaluated-element picker and Geometry Nodes insertion helpers."""

import math

import bpy
import gpu
import numpy as np
from bpy.types import Operator
from gpu_extras.batch import batch_for_shader

from . import cache as cache_mod
from . import project
from . import visibility
from . import utils
from .utils import domain_label, log

PICK_RADIUS = 20.0
DEFAULT_CIRCLE_RADIUS = 25.0

class VisibilityUnavailable(RuntimeError):
    """A visible-only gesture cannot yet be evaluated safely."""

# Box edges are grown by this many pixels before testing. Labels are drawn
# centred on their element, so a box edge that visually cuts through a number
# should still catch it; this is deliberately smaller than half a glyph so it
# never grabs an element that is clearly outside.
BOX_TOLERANCE = 4.0

# Refuse absurd depth reads (roughly a 4K viewport's worth of pixels).
MAX_DEPTH_PIXELS = 16_000_000

# Live picker state. The painted selection lives here, not in the scene
# StringProperty, so that a mouse-move does not rewrite (and force a re-parse
# of) a selection that may contain tens of thousands of indices.
_live = {
    "active": False,
    "values": set(),
    "array": np.empty(0, dtype=np.int64),
    "array_revision": -1,
    "revision": 0,
    "hover": -1,
    "mouse": (0.0, 0.0),
    "region": 0,
}

# Temporary viewport gesture state. Runtime-only; never saved to the .blend.
_gesture = {
    "active": False,
    "kind": 'POINT',
    "start": (0.0, 0.0),
    "current": (0.0, 0.0),
    "points": [],
    "radius": DEFAULT_CIRCLE_RADIUS,
    "region": 0,
}

# Depth snapshot taken by the draw callback for the picker to sample.
_depth = {
    "region": 0,
    "rect": None,
    "buf": None,
    "persp": None,
    "revision": -1,
    "size": None,
}

# Set by unregister() so a still-running modal bows out instead of touching
# classes that Blender has already removed.
_shutdown = False


# ---------------------------------------------------------------------------
# Live state accessors (read by draw.py)
# ---------------------------------------------------------------------------


def is_active():
    return _live["active"]


def live_values():
    """The live painted selection. Treat as read-only."""
    return _live["values"]


def live_array():
    """The live selection as a sorted int64 array, rebuilt only when it changes."""
    if _live["array_revision"] != _live["revision"]:
        values = _live["values"]
        array = np.fromiter(values, dtype=np.int64, count=len(values))
        array.sort()
        _live["array"] = array
        _live["array_revision"] = _live["revision"]
    return _live["array"]


def live_hover():
    return _live["hover"] if _live["active"] else -1


def _touch_live():
    _live["revision"] += 1


# ---------------------------------------------------------------------------
# Stored selection
# ---------------------------------------------------------------------------


def parse_selection(scene):
    """The stored evaluated-element selection as a sorted list of ints."""
    return utils.parse_index_string(getattr(scene, "gniv_selected_indices", ""))


def store_selection(scene, values, domain=None, obj_name=None, active=None):
    values = sorted({int(v) for v in values if int(v) >= 0})
    scene.gniv_selected_indices = utils.format_index_string(values)
    if domain is not None:
        scene.gniv_selected_domain = domain
    if obj_name is not None:
        scene.gniv_selected_object = obj_name
    if active is None:
        active = values[-1] if values else -1
    scene.gniv_selected_index = int(active)
    utils.tag_redraw_all_view3d()


def clear_selection(scene):
    obj = getattr(bpy.context, "active_object", None)
    store_selection(
        scene, [],
        domain=scene.gniv_domain,
        obj_name=obj.name if obj is not None else "",
        active=-1,
    )


def selection_matches_context(scene, obj):
    """The stored selection is only meaningful on its own object and domain."""
    if obj is None:
        return False
    if getattr(scene, "gniv_selected_domain", scene.gniv_domain) != scene.gniv_domain:
        return False
    stored_object = getattr(scene, "gniv_selected_object", "")
    return not stored_object or stored_object == obj.name


# ---------------------------------------------------------------------------
# Projection of cached elements
# ---------------------------------------------------------------------------


def _candidate_slice(scene):
    """Picking is limited to the indices the viewer is actually showing.

    The index range filter applies; "Only Picked" does not, or it would be
    impossible to add to an existing selection.
    """
    n = cache_mod.count()
    if n == 0:
        return slice(0, 0)
    if getattr(scene, "gniv_use_range", False):
        lo = min(scene.gniv_range_min, scene.gniv_range_max)
        hi = max(scene.gniv_range_min, scene.gniv_range_max)
        return cache_mod.rows_for_range(lo, hi)
    return slice(0, n)


def _project_cached_elements(context):
    """Project candidate evaluated positions to viewport pixel coordinates."""
    obj = context.active_object
    if (cache_mod.cache["dirty"] or obj is None
            or cache_mod.cache["object"] != obj.name
            or cache_mod.cache["domain"] != context.scene.gniv_domain):
        # Queries run from modal events, never from a draw callback. Refresh
        # here so a click immediately after a node edit cannot use old indices.
        cache_mod.cancel_pending()
        cache_mod.rebuild(context.scene, context.evaluated_depsgraph_get(), obj)
    positions_all = cache_mod.cache.get("positions")
    indices_all = cache_mod.cache.get("indices")
    if positions_all is None or indices_all is None or len(positions_all) == 0:
        return None

    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None:
        return None

    rows = _candidate_slice(context.scene)
    if rows.stop <= rows.start:
        return None

    positions = positions_all[rows]
    indices = indices_all[rows]

    sx, sy, ndc_z, valid = project.project(positions, region, rv3d)
    valid = valid & (sx >= 0.0) & (sx < region.width) & (sy >= 0.0) & (sy < region.height)

    return sx, sy, ndc_z, indices, valid, positions


# ---------------------------------------------------------------------------
# X-Ray / occlusion
# ---------------------------------------------------------------------------


def _xray_enabled(context):
    """Match Blender's viewport selection-through behaviour as closely as possible."""
    space = getattr(context, "space_data", None)
    if space is None or getattr(space, "type", None) != 'VIEW_3D':
        return False

    shading = getattr(space, "shading", None)
    if shading is None:
        return False

    if getattr(shading, "type", 'SOLID') == 'WIREFRAME':
        return bool(getattr(shading, "show_xray_wireframe", True))
    return getattr(shading, "type", 'SOLID') == 'SOLID' and bool(getattr(shading, "show_xray", False))


def _toggle_xray(context):
    """Toggle viewport X-Ray without leaving the modal picker."""
    space = getattr(context, "space_data", None)
    if space is None or getattr(space, "type", None) != 'VIEW_3D':
        return False

    shading = getattr(space, "shading", None)
    if shading is None:
        return False

    if getattr(shading, "type", 'SOLID') == 'WIREFRAME':
        shading.show_xray_wireframe = not bool(getattr(shading, "show_xray_wireframe", False))
    elif getattr(shading, "type", 'SOLID') == 'SOLID':
        shading.show_xray = not bool(getattr(shading, "show_xray", False))
    else:
        return False

    utils.tag_redraw_all_view3d()
    return True


def capture_depth(context, region, rv3d):
    """Snapshot the depth buffer for the picker. Called from the draw callback.

    ``read_depth`` is only legal inside a draw callback, so the picker cannot
    take this itself. Reject snapshots after view, size or geometry changes.
    One full-region snapshot also covers a gesture's next mouse position.
    """
    if not _live["active"] or _live["region"] != region.as_pointer():
        return
    if _xray_enabled(context):
        _depth["buf"] = None
        return

    # Capture the whole region: the mouse may move between redraws and its
    # next circle/box must not fall outside the previous gesture rectangle.
    rect = (0, 0, region.width, region.height)
    if rect is None:
        _depth["buf"] = None
        return

    if rect[2] * rect[3] > MAX_DEPTH_PIXELS:
        log.debug("Depth capture rect too large (%d x %d); skipping", rect[2], rect[3])
        _depth["buf"] = None
        return

    buf = project.read_depth_rect(*rect)
    _depth["buf"] = buf
    _depth["rect"] = rect
    _depth["region"] = region.as_pointer()
    _depth["persp"] = np.asarray(rv3d.perspective_matrix, dtype=np.float64).tobytes()
    _depth["revision"] = cache_mod.cache["revision"]
    _depth["size"] = (region.width, region.height)


def _clear_depth():
    _depth["buf"] = None
    _depth["rect"] = None
    _depth["region"] = 0
    _depth["persp"] = None
    _depth["revision"] = -1
    _depth["size"] = None


def _visible_from_depth(context, rows, sx, sy, positions, indices=None):
    """Vectorised occlusion test against the last depth snapshot.

    Returns a boolean mask over ``rows``, or None when no usable snapshot
    exists.
    """
    buf = _depth["buf"]
    if buf is None or _depth["rect"] is None:
        return None

    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None:
        return None
    if _depth["region"] != region.as_pointer():
        return None
    if (_depth["revision"] != cache_mod.cache["revision"]
            or _depth["size"] != (region.width, region.height)):
        return None

    current = np.asarray(rv3d.perspective_matrix, dtype=np.float64).tobytes()
    if current != _depth["persp"]:
        # The view moved since the snapshot was taken.
        return None

    x0, y0 = _depth["rect"][0], _depth["rect"][1]
    sampled = project.sample_depth(buf, x0, y0, sx[rows], sy[rows])
    if not np.isfinite(sampled).all():
        return None
    targets = positions[rows]
    if context.scene.gniv_domain == 'INSTANCE' and indices is not None:
        targets, _hits = visibility.surface_positions(
            cache_mod.cache["instance_surfaces"], indices[rows], targets,
            sx[rows], sy[rows], region, rv3d)
    win_z = project.window_depths(targets, region, rv3d)
    return project.visible_against_depth(buf, x0, y0, sx[rows], sy[rows], win_z)


def _filter_visible_rows(context, rows, sx, sy, positions, trace_label=None, indices=None):
    """Drop rows hidden behind other geometry, honouring viewport X-Ray.

    With Debug Logging enabled, report the visibility method and survivor
    count. Missing/stale depth aborts a gesture without changing selection.
    """
    before = len(rows)
    if before == 0:
        if trace_label:
            log.debug("Pick trace [%s]: visibility input=0", trace_label)
        return rows

    if _xray_enabled(context):
        if trace_label:
            log.debug(
                "Pick trace [%s]: visibility X-Ray ON -> %d/%d kept (occlusion bypassed)",
                trace_label, before, before,
            )
        return rows

    mask = _visible_from_depth(context, rows, sx, sy, positions, indices)
    if mask is not None:
        filtered = rows[mask]
        if trace_label:
            log.debug(
                "Pick trace [%s]: visibility surface/depth-buffer -> %d/%d kept",
                trace_label, len(filtered), before,
            )
        return filtered

    # Scene.ray_cast is not a substitute for rasterised viewport visibility:
    # instanced geometry and non-mesh occluders need not be represented there.
    # Never silently turn an X-Ray-off gesture into through-selection.
    utils.tag_redraw_all_view3d()
    raise VisibilityUnavailable("Viewport depth is refreshing; try the gesture again")


# ---------------------------------------------------------------------------
# Element queries
# ---------------------------------------------------------------------------


def _resolve(context, projected, rows, score, trace_label=None):
    """Shared tail: filter by occlusion and pick an active element."""
    sx, sy, _ndc_z, indices, _valid, positions = projected
    if trace_label:
        log.debug(
            "Pick trace [%s]: shape candidates=%d; cached=%d",
            trace_label, len(rows), len(indices),
        )
    if len(rows) == 0:
        if trace_label:
            log.debug("Pick trace [%s]: final=0 (nothing inside selection shape)", trace_label)
        return [], -1

    rows = _filter_visible_rows(
        context, rows, sx, sy, positions, trace_label=trace_label, indices=indices)
    if len(rows) == 0:
        if trace_label:
            log.debug("Pick trace [%s]: final=0 (all candidates rejected by visibility)", trace_label)
        return [], -1

    nearest_row = int(rows[int(np.argmin(score(rows)))])
    values = indices[rows].tolist()
    hovered = int(indices[nearest_row])
    if trace_label:
        preview = values[:24]
        suffix = " ..." if len(values) > len(preview) else ""
        log.debug(
            "Pick trace [%s]: final=%d; active=%d; indices=%s%s",
            trace_label, len(values), hovered, preview, suffix,
        )
    return values, hovered


def _elements_in_radius(context, mouse_x, mouse_y, radius, trace_label=None):
    projected = _project_cached_elements(context)
    if projected is None:
        return [], -1

    sx, sy, ndc_z, _indices, valid, _positions = projected
    dx = sx - float(mouse_x)
    dy = sy - float(mouse_y)
    dist2 = dx * dx + dy * dy
    rows = np.nonzero(valid & (dist2 <= float(radius * radius)))[0]

    # Prefer screen-space proximity; depth only breaks near-ties.
    def score(r):
        return dist2[r] + np.maximum(np.nan_to_num(ndc_z[r], nan=1e6) + 1.0, 0.0) * 1e-6

    return _resolve(context, projected, rows, score, trace_label=trace_label)


def _ui_scale(context):
    prefs = getattr(context, "preferences", None)
    if prefs is None:
        return 1.0
    return float(getattr(prefs.system, "ui_scale", 1.0)) or 1.0


def _nearest_element_index(context, mouse_x, mouse_y):
    try:
        _, hovered = _elements_in_radius(context, mouse_x, mouse_y, PICK_RADIUS)
    except VisibilityUnavailable:
        return -1
    return hovered


def _elements_in_box(context, start, end, trace_label=None):
    projected = _project_cached_elements(context)
    if projected is None:
        if trace_label:
            log.debug("Pick trace [%s]: projection unavailable", trace_label)
        return [], -1

    sx, sy, _ndc_z, _indices, valid, _positions = projected
    if trace_label:
        log.debug(
            "Pick trace [%s]: projected=%d valid=%d region=%dx%d start=(%.1f, %.1f) end=(%.1f, %.1f)",
            trace_label, len(valid), int(np.count_nonzero(valid)),
            context.region.width, context.region.height,
            float(start[0]), float(start[1]), float(end[0]), float(end[1]),
        )
    lo_x, hi_x = sorted((float(start[0]), float(end[0])))
    lo_y, hi_y = sorted((float(start[1]), float(end[1])))

    cx = (lo_x + hi_x) * 0.5
    cy = (lo_y + hi_y) * 0.5

    tolerance = BOX_TOLERANCE * _ui_scale(context)
    lo_x -= tolerance
    lo_y -= tolerance
    hi_x += tolerance
    hi_y += tolerance

    rows = np.nonzero(
        valid & (sx >= lo_x) & (sx <= hi_x) & (sy >= lo_y) & (sy <= hi_y)
    )[0]

    if trace_label:
        log.debug(
            "Pick trace [%s]: box bounds=(%.1f..%.1f, %.1f..%.1f) -> %d rows before visibility",
            trace_label, lo_x, hi_x, lo_y, hi_y, len(rows),
        )

    return _resolve(
        context, projected, rows,
        lambda r: (sx[r] - cx) ** 2 + (sy[r] - cy) ** 2,
        trace_label=trace_label,
    )


def _points_inside_polygon(xs, ys, polygon):
    """Vectorised 2D ray-crossing test."""
    inside = np.zeros(len(xs), dtype=bool)
    if len(polygon) < 3:
        return inside

    xj, yj = polygon[-1]
    for xi, yi in polygon:
        xi_f, yi_f = float(xi), float(yi)
        xj_f, yj_f = float(xj), float(yj)
        crosses = (yi_f > ys) != (yj_f > ys)
        if crosses.any():
            denom = yj_f - yi_f
            if abs(denom) < 1e-12:
                denom = 1e-12
            x_intersect = (xj_f - xi_f) * (ys - yi_f) / denom + xi_f
            inside ^= crosses & (xs < x_intersect)
        xj, yj = xi_f, yi_f
    return inside


def _elements_in_lasso(context, polygon, trace_label=None):
    projected = _project_cached_elements(context)
    if projected is None:
        if trace_label:
            log.debug("Pick trace [%s]: projection unavailable", trace_label)
        return [], -1

    sx, sy, _ndc_z, _indices, valid, _positions = projected
    if trace_label:
        log.debug(
            "Pick trace [%s]: projected=%d valid=%d polygon_points=%d",
            trace_label, len(valid), int(np.count_nonzero(valid)), len(polygon),
        )
    rows = np.nonzero(valid & _points_inside_polygon(sx, sy, polygon))[0]

    px, py = polygon[-1]
    if trace_label:
        log.debug(
            "Pick trace [%s]: lasso -> %d rows before visibility",
            trace_label, len(rows),
        )
    return _resolve(
        context, projected, rows,
        lambda r: (sx[r] - px) ** 2 + (sy[r] - py) ** 2,
        trace_label=trace_label,
    )


# ---------------------------------------------------------------------------
# Active Blender selection tool + gesture overlay
# ---------------------------------------------------------------------------


def _active_workspace_tool(context):
    try:
        return context.workspace.tools.from_space_view3d_mode(context.mode, create=False)
    except (AttributeError, RuntimeError, TypeError):
        log.debug("Could not read the active workspace tool", exc_info=True)
        return None


def _active_selection_style(context):
    """BOX / CIRCLE / LASSO / POINT, from the current 3D View select tool."""
    tool = _active_workspace_tool(context)
    idname = (getattr(tool, "idname", "") or "").lower()
    if "select_box" in idname:
        return 'BOX'
    if "select_circle" in idname:
        return 'CIRCLE'
    if "select_lasso" in idname:
        return 'LASSO'
    return 'POINT'


def _active_circle_radius(context):
    tool = _active_workspace_tool(context)
    if tool is not None:
        try:
            props = tool.operator_properties("view3d.select_circle")
            radius = float(getattr(props, "radius", DEFAULT_CIRCLE_RADIUS))
            if radius > 1.0:
                return radius
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
            log.debug("Could not read circle-select radius", exc_info=True)
    return DEFAULT_CIRCLE_RADIUS


def _set_gesture(active=False, kind='POINT', start=None, current=None,
                 points=None, radius=None, region_ptr=None):
    _gesture["active"] = bool(active)
    _gesture["kind"] = kind
    if start is not None:
        _gesture["start"] = (float(start[0]), float(start[1]))
    if current is not None:
        _gesture["current"] = (float(current[0]), float(current[1]))
    if points is not None:
        _gesture["points"] = [(float(x), float(y)) for x, y in points]
    if radius is not None:
        _gesture["radius"] = float(radius)
    if region_ptr is not None:
        _gesture["region"] = region_ptr
    utils.tag_redraw_all_view3d()


def clear_gesture():
    _set_gesture(active=False, kind='POINT', points=[])


def draw_gesture_overlay(region):
    """Box/Lasso/Circle feedback, drawn only in the region the picker runs in."""
    if not _gesture["active"]:
        return
    if _gesture["region"] and _gesture["region"] != region.as_pointer():
        return

    kind = _gesture["kind"]
    start = _gesture["start"]
    current = _gesture["current"]

    if kind == 'BOX':
        x0, y0 = start
        x1, y1 = current
        vertices = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    elif kind == 'LASSO':
        vertices = list(_gesture["points"])
        if len(vertices) > 2:
            vertices.append(vertices[0])
    elif kind == 'CIRCLE':
        cx, cy = current
        radius = max(float(_gesture["radius"]), 2.0)
        steps = 48
        vertices = [
            (cx + math.cos((i / steps) * math.tau) * radius,
             cy + math.sin((i / steps) * math.tau) * radius)
            for i in range(steps + 1)
        ]
    else:
        return

    if len(vertices) < 2:
        return

    try:
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": vertices})
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(1.5)
        shader.bind()
        shader.uniform_float("color", (1.0, 1.0, 1.0, 0.85))
        batch.draw(shader)
    except (RuntimeError, SystemError, TypeError, ValueError):
        log.debug("Gesture overlay draw failed", exc_info=True)
    finally:
        try:
            gpu.state.line_width_set(1.0)
            gpu.state.blend_set('NONE')
        except (RuntimeError, SystemError):
            log.debug("Could not restore GPU state", exc_info=True)


# ---------------------------------------------------------------------------
# Geometry Nodes group generation
# ---------------------------------------------------------------------------


def _active_geometry_nodes_group(obj):
    if obj is None:
        return None

    mod = getattr(obj.modifiers, "active", None)
    if mod is not None and getattr(mod, "type", None) == 'NODES':
        group = getattr(mod, "node_group", None)
        if group is not None:
            return group

    for mod in reversed(obj.modifiers):
        if mod.type == 'NODES' and mod.node_group is not None:
            return mod.node_group
    return None


def _visible_geometry_node_editor(context, base_group):
    """Prefer the Geometry Nodes tree the user is currently looking at."""
    screen = getattr(context, "screen", None)
    if screen is None:
        return None, None

    for area in screen.areas:
        if area.type != 'NODE_EDITOR':
            continue
        space = area.spaces.active
        if getattr(space, "tree_type", None) != 'GeometryNodeTree':
            continue
        edit_tree = getattr(space, "edit_tree", None)
        if edit_tree is None:
            continue

        candidate = (edit_tree, tuple(getattr(space, "cursor_location", (0.0, 0.0))))
        if base_group is not None:
            if edit_tree == base_group or getattr(space, "node_tree", None) == base_group:
                return candidate
    # A pinned editor may show another object's node tree. Never insert the
    # active object's selection into that unrelated tree as a fallback.
    return (None, None)


def _target_geometry_nodes_tree(context, obj):
    base = _active_geometry_nodes_group(obj)
    edit_tree, cursor = _visible_geometry_node_editor(context, base)
    if edit_tree is not None:
        return edit_tree, cursor
    return base, None


def _first_socket(collection, name):
    """The enabled socket of this name, falling back to the first match.

    The Compare node carries an A/B pair per data type and only the ones for
    the active type are enabled.
    """
    fallback = None
    for socket in collection:
        if socket.name == name:
            if fallback is None:
                fallback = socket
            if getattr(socket, "enabled", True) and not getattr(socket, "hide", False):
                return socket
    return fallback


# Layout geometry for the generated group.
COL_INDEX = -1000.0
COL_COMPARE = -700.0
COL_AND = -430.0
COL_OR = -150.0
COL_STEP = 200.0

# Above this many separate ranges the Compare/AND nodes are collapsed so the
# group stays readable instead of sprawling over several screens.
COLLAPSE_ABOVE = 8

# Above this many ranges the selection is fragmented enough to be worth
# mentioning to the user.
FRAGMENTED_ABOVE = 64


def _new_compare(group, operation, value, label, x, y, hide=False):
    node = group.nodes.new("FunctionNodeCompare")
    node.data_type = 'INT'
    node.operation = operation
    node.label = label
    node.location = (x, y)
    node.hide = hide
    b_socket = _first_socket(node.inputs, "B")
    if b_socket is None:
        raise RuntimeError("Could not find Compare B integer input")
    b_socket.default_value = int(value)
    return node


def _link_index(group, index_node, compare):
    a_socket = _first_socket(compare.inputs, "A")
    if a_socket is None:
        raise RuntimeError("Could not find Compare A integer input")
    group.links.new(index_node.outputs[0], a_socket)


def _find_existing_group(domain, index_string):
    """Reuse an identical generated group rather than leaving .001 duplicates."""
    for group in bpy.data.node_groups:
        if not group.get("gniv_generated"):
            continue
        if group.get("gniv_domain") != domain:
            continue
        if group.get("gniv_indices") != index_string:
            continue
        return group
    return None


def _run_socket(group, index_node, start, end, y, hide):
    """One range's boolean output, as ``Index >= start AND Index <= end``."""
    if start == end:
        node = _new_compare(
            group, 'EQUAL', start, "Index = %d" % start, COL_AND, y, hide=hide)
        _link_index(group, index_node, node)
        return node.outputs[0]

    gap = 30.0 if hide else 110.0
    ge_node = _new_compare(
        group, 'GREATER_EQUAL', start, "Index >= %d" % start,
        COL_COMPARE, y + gap * 0.5, hide=hide)
    le_node = _new_compare(
        group, 'LESS_EQUAL', end, "Index <= %d" % end,
        COL_COMPARE, y - gap * 0.5, hide=hide)
    _link_index(group, index_node, ge_node)
    _link_index(group, index_node, le_node)

    and_node = group.nodes.new("FunctionNodeBooleanMath")
    and_node.operation = 'AND'
    and_node.label = "Index %d–%d" % (start, end)
    and_node.location = (COL_AND, y)
    and_node.hide = hide
    group.links.new(ge_node.outputs[0], and_node.inputs[0])
    group.links.new(le_node.outputs[0], and_node.inputs[1])
    return and_node.outputs[0]


def _reduce_or(group, sockets, hide):
    """Combine boolean sockets with a balanced OR tree.

    A linear chain puts every range at a different depth and draws as a long
    diagonal staircase. Pairing them halves the depth at each step, so the
    result is log2(n) deep and lays out as a readable binary tree.
    """
    level = list(sockets)
    x = COL_OR

    while len(level) > 1:
        nxt = []
        for i in range(0, len(level) - 1, 2):
            socket_a, y_a = level[i]
            socket_b, y_b = level[i + 1]
            y = (y_a + y_b) * 0.5

            node = group.nodes.new("FunctionNodeBooleanMath")
            node.operation = 'OR'
            node.location = (x, y)
            node.hide = hide
            group.links.new(socket_a, node.inputs[0])
            group.links.new(socket_b, node.inputs[1])
            nxt.append((node.outputs[0], y))

        if len(level) % 2:
            # Odd one out rides up to the next level untouched.
            nxt.append(level[-1])

        level = nxt
        x += COL_STEP

    return level[0][0], level[0][1], x


def _build_selection_group(values, domain):
    """One reusable Geometry Nodes field group with a Boolean output."""
    name = domain_label(domain)
    index_string = utils.format_index_string(values)
    runs = utils.compress_ranges(values)

    existing = _find_existing_group(domain, index_string)
    if existing is not None:
        log.debug("Reusing generated node group %r", existing.name)
        return existing, len(runs), True

    subgroup = bpy.data.node_groups.new(
        name="GNIV Picked %s Selection" % name,
        type="GeometryNodeTree",
    )
    try:
        subgroup["gniv_generated"] = True
        subgroup["gniv_domain"] = domain
        subgroup["gniv_indices"] = index_string

        interface_socket = subgroup.interface.new_socket(
            name="Selection",
            in_out='OUTPUT',
            socket_type='NodeSocketBool',
        )
        try:
            interface_socket.description = (
                "True for the evaluated %s indices picked with GN Index Viewer" % name.lower()
            )
        except (AttributeError, TypeError):
            log.debug("Could not set the group output description", exc_info=True)

        hide = len(runs) > COLLAPSE_ABOVE
        row = 70.0 if hide else 260.0
        span = (len(runs) - 1) * row
        top = span * 0.5

        index_node = subgroup.nodes.new("GeometryNodeInputIndex")
        index_node.location = (COL_INDEX, 0.0)
        index_node.label = "%s Index" % name

        run_sockets = []
        for i, (start, end) in enumerate(runs):
            y = top - i * row
            run_sockets.append((_run_socket(subgroup, index_node, start, end, y, hide), y))

        final_socket, final_y, next_x = _reduce_or(subgroup, run_sockets, hide)

        output = subgroup.nodes.new("NodeGroupOutput")
        output.location = (next_x, final_y)
        output.label = "Picked %s Selection" % name

        selection_input = _first_socket(output.inputs, "Selection")
        if selection_input is None:
            raise RuntimeError("Could not create Selection output on generated node group")
        subgroup.links.new(final_socket, selection_input)

        log.debug("Built group with %d ranges, %d nodes", len(runs), len(subgroup.nodes))
        return subgroup, len(runs), False
    except Exception:
        # The caller has no subgroup reference until this function returns.
        # Roll back here so a half-built group cannot be reused on retry.
        bpy.data.node_groups.remove(subgroup, do_unlink=True)
        raise


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


def _mouse_in_region(context, event):
    # mouse_region_x/y can be relative to the sidebar that invoked the
    # operator. Window coordinates remain correct across temp_override.
    return (float(event.mouse_x - context.region.x),
            float(event.mouse_y - context.region.y))


class GNIV_OT_pick_elements(Operator):
    bl_idname = "view3d.gniv_pick_elements"
    bl_label = "Pick Evaluated Elements"
    bl_description = (
        "Select evaluated elements using the active Blender Select Box, Circle or Lasso tool; "
        "viewport X-Ray controls whether hidden elements can be selected"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if is_active():
            cls.poll_message_set("An evaluated-element picker is already running")
            return False
        if context.area is None or context.area.type != 'VIEW_3D':
            cls.poll_message_set("Run the picker from a 3D Viewport")
            return False
        if context.active_object is None:
            cls.poll_message_set("Select an object first")
            return False
        return True

    def invoke(self, context, event):
        if is_active():
            return {'CANCELLED'}
        region = context.region
        if region is None or region.type != 'WINDOW':
            region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
        if region is None:
            self.report({'ERROR'}, "No 3D viewport window region found")
            return {'CANCELLED'}
        self._view_region = region
        self._scene = context.scene
        with context.temp_override(region=region):
            return self._invoke_in_view(context, event)

    def _invoke_in_view(self, context, event):
        scene = context.scene

        if not scene.gniv_show_indices:
            scene.gniv_show_indices = True

        try:
            cache_mod.cancel_pending()
            depsgraph = context.evaluated_depsgraph_get()
            cache_mod.rebuild(scene, depsgraph, context.active_object)
        except (AttributeError, RuntimeError) as exc:
            self.report({'ERROR'}, "Could not read evaluated geometry: %s" % exc)
            return {'CANCELLED'}

        if not cache_mod.cache["total"]:
            self.report(
                {'ERROR'},
                "The evaluated geometry has no %s" % domain_label(scene.gniv_domain, plural=True).lower(),
            )
            return {'CANCELLED'}

        obj = context.active_object
        if not selection_matches_context(scene, obj):
            clear_selection(scene)

        self._object_name = obj.name
        self._domain = scene.gniv_domain
        self._dragging = False
        self._paint_mode = None
        self._replace_started = False
        self._style = _active_selection_style(context)
        self._circle_radius = _active_circle_radius(context)
        log.debug(
            "Picker invoked: object=%r domain=%s tool=%s X-Ray=%s cached_elements=%d",
            self._object_name, self._domain, self._style, _xray_enabled(context),
            cache_mod.count(),
        )
        self._start = _mouse_in_region(context, event)
        self._lasso = []

        region_ptr = context.region.as_pointer()

        _live["active"] = True
        _live["values"] = set(parse_selection(scene))
        _live["hover"] = -1
        _live["mouse"] = self._start
        _live["region"] = region_ptr
        _touch_live()

        scene.gniv_selected_domain = self._domain
        scene.gniv_selected_object = self._object_name

        _clear_depth()
        clear_gesture()
        _gesture["region"] = region_ptr

        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set('CROSSHAIR')

        xray_text = "X-Ray ON: through-select" if _xray_enabled(context) else "X-Ray OFF: visible only"
        self.report(
            {'INFO'},
            "%s picker • %s • Shift add • Ctrl remove • RMB/Enter keep • Esc clear" % (
                self._style.title(), xray_text),
        )
        utils.tag_redraw_all_view3d()
        return {'RUNNING_MODAL'}

    # -- selection updates --------------------------------------------------

    def _selection_mode_from_event(self, event):
        if event.ctrl:
            return 'REMOVE'
        if event.shift:
            return 'ADD'
        return 'REPLACE'

    def _apply_values(self, context, values, hovered=-1, continuous=False):
        values = set(values)
        selection = _live["values"]

        if self._paint_mode == 'REPLACE':
            if continuous:
                if not self._replace_started:
                    selection.clear()
                    self._replace_started = True
                selection.update(values)
            else:
                selection.clear()
                selection.update(values)
        elif self._paint_mode == 'ADD':
            selection.update(values)
        elif self._paint_mode == 'REMOVE':
            selection.difference_update(values)

        _live["hover"] = hovered
        _touch_live()
        utils.tag_redraw_all_view3d()

    def _paint_circle(self, context, event):
        mouse_x, mouse_y = _mouse_in_region(context, event)
        values, hovered = _elements_in_radius(
            context, mouse_x, mouse_y, self._circle_radius)
        self._apply_values(context, values, hovered, continuous=True)

    def _finish_drag(self, context, event):
        current = _mouse_in_region(context, event)

        trace_label = "%s/%s" % (self._domain, self._style)
        log.debug(
            "Pick trace [%s]: finishing gesture; X-Ray=%s paint_mode=%s",
            trace_label, _xray_enabled(context), self._paint_mode,
        )

        if self._style == 'BOX':
            values, hovered = _elements_in_box(
                context, self._start, current, trace_label=trace_label)
        elif self._style == 'LASSO':
            if not self._lasso or current != self._lasso[-1]:
                self._lasso.append(current)
            if len(self._lasso) >= 3:
                values, hovered = _elements_in_lasso(
                    context, self._lasso, trace_label=trace_label)
            else:
                values, hovered = _elements_in_radius(
                    context, current[0], current[1], PICK_RADIUS,
                    trace_label=trace_label)
        elif self._style == 'POINT':
            values, hovered = _elements_in_radius(
                context, current[0], current[1], PICK_RADIUS,
                trace_label=trace_label)
        else:
            return

        self._apply_values(context, values, hovered, continuous=False)

    def _finish_picker(self, context, clear=False):
        scene = context.scene
        if clear:
            _live["values"].clear()
            _touch_live()

        values = sorted(_live["values"])

        _live["active"] = False
        _live["hover"] = -1
        clear_gesture()
        _clear_depth()

        try:
            context.window.cursor_modal_restore()
        except (AttributeError, RuntimeError):
            log.debug("Could not restore the modal cursor", exc_info=True)

        preview = values[:24]
        suffix = " ..." if len(values) > len(preview) else ""
        log.debug(
            "Picker finish: object=%r domain=%s clear=%s stored_count=%d indices=%s%s",
            self._object_name, self._domain, clear, len(values), preview, suffix,
        )

        # The one and only write of the stored selection.
        store_selection(
            scene, values,
            domain=self._domain,
            obj_name=self._object_name,
            active=values[-1] if values else -1,
        )
        utils.tag_redraw_all_view3d()

        if clear:
            self.report({'INFO'}, "Cleared picked selection")
        else:
            self.report({'INFO'}, "Stored %d evaluated %s" % (
                len(values), domain_label(self._domain, plural=(len(values) != 1)).lower()))
        return {'FINISHED'}

    # -- modal --------------------------------------------------------------

    def _event_inside_view_window(self, context, event):
        """Header/tool/sidebar clicks pass through so viewport controls stay usable."""
        area = getattr(context, "area", None)
        if area is None or area.type != 'VIEW_3D':
            return True

        window_region = self._view_region
        if window_region is None:
            return True

        x = int(getattr(event, "mouse_x", 0))
        y = int(getattr(event, "mouse_y", 0))
        # Overlapping sidebars/toolbars can cover part of the WINDOW region.
        for region in area.regions:
            if region.type in {'UI', 'TOOLS', 'HEADER', 'TOOL_HEADER'}:
                if (region.width > 1 and region.height > 1
                        and region.x <= x < region.x + region.width
                        and region.y <= y < region.y + region.height):
                    return False
        return (window_region.x <= x < window_region.x + window_region.width
                and window_region.y <= y < window_region.y + window_region.height)

    def modal(self, context, event):
        if (_shutdown or not _live['active'] or context.scene != self._scene
                or context.area is None or context.area.type != 'VIEW_3D'
                or self._view_region not in tuple(context.area.regions)):
            self.cancel(context)
            return {'CANCELLED'}
        with context.temp_override(region=self._view_region):
            try:
                return self._modal_in_view(context, event)
            except VisibilityUnavailable as exc:
                self._dragging = False
                self._paint_mode = None
                self._replace_started = False
                clear_gesture()
                self.report({'WARNING'}, "%s; picked selection kept" % exc)
                return {'RUNNING_MODAL'}

    def _modal_in_view(self, context, event):
        if _shutdown:
            self.cancel(context)
            return {'CANCELLED'}

        scene = context.scene

        # The domain enum stays clickable in the sidebar while picking, so a
        # mid-gesture change would leave the live set holding indices from a
        # domain that no longer exists.
        if not scene.gniv_show_indices:
            self.cancel(context)
            return {'CANCELLED'}

        if scene.gniv_domain != self._domain:
            self.report({'WARNING'}, "Domain changed — picker cancelled")
            self.cancel(context)
            return {'CANCELLED'}

        obj = context.active_object
        if obj is None or obj.name != self._object_name:
            self.report({'WARNING'}, "Active object changed — picker cancelled")
            self.cancel(context)
            return {'CANCELLED'}

        mouse_events = {
            'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE',
            'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'MOUSEMOVE',
        }
        if event.type in mouse_events and not self._event_inside_view_window(context, event):
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and self._dragging:
                self._dragging = False
                self._paint_mode = None
                self._replace_started = False
                clear_gesture()
            return {'PASS_THROUGH'}

        # Blender's own X-Ray shortcut, handled here so the painted selection
        # survives a through-select change.
        if event.type == 'Z' and event.value == 'PRESS' and event.alt:
            _toggle_xray(context)
            return {'RUNNING_MODAL'}

        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            # Viewport navigation and circle-radius changes stay available.
            _clear_depth()
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            current = _mouse_in_region(context, event)
            _live["mouse"] = current

            if self._dragging:
                if self._style == 'CIRCLE':
                    self._circle_radius = _active_circle_radius(context)
                    _set_gesture(True, 'CIRCLE', self._start, current,
                                 radius=self._circle_radius)
                    self._paint_circle(context, event)
                elif self._style == 'BOX':
                    _set_gesture(True, 'BOX', self._start, current)
                elif self._style == 'LASSO':
                    if not self._lasso or (
                        abs(current[0] - self._lasso[-1][0])
                        + abs(current[1] - self._lasso[-1][1]) >= 2.0
                    ):
                        self._lasso.append(current)
                    _set_gesture(True, 'LASSO', self._start, current, points=self._lasso)
            else:
                hovered = _nearest_element_index(context, current[0], current[1])
                if hovered != _live["hover"]:
                    _live["hover"] = hovered
                    _touch_live()
                utils.tag_redraw_all_view3d()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            self._style = _active_selection_style(context)
            self._circle_radius = _active_circle_radius(context)
            self._paint_mode = self._selection_mode_from_event(event)
            self._replace_started = False
            self._dragging = True
            self._start = _mouse_in_region(context, event)
            self._lasso = [self._start]

            _set_gesture(True, self._style, self._start, self._start,
                         points=self._lasso, radius=self._circle_radius)

            if self._style == 'CIRCLE':
                self._paint_circle(context, event)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if self._dragging:
                self._finish_drag(context, event)
            self._dragging = False
            self._paint_mode = None
            self._replace_started = False
            clear_gesture()
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS' and event.type in {'RET', 'NUMPAD_ENTER', 'SPACE', 'RIGHTMOUSE'}:
            return self._finish_picker(context, clear=False)

        if event.type == 'ESC' and event.value == 'PRESS':
            # Esc discards the picked selection. Right Mouse / Enter / Space
            # are the keep-and-exit route.
            return self._finish_picker(context, clear=True)

        return {'RUNNING_MODAL'}

    def cancel(self, context):
        _live["active"] = False
        _live["hover"] = -1
        clear_gesture()
        _clear_depth()
        try:
            context.window.cursor_modal_restore()
        except (AttributeError, RuntimeError):
            log.debug("Could not restore the modal cursor", exc_info=True)
        utils.tag_redraw_all_view3d()


class GNIV_OT_clear_selection(Operator):
    bl_idname = "view3d.gniv_clear_selection"
    bl_label = "Clear Picked Selection"
    bl_description = "Clear the evaluated-element selection stored by GN Index Viewer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(getattr(context.scene, "gniv_selected_indices", ""))

    def execute(self, context):
        if _live["active"]:
            _live["values"].clear()
            _touch_live()
        clear_selection(context.scene)
        return {'FINISHED'}


class GNIV_OT_add_index_selection(Operator):
    bl_idname = "node.gniv_add_index_selection"
    bl_label = "Add Index Selection"
    bl_description = (
        "Create one compact Geometry Nodes group node whose Selection output "
        "matches all picked indices"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        scene = context.scene
        obj = context.active_object
        if obj is None:
            cls.poll_message_set("Select an object first")
            return False
        if not getattr(scene, "gniv_selected_indices", ""):
            cls.poll_message_set("Pick evaluated elements first")
            return False
        if not selection_matches_context(scene, obj):
            cls.poll_message_set("The picked selection belongs to another object or domain")
            return False
        return True

    def execute(self, context):
        scene = context.scene
        values = parse_selection(scene)
        if not values:
            self.report({'ERROR'}, "Pick evaluated elements first")
            return {'CANCELLED'}

        obj = context.active_object
        domain = getattr(scene, "gniv_selected_domain", scene.gniv_domain)
        tree, cursor = _target_geometry_nodes_tree(context, obj)
        if tree is None:
            self.report({'ERROR'}, "No editable Geometry Nodes tree found")
            return {'CANCELLED'}

        subgroup = None
        reused = False
        group_node = None
        try:
            subgroup, run_count, reused = _build_selection_group(values, domain)

            group_node = tree.nodes.new("GeometryNodeGroup")
            group_node.node_tree = subgroup
            name = domain_label(domain)
            group_node.label = "Picked %s: %d" % (
                domain_label(domain, plural=(len(values) != 1)), len(values))
            group_node.name = "GNIV Picked %s Selection" % name
            group_node.width = 190.0

            if cursor is not None:
                group_node.location = cursor
            else:
                others = [n for n in tree.nodes if n != group_node]
                right = max((n.location.x + n.width for n in others), default=0.0)
                y = max((n.location.y for n in others), default=0.0)
                group_node.location = (right + 160.0, y)

            for node in tree.nodes:
                node.select = False
            group_node.select = True
            tree.nodes.active = group_node

        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
            log.warning("Could not create picked-selection group", exc_info=True)
            try:
                if group_node is not None and group_node.name in tree.nodes:
                    tree.nodes.remove(group_node)
            except (ReferenceError, RuntimeError):
                log.debug("Could not roll back the group node", exc_info=True)
            try:
                if subgroup is not None and not reused:
                    bpy.data.node_groups.remove(subgroup, do_unlink=True)
            except (ReferenceError, RuntimeError):
                log.debug("Could not roll back the generated group", exc_info=True)
            self.report({'ERROR'}, "Could not create picked-selection group: %s" % exc)
            return {'CANCELLED'}

        self.report(
            {'INFO'},
            "%s Picked %s Selection node for %d indices (%d compact range%s)" % (
                "Reused" if reused else "Added",
                domain_label(domain).lower(),
                len(values),
                run_count,
                "" if run_count == 1 else "s"),
        )
        if run_count > FRAGMENTED_ABOVE:
            self.report(
                {'WARNING'},
                "Selection splits into %d ranges — picking contiguous runs "
                "keeps the generated group small" % run_count,
            )
        return {'FINISHED'}


classes = (
    GNIV_OT_pick_elements,
    GNIV_OT_clear_selection,
    GNIV_OT_add_index_selection,
)


def set_shutdown(value):
    """Let a running modal bow out before its class is unregistered."""
    global _shutdown
    _shutdown = bool(value)
    if value:
        reset_runtime()


def reset_runtime():
    """Discard transient state after disable or loading another file."""
    _live["active"] = False
    _live["hover"] = -1
    _live["values"].clear()
    _touch_live()
    _clear_depth()
    _gesture["active"] = False
