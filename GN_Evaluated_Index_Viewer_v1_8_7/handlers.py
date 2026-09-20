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

"""Depsgraph / frame / file-load handlers."""

import bpy
from bpy.app.handlers import persistent

from . import cache as cache_mod
from . import draw as draw_mod
from . import picker as picker_mod
from .utils import log


def _relevant_update(depsgraph, obj):
    """Return a reason when evaluated geometry may have changed.

    Geometry Nodes edits can arrive as a NodeTree depsgraph update without an
    accompanying Object update.  v1.8.3 only watched the object itself, which
    could leave the instance cache stale until a later unrelated action forced
    a rebuild.
    """
    original = obj.original
    for update in depsgraph.updates:
        update_id = update.id

        # Node-tree edits are cheap compared with carrying stale evaluated GN
        # data.  Rebuild for any node tree while the viewer is active; this also
        # catches nested geometry-node groups.
        if isinstance(update_id, bpy.types.NodeTree):
            return "node tree %s changed" % getattr(update_id, "name", "<unnamed>")

        try:
            same_object = update_id.original == original
        except (AttributeError, ReferenceError):
            same_object = update_id == original
        if not same_object:
            continue
        if update.is_updated_geometry:
            return "tracked object geometry changed"
        if update.is_updated_transform:
            return "tracked object transform changed"
    return None


@persistent
def on_depsgraph_update(scene, depsgraph):
    if not getattr(scene, "gniv_show_indices", False):
        cache_mod.cache["dirty"] = True
        return

    obj = getattr(bpy.context, "active_object", None)
    if obj is None:
        return

    if cache_mod.cache["dirty"] or cache_mod.cache["object"] != obj.name:
        log.debug(
            "Depsgraph: scheduling rebuild (dirty=%s cached_object=%r active=%r)",
            cache_mod.cache["dirty"], cache_mod.cache["object"], obj.name,
        )
        cache_mod.request_rebuild()
        return

    reason = _relevant_update(depsgraph, obj)
    if reason:
        # Never evaluate geometry from inside the handler itself — defer it.
        log.debug("Depsgraph: scheduling rebuild because %s", reason)
        cache_mod.request_rebuild()


@persistent
def on_frame_change(scene, depsgraph=None):
    if not getattr(scene, "gniv_show_indices", False):
        cache_mod.cache["dirty"] = True
        return
    log.debug("Frame change: scheduling rebuild")
    cache_mod.request_rebuild()


@persistent
def on_load_post(dummy):
    """A .blend saved with the toggle on must re-arm the draw handler."""
    # Blender removes nonpersistent timers on load, but module booleans survive.
    # Leaving _timer_pending set would prevent every subsequent rebuild.
    cache_mod.cancel_pending()
    picker_mod.reset_runtime()
    cache_mod.reset()
    draw_mod.clear_caches()

    scene = getattr(bpy.context, "scene", None)
    if scene is not None and getattr(scene, "gniv_show_indices", False):
        draw_mod.ensure_draw_handler()
        cache_mod.request_rebuild()
    else:
        draw_mod.remove_draw_handler()


_handlers = (
    (bpy.app.handlers.depsgraph_update_post, on_depsgraph_update),
    (bpy.app.handlers.frame_change_post, on_frame_change),
    (bpy.app.handlers.load_post, on_load_post),
)


def register():
    for handler_list, func in _handlers:
        if func not in handler_list:
            handler_list.append(func)


def unregister():
    for handler_list, func in _handlers:
        if func in handler_list:
            handler_list.remove(func)
