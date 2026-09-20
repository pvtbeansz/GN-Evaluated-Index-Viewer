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

"""GN Evaluated Index Viewer.

Metadata lives in blender_manifest.toml — this release officially supports
Blender 5.2+, so there is deliberately no legacy bl_info block here.
"""

# Canonical Blender reload idiom: on first import `bpy` is not yet bound, so
# the else branch runs. On reload the module globals survive, so the names
# below already exist. Order matters — `draw` binds the cache dict at module
# level, so it must be reloaded after `cache`.
if "bpy" in locals():
    import importlib

    utils = importlib.reload(utils)              # noqa: F821
    project = importlib.reload(project)          # noqa: F821
    visibility = importlib.reload(visibility)    # noqa: F821
    cache = importlib.reload(cache)              # noqa: F821
    picker = importlib.reload(picker)            # noqa: F821
    draw = importlib.reload(draw)                # noqa: F821
    handlers = importlib.reload(handlers)        # noqa: F821
    properties = importlib.reload(properties)    # noqa: F821
    ui = importlib.reload(ui)                    # noqa: F821
else:
    from . import utils, project, visibility, cache, picker, draw, handlers, properties, ui

import bpy


def _apply_preferences():
    try:
        prefs = bpy.context.preferences.addons[__package__].preferences
    except (AttributeError, KeyError):
        return
    utils.set_debug_logging(getattr(prefs, "debug_logging", False))


def register():
    picker.set_shutdown(False)

    for cls in picker.classes + ui.classes:
        bpy.utils.register_class(cls)

    properties.register()
    handlers.register()
    _apply_preferences()

    # A .blend saved with the toggle on, opened before this add-on was enabled.
    scene = getattr(bpy.context, "scene", None)
    if scene is not None and getattr(scene, "gniv_show_indices", False):
        draw.ensure_draw_handler()
        cache.request_rebuild()


def unregister():
    # Tell any running modal picker to bail out before its class disappears.
    picker.set_shutdown(True)

    draw.remove_draw_handler()
    draw.clear_caches()
    handlers.unregister()
    cache.cancel_pending()
    cache.reset()
    properties.unregister()

    for cls in reversed(picker.classes + ui.classes):
        bpy.utils.unregister_class(cls)
