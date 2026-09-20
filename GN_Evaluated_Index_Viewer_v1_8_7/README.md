# GN Evaluated Index Viewer

Displays point, edge, or face indices from the **evaluated** Geometry Nodes
result in the 3D Viewport, and turns painted selections into a compact
Index-based boolean selection node.

Targets **Blender 5.2 or newer**, retaining the existing minimum version.
The supplied v1.8.6 diagnostic log came from **Blender 5.2.2 LTS**.
**v1.8.7 has passed 37 automated logic tests with Blender test doubles; it has
not been run in a real Blender viewport in this review environment.** See
`REVIEW_AND_TESTING.md` for the remaining Blender checks and limitations.

## Install or update

1. In **Preferences > Add-ons**, expand **GN Evaluated Index Viewer**. When
   replacing an older installed copy, use its **Uninstall GN Evaluated Index
   Viewer** button (Blender's native extension uninstaller).
2. Restart Blender after removing the old version so no old Python module
   remains loaded.
3. Use **Install from Disk** in Preferences, select
   `GN_Evaluated_Index_Viewer_v1_8_7.zip`, and enable the extension. Install the
   ZIP itself; do not unpack it first.
4. In a 3D Viewport, open **N > GN Index**, enable the viewer, and choose the
   geometry domain.

The extension ID is unchanged, and the native uninstall button remains in
its expanded Add-ons preferences entry.

## Geometry components

The add-on reads the whole evaluated geometry via `Object.evaluated_geometry()`,
so a Geometry Nodes tree that outputs points, curves or instances is labelled
as well as one that outputs a mesh. The Domain dropdown offers:

| Domain | Component | Blender |
| --- | --- | --- |
| Mesh Points / Edges / Faces | mesh | 5.2+ |
| Cloud Points | point cloud | 5.2+ |
| Curve Points / Curves | curves | 5.2+ |
| Instances | instances | 5.2+ |

The status box lists which components the current evaluated geometry actually
has (`Has: mesh · inst`), so you know what there is to pick before you pick
it. Choosing a domain the geometry doesn't have says so rather than failing
silently.

A legacy mesh-only fallback remains in the code defensively, but Blender
versions below 5.2 are outside the supported/tested range for this release.

Only the mesh has normals, so **backface culling** is unavailable on the
other components. **Depth Test** still works without normals.

For directly matched mesh instances, visible-only picking and label Depth
Test compare the instance's front surface against viewport depth. The origin
may sit inside its own mesh without making the instance unpickable. Another
instance or an opaque object in front still blocks it. Picking remains based
on the projected index/origin position, not the whole object's silhouette.

Unmatched, nested, ambiguous or non-mesh instances retain origin-depth
visibility and can still be unpickable with X-Ray off. The sidebar reports
how many instance surfaces were matched when coverage is incomplete. This
is a known limitation, not a reason to silently select hidden elements.

## Panel layout

- **GN Index Viewer** — the checkbox in the panel header is the single
  show/hide toggle. Domain, refresh, and the index range live here.
- **Pick for Geometry Nodes** — the picker, the current selection, the
  *Only Picked* filter, and *Add Selection to GN*.
- **Display Settings** — *Only Picked*, occlusion, font size, colour, label
  thinning, and the *Max Labels* performance ceiling. Collapsed by default.

**Only Picked.** Off by default. When on, nothing is labelled until you pick
it — the picker still reaches every element, so you aim with the cursor and
the number appears as you go. Useful on dense meshes where a full label pass
is unreadable. It overrides the index range.

**Index Range vs Max Labels.** The index range is deterministic: it decides
*which* indices are drawn, from any camera angle, and it also decides which
slice of a very large mesh is cached at all. Max Labels is a per-viewport
performance ceiling applied last, after culling and thinning, so which labels
survive depends on the view. Use the range to choose what you see; leave Max
Labels alone unless you hit a slowdown.

## Evaluated element picker

The picker always uses the domain currently selected in the add-on.

Click **Pick / Paint** and use:

- **LMB drag** — replace/paint the selection.
- **Shift + LMB drag** — add elements.
- **Ctrl + LMB drag** — remove elements.
- **Enter / Space / Right Mouse** — finish picking and keep the selection.
- **Esc** — finish picking and clear the picked selection.
- **Alt + Z** — toggle viewport X-Ray without leaving pick mode.
- **Middle Mouse / wheel** — viewport navigation remains available.
- Viewport header/tool controls can be clicked while pick mode stays active.
- If an X-Ray-off gesture has no fresh depth snapshot, it leaves the picked
  selection unchanged and asks you to retry after the viewport redraws.

Picking is limited to the indices the viewer is currently showing, so an
active index range narrows what the picker can hit. The *Only Picked* filter
does not narrow it — otherwise nothing would ever be pickable.

**Add Selection to GN** creates an Index-based boolean selection in the
active Geometry Nodes tree. Consecutive indices are compressed into ranges
(`Index >= start` AND `Index <= end`), and the ranges are combined with a
balanced OR tree, all inside one group node.

The generated group grows with the number of *ranges*, not the number of
indices: 5,000 consecutive faces is one range and five nodes, while 50
scattered faces is 50 ranges and around 200. Painting contiguous runs keeps
it small. Pressing it twice for the same indices reuses the
existing generated group instead of leaving `.001` duplicates behind.

## Building the extension

`__pycache__` must not be shipped — the Extensions Platform validator
rejects it.

```bash
cd /path/to/gn_index_viewer
zip -r ../GN_Evaluated_Index_Viewer_v1_8_7.zip . -x '*/__pycache__/*' '__pycache__/*' '*.pyc'
```

Or let Blender do it, which also validates the manifest:

```bash
blender --command extension build --source-dir gn_index_viewer
```

## Licence and redistribution

Copyright 2026 PvtBeans. Licensed **GPL-2.0-or-later** — the full text is in
`LICENSE.txt`, and every source file carries an SPDX header.

Blender add-ons that use the Python API have to be GPL-compatible, so this is
not really a choice. It is worth being clear about what the GPL does and does
not do:

- It **does** require anyone who redistributes this, modified or not, to pass
  on the same freedoms, ship the source, and keep the copyright notices
  intact. Nobody can take it closed-source or add extra restrictions.
- It **does not** forbid charging money. The GPL explicitly permits selling
  copies. What it guarantees is that whoever buys a copy may immediately give
  it away for free, so reselling it is legal but pointless.

The practical protection is that the free original is easy to find. If you
were charged for this add-on, you were overcharged — get it free, and ask
questions, at https://www.youtube.com/@Pvtbeansz

## Troubleshooting

Enable **Debug Logging** in Preferences > Add-ons > GN Evaluated Index Viewer
to print diagnostics to the system console. When enabled you should immediately
see `[GNIV] Debug logging enabled`; if you do not, the console is not receiving
Python output. Rebuild scheduling, Geometry Nodes tree invalidation, cache
rebuilds and picking diagnostics are then reported with a `[GNIV]` prefix.

## Changelog

### v1.8.7

- Fixes self-occlusion of directly matched mesh instances: compare their
  front surfaces with viewport depth rather than their internal origins.
- Matches complete transforms, never depsgraph iteration order; caches copied
  mesh BVHs without retaining evaluated RNA objects.
- Shares the instance-surface correction with the label Depth Test.
- Removes visibility bypasses for missing depth and large selections. Missing
  or stale depth preserves selection and requests a retry.
- Captures the whole viewport region so a fast gesture cannot leave the
  preceding frame's depth rectangle; checks geometry revision and viewport size.
- Uses framebuffer viewport offsets for depth readback, pixel-centre rays,
  and near/far clipping. Removes the large percentage-of-distance depth bias.
- Corrects sidebar-to-window mouse coordinates and prevents concurrent
  pickers; a drag released over the sidebar no longer remains stuck.
- Clears stale timer and live-picker state after opening a file, and refreshes
  dirty geometry before a pick query.
- Avoids inserting a selection into an unrelated pinned Geometry Nodes tree.
- Rolls back partially created node groups when construction fails.
- Enforces stored-selection size and integer bounds; corrects cache-count
  logging and the no-normals status message.
- Includes 37 automated regression tests and explicit live-test instructions.


### v1.8.6

- Diagnostics-only picker tracing when **Debug Logging** is enabled. No selection
  behaviour has been changed from v1.8.4.
- Logs the cached/projected element count, valid screen projections, selection-shape
  hits, visibility method and survivor count, final indices, X-Ray state, active
  selection tool, and stored selection count.
- Intended to isolate intermittent picker failures before changing the selection or
  occlusion algorithms.

### v1.8.4

- Rebinds the debug log handler whenever Debug Logging is toggled, fixing a
  Windows/System Console timing issue where the handler could remain attached
  to a stale stderr stream and appear silent.
- Prints an immediate `[GNIV] Debug logging enabled` sentinel so console output
  can be verified before reproducing a bug.
- Treats Geometry Nodes `NodeTree` depsgraph updates as cache-invalidating. This
  addresses stale evaluated-instance data that could persist until the viewer
  was toggled or another unrelated edit forced a rebuild.
- Adds debug messages for rebuild requests, coalesced timers, timer execution,
  domain changes and the exact depsgraph reason that triggered a rebuild.

### v1.8.3

- Raises the officially supported minimum Blender version to **5.2.0**.
- Documents **Blender 5.2.2 LTS** as the currently tested release.
- Removes current UI/documentation claims of support for untested Blender
  4.x versions. The legacy mesh fallback remains in the code, but is not part
  of the supported compatibility promise.

### v1.8.2

- Adds a **Selected Colour** control in Display Settings. Picked/selected index
  labels now use this user-configurable colour instead of a hard-coded green.
- The default remains the existing green, so upgrading does not change the
  viewer's appearance unless the setting is customised.

### v1.8.1

- Fixes Blender 4.5+ evaluated-geometry components becoming invalid immediately
  after discovery. The `GeometrySet` returned by `evaluated_geometry()` is now
  kept alive until mesh/point-cloud/curve/instance data has been copied into
  the add-on cache.
- Adds defensive `ReferenceError` handling around component discovery/counting
  and extraction so an invalidated RNA datablock reports a cache error instead
  of crashing the picker.

### v1.8.0

- Reads every component of the evaluated geometry on Blender 4.5+, not just
  the mesh: point clouds, curve control points, whole curves, and instances.
  Blender 4.2–4.4 keep the previous mesh-only behaviour, detected at runtime
  rather than gated behind a raised minimum version.
- Instance indices are read from the `instance_transform` attribute. The flat
  matrix layout is detected at runtime rather than assumed, since row-major
  and column-major put the translation in different places.
- Curve positions come from the `position` attribute, and whole-curve
  positions are the mean of each curve's control points, using
  `curve_offset_data` where available and walking curve slices where not.
- The status box lists the components the evaluated geometry actually has.
- No more `to_mesh_clear()` on the 4.5+ path — GeometrySet components are
  owned by the depsgraph and must not be freed.

### v1.7.3

- **Esc** now clears the picked selection and exits, taking over what
  Shift+Esc did. Right Mouse, Enter and Space are the keep-and-exit route.
  Shift+Esc is gone — Esc and Right Mouse were doing the same thing, so the
  confirm and cancel actions now sit on separate keys as they do elsewhere
  in Blender.

### v1.7.2

- The generated selection group combines ranges with a balanced OR tree
  instead of a linear chain. Node count is unchanged, but depth drops from
  *n* to log2(*n*) — 30 ranges go from 31 levels deep to 7 — and the layout
  reads as a tree instead of a long diagonal staircase.
- Above 8 ranges the Compare and Boolean nodes are collapsed, so a fragmented
  selection stays compact rather than sprawling across several screens.
- Above 64 ranges the operator warns that the selection is fragmented, since
  the group grows with the number of ranges, not the number of indices.
- Removed explanatory note rows from the sidebar — Blender truncates labels
  rather than wrapping them, so they were being cut off. The information
  lives in the tooltips. Picker hints shortened for the same reason.

### v1.7.1

- Labels are drawn centred on their element instead of offset up-and-right.
  The old offset meant a number could look like it was inside a selection box
  while its actual anchor sat outside, so a column of faces would appear to be
  skipped. What you aim at is now what gets picked.
- Box selection grows its edges by 4px (scaled by interface scale) so an edge
  drawn through the middle of a number still catches it.
- **Only Picked** moved to Display Settings and is available at all times, not
  just when something is already picked. With it on and nothing picked, no
  labels are drawn at all — they appear as you pick them, including the
  element under the cursor. Off by default.

### v1.7.0

- Picker occlusion no longer ray-casts once per candidate element. A depth
  snapshot taken by the draw callback is sampled instead, so box and lasso
  selections over dense meshes stay responsive. Ray-casting remains as a
  fallback when the depth buffer is unavailable, capped at 1500 candidates.
- The painted selection lives in memory during the modal and is written to
  the scene exactly once, on exit, as compact ranges (`0-512,700`) rather
  than one integer per index.
- The picked selection is now tied to the object it was picked on, not just
  the domain. Switching object no longer mis-highlights or mis-applies it.
- New **Only Picked** filter.
- Display settings moved into their own collapsible sub-panel; the duplicate
  show/hide button at the bottom of the panel is gone.
- Depsgraph updates are filtered, so unrelated scene changes no longer
  trigger a full re-extraction of the evaluated mesh, and rebuilds are
  deferred out of the handler.
- Labels respect interface scaling on HiDPI displays.
- Range and selection filtering happen before projection, not after.
- The gesture overlay only draws in the viewport the picker is running in.
- Enter/Space/Right Mouse now check for a key press rather than firing on
  release as well.
- Changing domain or active object mid-pick cancels cleanly.
- Disabling the add-on while the picker runs no longer leaves a modal
  attached to an unregistered class.
- Max Labels ceiling lowered from 100,000 to 20,000.
- Broad `except: pass` handlers replaced with narrow, logged handlers.
- `bl_info` removed; the manifest is the single source of metadata.

### v1.6.1

- Esc exits while preserving the current picked selection; Shift+Esc exits
  and clears it.
- Alt+Z and viewport chrome usable while the picker remains active.
- Picked indices are highlighted by recolouring existing labels rather than
  drawing duplicate text, and are exempt from label thinning.
- Preferences gained an **Uninstall** button using Blender's native
  extension uninstaller.

### v1.5.0

- Picker respects the active Select Box, Circle, or Lasso tool.
- Viewport X-Ray controls whether hidden evaluated elements can be selected.
- Generated logic is packaged inside one Geometry Nodes group node.
