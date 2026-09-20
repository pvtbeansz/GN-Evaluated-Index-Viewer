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

"""Operators and the sidebar panels."""

import os

from bpy.props import BoolProperty
from bpy.types import AddonPreferences, Operator, Panel

from . import cache as cache_mod
from . import picker as picker_mod
from . import utils
from .utils import domain_label

YOUTUBE_URL = "https://www.youtube.com/@Pvtbeansz"


def _update_debug_logging(self, context):
    utils.set_debug_logging(self.debug_logging)


class GNIV_AddonPreferences(AddonPreferences):
    """Preferences shown inside Blender Preferences > Add-ons."""

    # For extension add-ons Blender expects the full runtime package name.
    bl_idname = __package__

    debug_logging: BoolProperty(
        name="Debug Logging",
        description=(
            "Print detailed diagnostics to the system console. "
            "Useful when labels or picking behave unexpectedly"
        ),
        default=False,
        update=_update_debug_logging,
    )

    def draw(self, context):
        layout = self.layout

        col = layout.column()
        col.prop(self, "debug_logging")

        # Installed extensions live at <repository>/<package_id>/. Passing
        # Blender its repository directory and package ID lets Blender's own
        # extension manager disable and uninstall the package cleanly.
        package_dir = os.path.dirname(os.path.abspath(__file__))
        repo_directory = os.path.dirname(package_dir)
        pkg_id = os.path.basename(package_dir)

        link = layout.box()
        link.label(text="Free and open source (GPL-2.0-or-later)", icon='FUND')
        note = link.column(align=True)
        note.scale_y = 0.8
        note.label(text="If you paid for this, you were overcharged.")
        note.label(text="Questions and bug reports: drop a comment.")
        link.operator(
            "wm.url_open",
            text="PvtBeans on YouTube",
            icon='URL',
        ).url = YOUTUBE_URL

        box = layout.box()
        box.label(text="Extension Management", icon='PREFERENCES')
        row = box.row()
        row.alert = True
        op = row.operator(
            "extensions.package_uninstall",
            text="Uninstall GN Evaluated Index Viewer",
            icon='TRASH',
        )
        op.repo_directory = repo_directory
        op.pkg_id = pkg_id

        note = box.row()
        note.scale_y = 0.8
        note.label(text="Uses Blender's native extension uninstaller.")


class GNIV_OT_refresh(Operator):
    bl_idname = "view3d.gniv_refresh"
    bl_label = "Refresh"
    bl_description = "Re-read the evaluated geometry"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        cache_mod.request_rebuild()
        utils.tag_redraw_all_view3d()
        return {'FINISHED'}


class GNIV_OT_fit_range(Operator):
    bl_idname = "view3d.gniv_fit_range"
    bl_label = "Fit to Geometry"
    bl_description = "Set the range to cover every index in the evaluated geometry"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return cache_mod.cache["total"] > 0

    def execute(self, context):
        scene = context.scene
        # Direct ID-property writes skip the clamping update callbacks.
        scene["gniv_range_min"] = 0
        scene["gniv_range_max"] = cache_mod.max_index()
        cache_mod.request_rebuild()
        utils.tag_redraw_all_view3d()
        return {'FINISHED'}


class GNIV_PT_panel(Panel):
    bl_label = "GN Index Viewer"
    bl_idname = "GNIV_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GN Index"

    def draw_header(self, context):
        self.layout.prop(context.scene, "gniv_show_indices", text="")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        scene = context.scene
        obj = context.active_object
        cache = cache_mod.cache

        body = layout.column()
        body.enabled = scene.gniv_show_indices

        row = body.row(align=True)
        row.prop(scene, "gniv_domain", text="Domain")
        row.operator("view3d.gniv_refresh", text="", icon='FILE_REFRESH')

        # ---- index range --------------------------------------------------
        header = body.row(heading="Index Range")
        header.prop(scene, "gniv_use_range", text="")

        rng = body.column(align=True)
        rng.enabled = scene.gniv_use_range and not scene.gniv_only_selected
        rng.prop(scene, "gniv_range_min", text="From")
        rng.prop(scene, "gniv_range_max", text="To")
        rng.operator("view3d.gniv_fit_range", icon='FULLSCREEN_ENTER')

        info = body.row()
        info.scale_y = 0.8
        if cache["total"]:
            info.label(text="Available: 0 – %d" % cache_mod.max_index())
        else:
            info.label(text="Available: (no geometry)")

        # ---- status -------------------------------------------------------
        layout.separator()
        if obj is None:
            layout.label(text="Select an object.", icon='INFO')
            return

        status = layout.column(align=True)
        status.scale_y = 0.85
        status.label(text="Object: %s" % obj.name, icon='OBJECT_DATA')
        if cache["status"]:
            status.label(text=cache["status"], icon='ERROR')
        else:
            status.label(text="Elements: %d" % cache["total"])
            components = cache_mod.available_components()
            if components:
                status.label(text="Has: %s" % components)
            if cache["truncated"]:
                status.label(
                    text="Cached %d–%d only." % (
                        cache["start"], cache["start"] + cache_mod.BUILD_LIMIT - 1),
                    icon='ERROR',
                )
            if cache["normals"] is None and scene.gniv_occlusion == 'BACKFACE':
                status.label(text="No normals: backface culling off.", icon='INFO')
            if scene.gniv_domain == 'INSTANCE' and cache_mod.count():
                mapped = len(cache["instance_surfaces"])
                if mapped < cache_mod.count():
                    status.label(text="Instance surfaces: %d/%d." % (mapped, cache_mod.count()), icon='INFO')
                    status.label(text="Other instances use origin depth.")


class GNIV_PT_picker(Panel):
    bl_label = "Pick for Geometry Nodes"
    bl_idname = "GNIV_PT_picker"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GN Index"
    bl_parent_id = "GNIV_PT_panel"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        obj = context.active_object

        name = domain_label(scene.gniv_domain, plural=True)
        layout.operator(
            "view3d.gniv_pick_elements",
            text="Pick / Paint %s" % name,
            icon='EYEDROPPER',
        )

        selected = picker_mod.parse_selection(scene)
        matches = picker_mod.selection_matches_context(scene, obj)

        row = layout.row(align=True)
        if selected:
            stored_name = domain_label(
                getattr(scene, "gniv_selected_domain", scene.gniv_domain),
                plural=(len(selected) != 1),
            )
            row.label(text="Picked: %d %s" % (len(selected), stored_name),
                      icon='RESTRICT_SELECT_OFF')
            row.operator("view3d.gniv_clear_selection", text="", icon='X')

            preview = ", ".join(str(v) for v in selected[:8])
            if len(selected) > 8:
                preview += ", …"
            preview_row = layout.row()
            preview_row.scale_y = 0.8
            preview_row.label(text=preview)
        else:
            row.label(text="Picked: none", icon='RESTRICT_SELECT_OFF')

        if selected and not matches:
            warn = layout.row()
            warn.alert = True
            stored_object = getattr(scene, "gniv_selected_object", "")
            if stored_object and obj is not None and stored_object != obj.name:
                warn.label(text="Picked on “%s”." % stored_object, icon='ERROR')
            else:
                warn.label(text="Picked on another domain.", icon='ERROR')

        layout.operator(
            "node.gniv_add_index_selection",
            text="Add Selection to GN",
            icon='NODETREE',
        )

        help_col = layout.column(align=True)
        help_col.scale_y = 0.75
        help_col.label(text="Box / Circle / Lasso tool")
        help_col.label(text="Alt+Z: X-Ray")
        help_col.label(text="Shift: add   Ctrl: remove")
        help_col.label(text="RMB/Enter: keep   Esc: clear")


class GNIV_PT_settings(Panel):
    bl_label = "Display Settings"
    bl_idname = "GNIV_PT_settings"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GN Index"
    bl_parent_id = "GNIV_PT_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        scene = context.scene
        col = layout.column()
        col.enabled = scene.gniv_show_indices

        col.prop(scene, "gniv_only_selected")
        col.separator()

        col.prop(scene, "gniv_occlusion", text="Hidden Geometry")
        col.separator()

        col.prop(scene, "gniv_font_size")
        col.prop(scene, "gniv_color", text="Colour")
        col.prop(scene, "gniv_selected_color", text="Selected Colour")
        col.separator()

        col.prop(scene, "gniv_thin_labels")
        thin = col.column(align=True)
        thin.enabled = scene.gniv_thin_labels
        thin.prop(scene, "gniv_spacing")
        thin.prop(scene, "gniv_priority")
        col.separator()

        col.prop(scene, "gniv_max_labels")


classes = (
    GNIV_AddonPreferences,
    GNIV_OT_refresh,
    GNIV_OT_fit_range,
    GNIV_PT_panel,
    GNIV_PT_picker,
    GNIV_PT_settings,
)
