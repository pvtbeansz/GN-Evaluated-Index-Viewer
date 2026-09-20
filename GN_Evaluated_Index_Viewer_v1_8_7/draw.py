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

"""Viewport drawing.

The draw callback only projects cached world-space positions into screen
space and writes text. No depsgraph access, no mesh evaluation.
"""

import blf
import bpy
import numpy as np

from . import cache as cache_mod
from . import picker as picker_mod
from . import project
from . import visibility
from . import utils
from .utils import log

_draw_handle = None
_cache = cache_mod.cache

# Parsing the stored index string on every redraw is wasteful, so the result
# is memoised against the raw string it came from.
_selection_cache = {"raw": None, "values": frozenset(), "array": np.empty(0, dtype=np.int64)}
_EMPTY_SELECTION = (frozenset(), np.empty(0, dtype=np.int64))

# {font_size: {digit_count: (width, height)}}
_text_dimensions = {}

HOVER_COLOUR = (1.0, 0.55, 0.12, 1.0)


def ensure_draw_handler():
    global _draw_handle
    if _draw_handle is None:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            draw_callback, (), 'WINDOW', 'POST_PIXEL'
        )


def remove_draw_handler():
    global _draw_handle
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None


def clear_caches():
    _text_dimensions.clear()
    invalidate_selection_cache()


def invalidate_selection_cache():
    _selection_cache["raw"] = None
    _selection_cache["values"] = frozenset()
    _selection_cache["array"] = np.empty(0, dtype=np.int64)


def stored_selection(scene):
    """The picked selection, parsed at most once per distinct stored string."""
    raw = getattr(scene, "gniv_selected_indices", "") or ""
    if _selection_cache["raw"] != raw:
        values = utils.parse_index_string(raw)
        _selection_cache["raw"] = raw
        _selection_cache["values"] = frozenset(values)
        _selection_cache["array"] = np.asarray(values, dtype=np.int64)
    return _selection_cache["values"], _selection_cache["array"]


def selection_applies(scene, obj):
    """The stored selection only means anything on the object and domain it
    was picked on."""
    if obj is None:
        return False
    if getattr(scene, "gniv_selected_domain", scene.gniv_domain) != scene.gniv_domain:
        return False
    stored_object = getattr(scene, "gniv_selected_object", "")
    return not stored_object or stored_object == obj.name


def active_selection(scene, obj):
    """``(set, sorted int64 array)`` — live while the picker is modal,
    otherwise the stored selection."""
    if picker_mod.is_active():
        return picker_mod.live_values(), picker_mod.live_array()
    if not selection_applies(scene, obj):
        return _EMPTY_SELECTION
    return stored_selection(scene)


def _candidate_rows(scene, selection, hover_value):
    """Choose which cached rows are worth projecting at all."""
    n = cache_mod.count()
    if n == 0:
        return None

    if scene.gniv_only_selected:
        visible = selection
        if hover_value >= 0 and hover_value not in selection:
            visible = set(selection)
            visible.add(hover_value)
        if not visible:
            return None
        return cache_mod.rows_for_values(visible)

    if scene.gniv_use_range:
        lo = min(scene.gniv_range_min, scene.gniv_range_max)
        hi = max(scene.gniv_range_min, scene.gniv_range_max)
        return cache_mod.rows_for_range(lo, hi)

    return slice(0, n)


def _rows_empty(rows):
    if rows is None:
        return True
    if isinstance(rows, slice):
        return rows.stop <= rows.start
    return len(rows) == 0


def _measurer(font_id, font_size):
    """Measure label sizes, memoised by digit count.

    Blender's default UI font has tabular digits, so every n-digit index is
    the same width. Measuring one sample per length keeps this off the
    per-label path without assuming a fixed glyph width.
    """
    cached = _text_dimensions.setdefault(font_size, {})

    def measure(text):
        length = len(text)
        size = cached.get(length)
        if size is None:
            size = blf.dimensions(font_id, "8" * length)
            cached[length] = size
        return size

    return measure


def _thin(sx, sy, ndc_z, keep, scene):
    """Keep one label per screen cell."""
    cell_w = float(max(scene.gniv_spacing, 1))
    cell_h = float(max(scene.gniv_font_size + 4, 8))

    if scene.gniv_priority == 'NEAR':
        order = np.argsort(ndc_z[keep], kind='stable')
    else:
        order = np.arange(len(keep))

    candidates = keep[order]
    cells = np.stack([
        np.floor(sx[candidates] / cell_w).astype(np.int64),
        np.floor(sy[candidates] / cell_h).astype(np.int64),
    ], axis=1)
    # return_index gives the first occurrence in `candidates`, which is the
    # nearest one because `candidates` was depth-sorted above.
    _, first = np.unique(cells, axis=0, return_index=True)
    return np.sort(candidates[first])


def draw_callback():
    context = bpy.context

    area = getattr(context, "area", None)
    if area is None or area.type != 'VIEW_3D':
        return

    scene = getattr(context, "scene", None)
    if scene is None or not getattr(scene, "gniv_show_indices", False):
        return

    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None:
        return

    # Capture depth before anything of ours is drawn, so the picker samples
    # the scene's own depth rather than our overlay lines.
    picker_mod.capture_depth(context, region, rv3d)

    # Gesture feedback is drawn before the label early-outs below, so the
    # selection rectangle stays visible even when no labels survive culling.
    picker_mod.draw_gesture_overlay(region)

    obj = context.active_object
    if obj is None:
        return

    if (_cache["dirty"]
            or _cache["object"] != obj.name
            or _cache["domain"] != scene.gniv_domain):
        cache_mod.request_rebuild()
        return

    selection, selection_array = active_selection(scene, obj)
    hover_value = picker_mod.live_hover()

    rows = _candidate_rows(scene, selection, hover_value)
    if _rows_empty(rows):
        return

    positions = _cache["positions"][rows]
    indices = _cache["indices"][rows]
    normals = None if _cache["normals"] is None else _cache["normals"][rows]

    persp = project.perspective_matrix(rv3d)
    sx, sy, ndc_z, valid = project.project(positions, region, rv3d, persp=persp)

    margin = 50.0
    mask = valid
    mask &= (sx > -margin) & (sx < region.width + margin)
    mask &= (sy > -margin) & (sy < region.height + margin)
    if not mask.any():
        return

    # ---- occlusion --------------------------------------------------------
    occlusion = scene.gniv_occlusion
    view_vecs = None

    if occlusion in {'BACKFACE', 'DEPTH'} and normals is not None:
        view_vecs = project.view_vectors(positions, rv3d)
        facing = np.einsum('ij,ij->i', normals, view_vecs)
        mask &= facing < 0.0
        if not mask.any():
            return

    if occlusion == 'DEPTH':
        keep = np.nonzero(mask)[0]
        rect = project.rect_from_points(sx[keep], sy[keep], region, margin=1.0)
        if rect is not None:
            depth = project.read_depth_rect(*rect)
            if depth is not None:
                if view_vecs is None:
                    view_vecs = project.view_vectors(positions, rv3d)
                win_z = project.window_depths(
                    positions[keep], region, rv3d,
                    persp=persp, view_vecs=view_vecs[keep],
                )
                if scene.gniv_domain == 'INSTANCE':
                    targets, _hits = visibility.surface_positions(
                        _cache["instance_surfaces"], indices[keep], positions[keep],
                        sx[keep], sy[keep], region, rv3d)
                    win_z = project.window_depths(targets, region, rv3d, persp=persp)
                visible = project.visible_against_depth(
                    depth, rect[0], rect[1], sx[keep], sy[keep], win_z,
                )
                mask = np.zeros(len(positions), dtype=bool)
                mask[keep[visible]] = True

    keep = np.nonzero(mask)[0]
    if len(keep) == 0:
        return

    # ---- priority ---------------------------------------------------------
    # Picked and hovered labels are recoloured in place rather than drawn a
    # second time, and are exempt from thinning so a selected index never
    # vanishes because a neighbour won its screen cell.
    if hover_value >= 0:
        priority_array = np.append(selection_array, np.int64(hover_value))
    else:
        priority_array = selection_array

    if len(priority_array):
        is_priority = np.isin(indices[keep], priority_array)
        priority_rows = keep[is_priority]
        normal_rows = keep[~is_priority]
    else:
        priority_rows = np.empty(0, dtype=np.int64)
        normal_rows = keep

    if scene.gniv_thin_labels and len(normal_rows) > 1:
        normal_rows = _thin(sx, sy, ndc_z, normal_rows, scene)

    max_labels = int(scene.gniv_max_labels)
    if len(priority_rows) >= max_labels:
        display_rows = priority_rows[:max_labels]
    else:
        remaining = max_labels - len(priority_rows)
        display_rows = np.sort(np.concatenate((priority_rows, normal_rows[:remaining])))

    if len(display_rows) == 0:
        return

    # ---- text -------------------------------------------------------------
    font_id = 0
    ui_scale = 1.0
    prefs = getattr(context, "preferences", None)
    if prefs is not None:
        ui_scale = float(getattr(prefs.system, "ui_scale", 1.0)) or 1.0

    font_size = max(int(round(scene.gniv_font_size * ui_scale)), 1)
    blf.size(font_id, font_size)
    base_colour = tuple(scene.gniv_color)
    selected_colour = tuple(scene.gniv_selected_color)
    measure = _measurer(font_id, font_size)

    shadow = False
    try:
        blf.enable(font_id, blf.SHADOW)
        blf.shadow(font_id, 3, 0.0, 0.0, 0.0, 0.8)
        blf.shadow_offset(font_id, 1, -1)
        shadow = True
    except (AttributeError, RuntimeError, ValueError):
        log.debug("Label shadow unavailable", exc_info=True)

    try:
        for row in display_rows:
            index_value = int(indices[row])
            if index_value == hover_value:
                colour = HOVER_COLOUR
            elif index_value in selection:
                colour = selected_colour
            else:
                colour = base_colour

            text = str(index_value)
            width, height = measure(text)
            # Centred on the element, so the label the user aims at and the
            # position the picker tests are the same point.
            blf.color(font_id, colour[0], colour[1], colour[2], colour[3])
            blf.position(
                font_id,
                float(sx[row]) - width * 0.5,
                float(sy[row]) - height * 0.5,
                0.0,
            )
            blf.draw(font_id, text)
    finally:
        if shadow:
            try:
                blf.disable(font_id, blf.SHADOW)
            except (AttributeError, RuntimeError, ValueError):
                log.debug("Could not disable label shadow", exc_info=True)
