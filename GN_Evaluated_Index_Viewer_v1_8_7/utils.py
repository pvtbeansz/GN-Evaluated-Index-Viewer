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

"""Small helpers shared across the add-on."""

import logging
import sys

import bpy

log = logging.getLogger("gn_index_viewer")
log.propagate = False


def _bind_log_handler():
    """Bind logging to Blender's *current* console stream.

    On Windows the System Console can be opened after the extension was
    imported.  A StreamHandler created at import time can therefore retain a
    stale stderr object and appear silent.  Recreate it whenever debug logging
    is toggled so it follows the console that exists now.
    """
    for handler in list(log.handlers):
        log.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    stream = sys.stderr if sys.stderr is not None else sys.stdout
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("[GNIV] %(levelname)s: %(message)s"))
    log.addHandler(handler)


_bind_log_handler()
log.setLevel(logging.WARNING)


# Guard against a malformed stored string expanding into something enormous.
MAX_PARSED_INDICES = 1_000_000
MAX_INDEX = 2**31 - 1


def set_debug_logging(enabled):
    # Rebind every time: Window > Toggle System Console may have happened after
    # the add-on was imported, especially on Windows.
    _bind_log_handler()
    log.setLevel(logging.DEBUG if enabled else logging.WARNING)
    if enabled:
        # print() is intentional as a one-shot sentinel.  If this line appears
        # but later log lines do not, we know the issue is the logging handler
        # rather than Blender's console itself.
        try:
            print("[GNIV] Debug logging enabled")
        except Exception:
            pass
        log.debug("Logger attached; Blender %s", bpy.app.version_string)


def tag_redraw_all_view3d():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# ---------------------------------------------------------------------------
# Index ranges
# ---------------------------------------------------------------------------


def compress_ranges(values):
    """Turn [1, 2, 3, 7, 9, 10] into [(1, 3), (7, 7), (9, 10)]."""
    values = sorted(set(int(v) for v in values))
    if not values:
        return []
    ranges = []
    start = prev = values[0]
    for value in values[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append((start, prev))
        start = prev = value
    ranges.append((start, prev))
    return ranges


def format_index_string(values):
    """Serialise indices compactly, e.g. ``0-512,700,900-1200``.

    Painted selections are frequently tens of thousands of consecutive
    indices; storing them one-by-one produces a multi-hundred-kilobyte
    string that then has to be re-parsed on every viewport redraw.
    """
    parts = []
    for start, end in compress_ranges(values):
        parts.append(str(start) if start == end else "%d-%d" % (start, end))
    return ",".join(parts)


def parse_index_string(raw):
    """Parse ``0-512,700`` (and the legacy ``0,1,2`` form) into a sorted list."""
    raw = (raw or "").replace(";", ",")
    result = set()
    for token in raw.split(","):
        if len(result) >= MAX_PARSED_INDICES:
            log.warning("Stored selection exceeds %d indices; truncating", MAX_PARSED_INDICES)
            break
        token = token.strip()
        if not token:
            continue

        if "-" in token:
            lo_text, _, hi_text = token.partition("-")
            try:
                lo = int(lo_text)
                hi = int(hi_text)
            except ValueError:
                log.debug("Ignoring malformed index range %r", token)
                continue
            if hi < lo:
                lo, hi = hi, lo
            lo = max(lo, 0)
            hi = min(hi, MAX_INDEX)
            if hi < lo:
                continue
            if len(result) + (hi - lo + 1) > MAX_PARSED_INDICES:
                log.warning("Stored selection exceeds %d indices; truncating", MAX_PARSED_INDICES)
                hi = lo + MAX_PARSED_INDICES - len(result) - 1
            result.update(range(lo, hi + 1))
            continue

        try:
            value = int(token)
        except ValueError:
            log.debug("Ignoring malformed index token %r", token)
            continue
        if 0 <= value <= MAX_INDEX:
            result.add(value)

        if len(result) > MAX_PARSED_INDICES:
            log.warning("Stored selection exceeds %d indices; truncating", MAX_PARSED_INDICES)
            break

    return sorted(result)


DOMAIN_LABELS = {
    'POINT': ('Mesh Point', 'Mesh Points'),
    'EDGE': ('Mesh Edge', 'Mesh Edges'),
    'FACE': ('Mesh Face', 'Mesh Faces'),
    'CLOUD': ('Cloud Point', 'Cloud Points'),
    'CURVE_POINT': ('Curve Point', 'Curve Points'),
    'CURVE': ('Curve', 'Curves'),
    'INSTANCE': ('Instance', 'Instances'),
}


def domain_label(domain, plural=False):
    pair = DOMAIN_LABELS.get(domain, ('Element', 'Elements'))
    return pair[1] if plural else pair[0]
