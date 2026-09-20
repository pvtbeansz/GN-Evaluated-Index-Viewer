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

"""Scene properties and their update callbacks."""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatVectorProperty, IntProperty, StringProperty

from . import cache as cache_mod
from . import draw as draw_mod
from . import utils
from .utils import log


# One static list shared by both domain enums. Static on purpose: a dynamic
# items callback has to keep a Python reference to the returned strings or
# Blender garbage-collects them mid-draw and corrupts the enum.
DOMAIN_ITEMS = [
    ('POINT', "Mesh Points", "Vertex indices of the evaluated mesh"),
    ('EDGE', "Mesh Edges", "Edge indices of the evaluated mesh"),
    ('FACE', "Mesh Faces", "Face indices of the evaluated mesh"),
    None,
    ('CLOUD', "Cloud Points", "Point indices of the evaluated point cloud"),
    ('CURVE_POINT', "Curve Points", "Control point indices of the evaluated curves"),
    ('CURVE', "Curves", "Curve indices of the evaluated curves"),
    None,
    ('INSTANCE', "Instances", "Indices of the evaluated instances"),
]


def _update_show_indices(self, context):
    log.debug("Viewer toggle changed: %s", bool(self.gniv_show_indices))
    if self.gniv_show_indices:
        draw_mod.ensure_draw_handler()
        cache_mod.request_rebuild()
    else:
        draw_mod.remove_draw_handler()
    utils.tag_redraw_all_view3d()


def _update_rebuild(self, context):
    log.debug("Domain/settings rebuild requested; domain=%s", getattr(self, "gniv_domain", "?"))
    cache_mod.request_rebuild()
    utils.tag_redraw_all_view3d()


def _update_redraw(self, context):
    utils.tag_redraw_all_view3d()


def _range_touched(self, context):
    """Only a truncated cache has to be rebuilt when the range moves.

    For everything else the range is applied at draw time, so a redraw is
    enough.
    """
    if cache_mod.cache["truncated"]:
        cache_mod.request_rebuild()
    utils.tag_redraw_all_view3d()


def _update_range_min(self, context):
    # Direct ID-property writes bypass the update callback, avoiding recursion.
    if self.gniv_range_min > self.gniv_range_max:
        self["gniv_range_max"] = self.gniv_range_min
    _range_touched(self, context)


def _update_range_max(self, context):
    if self.gniv_range_max < self.gniv_range_min:
        self["gniv_range_min"] = self.gniv_range_max
    _range_touched(self, context)


_properties = {
    "gniv_show_indices": BoolProperty(
        name="Show Evaluated Indices",
        description="Display indices from the object's evaluated geometry",
        default=False,
        update=_update_show_indices,
    ),
    "gniv_domain": EnumProperty(
        name="Domain",
        description="Geometry domain whose indices are displayed",
        items=DOMAIN_ITEMS,
        default='POINT',
        update=_update_rebuild,
    ),
    "gniv_use_range": BoolProperty(
        name="Index Range",
        description="Only draw labels for indices inside the chosen range",
        default=False,
        update=_range_touched,
    ),
    "gniv_range_min": IntProperty(
        name="From",
        description="Lowest index to display",
        default=0,
        min=0,
        soft_max=1000,
        update=_update_range_min,
    ),
    "gniv_range_max": IntProperty(
        name="To",
        description="Highest index to display",
        default=50,
        min=0,
        soft_max=1000,
        update=_update_range_max,
    ),
    "gniv_only_selected": BoolProperty(
        name="Only Picked",
        description=(
            "Draw labels only for picked indices and the element under the "
            "cursor while picking. With nothing picked, no labels are drawn. "
            "Takes priority over the index range"
        ),
        default=False,
        update=_update_redraw,
    ),
    "gniv_thin_labels": BoolProperty(
        name="Thin Labels",
        description="Keep only one label per screen cell so crowded geometry stays readable",
        default=True,
        update=_update_redraw,
    ),
    "gniv_spacing": IntProperty(
        name="Min Spacing",
        description="Minimum horizontal gap between labels, in pixels",
        default=26,
        min=4,
        max=200,
        update=_update_redraw,
    ),
    "gniv_priority": EnumProperty(
        name="Keep",
        description="Which label wins when several fall in the same cell",
        items=[
            ('NEAR', "Nearest", "Keep the label closest to the viewer"),
            ('INDEX', "Lowest Index", "Keep the lowest index — stable while orbiting"),
        ],
        default='NEAR',
        update=_update_redraw,
    ),
    "gniv_occlusion": EnumProperty(
        name="Hidden Geometry",
        description="How to treat labels facing away from the view or behind other surfaces",
        items=[
            ('NONE', "Show All", "Draw every label, including through the mesh"),
            ('BACKFACE', "Cull Backfaces", "Hide labels on geometry facing away from the view"),
            ('DEPTH', "Depth Test", "Also hide labels behind other surfaces (reads the depth buffer)"),
        ],
        default='BACKFACE',
        update=_update_redraw,
    ),
    "gniv_font_size": IntProperty(
        name="Font Size",
        description="Viewport label size, before interface scaling",
        default=14,
        min=8,
        max=48,
        update=_update_redraw,
    ),
    "gniv_max_labels": IntProperty(
        name="Max Labels",
        description=(
            "Safety ceiling on how many labels are drawn per viewport. "
            "Applied after culling and thinning — use Index Range to choose "
            "which indices you see"
        ),
        default=2000,
        min=1,
        max=20000,
        soft_max=10000,
        update=_update_redraw,
    ),
    "gniv_selected_index": IntProperty(
        name="Active Picked Index",
        description="Last/active evaluated element in the picked selection",
        default=-1,
        min=-1,
    ),
    "gniv_selected_indices": StringProperty(
        name="Picked Indices",
        description="Evaluated element indices picked by the picker, stored as compact ranges",
        default="",
        options={'HIDDEN'},
    ),
    "gniv_selected_domain": EnumProperty(
        name="Picked Domain",
        description="Domain used when the current picker selection was created",
        items=DOMAIN_ITEMS,
        default='POINT',
        options={'HIDDEN'},
    ),
    "gniv_selected_object": StringProperty(
        name="Picked Object",
        description="Object the current picker selection was created on",
        default="",
        options={'HIDDEN'},
    ),
    "gniv_color": FloatVectorProperty(
        name="Colour",
        description="Normal label colour",
        subtype='COLOR',
        size=4,
        default=(1.0, 1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
        update=_update_redraw,
    ),
    "gniv_selected_color": FloatVectorProperty(
        name="Selected Colour",
        description="Colour used for picked/selected index labels",
        subtype='COLOR',
        size=4,
        default=(0.35, 1.0, 0.45, 1.0),
        min=0.0,
        max=1.0,
        update=_update_redraw,
    ),
}


def register():
    for name, prop in _properties.items():
        setattr(bpy.types.Scene, name, prop)


def unregister():
    for name in _properties:
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
