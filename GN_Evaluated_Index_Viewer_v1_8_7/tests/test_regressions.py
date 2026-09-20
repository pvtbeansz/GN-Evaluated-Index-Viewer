# SPDX-FileCopyrightText: 2026 PvtBeans
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run with normal Python + NumPy. Blender interfaces are test doubles.

These are logic/regression tests, not a Blender GPU or installation test.
"""
import importlib
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def module(name, **attributes):
    result = types.ModuleType(name)
    result.__dict__.update(attributes)
    sys.modules[name] = result
    return result


class Operator:
    def report(self, kind, text):
        self.last_report = (kind, text)

    @classmethod
    def poll_message_set(cls, message):
        cls.message = message


class KDTree:
    def __init__(self, count):
        self.points = []

    def insert(self, point, index):
        self.points.append((np.array(point), index))

    def balance(self):
        pass

    def find_range(self, point, radius):
        return [(p, i, float(np.linalg.norm(p - point))) for p, i in self.points
                if np.linalg.norm(p - point) <= radius]


class BoxBVH:
    """Independent slab intersection for the cube fixtures, including holes via None."""
    @classmethod
    def FromPolygons(cls, vertices, faces, all_triangles=False):
        return cls(np.min(vertices, axis=0), np.max(vertices, axis=0))

    def __init__(self, lower=(-.5, -.5, -.5), upper=(.5, .5, .5)):
        self.lower, self.upper = np.array(lower), np.array(upper)

    def ray_cast(self, origin, direction):
        origin, direction = np.array(origin), np.array(direction)
        near, far = -np.inf, np.inf
        for axis in range(3):
            if abs(direction[axis]) < 1e-12:
                if not self.lower[axis] <= origin[axis] <= self.upper[axis]:
                    return None, None, None, None
            else:
                a = (self.lower[axis] - origin[axis]) / direction[axis]
                b = (self.upper[axis] - origin[axis]) / direction[axis]
                near, far = max(near, min(a, b)), min(far, max(a, b))
        if far < max(0, near):
            return None, None, None, None
        distance = near if near >= 0 else far
        return origin + direction * distance, None, 0, distance


bpy_types = module('bpy.types', Operator=Operator, NodeTree=type('NodeTree', (), {}))
app_handlers = module('bpy.app.handlers', persistent=lambda function: function,
                      depsgraph_update_post=[], frame_change_post=[], load_post=[])
app = module('bpy.app', handlers=app_handlers, version_string='TEST DOUBLE')
bpy = module('bpy', types=bpy_types, app=app, context=NS(window_manager=None), data=NS())
gpu = module('gpu', state=NS())
module('blf')
module('bpy_extras', view3d_utils=NS())
module('gpu_extras')
module('gpu_extras.batch', batch_for_shader=lambda *a, **k: None)
module('mathutils', Vector=lambda value: np.asarray(value))
module('mathutils.bvhtree', BVHTree=BoxBVH)
module('mathutils.kdtree', KDTree=KDTree)
# Bypass __init__/register, which are intentionally Blender-only.
package = module('gn_index_viewer')
package.__path__ = [str(ROOT)]
utils = importlib.import_module('gn_index_viewer.utils')
project = importlib.import_module('gn_index_viewer.project')
visibility = importlib.import_module('gn_index_viewer.visibility')
cache = importlib.import_module('gn_index_viewer.cache')
picker = importlib.import_module('gn_index_viewer.picker')
draw = importlib.import_module('gn_index_viewer.draw')
handlers = importlib.import_module('gn_index_viewer.handlers')


def context(domain='INSTANCE'):
    region = NS(width=200, height=200, x=70, y=40, type='WINDOW', as_pointer=lambda: 7)
    return NS(region=region, region_data=NS(perspective_matrix=np.eye(4)),
              scene=NS(gniv_domain=domain),
              space_data=NS(type='VIEW_3D', shading=NS(type='SOLID', show_xray=False)))


def transform(x=0, y=0, z=0, scale=.2):
    matrix = np.diag([scale, scale, scale, 1.0])
    matrix[:3, 3] = (x, y, z)
    return matrix


def snapshot(ctx, depth):
    picker._depth.update(buf=np.full((200, 200), depth, dtype=np.float32),
                         rect=(0, 0, 200, 200), region=7,
                         persp=np.eye(4).tobytes(), revision=cache.cache['revision'], size=(200, 200))


class ProjectionTests(unittest.TestCase):
    def test_missing_depth_is_not_visible(self):
        actual = project.visible_against_depth(np.array([[.5]]), 0, 0,
                                               np.array([0., 3.]), np.array([0., 3.]), np.array([.4, .4]))
        self.assertEqual(actual.tolist(), [True, False])

    def test_no_percentage_bias_selects_far_side_of_thin_geometry(self):
        ctx = context()
        positions = np.array([[0., 0., .0001]])
        actual = project.window_depths(positions, ctx.region, ctx.region_data)
        self.assertAlmostEqual(actual[0], .50005)
        self.assertFalse(project.visible_against_depth(np.array([[.5]]), 0, 0,
                         np.array([0.]), np.array([0.]), actual)[0])

    def test_near_far_clipping_and_nan(self):
        ctx = context()
        positions = np.array([[0, 0, -2], [0, 0, 0], [0, 0, 2], [np.nan, 0, 0]])
        self.assertEqual(project.project(positions, ctx.region, ctx.region_data)[3].tolist(),
                         [False, True, False, False])

    def test_depth_read_uses_framebuffer_viewport_offset(self):
        calls = []
        fb = NS(read_depth=lambda *args: calls.append(args) or np.ones((2, 3)))
        with patch.object(gpu, 'state', NS(viewport_get=lambda: (70, 40, 200, 200),
                                         active_framebuffer_get=lambda: fb)):
            depth = project.read_depth_rect(5, 6, 3, 2)
        self.assertEqual(calls, [(75, 46, 3, 2)])
        self.assertEqual(depth.shape, (2, 3))

    def test_orthographic_rays_start_at_near_plane(self):
        ctx = context()
        origins, directions, valid = project.screen_rays(np.array([100.]), np.array([100.]),
                                                         ctx.region, ctx.region_data)
        np.testing.assert_allclose(origins[0], [.005, .005, -1])
        np.testing.assert_allclose(directions[0], [0, 0, 1])
        self.assertTrue(valid[0])

    def test_perspective_rays_hit_pixel_ray(self):
        ctx = context()
        n, f = .1, 100.
        ctx.region_data.perspective_matrix = np.array([
            [1,0,0,0], [0,1,0,0], [0,0,-(f+n)/(f-n),-2*f*n/(f-n)], [0,0,-1,0]])
        origins, directions, valid = project.screen_rays(np.array([100.]), np.array([100.]),
                                                         ctx.region, ctx.region_data)
        self.assertTrue(valid[0])
        self.assertAlmostEqual(origins[0, 2], -.1)
        self.assertLess(directions[0, 2], 0)
        sx, sy, _, _ = project.project(origins + directions * 5, ctx.region, ctx.region_data)
        np.testing.assert_allclose([sx[0], sy[0]], [100.5, 100.5])


class InstancePickingTests(unittest.TestCase):
    def setUp(self):
        cache.reset()
        picker.reset_runtime()
        self.ctx = context()

    def setup_surfaces(self, matrices):
        cache.cache['instance_surfaces'] = {i: (BoxBVH(), np.linalg.inv(m), m)
                                            for i, m in enumerate(matrices)}
        self.positions = np.array([m[:3, 3] for m in matrices])
        self.indices = np.arange(len(matrices))
        self.sx, self.sy, _, _ = project.project(self.positions, self.ctx.region, self.ctx.region_data)

    def filter(self):
        return picker._filter_visible_rows(self.ctx, self.indices, self.sx, self.sy,
                                            self.positions, indices=self.indices).tolist()

    def test_eight_visible_cube_instances_are_not_self_occluded(self):
        self.setup_surfaces([transform(x=x, scale=.1) for x in np.linspace(-.8, .8, 8)])
        snapshot(self.ctx, .475)
        # The old algorithm rejects every origin (depth .5) against .475.
        self.assertFalse(project.visible_against_depth(picker._depth['buf'], 0, 0, self.sx, self.sy,
                                                       np.full(8, .5)).any())
        self.assertEqual(self.filter(), list(range(8)))

    def test_front_instance_blocks_back_instance_with_same_source(self):
        self.setup_surfaces([transform(z=-.3), transform(z=.3)])
        snapshot(self.ctx, .3)
        self.assertEqual(self.filter(), [0])

    def test_separate_foreground_object_blocks_all_instances(self):
        self.setup_surfaces([transform(z=-.3), transform(z=.3)])
        snapshot(self.ctx, .2)
        self.assertEqual(self.filter(), [])

    def test_xray_is_the_explicit_through_selection_path(self):
        self.setup_surfaces([transform(z=-.3), transform(z=.3)])
        snapshot(self.ctx, .2)
        self.ctx.space_data.shading.show_xray = True
        self.assertEqual(self.filter(), [0, 1])

    def test_negative_scale_and_rotation_have_surface_depth(self):
        angle = .6
        rotation = np.array([[np.cos(angle),0,np.sin(angle),0], [0,1,0,0],
                             [-np.sin(angle),0,np.cos(angle),0], [0,0,0,1]])
        matrix = rotation @ np.diag([-.5, .3, .2, 1.])
        self.setup_surfaces([matrix])
        targets, hits = visibility.surface_positions(cache.cache['instance_surfaces'], self.indices,
                        self.positions, self.sx, self.sy, self.ctx.region, self.ctx.region_data)
        self.assertTrue(hits[0])
        self.assertLess(targets[0,2], 0)
        snapshot(self.ctx, float(project.window_depths(targets, self.ctx.region, self.ctx.region_data)[0]))
        self.assertEqual(self.filter(), [0])

    def test_unmapped_instance_does_not_gain_visibility(self):
        self.setup_surfaces([transform()])
        cache.cache['instance_surfaces'] = {}
        snapshot(self.ctx, .4)
        self.assertEqual(self.filter(), [])

    def test_missing_depth_preserves_selection_by_aborting_query(self):
        self.setup_surfaces([transform()])
        picker._live['values'] = {17}
        with self.assertRaises(picker.VisibilityUnavailable):
            self.filter()
        self.assertEqual(picker._live['values'], {17})

    def test_old_geometry_revision_invalidates_snapshot(self):
        self.setup_surfaces([transform()])
        snapshot(self.ctx, .4)
        cache.cache['revision'] += 1
        with self.assertRaises(picker.VisibilityUnavailable):
            self.filter()

    def test_resized_view_invalidates_snapshot(self):
        self.setup_surfaces([transform()])
        snapshot(self.ctx, .4)
        self.ctx.region.width += 1
        with self.assertRaises(picker.VisibilityUnavailable):
            self.filter()

    def test_large_missing_depth_does_not_bypass_occlusion(self):
        self.ctx.scene.gniv_domain = 'POINT'
        rows = np.arange(1501)
        with self.assertRaises(picker.VisibilityUnavailable):
            picker._filter_visible_rows(self.ctx, rows, np.zeros(len(rows)), np.zeros(len(rows)),
                                         np.zeros((len(rows), 3)))

    def test_box_circle_and_lasso_share_instance_surface_visibility(self):
        self.setup_surfaces([transform(x=-.4), transform(x=.4)])
        snapshot(self.ctx, .45)
        self.ctx.active_object = NS(name='Cube')
        self.ctx.scene.gniv_use_range = False
        cache.cache.update(object='Cube', domain='INSTANCE', dirty=False,
                           positions=self.positions, indices=self.indices)
        self.assertEqual(picker._elements_in_box(self.ctx,(0,0),(200,200))[0], [0,1])
        self.assertEqual(picker._elements_in_radius(self.ctx,60,100,15)[0], [0])
        self.assertEqual(picker._elements_in_lasso(self.ctx,[(120,80),(160,80),(160,120),(120,120)])[0], [1])

    def test_nonzero_cached_indices_use_correct_surfaces(self):
        self.setup_surfaces([transform(z=-.3), transform(z=.3)])
        self.indices = np.array([200001,200003])
        cache.cache['instance_surfaces'] = {200001: cache.cache['instance_surfaces'][0],
                                            200003: cache.cache['instance_surfaces'][1]}
        snapshot(self.ctx,.3)
        result = picker._filter_visible_rows(self.ctx,np.array([0,1]),self.sx,self.sy,
                                            self.positions,indices=self.indices)
        self.assertEqual(result.tolist(),[0])

    def test_material_preview_does_not_inherit_solid_xray(self):
        self.ctx.space_data.shading.type = 'MATERIAL'
        self.ctx.space_data.shading.show_xray = True
        self.assertFalse(picker._xray_enabled(self.ctx))


class SurfaceMappingTests(unittest.TestCase):
    def make_scene(self, matrices):
        owner = NS(original=object())
        mesh = NS(vertices=[NS(co=p) for p in [(-.5,-.5,-.5), (.5,-.5,-.5),
                  (.5,.5,-.5), (-.5,.5,-.5), (-.5,-.5,.5), (.5,-.5,.5),
                  (.5,.5,.5), (-.5,.5,.5)]], polygons=[NS(vertices=(0,1,2,3))],
                  loop_triangles=[NS(vertices=(0,1,2)), NS(vertices=(0,2,3))],
                  calc_loop_triangles=lambda: None)
        released = []
        obj = NS(type='MESH', as_pointer=lambda: 42, to_mesh=lambda: mesh,
                 to_mesh_clear=lambda: released.append(True))
        entries = [NS(is_instance=True, parent=owner, object=obj, matrix_world=m) for m in matrices]
        return NS(object_instances=entries), owner, released

    def test_maps_transforms_not_iterator_order_and_releases_mesh(self):
        matrices = [transform(x=-.5), transform(x=.5)]
        depsgraph, owner, released = self.make_scene(matrices[::-1])
        surfaces = visibility.build_instance_surfaces(depsgraph, owner, np.array(matrices), start=100)
        self.assertEqual(set(surfaces), {100,101})
        np.testing.assert_allclose(surfaces[100][2], matrices[0])
        self.assertEqual(len(released), 1)

    def test_identical_origins_different_rotations_are_not_confused(self):
        matrices = [transform(), transform()]
        matrices[1][0,0] *= -1
        depsgraph, owner, _ = self.make_scene(matrices[::-1])
        surfaces = visibility.build_instance_surfaces(depsgraph, owner, np.array(matrices))
        self.assertEqual(set(surfaces), {0,1})

    def test_ambiguous_coincident_references_are_not_guessed(self):
        matrices = [transform(), transform()]
        depsgraph, owner, _ = self.make_scene(matrices)
        self.assertEqual(visibility.build_instance_surfaces(depsgraph, owner, np.array(matrices)), {})

    def test_singular_transforms_do_not_crash(self):
        matrices = [transform(scale=0)]
        depsgraph, owner, _ = self.make_scene(matrices)
        self.assertEqual(visibility.build_instance_surfaces(depsgraph, owner, np.array(matrices)), {})

    def test_unrelated_owner_is_not_mapped(self):
        matrices = [transform()]
        depsgraph, owner, _ = self.make_scene(matrices)
        other = NS(original=object())
        self.assertEqual(visibility.build_instance_surfaces(depsgraph, other, np.array(matrices)), {})

    def test_surface_budget_falls_back_conservatively(self):
        matrices = [transform()]
        depsgraph, owner, _ = self.make_scene(matrices)
        with patch.object(visibility, 'MAX_SURFACE_TRIANGLES', 0):
            self.assertEqual(visibility.build_instance_surfaces(depsgraph, owner, np.array(matrices)), {})


class InstanceMatrixTests(unittest.TestCase):
    def test_bulk_and_rna_matrix_layouts_include_rotation_at_origin(self):
        matrices = np.array([np.eye(4), [[0,-1,0,0],[1,0,0,0],[0,0,1,0],[0,0,0,1]], transform(x=.5)])
        for transpose in (False, True):
            class Data:
                def __len__(self): return len(matrices)
                def __getitem__(self, i): return NS(value=matrices[i])
                def foreach_get(self, field, buffer):
                    buffer[:] = (matrices.transpose(0,2,1) if transpose else matrices).ravel()
            instances = NS(attributes={'instance_transform': NS(data=Data())})
            np.testing.assert_allclose(cache._instance_transforms(instances), matrices)


class StateTests(unittest.TestCase):
    def setUp(self):
        picker.reset_runtime()

    def test_sidebar_mouse_coordinates_use_window_origin(self):
        ctx = context()
        event = NS(mouse_x=120, mouse_y=100, mouse_region_x=-700, mouse_region_y=5)
        self.assertEqual(picker._mouse_in_region(ctx,event), (50.,60.))

    def test_second_picker_cannot_start(self):
        picker._live['active'] = True
        self.assertFalse(picker.GNIV_OT_pick_elements.poll(NS()))

    def test_release_over_sidebar_ends_drag_without_picking(self):
        ctx = context()
        sidebar = NS(type='UI',x=220,y=40,width=50,height=200)
        ctx.area = NS(type='VIEW_3D', regions=[ctx.region,sidebar])
        op = picker.GNIV_OT_pick_elements()
        op._view_region = ctx.region
        self.assertFalse(op._event_inside_view_window(ctx, NS(mouse_x=230,mouse_y=80)))

    def test_file_load_clears_stale_timer_flag_and_live_selection(self):
        registered = set()
        app.timers = NS(is_registered=lambda fn: fn in registered,
                        unregister=lambda fn: registered.discard(fn),
                        register=lambda fn, **kwargs: registered.add(fn))
        cache._timer_pending = True
        picker._live['active'] = True
        picker._live['values'] = {12}
        bpy.context.scene = NS(gniv_show_indices=True)
        with patch.object(draw, 'ensure_draw_handler'):
            handlers.on_load_post(None)
        self.assertIn(cache._rebuild_timer, registered)
        self.assertFalse(picker.is_active())
        self.assertEqual(picker.live_values(), set())
        cache.cancel_pending()

    def test_unrelated_pinned_node_editor_is_not_targeted(self):
        base, unrelated = object(), object()
        space = NS(tree_type='GeometryNodeTree',edit_tree=unrelated,node_tree=unrelated)
        ctx = NS(screen=NS(areas=[NS(type='NODE_EDITOR',spaces=NS(active=space))]))
        self.assertEqual(picker._visible_geometry_node_editor(ctx,base), (None,None))

    def test_nested_editor_of_active_tree_is_targeted(self):
        base, nested = object(), object()
        space = NS(tree_type='GeometryNodeTree',edit_tree=nested,node_tree=base,cursor_location=(8,9))
        ctx = NS(screen=NS(areas=[NS(type='NODE_EDITOR',spaces=NS(active=space))]))
        self.assertEqual(picker._visible_geometry_node_editor(ctx,base), (nested,(8,9)))

    def test_failed_group_build_is_rolled_back(self):
        class Group(dict):
            interface = NS(new_socket=lambda **kw: (_ for _ in ()).throw(RuntimeError('injected failure')))
        class Groups(list):
            def new(self, **kw):
                group = Group(); self.append(group); return group
            def remove(self, group, **kw): super().remove(group)
        bpy.data.node_groups = Groups()
        with self.assertRaisesRegex(RuntimeError, 'injected failure'):
            picker._build_selection_group([1,2], 'POINT')
        self.assertEqual(bpy.data.node_groups, [])


class SelectionStringTests(unittest.TestCase):
    def test_range_roundtrip(self):
        values = [0,1,2,7,9,10]
        self.assertEqual(utils.parse_index_string(utils.format_index_string(values)), values)

    def test_multiple_ranges_cannot_overflow_capacity(self):
        with patch.object(utils, 'MAX_PARSED_INDICES', 4):
            self.assertEqual(utils.parse_index_string('0-100,200-300,900,1000-2000'), [0,1,2,3])

    def test_out_of_integer_domain_values_are_ignored_or_clamped(self):
        self.assertEqual(utils.parse_index_string('0,999999999999999999999999,2147483647-2147483650'),
                         [0,2147483647])

    def test_malformed_and_reversed_ranges(self):
        self.assertEqual(utils.parse_index_string('x,-1,4-2,;;0'), [0,2,3,4])


if __name__ == '__main__':
    unittest.main(verbosity=2)
