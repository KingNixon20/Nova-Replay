#!/usr/bin/env python3
import os
import sys
import subprocess
import threading
import gi
gi.require_version('Gtk', '3.0')
# Import pycairo immediately after requiring Gtk so the GI foreign struct converter
# for cairo.Context is registered before GDK/GTK types are loaded.
try:
    import cairo
except Exception:
    sys.stderr.write("Missing required Python 'cairo' module (pycairo).\n")
    sys.stderr.write("Install on Debian/Ubuntu: sudo apt install python3-cairo python3-gi-cairo\n")
    sys.stderr.write("Or install via pip: pip3 install pycairo\n")
    raise
# sanity check: ensure the module provides cairo.Context
if not hasattr(cairo, 'Context'):
    sys.stderr.write("Imported 'cairo' does not expose 'Context' type; ensure pycairo is installed (not gi.repository.cairo).\n")
    raise ImportError("pycairo missing or incorrect module imported")
try:
    gi.require_version('Gst', '1.0')
except Exception:
    pass
from gi.repository import Gtk, GLib, Gdk, Pango, GdkPixbuf, PangoCairo
try:
    from gi.repository import Gst
    Gst.init(None)
except Exception:
    Gst = None
try:
    from gi.repository import Notify
    try:
        Notify.init("Nova Replay")
    except Exception:
        # some environments initialize differently; ignore if it fails
        pass
except Exception:
    Notify = None
import time
import recorder
import json
from thumbnail_renderer import render_decorated_thumbnail
import shutil
import uuid
import math
from collections import deque
import copy

from datetime import datetime, timezone


def move_to_trash(path: str) -> str:
    """Move a file to the FreeDesktop Trash location and write a .trashinfo file.

    Returns the destination path in the trash on success or raises on error.
    """
    trash_home = os.path.expanduser(os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share')))
    trash_dir = os.path.join(trash_home, 'Trash')
    files_dir = os.path.join(trash_dir, 'files')
    info_dir = os.path.join(trash_dir, 'info')
    os.makedirs(files_dir, exist_ok=True)
    os.makedirs(info_dir, exist_ok=True)

    base = os.path.basename(path)
    name, ext = os.path.splitext(base)
    dest = base
    i = 1
    while os.path.exists(os.path.join(files_dir, dest)):
        dest = f"{name}_{i}{ext}"
        i += 1

    dest_path = os.path.join(files_dir, dest)
    shutil.move(path, dest_path)

    info_path = os.path.join(info_dir, dest + '.trashinfo')
    try:
            with open(info_path, 'w') as f:
                f.write('[Trash Info]\n')
                f.write(f'Path={os.path.abspath(path)}\n')
                f.write(f'DeletionDate={datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") }\n')
    except Exception:
        pass

    return dest_path


class HotkeyManager:
    """Best-effort global hotkey registration using `pynput`.

    Registers Ctrl+Alt+R to call the provided toggle callback. If `pynput`
    is not available, this becomes a no-op.
    """

    def __init__(self, toggle_callback):
        self.toggle_callback = toggle_callback
        self.hk = None
        self.thread = None
        try:
            from pynput.keyboard import GlobalHotKeys
            self.GlobalHotKeys = GlobalHotKeys
        except Exception:
            self.GlobalHotKeys = None

    def start(self):
        if not self.GlobalHotKeys:
            return

        def on_activate_toggle():
            try:
                self.toggle_callback()
            except Exception:
                pass

        self.hk = self.GlobalHotKeys({'<ctrl>+<alt>+r': on_activate_toggle})
        self.thread = threading.Thread(target=self.hk.run, daemon=True)
        self.thread.start()

    def stop(self):
        if self.hk:
            try:
                self.hk.stop()
            except Exception:
                pass
            self.hk = None


class Timeline(Gtk.DrawingArea):
    """Unified timeline with trim handles and playhead.

    - Supports set_range(min_sec, max_sec) and set_value(sec) for compatibility
    - `on_seek(sec)` callback for seek requests
    - `on_changed(start,end)` callback when trim handles are changed
    """
    def __init__(self):
        super().__init__()
        self.items = []
        self.project_start = 0.0
        self.total = 1.0
        self.set_size_request(-1, 64)
        self.connect('draw', self._on_draw)
        self.connect('button-press-event', self._on_press)
        self.connect('button-release-event', self._on_release)
        self.connect('motion-notify-event', self._on_motion)
        self.set_events(self.get_events() | Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK)

        # playhead (seconds)
        self.playhead = 0.0
        # trim handles (seconds)
        self.trim_start = 0.0
        self.trim_end = 0.0

        # callbacks
        self.on_seek = None
        self.on_changed = None

        # drag state
        self._dragging = False
        self._drag_type = None  # 'left', 'right', 'playhead', None
        self._drag_start_x = 0
        self._block_callbacks = set()

        # visual params
        self._pad = 6
        self._handle_w = 10
        self._playhead_w = 2

    def set_items(self, items):
        self.items = list(items or [])
        # compute project range if items provided
        if self.items:
            min_start = None
            max_end = None
            for it in self.items:
                try:
                    d = float(it.get('duration', 0) or 0)
                except Exception:
                    d = 0.0
                s = float(it.get('start', 0) or 0)
                end = s + max(0.0, d)
                if min_start is None or s < min_start:
                    min_start = s
                if max_end is None or end > max_end:
                    max_end = end
            self.project_start = min_start or 0.0
            self.total = max(1.0, (max_end or 0.0) - self.project_start)
        self.queue_draw()

    # Compatibility methods used elsewhere in the app
    def set_range(self, a, b):
        try:
            self.project_start = float(a)
            self.total = max(1.0, float(b) - float(a))
            # clamp trims and playhead
            if self.trim_start < self.project_start:
                self.trim_start = self.project_start
            if self.trim_end <= self.project_start:
                self.trim_end = self.project_start + self.total
            if self.playhead < self.project_start or self.playhead > self.project_start + self.total:
                self.playhead = self.project_start
        except Exception:
            pass
        self.queue_draw()

    def set_value(self, sec):
        try:
            self.playhead = float(sec)
        except Exception:
            self.playhead = self.project_start
        self.queue_draw()

    def set_value_silent(self, sec):
        """Set playhead without forcing a redraw every call. Will redraw at most
        once every 250ms to keep UI responsive without flooding the main loop."""
        try:
            self.playhead = float(sec)
        except Exception:
            self.playhead = self.project_start
        try:
            now = time.time()
            last = getattr(self, '_last_draw', 0)
            if now - last > 0.25:
                self.queue_draw()
                self._last_draw = now
        except Exception:
            pass

    def get_trim(self):
        return (self.trim_start, self.trim_end)

    def set_trim(self, s, e):
        try:
            s = float(s)
            e = float(e)
            if s < self.project_start:
                s = self.project_start
            if e > self.project_start + self.total:
                e = self.project_start + self.total
            if e < s:
                e = s
            self.trim_start = s
            self.trim_end = e
            if self.on_changed and 'on_changed' not in self._block_callbacks:
                try:
                    self.on_changed(s, e)
                except Exception:
                    pass
        except Exception:
            pass
        self.queue_draw()

    def handler_block_by_func(self, func):
        # emulate Gtk.Range handler blocking used elsewhere; accept functions loosely
        try:
            name = getattr(func, '__name__', str(func))
            self._block_callbacks.add(name)
        except Exception:
            pass

    def handler_unblock_by_func(self, func):
        try:
            name = getattr(func, '__name__', str(func))
            if name in self._block_callbacks:
                self._block_callbacks.remove(name)
        except Exception:
            pass

    def _sec_to_x(self, sec, w):
        if self.total <= 0:
            return 0
        return int(((sec - self.project_start) / self.total) * w)

    def _x_to_sec(self, x, w):
        return (x / float(w)) * self.total + self.project_start if w > 0 else self.project_start

    def _on_draw(self, widget, cr):
        alloc = self.get_allocation()
        w, h = alloc.width, alloc.height
        # background
        cr.set_source_rgb(0.06, 0.06, 0.06)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        pad = self._pad
        track_h = h - pad * 2

        # draw full track
        cr.set_source_rgb(0.12, 0.12, 0.12)
        cr.rectangle(pad, pad, max(4, w - pad * 2), track_h)
        cr.fill()

        # draw trim range
        try:
            ts = max(self.project_start, min(self.trim_start, self.project_start + self.total))
            te = max(self.project_start, min(self.trim_end or (self.project_start + self.total), self.project_start + self.total))
            x1 = pad + self._sec_to_x(ts, w - pad * 2)
            x2 = pad + self._sec_to_x(te, w - pad * 2)
            cr.set_source_rgba(0.0, 0.6, 0.9, 0.25)
            cr.rectangle(x1, pad, max(2, x2 - x1), track_h)
            cr.fill()
            # draw handles
            cr.set_source_rgb(0.0, 0.6, 0.9)
            cr.rectangle(x1 - (self._handle_w//2), pad, self._handle_w, track_h)
            cr.rectangle(x2 - (self._handle_w//2), pad, self._handle_w, track_h)
            cr.fill()
        except Exception:
            pass

        # draw items if present (project timeline)
        if self.items and self.total > 0:
            for i, it in enumerate(self.items):
                try:
                    s = float(it.get('start', 0) or 0)
                    dur = float(it.get('duration', 0) or 0)
                    if dur <= 0:
                        continue
                    rel_start = (s - self.project_start) / self.total if self.total > 0 else 0
                    rel_end = ((s + dur) - self.project_start) / self.total if self.total > 0 else rel_start
                    x1 = pad + int(rel_start * (w - pad * 2))
                    x2 = pad + int(rel_end * (w - pad * 2))
                    width = max(4, x2 - x1)
                    base = 0.12 + ((i % 4) * 0.04)
                    cr.set_source_rgb(base, 0.18, 0.22)
                    cr.rectangle(x1 + 1, pad, width - 2, track_h)
                    cr.fill()
                except Exception:
                    pass

        # draw playhead
        try:
            px = pad + self._sec_to_x(self.playhead, w - pad * 2)
            cr.set_source_rgb(1, 1, 1)
            cr.rectangle(px - (self._playhead_w//2), pad, self._playhead_w, track_h)
            cr.fill()
        except Exception:
            pass

        return False

    def _on_press(self, widget, event):
        try:
            if event.type != Gdk.EventType.BUTTON_PRESS:
                return False
            alloc = self.get_allocation()
            w = alloc.width - self._pad * 2
            x = int(event.x - self._pad)
            # determine if click near left or right handle
            left_x = self._sec_to_x(self.trim_start, w)
            right_x = self._sec_to_x(self.trim_end or (self.project_start + self.total), w)
            # tolerance
            tol = max(8, self._handle_w)
            if abs(x - left_x) <= tol:
                self._drag_type = 'left'
            elif abs(x - right_x) <= tol:
                self._drag_type = 'right'
            else:
                # click-to-seek: update playhead and call on_seek
                sec = self._x_to_sec(max(0, min(w, x)), w)
                self.playhead = sec
                if self.on_seek and 'on_seek' not in self._block_callbacks:
                    try:
                        self.on_seek(sec)
                    except Exception:
                        pass
                self._drag_type = 'playhead'
            self._dragging = True
            self._drag_start_x = x
            self.queue_draw()
        except Exception:
            pass
        return True

    def _on_motion(self, widget, event):
        try:
            if not self._dragging:
                return False
            alloc = self.get_allocation()
            w = alloc.width - self._pad * 2
            x = int(event.x - self._pad)
            x = max(0, min(w, x))
            sec = self._x_to_sec(x, w)
            if self._drag_type == 'left':
                # clamp
                new_s = min(sec, self.trim_end if self.trim_end else (self.project_start + self.total))
                self.trim_start = max(self.project_start, new_s)
                if self.on_changed and 'on_changed' not in self._block_callbacks:
                    try:
                        self.on_changed(self.trim_start, self.trim_end)
                    except Exception:
                        pass
            elif self._drag_type == 'right':
                new_e = max(sec, self.trim_start)
                self.trim_end = min(self.project_start + self.total, new_e)
                if self.on_changed and 'on_changed' not in self._block_callbacks:
                    try:
                        self.on_changed(self.trim_start, self.trim_end)
                    except Exception:
                        pass
            elif self._drag_type == 'playhead':
                self.playhead = sec
                if self.on_seek and 'on_seek' not in self._block_callbacks:
                    try:
                        self.on_seek(sec)
                    except Exception:
                        pass
            self.queue_draw()
        except Exception:
            pass
        return True

    def _on_release(self, widget, event):
        try:
            self._dragging = False
            self._drag_type = None
            self._drag_start_x = 0
        except Exception:
            pass
        self.queue_draw()
        return True


class TrimTool(Gtk.DrawingArea):
    """Interactive trim selector for a single clip.

    Displays a horizontal range [start..end] within [0..duration]. Users can
    drag the left/right handles to set trim points. Callback `on_changed(start,end)`
    is invoked when the user releases the mouse after a change.
    """
    def __init__(self):
        super().__init__()
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self.set_size_request(-1, 48)
        self.connect('draw', self._on_draw)
        self.connect('button-press-event', self._on_press)
        self.connect('button-release-event', self._on_release)
        self.connect('motion-notify-event', self._on_motion)
        self.set_events(self.get_events() | Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
        self._dragging = False
        self._drag_side = None
        self._drag_start_x = 0
        self.on_changed = None

    def set_range(self, duration, start=0.0, end=None):
        self.duration = float(duration or 0.0)
        self.start = float(start or 0.0)
        self.end = float(end if end is not None else self.duration)
        if self.end > self.duration:
            self.end = self.duration
        if self.start < 0:
            self.start = 0.0
        if self.start > self.end:
            self.start = max(0.0, self.end - 0.1)
        self.queue_draw()

    def get_trim(self):
        return (self.start, self.end)

    def _hit_test(self, x):
        alloc = self.get_allocation()
        w = float(max(1, alloc.width))
        if self.duration <= 0:
            return None
        rel_s = self.start / self.duration
        rel_e = self.end / self.duration
        x_s = rel_s * w
        x_e = rel_e * w
        tol = 8
        if abs(x - x_s) <= tol:
            return 'left'
        if abs(x - x_e) <= tol:
            return 'right'
        if x > x_s and x < x_e:
            return 'middle'
        return None

    def _on_press(self, widget, event):
        if event.button != 1:
            return False
        h = self._hit_test(event.x)
        if not h:
            return False
        self._dragging = True
        self._drag_side = h
        self._drag_start_x = event.x
        return True

    def _on_motion(self, widget, event):
        if not self._dragging:
            return False
        alloc = self.get_allocation()
        w = float(max(1, alloc.width))
        dx = event.x - self._drag_start_x
        dt = (dx / w) * self.duration if self.duration > 0 else 0
        if self._drag_side == 'left':
            new_s = max(0.0, min(self.start + dt, self.end - 0.01))
            self.start = new_s
        elif self._drag_side == 'right':
            new_e = min(self.duration, max(self.end + dt, self.start + 0.01))
            self.end = new_e
        elif self._drag_side == 'middle':
            # move range
            span = self.end - self.start
            new_s = max(0.0, min(self.start + dt, self.duration - span))
            self.start = new_s
            self.end = new_s + span
        self._drag_start_x = event.x
        self.queue_draw()
        return True

    def _on_release(self, widget, event):
        if event.button != 1:
            return False
        if self._dragging:
            self._dragging = False
            self._drag_side = None
            if callable(self.on_changed):
                try:
                    self.on_changed(self.start, self.end)
                except Exception:
                    pass
        return True

    def _on_draw(self, widget, cr):
        alloc = self.get_allocation()
        w, h = alloc.width, alloc.height
        cr.set_source_rgb(0.08, 0.08, 0.08)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        if self.duration <= 0:
            return False
        rel_s = self.start / self.duration
        rel_e = self.end / self.duration
        x1 = int(rel_s * w)
        x2 = int(rel_e * w)
        # draw range
        cr.set_source_rgba(0.14, 0.5, 0.9, 0.18)
        cr.rectangle(x1, 6, max(2, x2 - x1), h - 12)
        cr.fill()
        # handles
        cr.set_source_rgb(0.9, 0.9, 0.9)
        cr.rectangle(x1 - 4, 6, 8, h - 12)
        cr.fill()
        cr.rectangle(x2 - 4, 6, 8, h - 12)
        cr.fill()
        # labels
        try:
            lbl = f"{self.start:.2f}s - {self.end:.2f}s"
            cr.set_source_rgb(1, 1, 1)
            layout = Pango.Layout.new(self.get_pango_context())
            layout.set_text(lbl, -1)
            cr.move_to(6, 6)
            PangoCairo.show_layout(cr, layout)
        except Exception:
            pass
        return False

    def _on_press(self, widget, event):
        if event.button != 1:
            return False
        alloc = self.get_allocation()
        x = event.x
        w = float(max(1, alloc.width))
        rel = x / w
        # determine clicked item
        pos = self.project_start + rel * self.total if self.total > 0 else self.project_start
        for idx, it in enumerate(self.items):
            s = float(it.get('start', 0) or 0)
            dur = float(it.get('duration', 0) or 0)
            if pos >= s and pos <= s + dur:
                # inside this item
                # determine if near left/right edge (10px tolerance)
                # compute item's on-screen rect
                start_rel = (s - self.project_start) / self.total if self.total > 0 else 0
                end_rel = ((s + dur) - self.project_start) / self.total if self.total > 0 else 0
                start_x = start_rel * w
                end_x = end_rel * w
                tol = 10
                if abs(x - start_x) <= tol:
                    self._drag_type = 'resize-left'
                elif abs(x - end_x) <= tol:
                    self._drag_type = 'resize-right'
                else:
                    self._drag_type = 'move'
                self._dragging = True
                self._drag_idx = idx
                self._drag_start_x = x
                self._orig_start = s
                self._orig_duration = dur
                if callable(self.on_select):
                    try:
                        self.on_select(idx)
                    except Exception:
                        pass
                return True
        # click on empty area -> seek
        if self.total > 0:
            sec = self.project_start + rel * self.total
            if callable(self.on_seek):
                try:
                    self.on_seek(sec)
                except Exception:
                    pass
        return True

    def _on_motion(self, widget, event):
        if not self._dragging or self._drag_idx is None:
            return False
        alloc = self.get_allocation()
        w = float(max(1, alloc.width))
        dx = event.x - self._drag_start_x
        dt = (dx / w) * self.total if self.total > 0 else 0
        idx = self._drag_idx
        it = self.items[idx]
        # snap grid (seconds)
        snap = getattr(self, '_snap', 0.1)
        if self._drag_type == 'move':
            new_start = max(0.0, self._orig_start + dt)
            if snap and snap > 0:
                new_start = round(new_start / snap) * snap
            it['start'] = new_start
        elif self._drag_type == 'resize-left':
            new_start = max(0.0, self._orig_start + dt)
            if snap and snap > 0:
                new_start = round(new_start / snap) * snap
            new_dur = max(0.1, self._orig_duration - (new_start - self._orig_start))
            if snap and snap > 0:
                new_dur = max(0.1, round(new_dur / snap) * snap)
            it['start'] = new_start
            it['duration'] = new_dur
        elif self._drag_type == 'resize-right':
            new_dur = max(0.1, self._orig_duration + dt)
            if snap and snap > 0:
                new_dur = max(0.1, round(new_dur / snap) * snap)
            it['duration'] = new_dur
        self.queue_draw()
        return True

    def _on_release(self, widget, event):
        if event.button != 1:
            return False
        if self._dragging:
            self._dragging = False
            self._drag_idx = None
            self._drag_type = None
            # inform selection changed (final)
            # inform selection changed (final)
            if callable(self.on_select):
                try:
                    self.on_select(None)
                except Exception:
                    pass
            # inform that items changed after drag
            if callable(self.on_changed):
                try:
                    # give a deep copy to avoid mutation issues
                    self.on_changed(copy.deepcopy(self.items))
                except Exception:
                    pass
        return True

    def _on_draw(self, widget, cr):
        alloc = self.get_allocation()
        w, h = alloc.width, alloc.height
        # background
        cr.set_source_rgb(0.06, 0.06, 0.06)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        if not self.items or self.total <= 0:
            return False
        pad = 6
        for i, it in enumerate(self.items):
            s = float(it.get('start', 0) or 0)
            dur = float(it.get('duration', 0) or 0)
            if dur <= 0:
                continue
            rel_start = (s - self.project_start) / self.total if self.total > 0 else 0
            rel_end = ((s + dur) - self.project_start) / self.total if self.total > 0 else rel_start
            x1 = int(rel_start * w)
            x2 = int(rel_end * w)
            width = max(4, x2 - x1)
            # color variant
            base = 0.12 + ((i % 4) * 0.04)
            cr.set_source_rgb(base, 0.18, 0.22)
            cr.rectangle(x1 + 1, pad, width - 2, h - (pad * 2))
            cr.fill()
            # draw handles
            cr.set_source_rgb(0.9, 0.9, 0.9)
            cr.rectangle(x1, pad, 4, h - (pad * 2))
            cr.fill()
            cr.rectangle(x2 - 4, pad, 4, h - (pad * 2))
            cr.fill()
            # label
            try:
                lbl = it.get('label') or os.path.basename(it.get('filename', ''))
                cr.set_source_rgb(1, 1, 1)
                layout = Pango.Layout.new(self.get_pango_context())
                layout.set_text(lbl, -1)
                layout.set_width((width - 8) * Pango.SCALE)
                layout.set_ellipsize(Pango.EllipsizeMode.END)
                cr.move_to(x1 + 6, pad + 4)
                PangoCairo.show_layout(cr, layout)
            except Exception:
                pass
        return False

    # (duplicate timeline interaction block removed — TrimTool keeps its own handlers)

class NovaReplayWindow(Gtk.Window):
    @staticmethod
    def get_img_file(name):
        # Prefer images bundled inside an AppImage via $APPDIR/usr/share/nova-replay/img
        appdir = os.environ.get('APPDIR')
        if appdir:
            p = os.path.join(appdir, 'usr', 'share', 'nova-replay', 'img', name)
            if os.path.exists(p):
                return p
        # fall back to img/ next to this script
        p = os.path.join(os.path.dirname(__file__), 'img', name)
        if os.path.exists(p):
            return p
        # fall back to project root img/ (when running from repo root)
        p = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), 'img', name)
        return p

    def __init__(self):
        super().__init__(title="Nova")
        # Smaller default size so the window opens compactly (widened)
        self.set_default_size(1400, 800)
        # Allow arbitrary resizing; set a very small minimum so user can resize to any size
        self.set_resizable(True)
        try:
            self.set_size_request(1, 1)
        except Exception:
            pass
        # ensure a settings placeholder exists so UI can read defaults
        self.settings = {}

        # CSS styling for a modern look
        css = b"""
        .headerbar { background: #0f1720; color: #ffffff; }
        .primary-button { background: #ff4b4b; color: #fff; border-radius: 6px; padding: 6px; }
        .primary-button GtkLabel { color: #ffffff; }
        .primary-button.recording { background: #c0392b; }
        .recording { background: #c0392b; }
        .sidebar { background: #111216; color: #cfd8e3; padding: 2px; min-width: 0px; }
        .nav-button { background: transparent; color: #cfd8e3; border: none; padding: 0; margin: 2px; border-radius: 4px; min-width: 0px; min-height: 0px; }
        .nav-button GtkImage { margin: 4px; }
        .nav-button GtkLabel { color: #05021b; }
        /* thin, unobtrusive divider between sidebar and content */
        .side-divider {
            /* make divider invisible */
            background: transparent;
            min-width: 0px;
            margin: 0;
            border-left: none;
            box-shadow: none;
        }
        .selected-tile { border: 2px solid #2b6cb0; }
        .main-bg { background: #000000; }
        .clip-row { background: #02010c; border-radius: 6px; padding: 6px; }
        .clip-row { padding: 8px; }
        .dim-label { color: #94a3b8; }
        /* Segmented control (dark mode pill) */
        .segmented { background: transparent; border: none; padding: 2px; border-radius: 999px; }
        .segmented .seg-tab { background: transparent; color: #ffffff; border-radius: 999px; padding: 4px 10px; margin: 2px 4px; }
        .segmented .seg-tab.active { background: rgba(255,255,255,0.06); color: #ffffff; }
        .segmented .seg-tab GtkLabel { color: inherit; }
        .segmented-container { background: transparent; border-bottom: 1px solid #2e3134; padding-bottom: 4px; margin-bottom: 4px; }
        .placeholder { background: transparent; border: none; }
        /* lower tile area below thumbnails */
        .tile-lower { background: #141414; border-bottom-left-radius: 6px; border-bottom-right-radius: 6px; padding: 6px; }
        /* Custom client-side header styled like Windows 10-ish */
        .custom-header { background: #000000; }
        .custom-header .primary-button { background: #0078d7; color: #ffffff; border-radius: 4px; }
        .header-title { font-weight: 600; color: #ffffff; padding-left: 8px; }
        /* icon-only transparent buttons */
        .icon-button { background: transparent; border: none; padding: 0; }
        .icon-button GtkImage { margin: 0; }
        .icon-button:hover { background: rgba(255,255,255,0.06); border-radius: 4px; }
        .icon-button:active { background: rgba(255,255,255,0.10); }
        #exit-button GtkImage {
            min-height: 0px;
            min-width: 0px;
        }
        #min-button GtkImage {
            min-height: 0px;
            min-width: 0px;
        }
        """
        style_provider = Gtk.CssProvider()
        style_provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), style_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Custom client-side header bar (CSD) — spans full window width
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        try:
            # make header a bit taller than default
            try:
                top_bar.set_size_request(-1, 35)
            except Exception:
                pass
        except Exception:
            pass
        try:
            # avoid enforcing a minimum height so window can shrink fully
            try:
                top_bar.set_vexpand(False)
            except Exception:
                pass
        except Exception:
            pass
        top_bar.get_style_context().add_class('custom-header')

        # left-aligned area: app title or spacer
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        try:
            title_lbl = Gtk.Label(label='Nova')
            title_lbl.get_style_context().add_class('header-title')
            title_lbl.set_xalign(0)
            title_box.pack_start(title_lbl, False, False, 8)
        except Exception:
            pass

        # pack left: title
        top_bar.pack_start(title_box, False, False, 6)

        # right-aligned exit image (from img/exit.png) — compact and aligned to corner
        exit_img_widget = Gtk.Image()
        exit_path = os.path.join(os.path.dirname(__file__), 'img', 'exit.png')
        # store default and hover variant paths
        self._exit_default = exit_path
        self._exit_hover = self.get_img_file('exit1.png')
        self._exit_hovered = False
        if os.path.exists(exit_path):
            try:
                # load a small initial pixbuf to avoid oversized image
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(exit_path, -1, 20, True)
                exit_img_widget.set_from_pixbuf(pix)
            except Exception:
                try:
                    exit_img_widget.set_from_file(exit_path)
                except Exception:
                    exit_img_widget = Gtk.Image.new_from_icon_name('window-close', Gtk.IconSize.MENU)
        else:
            exit_img_widget = Gtk.Image.new_from_icon_name('window-close', Gtk.IconSize.MENU)

        exit_event = Gtk.EventBox()
        exit_event.set_name('exit-button')
        exit_event.add(exit_img_widget)
        exit_event.set_tooltip_text('Close')
        try:
            try:
                exit_event.set_size_request(-1, -1)
            except Exception:
                pass
            exit_event.set_halign(Gtk.Align.END)
            exit_event.set_margin_end(2)
        except Exception:
            pass

        def _on_exit_clicked(_, __=None):
            try:
                # gracefully destroy the app
                self.on_destroy()
            except Exception:
                try:
                    Gtk.main_quit()
                except Exception:
                    pass

        def _scale_exit_image(widget, allocation):
            try:
                h = max(12, min(28, allocation.height - 6))
                # choose hover or default image
                p = self._exit_hover if getattr(self, '_exit_hovered', False) and os.path.exists(self._exit_hover) else self._exit_default
                if os.path.exists(p):
                    try:
                        pix2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(p, -1, h, True)
                        GLib.idle_add(exit_img_widget.set_from_pixbuf, pix2)
                    except Exception:
                        pass
            except Exception:
                pass

        def _on_exit_enter(widget, event):
            try:
                self._exit_hovered = True
                try:
                    _scale_exit_image(top_bar, top_bar.get_allocation())
                except Exception:
                    pass
            except Exception:
                pass
            return False

        def _on_exit_leave(widget, event):
            try:
                self._exit_hovered = False
                try:
                    _scale_exit_image(top_bar, top_bar.get_allocation())
                except Exception:
                    pass
            except Exception:
                pass
            return False

        try:
            exit_event.connect('button-press-event', _on_exit_clicked)
            exit_event.connect('enter-notify-event', _on_exit_enter)
            exit_event.connect('leave-notify-event', _on_exit_leave)
        except Exception:
            pass

        try:
            top_bar.connect('size-allocate', _scale_exit_image)
        except Exception:
            pass

        top_bar.pack_end(exit_event, False, False, 4)

        # Maximize button (between minimize and exit) with hover image swapping
        max_img_widget = Gtk.Image()
        max_path = os.path.join(os.path.dirname(__file__), 'img', 'enlarge.png')
        self._max_default = max_path
        self._max_hover = self.get_img_file('enlarge1.png')
        self._max_hovered = False
        if os.path.exists(max_path):
            try:
                # use a smaller default pixbuf for the maximize button
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(max_path, -1, 14, True)
                max_img_widget.set_from_pixbuf(pix)
            except Exception:
                try:
                    max_img_widget.set_from_file(max_path)
                except Exception:
                    max_img_widget = Gtk.Image.new_from_icon_name('view-fullscreen', Gtk.IconSize.MENU)
        else:
            max_img_widget = Gtk.Image.new_from_icon_name('view-fullscreen', Gtk.IconSize.MENU)

        max_event = Gtk.EventBox()
        max_event.set_name('max-button')
        max_event.add(max_img_widget)
        max_event.set_tooltip_text('Maximize')
        try:
            try:
                max_event.set_size_request(-1, -1)
            except Exception:
                pass
            max_event.set_halign(Gtk.Align.END)
            max_event.set_margin_end(6)
        except Exception:
            pass

        def _on_max_clicked(_, __=None):
            try:
                w = self.get_window()
                if w and (w.get_state() & Gdk.WindowState.MAXIMIZED):
                    try:
                        self.unmaximize()
                    except Exception:
                        pass
                else:
                    try:
                        self.maximize()
                    except Exception:
                        pass
            except Exception:
                pass

        def _scale_max_image(widget, allocation):
            try:
                # keep the maximize icon smaller than other titlebar icons
                h = max(10, min(18, allocation.height - 10))
                p = self._max_hover if getattr(self, '_max_hovered', False) and os.path.exists(self._max_hover) else self._max_default
                if os.path.exists(p):
                    try:
                        pix2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(p, -1, h, True)
                        GLib.idle_add(max_img_widget.set_from_pixbuf, pix2)
                    except Exception:
                        pass
            except Exception:
                pass

        def _on_max_enter(widget, event):
            try:
                self._max_hovered = True
                try:
                    _scale_max_image(top_bar, top_bar.get_allocation())
                except Exception:
                    pass
            except Exception:
                pass
            return False

        def _on_max_leave(widget, event):
            try:
                self._max_hovered = False
                try:
                    _scale_max_image(top_bar, top_bar.get_allocation())
                except Exception:
                    pass
            except Exception:
                pass
            return False

        try:
            max_event.connect('button-press-event', _on_max_clicked)
            max_event.connect('enter-notify-event', _on_max_enter)
            max_event.connect('leave-notify-event', _on_max_leave)
        except Exception:
            pass

        try:
            top_bar.connect('size-allocate', _scale_max_image)
        except Exception:
            pass

        # pack maximize to the left of the exit button
        top_bar.pack_end(max_event, False, False, 6)

        # Minimize button (left of exit) with hover image swapping
        min_img_widget = Gtk.Image()
        min_path = os.path.join(os.path.dirname(__file__), 'img', 'min.png')
        self._min_default = min_path
        self._min_hover = self.get_img_file('min1.png')
        self._min_hovered = False
        if os.path.exists(min_path):
            try:
                # slightly smaller default pixbuf for the minimize button
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(min_path, -1, 16, True)
                min_img_widget.set_from_pixbuf(pix)
            except Exception:
                try:
                    min_img_widget.set_from_file(min_path)
                except Exception:
                    min_img_widget = Gtk.Image.new_from_icon_name('window-minimize', Gtk.IconSize.MENU)
        else:
            min_img_widget = Gtk.Image.new_from_icon_name('window-minimize', Gtk.IconSize.MENU)

        min_event = Gtk.EventBox()
        min_event.set_name('min-button')
        min_event.add(min_img_widget)
        min_event.set_tooltip_text('Minimize')
        try:
            # do not enforce a fixed size so the window can shrink
            try:
                min_event.set_size_request(-1, -1)
            except Exception:
                pass
            min_event.set_halign(Gtk.Align.END)
            min_event.set_margin_end(4)
        except Exception:
            pass

        def _on_min_clicked(_, __=None):
            try:
                self.iconify()
            except Exception:
                try:
                    self.window.iconify()
                except Exception:
                    pass

        def _scale_min_image(widget, allocation):
            try:
                # scale smaller range for the minimized control
                h = max(10, min(24, allocation.height - 8))
                p = self._min_hover if getattr(self, '_min_hovered', False) and os.path.exists(self._min_hover) else self._min_default
                if os.path.exists(p):
                    try:
                        pix2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(p, -1, h, True)
                        GLib.idle_add(min_img_widget.set_from_pixbuf, pix2)
                    except Exception:
                        pass
            except Exception:
                pass

        def _on_min_enter(widget, event):
            try:
                self._min_hovered = True
                try:
                    _scale_min_image(top_bar, top_bar.get_allocation())
                except Exception:
                    pass
            except Exception:
                pass
            return False

        def _on_min_leave(widget, event):
            try:
                self._min_hovered = False
                try:
                    _scale_min_image(top_bar, top_bar.get_allocation())
                except Exception:
                    pass
            except Exception:
                pass
            return False

        try:
            min_event.connect('button-press-event', _on_min_clicked)
            min_event.connect('enter-notify-event', _on_min_enter)
            min_event.connect('leave-notify-event', _on_min_leave)
        except Exception:
            pass

        try:
            top_bar.connect('size-allocate', _scale_min_image)
        except Exception:
            pass

        # pack minimize to the left of the exit button
        top_bar.pack_end(min_event, False, False, 6)





        # install as client-side titlebar so content sits below it
        try:
            self.set_titlebar(top_bar)
        except Exception:
            # fallback: add to top of main content if set_titlebar unsupported
            pass

        # Main layout: sidebar + content
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        main_box.get_style_context().add_class('main-bg')
        self.add(main_box)

        # Developer diagnostic: print preferred min/natural sizes for widgets
        # Set environment variable NOVA_PRINT_MINIMUMS=1 to enable and exit after printing.
        if os.environ.get('NOVA_PRINT_MINIMUMS'):
            def _print_min_sizes(widget, path='root'):
                try:
                    wmin, wnat = widget.get_preferred_width()
                except Exception:
                    wmin = wnat = None
                try:
                    hmin, hnat = widget.get_preferred_height()
                except Exception:
                    hmin = hnat = None
                print(f"{path}: {widget.__class__.__name__} minW={wmin} natW={wnat} minH={hmin} natH={hnat}")
                # recurse into children (handle Containers and Bins)
                try:
                    children = widget.get_children()
                except Exception:
                    try:
                        child = widget.get_child()
                        children = [child] if child is not None else []
                    except Exception:
                        children = []
                for i, c in enumerate(children):
                    _print_min_sizes(c, f"{path}/{c.__class__.__name__}[{i}]")

            def _do_print():
                print('--- Widget preferred size diagnostics ---')
                try:
                    _print_min_sizes(self)
                except Exception as e:
                    print('Diagnostic error:', e)
                sys.exit(0)

            def _schedule_print():
                try:
                    # ensure widgets are realized so preferred sizes are meaningful
                    try:
                        self.show_all()
                    except Exception:
                        pass
                    # delay a bit to allow layout to stabilize
                    GLib.timeout_add(200, _do_print)
                except Exception as e:
                    print('Schedule error:', e)
                return False

            GLib.idle_add(_schedule_print)

        # Runtime resize tracing: set NOVA_TRACE_RESIZE=1 to print allocations on resize
        if os.environ.get('NOVA_TRACE_RESIZE'):
            def _on_alloc(widget, allocation):
                try:
                    print(f'Window alloc: w={allocation.width} h={allocation.height}')
                    names = ['flow', 'clip_scrolled', 'preview_container', 'preview_widget', 'left_panel', 'right_panel', 'editor_flow_scrolled']
                    for n in names:
                        w = getattr(self, n, None)
                        if w is None:
                            continue
                        try:
                            alloc = w.get_allocation()
                            print(f'  {n}: alloc w={alloc.width} h={alloc.height}')
                        except Exception:
                            try:
                                pmin, pnat = w.get_preferred_width()
                                vmin, vnat = w.get_preferred_height()
                                print(f'  {n}: pref wmin={pmin} wnat={pnat} hmin={vmin} hnat={vnat}')
                            except Exception:
                                pass
                except Exception as e:
                    print('resize trace error:', e)
                return False

            def _attach_trace():
                try:
                    self.connect('size-allocate', _on_alloc)
                    print('Resize tracing attached')
                except Exception as e:
                    print('Attach trace failed:', e)
                return False

            GLib.idle_add(_attach_trace)

        # Sidebar (smaller)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        # allow sidebar to shrink naturally with the window (animation still controls visual collapse)
        try:
            sidebar.set_hexpand(False)
            # prevent vertical expansion so width-only animation doesn't affect height
            sidebar.set_vexpand(False)
            sidebar.set_halign(Gtk.Align.START)
            sidebar.set_margin_start(4)
            sidebar.set_margin_end(4)
        except Exception:
            pass
        sidebar.get_style_context().add_class('sidebar')
        # Sidebar uses icon-only buttons (icons loaded from ./img/ or packaged AppImage path)
        # Optional logo at top of sidebar
        logo_path = self.get_img_file('logo2.png')
        if os.path.exists(logo_path):
            try:
                logo_pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(logo_path, 40, 40, True)
                logo_img = Gtk.Image.new_from_pixbuf(logo_pix)
            except Exception:
                logo_img = Gtk.Image.new_from_file(logo_path)
            try:
                logo_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                logo_box.pack_start(logo_img, False, False, 6)
                logo_box.set_halign(Gtk.Align.CENTER)
                sidebar.pack_start(logo_box, False, False, 6)
            except Exception:
                sidebar.pack_start(logo_img, False, False, 6)

        # Helper: connect hover swap for icon buttons (default -> hover variant)
        def _connect_hover_swap(button, image_widget, default_path, hover_path, size_px=28):
            # store paths for potential reuse
            try:
                setattr(button, '_icon_default', default_path)
                setattr(button, '_icon_hover', hover_path)
                setattr(button, '_icon_hovered', False)
            except Exception:
                pass

            def _set_pix(p):
                try:
                    if os.path.exists(p):
                        pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(p, size_px, size_px, True)
                        image_widget.set_from_pixbuf(pix)
                except Exception:
                    try:
                        image_widget.set_from_file(p)
                    except Exception:
                        pass

            def _on_enter(w, e):
                try:
                    setattr(button, '_icon_hovered', True)
                    hp = getattr(button, '_icon_hover', None)
                    if hp and os.path.exists(hp):
                        _set_pix(hp)
                except Exception:
                    pass
                return False

            def _on_leave(w, e):
                try:
                    setattr(button, '_icon_hovered', False)
                    dp = getattr(button, '_icon_default', None)
                    if dp and os.path.exists(dp):
                        _set_pix(dp)
                except Exception:
                    pass
                return False

            try:
                button.connect('enter-notify-event', _on_enter)
                button.connect('leave-notify-event', _on_leave)
            except Exception:
                pass

        # Clips button (icon-only, scaled)
        btn_clips = Gtk.Button()
        clip_icon_path = self.get_img_file('clips.png')
        if os.path.exists(clip_icon_path):
            try:
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(clip_icon_path, 28, 28, True)
                btn_clips_img = Gtk.Image.new_from_pixbuf(pix)
            except Exception:
                btn_clips_img = Gtk.Image.new_from_file(clip_icon_path)
        else:
            btn_clips_img = Gtk.Image.new_from_icon_name('video-x-generic', Gtk.IconSize.MENU)
        btn_clips.add(btn_clips_img)
        btn_clips.set_tooltip_text('Clips')
        try:
            btn_clips.set_size_request(-1, -1)
        except Exception:
            pass
        btn_clips.set_relief(Gtk.ReliefStyle.NONE)
        try:
            btn_clips.set_hexpand(False)
            btn_clips.set_vexpand(False)
            btn_clips.set_halign(Gtk.Align.CENTER)
        except Exception:
            pass
        btn_clips.get_style_context().add_class('nav-button')
        def _on_clips_clicked(w):
            try:
                self.content_stack.set_visible_child_name('clips')
            except Exception:
                pass
            try:
                _set_nav_selected('clips')
            except Exception:
                pass
        btn_clips.connect("clicked", _on_clips_clicked)
        # pack clips button immediately so subsequent buttons appear below it
        sidebar.pack_start(btn_clips, False, False, 2)
        # wire hover swap (clips -> clips1)
        try:
            _connect_hover_swap(btn_clips, btn_clips_img, clip_icon_path, self.get_img_file('clips1.png'))
        except Exception:
            pass

        # Editor button (under Clips)
        btn_editor = Gtk.Button()
        editor_icon_path = self.get_img_file('editor.png')
        if os.path.exists(editor_icon_path):
            try:
                pix_e = GdkPixbuf.Pixbuf.new_from_file_at_scale(editor_icon_path, 28, 28, True)
                btn_editor_img = Gtk.Image.new_from_pixbuf(pix_e)
            except Exception:
                btn_editor_img = Gtk.Image.new_from_file(editor_icon_path)
        else:
            btn_editor_img = Gtk.Image.new_from_icon_name('applications-graphics', Gtk.IconSize.MENU)
        btn_editor.add(btn_editor_img)
        btn_editor.set_tooltip_text('Editor')
        try:
            btn_editor.set_size_request(-1, -1)
        except Exception:
            pass
        btn_editor.set_relief(Gtk.ReliefStyle.NONE)
        try:
            btn_editor.set_hexpand(False)
            btn_editor.set_vexpand(False)
            btn_editor.set_halign(Gtk.Align.CENTER)
        except Exception:
            pass
        btn_editor.get_style_context().add_class('nav-button')
        def _on_editor_clicked(w):
            try:
                self.content_stack.set_visible_child_name('editor')
            except Exception:
                pass
            try:
                _set_nav_selected('editor')
            except Exception:
                pass
        btn_editor.connect("clicked", _on_editor_clicked)
        sidebar.pack_start(btn_editor, False, False, 2)
        # wire hover swap (editor -> editor1)
        try:
            _connect_hover_swap(btn_editor, btn_editor_img, editor_icon_path, self.get_img_file('editor1.png'))
        except Exception:
            pass

        # Settings button
        btn_settings = Gtk.Button()
        settings_icon_path = self.get_img_file('settings.png')
        if os.path.exists(settings_icon_path):
            try:
                pix2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(settings_icon_path, 28, 28, True)
                btn_settings_img = Gtk.Image.new_from_pixbuf(pix2)
            except Exception:
                btn_settings_img = Gtk.Image.new_from_file(settings_icon_path)
        else:
            btn_settings_img = Gtk.Image.new_from_icon_name('emblem-system', Gtk.IconSize.MENU)
        btn_settings.add(btn_settings_img)
        btn_settings.set_tooltip_text('Settings')
        try:
            btn_settings.set_size_request(-1, -1)
        except Exception:
            pass
        btn_settings.set_relief(Gtk.ReliefStyle.NONE)
        try:
            btn_settings.set_hexpand(False)
            btn_settings.set_vexpand(False)
            btn_settings.set_halign(Gtk.Align.CENTER)
        except Exception:
            pass
        btn_settings.get_style_context().add_class('nav-button')
        def _on_settings_clicked(w):
            try:
                self.content_stack.set_visible_child_name('settings')
            except Exception:
                pass
            try:
                _set_nav_selected('settings')
            except Exception:
                pass
        btn_settings.connect("clicked", _on_settings_clicked)

        # keep settings anchored to the bottom
        sidebar.pack_end(btn_settings, False, False, 6)
        # wire hover swap (settings -> settings1)
        try:
            _connect_hover_swap(btn_settings, btn_settings_img, settings_icon_path, self.get_img_file('settings1.png'))
        except Exception:
            pass

        # store nav buttons and provide a helper to mark one as selected
        self._nav_buttons = {
            'clips': (btn_clips, btn_clips_img, clip_icon_path, self.get_img_file('clips1.png')),
            'editor': (btn_editor, btn_editor_img, editor_icon_path, self.get_img_file('editor1.png')),
            'settings': (btn_settings, btn_settings_img, settings_icon_path, self.get_img_file('settings1.png')),
        }

        def _set_nav_selected(name):
            for n, (b, img_w, def_p, hov_p) in self._nav_buttons.items():
                try:
                    # choose hover image for selected, default otherwise
                    target = hov_p if n == name and hov_p and os.path.exists(hov_p) else def_p
                    if target and os.path.exists(target):
                        pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(target, 28, 28, True)
                        GLib.idle_add(img_w.set_from_pixbuf, pix)
                except Exception:
                    pass

        # set initial selection based on stack visible child name or default to 'clips'
        try:
            visible = None
            try:
                visible = self.content_stack.get_visible_child_name()
            except Exception:
                visible = None
            _set_nav_selected(visible or 'clips')
        except Exception:
            pass

        # Wrap sidebar in an EventBox so we can receive enter/leave events
        self.sidebar = sidebar
        self._sidebar_current_width = 48
        self._sidebar_anim_id = None
        self._sidebar_anim_target = None
        self._sidebar_trigger_id = None
        self._sidebar_animating = False
        self._sidebar_hover_count = 0

        sidebar_event = Gtk.EventBox()
        try:
            sidebar_event.set_visible_window(False)
        except Exception:
            pass
        sidebar_event.add(self.sidebar)
        try:
            sidebar_event.set_vexpand(False)
        except Exception:
            pass

        # animation helper
        def _animate_sidebar_to(target_width: int):
            # Clamp target and start a single ticker; if one is already running
            # it will be canceled and replaced to pick up the new target.
            try:
                min_w, max_w = 48, 88
                tgt_val = int(target_width)
            except Exception:
                tgt_val = target_width
                min_w, max_w = 48, 88
            tgt_val = max(min_w, min(max_w, tgt_val))
            # cancel existing ticker to avoid overlaps
            try:
                if self._sidebar_anim_id:
                    GLib.source_remove(self._sidebar_anim_id)
                    self._sidebar_anim_id = None
            except Exception:
                pass
            self._sidebar_anim_target = tgt_val
            self._sidebar_animating = True

            def _tick():
                try:
                    cur = int(self._sidebar_current_width)
                    tgt = int(self._sidebar_anim_target)
                except Exception:
                    self._sidebar_animating = False
                    return False
                if cur == tgt:
                    self._sidebar_animating = False
                    self._sidebar_anim_id = None
                    return False
                # eased step (20% of remaining distance) for smoothness
                diff = tgt - cur
                step = int(diff * 0.20)
                if step == 0:
                    step = 1 if diff > 0 else -1
                new = cur + step
                # clamp to bounds and avoid overshoot
                if new < min_w:
                    new = min_w
                if new > max_w:
                    new = max_w
                if (diff > 0 and new > tgt) or (diff < 0 and new < tgt):
                    new = tgt
                try:
                    # track current width target for internal state but do not enforce a size
                    # so the pane can shrink without obstruction
                    self._sidebar_current_width = new
                    # visual animation removed to avoid enforcing minimum window width
                except Exception:
                    pass
                return True

            try:
                self._sidebar_anim_id = GLib.timeout_add(16, _tick)
            except Exception:
                self._sidebar_anim_id = None

        # enter/leave handlers
        def _cancel_sidebar_trigger():
            try:
                if getattr(self, '_sidebar_trigger_id', None):
                    GLib.source_remove(self._sidebar_trigger_id)
                    self._sidebar_trigger_id = None
            except Exception:
                pass

        def _do_expand():
            try:
                self._sidebar_trigger_id = None
                self._sidebar_hover_count = getattr(self, '_sidebar_hover_count', 0) + 1
                _animate_sidebar_to(88)
            except Exception:
                pass
            return False

        def _do_collapse():
            try:
                self._sidebar_trigger_id = None
                self._sidebar_hover_count = max(0, getattr(self, '_sidebar_hover_count', 0) - 1)
                if self._sidebar_hover_count == 0:
                    _animate_sidebar_to(48)
            except Exception:
                pass
            return False

        def _on_sidebar_enter(_, __):
            try:
                # cancel any collapse that was scheduled
                _cancel_sidebar_trigger()
                # schedule expand after small delay to avoid accidental triggers
                if not getattr(self, '_sidebar_trigger_id', None):
                    self._sidebar_trigger_id = GLib.timeout_add(100, _do_expand)
            except Exception:
                pass
            return False

        def _on_sidebar_leave(_, __):
            try:
                # cancel any pending expand trigger
                _cancel_sidebar_trigger()
                # schedule collapse after small delay; collapse will only run if hover_count hits 0
                if not getattr(self, '_sidebar_trigger_id', None):
                    self._sidebar_trigger_id = GLib.timeout_add(100, _do_collapse)
            except Exception:
                pass
            return False

        try:
            sidebar_event.connect('enter-notify-event', _on_sidebar_enter)
            sidebar_event.connect('leave-notify-event', _on_sidebar_leave)
        except Exception:
            pass

        main_box.pack_start(sidebar_event, False, False, 0)
        # thin black divider between sidebar and content — wrap in an EventBox
        divider = Gtk.Box()
        try:
            try:
                divider.set_size_request(-1, -1)
            except Exception:
                pass
            divider.get_style_context().add_class('side-divider')
        except Exception:
            pass
        divider_event = Gtk.EventBox()
        try:
            divider_event.set_visible_window(False)
        except Exception:
            pass
        divider_event.add(divider)
        try:
            # treat divider enter/leave as part of the hover zone
            divider_event.connect('enter-notify-event', _on_sidebar_enter)
            divider_event.connect('leave-notify-event', _on_sidebar_leave)
        except Exception:
            pass
        main_box.pack_start(divider_event, False, False, 0)

        # Content stack
        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)

        # Background image for main content (fills behind thumbnails)
        content_overlay = Gtk.Overlay()
        # keep overlay reference for dynamic background updates
        self.content_overlay = content_overlay
        bg_path = self.get_img_file('bg.png')
        self._bg_path = bg_path if os.path.exists(bg_path) else None
        # Background implemented as a DrawingArea so it never participates in size negotiation
        # (purely decorative and drawn with Cairo)
        try:
            self._bg_pixbuf_orig = None
        except Exception:
            self._bg_pixbuf_orig = None

        self._bg_draw = Gtk.DrawingArea()
        try:
            # ensure it doesn't affect preferred sizes
            self._bg_draw.set_size_request(1, 1)
            self._bg_draw.set_hexpand(False)
            self._bg_draw.set_vexpand(False)
            self._bg_draw.set_can_focus(False)
        except Exception:
            pass

        if self._bg_path:
            # cache original pixbuf
            try:
                self._bg_pixbuf_orig = GdkPixbuf.Pixbuf.new_from_file(self._bg_path)
            except Exception:
                self._bg_pixbuf_orig = None

            # add drawing area into overlay as a purely decorative background
            try:
                content_overlay.add(self._bg_draw)
                content_overlay.add_overlay(self.content_stack)
            except Exception:
                try:
                    content_overlay.add(self._bg_draw)
                    content_overlay.add_overlay(self.content_stack)
                except Exception:
                    pass

            # background draw handled by class method; connect to bound method
            try:
                self._bg_draw.connect('draw', self._draw_background)
            except Exception:
                pass

            def _on_overlay_alloc(w, allocation):
                try:
                    # redraw background when overlay size changes
                    try:
                        self._bg_draw.queue_draw()
                    except Exception:
                        pass
                except Exception:
                    pass

            content_overlay.connect('size-allocate', _on_overlay_alloc)
        else:
            # no bg image; just use stack directly
            content_overlay.add(self.content_stack)

        # helper to update background when user changes selection
        def _update_bg():
            try:
                if not getattr(self, '_bg_pixbuf_orig', None):
                    # nothing to draw
                    try:
                        if getattr(self, '_bg_draw', None):
                            GLib.idle_add(self._bg_draw.queue_draw)
                    except Exception:
                        pass
                    return False
                # schedule a redraw of the background drawing area
                try:
                    if getattr(self, '_bg_draw', None):
                        GLib.idle_add(self._bg_draw.queue_draw)
                except Exception:
                    pass
            except Exception:
                pass
            return False
        self._update_bg = _update_bg

        # Clips view (grid of thumbnails)
        clips_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        try:
            # remove empty space above the search area
            clips_box.set_margin_top(0)
        except Exception:
            pass
        # segmented control above the thumbnail browser
        self.clip_filter_mode = 'all'
        # search query for filtering thumbnails
        self.search_query = ''
        self._search_debounce_id = None
        # sorting options
        self.clip_sort_mode = 'date_created'  # date_created, date_modified, name, size
        self.clip_sort_order = 'desc'  # asc, desc
        seg_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        seg_box.get_style_context().add_class('segmented')
        # container provides transparent background + dividing line
        seg_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        seg_container.get_style_context().add_class('segmented-container')
        # Search box: placed above the segmented tabs
        try:
            search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            try:
                # add a little padding around the search box
                search_box.set_margin_top(8)
                search_box.set_margin_bottom(6)
                search_box.set_margin_start(6)
                search_box.set_margin_end(6)
            except Exception:
                pass
            self.search_entry = Gtk.SearchEntry()
            try:
                self.search_entry.set_placeholder_text('Search clips')
            except Exception:
                pass
            # give the entry some internal margin so it feels padded
            try:
                self.search_entry.set_margin_top(4)
                self.search_entry.set_margin_bottom(4)
                self.search_entry.set_margin_start(6)
                self.search_entry.set_margin_end(6)
            except Exception:
                pass
            self.search_entry.set_hexpand(True)

            def _do_search():
                try:
                    # commit the query and refresh
                    self.search_query = (self.search_entry.get_text() or '').strip()
                    self._search_debounce_id = None
                    self.refresh_clips()
                except Exception:
                    pass
                return False

            def _on_search_changed(entry):
                try:
                    # debounce rapid input
                    if getattr(self, '_search_debounce_id', None):
                        GLib.source_remove(self._search_debounce_id)
                        self._search_debounce_id = None
                    self._search_debounce_id = GLib.timeout_add(200, _do_search)
                except Exception:
                    pass

            self.search_entry.connect('changed', _on_search_changed)
            search_box.pack_start(self.search_entry, True, True, 0)

            # compact record button (icon) to the right of the search entry
            try:
                self.record_btn = Gtk.Button()
                rec_path = self.get_img_file('record.png')
                if os.path.exists(rec_path):
                    rec_pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(rec_path, 20, 20, True)
                    self.record_img = Gtk.Image.new_from_pixbuf(rec_pix)
                else:
                    self.record_img = Gtk.Image.new_from_icon_name('media-record', Gtk.IconSize.SMALL_TOOLBAR)
                self.record_btn.add(self.record_img)
                self.record_btn.set_tooltip_text('Record')
                try:
                    self.record_btn.set_size_request(-1, -1)
                except Exception:
                    pass
                try:
                    self.record_btn.set_relief(Gtk.ReliefStyle.NONE)
                except Exception:
                    pass
                try:
                    self.record_btn.get_style_context().add_class('icon-button')
                except Exception:
                    pass
                self.record_btn.connect('clicked', lambda w: self._toggle_recording_gui())
                search_box.pack_start(self.record_btn, False, False, 6)
            except Exception:
                pass

            # refresh button with a brief spinner animation
            try:
                refresh_btn = Gtk.Button()
                ref_path = self.get_img_file('refresh.png')
                if os.path.exists(ref_path):
                    ref_pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(ref_path, 18, 18, True)
                    ref_img = Gtk.Image.new_from_pixbuf(ref_pix)
                else:
                    ref_img = Gtk.Image.new_from_icon_name('view-refresh', Gtk.IconSize.SMALL_TOOLBAR)
                refresh_spinner = Gtk.Spinner()
                try:
                    refresh_spinner.set_size_request(-1, -1)
                except Exception:
                    pass
                refresh_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
                refresh_box.pack_start(ref_img, False, False, 0)
                refresh_box.pack_start(refresh_spinner, False, False, 0)
                refresh_spinner.hide()
                refresh_btn.add(refresh_box)
                refresh_btn.set_tooltip_text('Refresh Thumbnails')
                try:
                    refresh_btn.set_size_request(-1, -1)
                except Exception:
                    pass
                try:
                    refresh_btn.set_relief(Gtk.ReliefStyle.NONE)
                except Exception:
                    pass
                try:
                    refresh_btn.get_style_context().add_class('icon-button')
                except Exception:
                    pass

                def _on_refresh_clicked(widget):
                    try:
                        ref_img.hide()
                        refresh_spinner.show()
                        refresh_spinner.start()
                        self.refresh_clips()
                        def _stop_spin():
                            try:
                                refresh_spinner.stop()
                                refresh_spinner.hide()
                                ref_img.show()
                            except Exception:
                                pass
                            return False
                        GLib.timeout_add(700, _stop_spin)
                    except Exception:
                        pass

                self.refresh_btn = refresh_btn
                self.refresh_img = ref_img
                self.refresh_spinner = refresh_spinner
                refresh_btn.connect('clicked', _on_refresh_clicked)
                search_box.pack_start(refresh_btn, False, False, 6)
            except Exception:
                pass

            seg_container.pack_start(search_box, False, False, 6)
        except Exception:
            pass
        tabs = [('All Videos', 'all'), ('Full Sessions', 'full'), ('Favorites', 'favorites'), ('Clips', 'clips')]
        self._seg_buttons = []

        def on_seg_clicked(btn, mode):
            try:
                # mark active class on clicked, remove from others
                for b, m in self._seg_buttons:
                    ctx = b.get_style_context()
                    if b is btn:
                        ctx.add_class('active')
                    else:
                        try:
                            ctx.remove_class('active')
                        except Exception:
                            pass
                self.clip_filter_mode = mode
                GLib.idle_add(self.refresh_clips)
            except Exception:
                pass

        for label, mode in tabs:
            b = Gtk.ToggleButton()
            b.set_relief(Gtk.ReliefStyle.NONE)
            b_lbl = Gtk.Label(label=label)
            b.add(b_lbl)
            b.get_style_context().add_class('seg-tab')
            b.connect('clicked', lambda w, m=mode: on_seg_clicked(w, m))
            seg_box.pack_start(b, False, False, 4)
            self._seg_buttons.append((b, mode))

        # Sort dropdown (on the same row as tabs)
        try:
            self.sort_combo = Gtk.ComboBoxText()
            # Add sort options: (display_text, mode, order)
            sort_options = [
                ('Date Created (Newest)', 'date_created', 'desc'),
                ('Date Created (Oldest)', 'date_created', 'asc'),
                ('Date Modified (Newest)', 'date_modified', 'desc'),
                ('Date Modified (Oldest)', 'date_modified', 'asc'),
                ('Name (A-Z)', 'name', 'asc'),
                ('Name (Z-A)', 'name', 'desc'),
                ('Size (Largest)', 'size', 'desc'),
                ('Size (Smallest)', 'size', 'asc'),
            ]
            
            # Store the options for retrieval later
            self._sort_options = sort_options
            
            for display_text, mode, order in sort_options:
                self.sort_combo.append(f"{mode}_{order}", display_text)
            
            # Set default to Date Created (Newest)
            self.sort_combo.set_active_id('date_created_desc')
            
            # Make the combobox smaller
            try:
                self.sort_combo.set_size_request(180, -1)
            except Exception:
                pass
            
            def _on_sort_changed(combo):
                try:
                    active_id = combo.get_active_id()
                    if active_id:
                        for display_text, mode, order in sort_options:
                            if f"{mode}_{order}" == active_id:
                                self.clip_sort_mode = mode
                                self.clip_sort_order = order
                                self.refresh_clips()
                                break
                except Exception:
                    pass
            
            self.sort_combo.connect('changed', _on_sort_changed)
            seg_box.pack_end(self.sort_combo, False, False, 4)
        except Exception:
            pass

        # set initial active
        try:
            self._seg_buttons[0][0].get_style_context().add_class('active')
        except Exception:
            pass

        seg_container.pack_start(seg_box, False, False, 0)
        clips_box.pack_start(seg_container, False, False, 2)
        self.clip_scrolled = Gtk.ScrolledWindow()
        self.flow = Gtk.FlowBox()
        # Increase grid density: add two more columns
        self.flow_cols = 6
        self.flow.set_max_children_per_line(self.flow_cols)
        # allow a sensible minimum per line when narrow
        self.flow.set_min_children_per_line(3)
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_row_spacing(6)
        self.flow.set_column_spacing(6)
        # default thumb size (fixed to prevent growing when few items)
        self.thumb_size = (240, 245)
        # extra space reserved under the image for actions; will be reduced if needed
        self.tile_extra_h = 48
        # enforce maximum total tile height (image + lower area)
        self.MAX_TILE_HEIGHT = 150
        # how many rows of thumbnails to display (fixed grid height)
        self.grid_rows = 3
        # react to scroller resize to compute thumb sizes that fit container width
        def on_scrolled_alloc(w, allocation):
            try:
                # Maintain fixed thumbnail size; do not resize tiles based on container.
                # Determine how many columns fit, but keep tile size constant.
                width = allocation.width
                cols = max(1, getattr(self, 'flow_cols', 4))
                spacing = self.flow.get_column_spacing() or 6
                total_spacing = spacing * (cols - 1)
                # compute columns that can fit with the fixed thumb width
                thumb_w = self.thumb_size[0]
                possible_cols = max(1, (width + spacing) // (thumb_w + spacing))
                # update flow columns but do not change thumb_size
                if possible_cols != getattr(self, 'flow_cols', None):
                    self.flow_cols = possible_cols
                    try:
                        self.flow.set_max_children_per_line(self.flow_cols)
                    except Exception:
                        pass
                # compute columns that can fit with the fixed thumb width
                thumb_w = self.thumb_size[0]
                possible_cols = max(1, (width + spacing) // (thumb_w + spacing))
                # update flow columns but do not change thumb_size
                if possible_cols != getattr(self, 'flow_cols', None):
                    self.flow_cols = possible_cols
                    try:
                        self.flow.set_max_children_per_line(self.flow_cols)
                    except Exception:
                        pass
                # enforce a fixed scroller height based on `self.grid_rows`
                try:
                    row_spacing = self.flow.get_row_spacing() or 6
                    # compute total tile height respecting MAX_TILE_HEIGHT
                    tile_total = min(self.thumb_size[1] + getattr(self, 'tile_extra_h', 48), getattr(self, 'MAX_TILE_HEIGHT', 50))
                    padding = 12
                    # reduce a few pixels per row so the image visually fills the tile
                    per_row_shave = 8
                    desired_h = int(self.grid_rows * (tile_total + row_spacing) + padding - (self.grid_rows * per_row_shave))
                    if desired_h < 0:
                        desired_h = int(self.grid_rows * (tile_total + row_spacing) + padding)
                    try:
                        # allow scroller to expand/contract naturally and be fully scrollable
                        self.clip_scrolled.set_vexpand(True)
                    except Exception:
                        pass
                    try:
                        # do not force an explicit size request so the scroller does not limit window shrink
                        self.clip_scrolled.set_size_request(-1, -1)
                    except Exception:
                        pass
                except Exception:
                    pass
            except Exception:
                pass

        self.clip_scrolled.connect('size-allocate', on_scrolled_alloc)
        self.clip_scrolled.add(self.flow)
        clips_box.pack_start(self.clip_scrolled, True, True, 6)

        # (Trim controls removed — user will add trimming UI later)

        self.content_stack.add_titled(clips_box, 'clips', 'Clips')

        # Editor workspace
        editor_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        editor_box.get_style_context().add_class('editor-panel')

        # Left: media list / project panel
        left_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        # allow the left panel to shrink and expand naturally
        try:
            # ensure left media chooser has a reasonable minimum width
            left_panel.set_size_request(100, -1)
            left_panel.set_hexpand(False)
            left_panel.set_vexpand(True)
        except Exception:
            pass
        left_panel.get_style_context().add_class('editor-list')
        lbl_media = Gtk.Label(label='Media')
        lbl_media.set_xalign(0)
        lbl_media.get_style_context().add_class('dim-label')
        left_panel.pack_start(lbl_media, False, False, 6)

        # media grid (thumbnails) in a scrolled window
        self.editor_flow_scrolled = Gtk.ScrolledWindow()
        self.editor_flow = Gtk.FlowBox()
        self.editor_flow.set_row_spacing(6)
        self.editor_flow.set_column_spacing(6)
        self.editor_flow.set_max_children_per_line(1)
        self.editor_flow.set_min_children_per_line(1)
        # allow single selection so users can pick another clip while one is playing
        try:
            self.editor_flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        except Exception:
            # older GTK versions may not support selection; ignore
            pass
        self.editor_flow_scrolled.add(self.editor_flow)
        left_panel.pack_start(self.editor_flow_scrolled, True, True, 0)

        # media action buttons removed (managed from Export/Tools)

        # use a resizable paned layout: left media panel resizable, center+right fixed
        center_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        center_panel.get_style_context().add_class('video-preview')
        # preview container: use an Overlay so we can place a spinner above the video
        self.preview_container = Gtk.Overlay()
        try:
            # avoid enforcing a minimum preview size that would lock aspect ratio
            self.preview_container.set_size_request(-1, -1)
        except Exception:
            pass
        self.preview_container.get_style_context().add_class('preview-area')
        # allow preview to expand to fill available space when window is larger
        try:
            self.preview_container.set_hexpand(True)
            self.preview_container.set_vexpand(True)
        except Exception:
            pass
        # placeholder image widget for thumbnails when not playing
        self.preview_widget = Gtk.Image()
        self.preview_container.add(self.preview_widget)
        # spinner overlay (hidden until needed)
        self._spinner = Gtk.Spinner()
        self._spinner.set_halign(Gtk.Align.CENTER)
        self._spinner.set_valign(Gtk.Align.CENTER)
        self._spinner.set_no_show_all(True)
        self.preview_container.add_overlay(self._spinner)
        center_panel.pack_start(self.preview_container, True, True, 0)

        # playback controls
        ctrl_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.play_toggle = Gtk.ToggleButton()
        # keep a reference to the image so we can swap icons later
        self.play_img = Gtk.Image.new_from_icon_name('media-playback-start', Gtk.IconSize.MENU)
        self.play_toggle.add(self.play_img)
        self.play_toggle.set_tooltip_text('Play / Pause')
        self.play_toggle.connect('toggled', lambda w: self.on_play(w))
        ctrl_box.pack_start(self.play_toggle, False, False, 0)

        self.time_label = Gtk.Label(label='00:00 / 00:00')
        self.time_label.set_xalign(0)
        # last playhead UI update time (seconds) to throttle redraws
        self._last_playhead_update = None
        ctrl_box.pack_start(self.time_label, False, False, 6)

        # unified timeline widget for seeking and trimming
        self.timeline = Timeline()
        self.timeline.set_hexpand(True)
        # compatibility: hook up a seek adapter for the timeline's on_seek callback
        self.timeline.on_seek = self._on_timeline_seek
        # when user updates trim handles, record last trim
        def _on_trim_changed(s, e):
            try:
                self._last_trim = (s, e)
            except Exception:
                pass
        self.timeline.on_changed = _on_trim_changed
        # keep `trim_tool` name for backward compatibility
        self.trim_tool = self.timeline
        ctrl_box.pack_start(self.timeline, True, True, 0)
        center_panel.pack_start(ctrl_box, False, False, 0)

        # play selection toggle: when active, playback will stop at trim end
        self.play_selection_toggle = Gtk.ToggleButton(label='Play Range')
        self.play_selection_toggle.set_tooltip_text('Play only the selected trim range')
        ctrl_box.pack_start(self.play_selection_toggle, False, False, 6)
        # (Removed secondary Save As button — exporting is available from the Export action)

        # build center+right composite so paned can contain them as a single child
        center_right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        center_right.pack_start(center_panel, True, True, 6)

        # Right: tools / properties
        right_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        try:
            right_panel.set_size_request(-1, -1)
            right_panel.set_hexpand(False)
            right_panel.set_vexpand(True)
        except Exception:
            pass
        tools_lbl = Gtk.Label(label='Tools')
        tools_lbl.get_style_context().add_class('dim-label')
        right_panel.pack_start(tools_lbl, False, False, 6)

        # Tools actions: Save, Save Copy, Import
        tools_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        try:
            save_btn2 = Gtk.Button(label='Overwrite')
            save_btn2.set_tooltip_text('Overwrite current clip / project')
            save_btn2.connect('clicked', lambda w: self.on_save_as())
            # moved to export_box below
            # tools_box.pack_start(save_btn2, False, False, 0)
        except Exception:
            pass

        except Exception:
            pass

        try:
            import_btn = Gtk.Button(label='Import')
            import_btn.set_tooltip_text('Import media into recordings (same as Add)')
            # call existing add-media helper if available
            try:
                import_btn.connect('clicked', _on_add_media)
            except Exception:
                # fallback: open file chooser similar to add
                def _on_import_fallback(_):
                    try:
                        _on_add_media(None)
                    except Exception:
                        pass
                import_btn.connect('clicked', _on_import_fallback)
            tools_box.pack_start(import_btn, False, False, 0)
        except Exception:
            pass

        right_panel.pack_start(tools_box, False, False, 6)

        # Export / render options
        export_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        export_lbl = Gtk.Label(label='Export')
        export_lbl.set_xalign(0)
        export_box.pack_start(export_lbl, False, False, 0)
        export_btn = Gtk.Button(label='Export Trimmed Clip')
        def _on_export(_):
            sel = self.get_selected_clip()
            if not sel:
                self._alert('Select a clip first')
                return
            # Prefer explicit entries, but fall back to the timeline's trim if entries are not present
            try:
                start = float(self.start_entry.get_text())
                end = float(self.end_entry.get_text())
            except Exception:
                try:
                    if getattr(self, 'trim_tool', None):
                        start, end = self.trim_tool.get_trim()
                    else:
                        raise
                except Exception:
                    self._alert('Invalid start/end')
                    return
            # ask for destination
            dlg = Gtk.FileChooserDialog(title='Export Trimmed Clip', parent=self, action=Gtk.FileChooserAction.SAVE)
            dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, 'Export', Gtk.ResponseType.OK)
            res = dlg.run()
            if res == Gtk.ResponseType.OK:
                out = dlg.get_filename()
                dlg.destroy()
                # validate start/end
                try:
                    start = float(start)
                    end = float(end)
                except Exception:
                    self._alert('Invalid start/end')
                    return
                if start < 0:
                    start = 0.0
                if end <= start:
                    self._alert('Invalid start/end (end must be > start)')
                    return
                duration = end - start
                # ensure output filename has an extension; default to source's
                try:
                    src_ext = os.path.splitext(sel)[1]
                    if not src_ext:
                        src_ext = '.mp4'
                except Exception:
                    src_ext = '.mp4'
                try:
                    out_root, out_ext = os.path.splitext(out)
                    if not out_ext:
                        out = out + src_ext
                    # if user provided a different extension, leave as-is
                except Exception:
                    out = out + src_ext

                # use ffmpeg to trim (fast copy) with duration (-t) to ensure correct range
                try:
                    cmd = ['ffmpeg', '-y', '-ss', str(start), '-t', str(duration), '-i', sel, '-c', 'copy', out]
                    threading.Thread(target=subprocess.call, args=(cmd,), daemon=True).start()
                    self._alert(f'Exporting -> {out}')
                except Exception as e:
                    self._alert(f'Export failed: {e}')
            else:
                dlg.destroy()

        export_btn.connect('clicked', _on_export)
        export_box.pack_start(export_btn, False, False, 0)
        # place Overwrite button under Export
        try:
            try:
                export_box.pack_start(save_btn2, False, False, 0)
            except Exception:
                pass
        except Exception:
            pass
        right_panel.pack_start(export_box, False, False, 0)

        center_right.pack_start(right_panel, False, False, 6)

        # horizontal paned: left panel resizable, right side contains center+right
        pan = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        try:
            # allow both children to shrink when the window is made smaller
            pan.pack1(left_panel, resize=True, shrink=True)
            pan.pack2(center_right, resize=True, shrink=True)
        except Exception:
            # older GTK versions use add1/add2
            try:
                pan.add1(left_panel)
                pan.add2(center_right)
            except Exception:
                pass

        editor_box.pack_start(pan, True, True, 6)

        # helper: populate list from recordings dir
        def _on_list_row_activated(lb, row):
            try:
                path = getattr(row, '_path', None)
                if path:
                    self.select_clip(path)
            except Exception:
                pass

        # central selection handler
        def _sel_clip(path):
            try:
                self.select_clip(path)
            except Exception:
                pass

        self._populate_editor_list = lambda: self._fill_editor_list(_on_list_row_activated)
        self._populate_editor_list()

        self.content_stack.add_titled(editor_box, 'editor', 'Editor')

        # Settings view
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        settings_box.set_border_width(8)
        s_title = Gtk.Label(label="Settings")
        s_title.get_style_context().add_class('dim-label')
        s_title.set_xalign(0)
        settings_box.pack_start(s_title, False, False, 6)

        # Backend selection
        backend_label = Gtk.Label(label="Recording backend:")
        backend_label.set_xalign(0)
        settings_box.pack_start(backend_label, False, False, 0)
        self.backend_combo = Gtk.ComboBoxText()
        self.backend_combo.append_text("ffmpeg (x11grab)")
        self.backend_combo.append_text("pipewire (ffmpeg)")
        # In-process PipeWire capture via xdg-desktop-portal + GStreamer
        self.backend_combo.append_text("pipewire (GStreamer portal)")
        self.backend_combo.append_text("wl-screenrec (wl-screenrec)")
        self.backend_combo.set_active(0)
        try:
            self.backend_combo.set_tooltip_text('Select the recording backend. Choose ffmpeg for X11, pipewire for Wayland/pipewire, or wl-screenrec for wl-screenrec.')
        except Exception:
            pass
        settings_box.pack_start(self.backend_combo, False, False, 0)

        # Optional: Wayland output name for wl-screenrec (compositor output id)
        wl_out_label = Gtk.Label(label="Wayland output (optional):")
        wl_out_label.set_xalign(0)
        settings_box.pack_start(wl_out_label, False, False, 0)
        self.wl_output_entry = Gtk.Entry()
        try:
            # populate from persisted settings if present
            initial = self.settings.get('wl_output') if getattr(self, 'settings', None) else ''
            if initial:
                self.wl_output_entry.set_text(initial)
        except Exception:
            pass
        settings_box.pack_start(self.wl_output_entry, False, False, 0)
        try:
            self.wl_output_entry.set_tooltip_text('Optional Wayland output name for wl-screenrec (compositor output id). Leave empty for automatic selection.')
        except Exception:
            pass

        # Hotkey entry (informational)
        hotkey_label = Gtk.Label(label="Toggle recording hotkey:")
        hotkey_label.set_xalign(0)
        settings_box.pack_start(hotkey_label, False, False, 0)
        self.hotkey_entry = Gtk.Entry()
        self.hotkey_entry.set_text("Ctrl+Alt+R")
        try:
            self.hotkey_entry.set_tooltip_text('Hotkey used to toggle recording. Edit in settings but actual global registration may be best-effort.')
        except Exception:
            pass
        settings_box.pack_start(self.hotkey_entry, False, False, 0)

        # Opacity slider
        opacity_label = Gtk.Label(label="Window transparency:")
        opacity_label.set_xalign(0)
        settings_box.pack_start(opacity_label, False, False, 0)
        opacity_slider = Gtk.HScale.new_with_range(0.1, 1.0, 0.1)
        opacity_slider.set_value(1.0)
        opacity_slider.connect("value-changed", self.on_opacity_changed)
        try:
            opacity_slider.set_tooltip_text('Adjust the application window transparency for an unobtrusive overlay.')
        except Exception:
            pass
        settings_box.pack_start(opacity_slider, False, False, 0)

        # Background selector
        bg_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bg_lbl = Gtk.Label(label='Background:')
        bg_lbl.set_xalign(0)
        bg_row.pack_start(bg_lbl, False, False, 0)
        self.bg_combo = Gtk.ComboBoxText()
        for b in ('bg.png','bg1.png','bg3.png','bg4.png'):
            self.bg_combo.append_text(b)
        try:
            sel_bg = self.settings.get('bg_choice') if getattr(self, 'settings', None) else 'bg.png'
            if sel_bg in ('bg.png','bg1.png','bg3.png','bg4.png'):
                self.bg_combo.set_active(('bg.png','bg1.png','bg3.png','bg4.png').index(sel_bg))
            else:
                self.bg_combo.set_active(0)
        except Exception:
            pass
        def _on_bg_changed(combo):
            try:
                txt = combo.get_active_text()
                if not txt:
                    return
                # persist choice
                try:
                    if not getattr(self, 'settings', None):
                        self.settings = {}
                    self.settings['bg_choice'] = txt
                    try:
                        self.save_settings()
                    except Exception:
                        pass
                except Exception:
                    pass
                p = self.get_img_file(txt)
                if p and os.path.exists(p):
                    try:
                        self._bg_path = p
                        self._bg_pixbuf_orig = GdkPixbuf.Pixbuf.new_from_file(p)
                        # update now
                        try:
                            GLib.idle_add(self._update_bg)
                        except Exception:
                            pass
                    except Exception:
                        pass
                else:
                    try:
                        self._bg_pixbuf_orig = None
                        try:
                            if getattr(self, '_bg_draw', None):
                                GLib.idle_add(self._bg_draw.queue_draw)
                        except Exception:
                            pass
                    except Exception:
                        pass
            except Exception:
                pass

        self.bg_combo.connect('changed', _on_bg_changed)
        try:
            self.bg_combo.set_tooltip_text('Choose the background image for the app UI.')
        except Exception:
            pass
        bg_row.pack_start(self.bg_combo, False, False, 0)
        settings_box.pack_start(bg_row, False, False, 6)

        # Encoder settings (ffmpeg and generic encoder control)
        enc_frame = Gtk.Frame(label='Encoder')
        enc_frame.set_label_align(0.0, 0.5)
        enc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        enc_box.set_border_width(6)

        # (Engine is derived from 'Recording backend' above)

        # Video codec / container / preset / crf / bitrate / fps
        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        vc_lbl = Gtk.Label(label='Video codec:')
        vc_lbl.set_xalign(0)
        row1.pack_start(vc_lbl, False, False, 0)
        self.video_codec_combo = Gtk.ComboBoxText()
        for v in ('libx264','libx265','vp9','av1'):
            self.video_codec_combo.append_text(v)
        try:
            sel_v = self.settings.get('encoder', {}).get('video_codec') if getattr(self, 'settings', None) else 'libx264'
            self.video_codec_combo.set_active(['libx264','libx265','vp9','av1'].index(sel_v) if sel_v in ('libx264','libx265','vp9','av1') else 0)
        except Exception:
            pass
        row1.pack_start(self.video_codec_combo, False, False, 0)
        try:
            self.video_codec_combo.set_tooltip_text('Select the video codec to use for recording.')
        except Exception:
            pass

        cont_lbl = Gtk.Label(label='Container:')
        cont_lbl.set_xalign(0)
        row1.pack_start(cont_lbl, False, False, 8)
        self.container_combo = Gtk.ComboBoxText()
        for c in ('mp4','mkv','webm'):
            self.container_combo.append_text(c)
        try:
            sel_c = self.settings.get('encoder', {}).get('container') if getattr(self, 'settings', None) else 'mp4'
            self.container_combo.set_active(['mp4','mkv','webm'].index(sel_c) if sel_c in ('mp4','mkv','webm') else 0)
        except Exception:
            pass
        row1.pack_start(self.container_combo, False, False, 0)
        try:
            self.container_combo.set_tooltip_text('Choose the container/format for recorded files (mp4, mkv, webm).')
        except Exception:
            pass
        enc_box.pack_start(row1, False, False, 0)

        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        preset_lbl = Gtk.Label(label='Preset:')
        preset_lbl.set_xalign(0)
        row2.pack_start(preset_lbl, False, False, 0)
        self.preset_combo = Gtk.ComboBoxText()
        for p in ('ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow'):
            self.preset_combo.append_text(p)
        try:
            sel_p = self.settings.get('encoder', {}).get('preset') if getattr(self, 'settings', None) else 'medium'
            self.preset_combo.set_active(['ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow'].index(sel_p) if sel_p in ('ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow') else 5)
        except Exception:
            pass
        row2.pack_start(self.preset_combo, False, False, 0)
        try:
            self.preset_combo.set_tooltip_text('Encoding preset affecting speed vs quality. Faster presets reduce CPU cost.')
        except Exception:
            pass

        crf_lbl = Gtk.Label(label='CRF:')
        crf_lbl.set_xalign(0)
        row2.pack_start(crf_lbl, False, False, 8)
        adj_crf = Gtk.Adjustment(value=self.settings.get('encoder', {}).get('crf', 23), lower=0, upper=51, step_increment=1, page_increment=5, page_size=0)
        self.crf_spin = Gtk.SpinButton.new(adj_crf, 1, 0)
        row2.pack_start(self.crf_spin, False, False, 0)
        try:
            self.crf_spin.set_tooltip_text('Constant Rate Factor for quality (lower=better quality/larger file).')
        except Exception:
            pass
        enc_box.pack_start(row2, False, False, 0)

        row3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        br_lbl = Gtk.Label(label='Video kbps:')
        br_lbl.set_xalign(0)
        row3.pack_start(br_lbl, False, False, 0)
        adj_br = Gtk.Adjustment(value=self.settings.get('encoder', {}).get('bitrate_kbps', 4000), lower=0, upper=20000, step_increment=100, page_increment=500, page_size=0)
        self.bitrate_spin = Gtk.SpinButton.new(adj_br, 100, 0)
        row3.pack_start(self.bitrate_spin, False, False, 0)
        try:
            self.bitrate_spin.set_tooltip_text('Target video bitrate in kbps (used for bitrate-based encoding).')
        except Exception:
            pass

        fps_lbl = Gtk.Label(label='FPS:')
        fps_lbl.set_xalign(0)
        row3.pack_start(fps_lbl, False, False, 8)
        adj_fps = Gtk.Adjustment(value=self.settings.get('encoder', {}).get('fps', 60), lower=1, upper=240, step_increment=1, page_increment=5, page_size=0)
        self.fps_spin = Gtk.SpinButton.new(adj_fps, 1, 0)
        row3.pack_start(self.fps_spin, False, False, 0)
        try:
            self.fps_spin.set_tooltip_text('Set the frames-per-second capture rate for recordings.')
        except Exception:
            pass
        enc_box.pack_start(row3, False, False, 0)

        # Audio options
        row4 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ac_lbl = Gtk.Label(label='Audio codec:')
        ac_lbl.set_xalign(0)
        row4.pack_start(ac_lbl, False, False, 0)
        self.audio_codec_combo = Gtk.ComboBoxText()
        for a in ('aac','opus','mp3'):
            self.audio_codec_combo.append_text(a)
        try:
            sel_a = self.settings.get('encoder', {}).get('audio_codec') if getattr(self, 'settings', None) else 'aac'
            self.audio_codec_combo.set_active(['aac','opus','mp3'].index(sel_a) if sel_a in ('aac','opus','mp3') else 0)
        except Exception:
            pass
        row4.pack_start(self.audio_codec_combo, False, False, 0)
        try:
            self.audio_codec_combo.set_tooltip_text('Choose the audio codec for recording (AAC, Opus, MP3).')
        except Exception:
            pass

        ab_lbl = Gtk.Label(label='Audio kbps:')
        ab_lbl.set_xalign(0)
        row4.pack_start(ab_lbl, False, False, 8)
        adj_ab = Gtk.Adjustment(value=self.settings.get('encoder', {}).get('audio_bitrate_kbps', 128), lower=16, upper=512, step_increment=16, page_increment=64, page_size=0)
        self.audio_bitrate_spin = Gtk.SpinButton.new(adj_ab, 1, 0)
        row4.pack_start(self.audio_bitrate_spin, False, False, 0)
        try:
            self.audio_bitrate_spin.set_tooltip_text('Audio bitrate in kbps.')
        except Exception:
            pass
        enc_box.pack_start(row4, False, False, 0)

        # Threads / hwaccel
        row5 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        th_lbl = Gtk.Label(label='Threads:')
        th_lbl.set_xalign(0)
        row5.pack_start(th_lbl, False, False, 0)
        adj_th = Gtk.Adjustment(value=self.settings.get('encoder', {}).get('threads', 0), lower=0, upper=64, step_increment=1, page_increment=2, page_size=0)
        self.threads_spin = Gtk.SpinButton.new(adj_th, 1, 0)
        row5.pack_start(self.threads_spin, False, False, 0)
        try:
            self.threads_spin.set_tooltip_text('Number of encoder threads to use (0 = auto).')
        except Exception:
            pass

        hw_lbl = Gtk.Label(label='HW accel:')
        hw_lbl.set_xalign(0)
        row5.pack_start(hw_lbl, False, False, 8)
        self.hwaccel_combo = Gtk.ComboBoxText()
        for h in ('none','vaapi','nvenc','qsv'):
            self.hwaccel_combo.append_text(h)
        try:
            sel_h = self.settings.get('encoder', {}).get('hwaccel') if getattr(self, 'settings', None) else 'none'
            self.hwaccel_combo.set_active(['none','vaapi','nvenc','qsv'].index(sel_h) if sel_h in ('none','vaapi','nvenc','qsv') else 0)
        except Exception:
            pass
        row5.pack_start(self.hwaccel_combo, False, False, 0)
        try:
            self.hwaccel_combo.set_tooltip_text('Hardware acceleration method (vaapi, nvenc, qsv) or none.')
        except Exception:
            pass
        enc_box.pack_start(row5, False, False, 0)

        enc_frame.add(enc_box)
        settings_box.pack_start(enc_frame, False, False, 6)


        # Output directory open button
        out_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        out_lbl = Gtk.Label(label="Recordings folder:")
        out_lbl.set_xalign(0)
        out_box.pack_start(out_lbl, False, False, 0)

        change_btn = Gtk.Button(label='Change...')
        def change_dir(_):
            dlg = Gtk.FileChooserDialog(title='Select recordings folder', parent=getattr(self, 'win', None), action=Gtk.FileChooserAction.SELECT_FOLDER)
            dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
            try:
                # prefer current recorder setting if present
                cur = getattr(recorder, 'RECORDINGS_DIR', None)
                if cur:
                    dlg.set_current_folder(cur)
            except Exception:
                pass
            resp = dlg.run()
            if resp == Gtk.ResponseType.OK:
                folder = dlg.get_filename()
                try:
                    try:
                        recorder.RECORDINGS_DIR = folder
                    except Exception:
                        pass
                    self.settings['recordings_dir'] = folder
                    try:
                        self.save_settings()
                    except Exception:
                        pass
                except Exception as e:
                    self._alert(f"Failed to set recordings folder: {e}")
            dlg.destroy()
        change_btn.connect('clicked', change_dir)
        try:
            change_btn.set_tooltip_text('Change the recordings folder location.')
        except Exception:
            pass
        out_box.pack_start(change_btn, False, False, 0)
        settings_box.pack_start(out_box, False, False, 0)

        # Game auto-detection settings
        game_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        game_lbl = Gtk.Label(label='Game detection:')
        game_lbl.set_xalign(0)
        game_row.pack_start(game_lbl, False, False, 0)
        self.game_detect_check = Gtk.CheckButton(label='Automatically start recording when a Steam or Wine game starts')
        try:
            enabled = self.settings.get('game_detection') if getattr(self, 'settings', None) is not None else True
            self.game_detect_check.set_active(bool(enabled))
        except Exception:
            pass
        # autoclipping: automatically stop when game exits and notify
        self.auto_clip_check = Gtk.CheckButton(label='Enable autoclipping (auto-stop when game exits)')
        try:
            auto_enabled = self.settings.get('auto_clipping') if getattr(self, 'settings', None) is not None else False
            self.auto_clip_check.set_active(bool(auto_enabled))
            self.auto_clip_check.set_tooltip_text('When enabled, recordings started by game detection will be stopped automatically when the game exits, with notifications.')
        except Exception:
            pass
        game_row.pack_start(self.game_detect_check, False, False, 0)
        game_row.pack_start(self.auto_clip_check, False, False, 0)
        try:
            self.game_detect_check.set_tooltip_text('When enabled, Nova Replay will automatically start recording when matching game processes are detected.')
        except Exception:
            pass

        # Manual process picker + list
        picker_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.process_combo = Gtk.ComboBoxText()
        picker_row.pack_start(self.process_combo, True, True, 0)
        try:
            self.process_combo.set_tooltip_text('Dropdown of running process names to pick for manual monitoring.')
        except Exception:
            pass
        self.proc_label_entry = Gtk.Entry()
        self.proc_label_entry.set_placeholder_text('Label (optional)')
        picker_row.pack_start(self.proc_label_entry, False, False, 0)
        try:
            self.proc_label_entry.set_tooltip_text('Optional label for the manual process entry (shown in the list).')
        except Exception:
            pass
        add_proc_btn = Gtk.Button(label='Add')
        picker_row.pack_start(add_proc_btn, False, False, 0)
        try:
            add_proc_btn.set_tooltip_text('Add the selected process to the manual monitoring list.')
        except Exception:
            pass

        settings_box.pack_start(game_row, False, False, 0)
        settings_box.pack_start(picker_row, False, False, 0)

        # Manual list of processes
        self.manual_store = Gtk.ListStore(str, str)  # label, proc
        self.manual_view = Gtk.TreeView(model=self.manual_store)
        for i, title in enumerate(('Label','Process')):
            renderer = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(title, renderer, text=i)
            self.manual_view.append_column(col)
        manual_scroller = Gtk.ScrolledWindow()
        manual_scroller.set_size_request(-1, 120)
        manual_scroller.add(self.manual_view)
        settings_box.pack_start(manual_scroller, False, False, 0)
        try:
            manual_scroller.set_tooltip_text('List of manually configured processes monitored for auto-start recording.')
        except Exception:
            pass

        remove_btn = Gtk.Button(label='Remove Selected')
        settings_box.pack_start(remove_btn, False, False, 0)
        try:
            remove_btn.set_tooltip_text('Remove the selected manual process entry.')
        except Exception:
            pass

        def _populate_process_combo():
            try:
                self.process_combo.remove_all()
                out = subprocess.check_output(['ps','-eo','comm'], text=True)
                names = set()
                for line in out.splitlines()[1:]:
                    nm = line.strip()
                    if nm and nm not in names:
                        names.add(nm)
                        self.process_combo.append_text(nm)
                try:
                    self.process_combo.set_active(0)
                except Exception:
                    pass
            except Exception:
                pass

        def _add_manual(_):
            try:
                proc = self.process_combo.get_active_text() if getattr(self, 'process_combo', None) else None
                label = self.proc_label_entry.get_text() if getattr(self, 'proc_label_entry', None) else ''
                if not proc:
                    return
                self.manual_store.append([label or proc, proc])
                # persist
                try:
                    if not getattr(self, 'settings', None):
                        self.settings = {}
                    lst = self.settings.get('manual_games', [])
                    lst.append({'label': label or proc, 'proc': proc})
                    self.settings['manual_games'] = lst
                    self.save_settings()
                except Exception:
                    pass
            except Exception:
                pass

        def _remove_selected(_):
            try:
                sel = self.manual_view.get_selection()
                model, it = sel.get_selected()
                if it:
                    proc = model.get_value(it,1)
                    label = model.get_value(it,0)
                    model.remove(it)
                    try:
                        lst = self.settings.get('manual_games', [])
                        lst = [x for x in lst if not (x.get('proc')==proc and x.get('label')==label)]
                        self.settings['manual_games'] = lst
                        self.save_settings()
                    except Exception:
                        pass
            except Exception:
                pass

        add_proc_btn.connect('clicked', _add_manual)
        remove_btn.connect('clicked', _remove_selected)
        # initial populate
        _populate_process_combo()
        # populate manual_store from settings
        try:
            for it in self.settings.get('manual_games', []):
                self.manual_store.append([it.get('label', it.get('proc')), it.get('proc')])
        except Exception:
            pass

        # Apply/save (session-only)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect('clicked', lambda w: (self.save_settings(), self._alert("Settings saved")))
        settings_box.pack_end(apply_btn, False, False, 6)

        # Make settings scrollable so users can access all controls on smaller windows
        settings_scroller = Gtk.ScrolledWindow()
        settings_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        try:
            settings_scroller.set_shadow_type(Gtk.ShadowType.IN)
        except Exception:
            pass
        try:
            # Use a viewport to ensure Box packs correctly inside the scrolled window
            settings_scroller.add_with_viewport(settings_box)
        except Exception:
            try:
                settings_scroller.add(settings_box)
            except Exception:
                pass
        self.content_stack.add_titled(settings_scroller, 'settings', 'Settings')

        # Record view placeholder
        rec_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        rec_opts = Gtk.Box(spacing=6)
        self.mode_combo = Gtk.ComboBoxText()
        self.mode_combo.append_text("fullscreen")
        self.mode_combo.append_text("region")
        self.mode_combo.set_active(0)
        rec_opts.pack_start(self.mode_combo, False, False, 0)
        self.start_btn = Gtk.Button(label="Start Recording")
        self.start_btn.connect("clicked", self.on_start)
        self.stop_btn = Gtk.Button(label="Stop")
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", self.on_stop)
        rec_opts.pack_start(self.start_btn, False, False, 0)
        rec_opts.pack_start(self.stop_btn, False, False, 0)
        rec_box.pack_start(rec_opts, False, False, 12)
        self.content_stack.add_titled(rec_box, 'record', 'Record')

        main_box.pack_start(content_overlay, True, True, 0)

        # Recorder and hotkeys
        self.recorder = None
        self.selected_clip = None
        # flags for watcher auto-started recordings
        self._auto_started_by_watcher = False
        self._auto_started_pid = None
        self._auto_started_comm = None
        # media metadata cache: filename -> {'duration':float,'probe':{}}
        self._media_meta = {}
        # simple undo/redo stacks (deque for efficient pops)
        self._undo_stack = deque()
        self._redo_stack = deque()
        # timeline project model: list of {'filename','start','duration','label'}
        self.project_timeline = []
        self.timeline_selected_idx = None
        # load persisted settings (this will set recorder.RECORDINGS_DIR)
        try:
            self.load_settings()
        except Exception:
            pass
        # apply background choice from loaded settings (if any)
        try:
            sel = self.settings.get('bg_choice', None) if getattr(self, 'settings', None) else None
            if sel and getattr(self, 'bg_combo', None):
                try:
                    if sel in ('bg.png','bg1.png','bg3.png','bg4.png'):
                        self.bg_combo.set_active(('bg.png','bg1.png','bg3.png','bg4.png').index(sel))
                except Exception:
                    pass
            if sel:
                p = self.get_img_file(sel)
                if p and os.path.exists(p):
                    try:
                        self._bg_path = p
                        self._bg_pixbuf_orig = GdkPixbuf.Pixbuf.new_from_file(p)
                        GLib.idle_add(self._update_bg)
                    except Exception:
                        pass
            # apply preferred recording backend selection to UI
            try:
                pref = self.settings.get('preferred_backend', 'auto') if getattr(self, 'settings', None) else 'auto'
                if getattr(self, 'backend_combo', None):
                    if pref == 'ffmpeg-x11':
                        self.backend_combo.set_active(0)
                    elif pref == 'pipewire':
                        # pipewire (ffmpeg) at index 1
                        self.backend_combo.set_active(1)
                    elif pref == 'pipewire-gst':
                        # pipewire (GStreamer portal) at index 2
                        try:
                            self.backend_combo.set_active(2)
                        except Exception:
                            self.backend_combo.set_active(1)
                    elif pref == 'wl-screenrec':
                        # wl-screenrec at index 3 (after adding pipewire-gst)
                        try:
                            self.backend_combo.set_active(3)
                        except Exception:
                            self.backend_combo.set_active(0)
                    else:
                        self.backend_combo.set_active(0)
            except Exception:
                pass
        except Exception:
            pass
        # thumbnails folder lives under the current recordings dir
        self.thumbs_dir = os.path.join(recorder.RECORDINGS_DIR, 'thumbnails')
        os.makedirs(self.thumbs_dir, exist_ok=True)
        # populate editor media list now that recordings dir is set
        try:
            if hasattr(self, '_populate_editor_list'):
                self._populate_editor_list()
        except Exception:
            pass
        # debounce timer id for thumbnail refresh during resize
        self._thumb_resize_timer = None
        self.refresh_clips()

        # Optional global hotkey manager (best-effort)
        self.hotkeys = HotkeyManager(self.toggle_recording)
        self.hotkeys.start()
        # No separate secondary timeline: use the main inline `self.timeline` for trimming/seeking.
        self.timeline_widget = None
        # start background process watcher for game auto-detection
        try:
            self._start_process_watcher()
        except Exception:
            pass
        # keyboard shortcuts (global window binding)
        try:
            self.connect('key-press-event', self._on_key_press)
        except Exception:
            pass
        # mark that initialization finished (used by splash logic)
        try:
            self._loaded = True
        except Exception:
            self._loaded = True

    def on_opacity_changed(self, slider):
        opacity = slider.get_value()
        self.set_opacity(opacity)

    def on_destroy(self, *args):
        # cleanup recorder and hotkeys
        try:
            if self.recorder:
                self.recorder.stop()
        except Exception:
            pass
        try:
            self.hotkeys.stop()
        except Exception:
            pass
        Gtk.main_quit()

    def get_config_path(self):
        cfg_home = os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config'))
        app_dir = os.path.join(cfg_home, 'nova-replay')
        os.makedirs(app_dir, exist_ok=True)
        return os.path.join(app_dir, 'config.json')

    def load_settings(self):
        path = self.get_config_path()
        settings = {}
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    settings = json.load(f)
            except Exception:
                settings = {}
        # default recordings dir: ~/Videos/Nova
        default_dir = os.path.expanduser('~/Videos/Nova')
        if not settings.get('recordings_dir'):
            settings['recordings_dir'] = default_dir
        # ensure folder exists
        try:
            os.makedirs(settings['recordings_dir'], exist_ok=True)
        except Exception:
            # fallback to default in home
            settings['recordings_dir'] = os.path.expanduser('~')
            os.makedirs(settings['recordings_dir'], exist_ok=True)
        # set recorder module recordings dir
        try:
            recorder.set_recordings_dir(settings['recordings_dir'])
        except Exception:
            pass
        # backend
        settings['preferred_backend'] = settings.get('preferred_backend', 'auto')
        settings['hotkey'] = settings.get('hotkey', 'Ctrl+Alt+R')
        # persisted favorites mapping: filename -> bool
        settings['favorites'] = settings.get('favorites', {})
        # encoder settings (ffmpeg-friendly defaults)
        settings['encoder'] = settings.get('encoder', {
            'engine': 'ffmpeg',
            'video_codec': 'libx264',
            'crf': 23,
            'bitrate_kbps': 4000,
            'preset': 'medium',
            'fps': 60,
            'container': 'mp4',
            'audio_codec': 'aac',
            'audio_bitrate_kbps': 128,
            'threads': 0,
            'hwaccel': 'none',
        })
        # optional wl-screenrec output name (compositor output id)
        settings['wl_output'] = settings.get('wl_output', '')
        # Game detection: enable auto-start when Steam/Wine or manual entries detected
        settings['game_detection'] = settings.get('game_detection', True)
        # Manual games: list of {label, proc} entries
        settings['manual_games'] = settings.get('manual_games', [])
        self.settings = settings

    def save_settings(self):
        # gather current UI values
        try:
            rec_dir = recorder.RECORDINGS_DIR
        except Exception:
            rec_dir = os.path.expanduser('~/Videos/Nova')
        backend = 'auto'
        try:
            txt = self.backend_combo.get_active_text()
            if txt and 'ffmpeg' in txt and 'x11' in txt:
                backend = 'ffmpeg-x11'
            elif txt and 'pipewire' in txt and 'GStreamer' in txt:
                backend = 'pipewire-gst'
            elif txt and 'pipewire' in txt:
                backend = 'pipewire'
            elif txt and 'wl-screenrec' in txt:
                backend = 'wl-screenrec'
        except Exception:
            backend = self.settings.get('preferred_backend', 'auto')
        hotkey = self.hotkey_entry.get_text() if hasattr(self, 'hotkey_entry') else self.settings.get('hotkey', 'Ctrl+Alt+R')
        # gather encoder ui state if present
        enc = self.settings.get('encoder', {})
        try:
            # derive engine from Recording backend selection
            try:
                be_text = getattr(self, 'backend_combo', None).get_active_text() if getattr(self, 'backend_combo', None) else None
                if be_text and 'pipewire' in be_text:
                    engine_sel = 'pipewire'
                else:
                    engine_sel = 'ffmpeg'
            except Exception:
                engine_sel = enc.get('engine')

            enc_ui = {
                'engine': engine_sel,
                'video_codec': getattr(self, 'video_codec_combo', None).get_active_text() if getattr(self, 'video_codec_combo', None) else enc.get('video_codec'),
                'crf': int(getattr(self, 'crf_spin', None).get_value()) if getattr(self, 'crf_spin', None) else enc.get('crf'),
                'bitrate_kbps': int(getattr(self, 'bitrate_spin', None).get_value()) if getattr(self, 'bitrate_spin', None) else enc.get('bitrate_kbps'),
                'preset': getattr(self, 'preset_combo', None).get_active_text() if getattr(self, 'preset_combo', None) else enc.get('preset'),
                'fps': int(getattr(self, 'fps_spin', None).get_value()) if getattr(self, 'fps_spin', None) else enc.get('fps'),
                'container': getattr(self, 'container_combo', None).get_active_text() if getattr(self, 'container_combo', None) else enc.get('container'),
                'audio_codec': getattr(self, 'audio_codec_combo', None).get_active_text() if getattr(self, 'audio_codec_combo', None) else enc.get('audio_codec'),
                'audio_bitrate_kbps': int(getattr(self, 'audio_bitrate_spin', None).get_value()) if getattr(self, 'audio_bitrate_spin', None) else enc.get('audio_bitrate_kbps'),
                'threads': int(getattr(self, 'threads_spin', None).get_value()) if getattr(self, 'threads_spin', None) else enc.get('threads'),
                'hwaccel': getattr(self, 'hwaccel_combo', None).get_active_text() if getattr(self, 'hwaccel_combo', None) else enc.get('hwaccel'),
            }
            enc = enc_ui
        except Exception:
            pass

        cfg = {
            'recordings_dir': rec_dir,
            'preferred_backend': backend,
            'hotkey': hotkey,
            'favorites': self.settings.get('favorites', {}),
            'encoder': enc,
            'bg_choice': self.settings.get('bg_choice', 'bg.png'),
            'wl_output': getattr(self, 'wl_output_entry', None).get_text() if getattr(self, 'wl_output_entry', None) else self.settings.get('wl_output', ''),
            'game_detection': bool(getattr(self, 'game_detect_check', None).get_active() if getattr(self, 'game_detect_check', None) else self.settings.get('game_detection', True)),
            'auto_clipping': bool(getattr(self, 'auto_clip_check', None).get_active() if getattr(self, 'auto_clip_check', None) else self.settings.get('auto_clipping', False)),
            'manual_games': self.settings.get('manual_games', []),
        }
        try:
            with open(self.get_config_path(), 'w') as f:
                json.dump(cfg, f, indent=2)
            self.settings = cfg
        except Exception as e:
            raise

    # --- Process detection and auto-start logic ---
    def _get_process_list(self):
        """Return list of process dicts: {'pid','ppid','comm','args'}"""
        procs = []
        try:
            out = subprocess.check_output(['ps','-eo','pid,ppid,comm,args'], text=True, errors='ignore')
            lines = out.splitlines()
            for line in lines[1:]:
                try:
                    parts = line.strip().split(None, 3)
                    if len(parts) >= 3:
                        pid = parts[0]
                        ppid = parts[1]
                        comm = parts[2]
                        args = parts[3] if len(parts) > 3 else ''
                        procs.append({'pid': pid, 'ppid': ppid, 'comm': comm, 'args': args})
                except Exception:
                    continue
        except Exception:
            pass
        return procs

    def _process_watcher_tick(self):
        try:
            enabled = bool(getattr(self, 'game_detect_check', None).get_active() if getattr(self, 'game_detect_check', None) else self.settings.get('game_detection', True))
            if not enabled:
                return True
            procs = self._get_process_list()
            # build mapping pid->comm for parent checks
            pid_map = {p['pid']: p['comm'] for p in procs}
            # If recorder was auto-started previously, check if its triggering process still exists; stop if absent
            try:
                if getattr(self, '_auto_started_by_watcher', False) and (self.recorder and getattr(self.recorder, 'proc', None)):
                    # look for the PID in current processes
                    if not any(p['pid'] == str(self._auto_started_pid) for p in procs if p.get('pid')):
                        # process gone: stop recording
                        GLib.idle_add(self._alert, f"Autoclip: detected exit of '{self._auto_started_comm or ''}', stopping recording")
                        GLib.idle_add(self.on_stop, None)
                        self._auto_started_by_watcher = False
                        self._auto_started_pid = None
                        self._auto_started_comm = None
                        return True
            except Exception:
                pass

            # check wine-like processes
            for p in procs:
                comm = (p.get('comm') or '').lower()
                args = (p.get('args') or '').lower()
                if any(x in comm for x in ('wine','wine64','wine-preloader')) or 'proton' in args or '.wine' in args:
                    # start recording
                    if not (self.recorder and getattr(self.recorder, 'proc', None)):
                        # record whether this was auto-started and store pid for later auto-stop
                        try:
                            if getattr(self, 'auto_clip_check', None) and self.auto_clip_check.get_active():
                                self._auto_started_by_watcher = True
                                self._auto_started_pid = p.get('pid')
                                self._auto_started_comm = p.get('comm')
                                GLib.idle_add(self._alert, f"Autoclip: Detected Wine process '{p.get('comm')}', starting recording")
                            else:
                                self._auto_started_by_watcher = False
                                GLib.idle_add(self._alert, f"Detected Wine process '{p.get('comm')}', starting recording")
                        except Exception:
                            self._auto_started_by_watcher = False
                        GLib.idle_add(self.on_start, None)
                        return True
            # check steam-launched games heuristics: parent is steam or args reference steamapps
            for p in procs:
                comm = (p.get('comm') or '').lower()
                args = (p.get('args') or '').lower()
                ppid = p.get('ppid')
                parent = pid_map.get(ppid, '').lower()
                if 'steam' in parent or 'steam' in comm or 'steamapps' in args or 'steam/steamapps' in args or 'steam://run' in args:
                    if not (self.recorder and getattr(self.recorder, 'proc', None)):
                        try:
                            if getattr(self, 'auto_clip_check', None) and self.auto_clip_check.get_active():
                                self._auto_started_by_watcher = True
                                self._auto_started_pid = p.get('pid')
                                self._auto_started_comm = p.get('comm')
                                GLib.idle_add(self._alert, f"Autoclip: Detected Steam-launched process '{p.get('comm')}', starting recording")
                            else:
                                self._auto_started_by_watcher = False
                                GLib.idle_add(self._alert, f"Detected Steam-launched process '{p.get('comm')}', starting recording")
                        except Exception:
                            self._auto_started_by_watcher = False
                        GLib.idle_add(self.on_start, None)
                        return True
            # manual entries
            try:
                manual = self.settings.get('manual_games', []) if getattr(self, 'settings', None) else []
                for entry in manual:
                    target = (entry.get('proc') or '').lower()
                    if not target:
                        continue
                    for p in procs:
                        if target == (p.get('comm') or '').lower() or target in (p.get('args') or '').lower():
                            if not (self.recorder and getattr(self.recorder, 'proc', None)):
                                try:
                                    if getattr(self, 'auto_clip_check', None) and self.auto_clip_check.get_active():
                                        self._auto_started_by_watcher = True
                                        self._auto_started_pid = p.get('pid')
                                        self._auto_started_comm = entry.get('label') or entry.get('proc')
                                        GLib.idle_add(self._alert, f"Autoclip: Detected configured process '{self._auto_started_comm}', starting recording")
                                    else:
                                        self._auto_started_by_watcher = False
                                        GLib.idle_add(self._alert, f"Detected configured process '{entry.get('label') or entry.get('proc')}', starting recording")
                                except Exception:
                                    self._auto_started_by_watcher = False
                                GLib.idle_add(self.on_start, None)
                                return True
            except Exception:
                pass
        except Exception:
            pass
        return True

    def _start_process_watcher(self):
        try:
            # run every 2 seconds
            if getattr(self, '_process_watcher_id', None):
                try:
                    GLib.source_remove(self._process_watcher_id)
                except Exception:
                    pass
            self._process_watcher_id = GLib.timeout_add_seconds(2, self._process_watcher_tick)
        except Exception:
            pass

    def toggle_recording(self):
        # Called from hotkey thread — ensure GUI actions run in the main loop
        GLib.idle_add(self._toggle_recording_gui)

    def _toggle_recording_gui(self):
        if self.recorder and getattr(self.recorder, 'proc', None):
            self.on_stop(None)
        else:
            self.on_start(None)
        return False

    def update_record_button_state(self, is_recording: bool):
        try:
            if is_recording:
                # show stop: swap to stop image if available
                stop_path = self.get_img_file('stop.png')
                if hasattr(self, 'record_img') and os.path.exists(stop_path):
                    try:
                        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(stop_path, 20, 20, True)
                        self.record_img.set_from_pixbuf(pb)
                    except Exception:
                        pass
                try:
                    self.record_btn.get_style_context().add_class('recording')
                except Exception:
                    pass
            else:
                # show record: restore record image if available
                rec_path = self.get_img_file('record.png')
                if hasattr(self, 'record_img') and os.path.exists(rec_path):
                    try:
                        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(rec_path, 20, 20, True)
                        self.record_img.set_from_pixbuf(pb)
                    except Exception:
                        pass
                try:
                    self.record_btn.get_style_context().remove_class('recording')
                except Exception:
                    pass
        except Exception:
            pass

    def _set_image_cover(self, widget: Gtk.Image, path: str, target_w: int, target_h: int, overfill: int = 12):
        """Load `path` into `widget` as a pixbuf scaled to *cover* the target size.

        The pixbuf will be scaled so both dimensions are >= target (plus overfill),
        then set on the widget. Falls back to `set_from_file` on error.
        """
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(path)
            ow = pb.get_width()
            oh = pb.get_height()
            if ow == 0 or oh == 0:
                widget.set_from_file(path)
                return
            scale = max((target_w + overfill) / float(ow), (target_h + overfill) / float(oh))
            new_w = max(1, int(ow * scale))
            new_h = max(1, int(oh * scale))
            scaled = pb.scale_simple(new_w, new_h, GdkPixbuf.InterpType.BILINEAR)
            widget.set_from_pixbuf(scaled)
        except Exception:
            try:
                widget.set_from_file(path)
            except Exception:
                pass

    def on_start(self, _):
        mode = self.mode_combo.get_active_text()
        # determine preferred backend from settings
        pref = 'auto'
        try:
            txt = self.backend_combo.get_active_text()
            if txt and 'ffmpeg' in txt and 'x11' in txt:
                pref = 'ffmpeg-x11'
            elif txt and 'pipewire' in txt and 'GStreamer' in txt:
                pref = 'pipewire-gst'
            elif txt and 'pipewire' in txt:
                pref = 'pipewire'
            elif txt and 'wl-screenrec' in txt:
                pref = 'wl-screenrec'
        except Exception:
            pref = 'auto'
        # warn if chosen backend binary likely missing
        try:
            if pref in ('ffmpeg-x11', 'pipewire') and not shutil.which('ffmpeg'):
                self._alert('ffmpeg not found; recording will likely fail')
        except Exception:
            pass
        # let Recorder choose a timestamped filename to avoid overwriting
        enc_settings = self.settings.get('encoder', {}) if getattr(self, 'settings', None) else {}
        # pass wl_screenrec output preference into recorder settings so it can use it
        try:
            enc_settings['wl_output'] = self.settings.get('wl_output') if getattr(self, 'settings', None) else None
        except Exception:
            pass
        self.recorder = recorder.Recorder(mode=mode, preferred_backend=pref, settings=enc_settings)
        self.recorder.on_stop = self.on_record_stop
        # report recorder startup errors into the UI
        try:
            self.recorder.on_error = lambda msg: GLib.idle_add(self._alert, msg)
        except Exception:
            pass
        self.recorder.start()
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        # update header button state
        try:
            self.update_record_button_state(True)
        except Exception:
            pass

    def on_stop(self, _):
        if self.recorder:
            try:
                self.recorder.stop()
            except (BrokenPipeError, OSError, Exception):
                # swallow any pipe/IO errors during recorder shutdown
                pass
            self.start_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)
            try:
                self.update_record_button_state(False)
            except Exception:
                pass

    def on_record_stop(self, path):
        GLib.idle_add(self.refresh_clips)
        # ensure header button reflects stopped state when recording process ends
        GLib.idle_add(lambda: self.update_record_button_state(False))

    def refresh_clips(self):
        # populate flow grid
        for c in self.flow.get_children():
            self.flow.remove(c)
        files = sorted([f for f in os.listdir(recorder.RECORDINGS_DIR) if os.path.isfile(os.path.join(recorder.RECORDINGS_DIR, f))]) if os.path.exists(recorder.RECORDINGS_DIR) else []
        # apply tab filter
        mode = getattr(self, 'clip_filter_mode', 'all')
        if mode == 'all':
            filtered = files
        elif mode == 'full':
            # not implemented: full sessions detection; show empty
            filtered = []
        elif mode == 'clips':
            # heuristics for edited clips: filenames containing keywords
            keywords = ('edited', 'trim', 'cut', 'clip', '-clip', '_clip')
            filtered = [f for f in files if any(k in f.lower() for k in keywords)]
        else:
            filtered = files
        files = filtered
        # apply search filter (case-insensitive substring match)
        try:
            q = (getattr(self, 'search_query', '') or '').strip().lower()
            if q:
                files = [f for f in files if q in f.lower()]
        except Exception:
            pass
        # apply favorites filter
        try:
            if getattr(self, 'clip_filter_mode', 'all') == 'favorites':
                favs = self.settings.get('favorites', {}) if getattr(self, 'settings', None) else {}
                files = [f for f in files if favs.get(f, False)]
        except Exception:
            pass
        
        # apply sorting
        try:
            sort_mode = getattr(self, 'clip_sort_mode', 'date_created')
            sort_order = getattr(self, 'clip_sort_order', 'desc')
            
            if sort_mode == 'date_created':
                # Sort by file creation time (use st_ctime which is available on Linux)
                files = sorted(files, key=lambda f: os.stat(os.path.join(recorder.RECORDINGS_DIR, f)).st_ctime, reverse=(sort_order == 'desc'))
            elif sort_mode == 'date_modified':
                # Sort by file modification time
                files = sorted(files, key=lambda f: os.stat(os.path.join(recorder.RECORDINGS_DIR, f)).st_mtime, reverse=(sort_order == 'desc'))
            elif sort_mode == 'name':
                # Sort by filename
                files = sorted(files, key=lambda f: f.lower(), reverse=(sort_order == 'desc'))
            elif sort_mode == 'size':
                # Sort by file size
                files = sorted(files, key=lambda f: os.stat(os.path.join(recorder.RECORDINGS_DIR, f)).st_size, reverse=(sort_order == 'desc'))
        except Exception:
            # If sorting fails, keep the current order
            pass
        
        for f in files:
            path = os.path.join(recorder.RECORDINGS_DIR, f)
            # build a tile
            tile = Gtk.EventBox()
            tile_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            try:
                # enforce a fixed tile size so tiles cannot grow taller than the
                # configured thumbnail plus actions area, but clamp to MAX_TILE_HEIGHT
                orig_tw, orig_th = getattr(self, 'thumb_size', (240, 135))
                extra_h = getattr(self, 'tile_extra_h', 48)
                total_h = min(orig_th + extra_h, getattr(self, 'MAX_TILE_HEIGHT', 50))
                # compute image area height and lower area height
                img_h = max(0, min(orig_th, total_h))
                lower_h = max(0, total_h - img_h)
                # enforce 16:9 image area: compute target width from img_h
                target_w = int(max(48, round(img_h * 16.0 / 9.0)))
                # do not exceed configured thumb width
                try:
                    target_w = min(orig_tw, target_w)
                except Exception:
                    pass
                # allow tile width to be flexible so the grid can shrink horizontally
                try:
                    # do not enforce a fixed tile size; allow it to shrink with the layout
                    tile_box.set_size_request(-1, -1)
                except Exception:
                    pass
                tile_box.set_hexpand(False)
                tile_box.set_vexpand(False)
            except Exception:
                img_h = getattr(self, 'thumb_size', (240,135))[1]
                lower_h = getattr(self, 'tile_extra_h', 48)
                pass
            thumb_path = os.path.join(self.thumbs_dir, f + '.png')
            # use the computed image height and target width for decorated filenames
            target_h = img_h
            try:
                decor_base = os.path.splitext(thumb_path)[0] + f'_{target_w}x{target_h}_decor'
            except Exception:
                decor_base = os.path.splitext(thumb_path)[0]
            normal_decor = decor_base + '.png'
            hover_decor = decor_base + '_hover.png'
            if os.path.exists(normal_decor):
                img = Gtk.Image()
                img._normal = normal_decor
                img._hover = hover_decor if os.path.exists(hover_decor) else normal_decor
                try:
                    # set the pixbuf so it covers the image area (img_h)
                    self._set_image_cover(img, normal_decor, target_w, img_h, overfill=12)
                    try:
                        # do not enforce a fixed size that blocks shrink; allow natural scaling
                        img.set_size_request(-1, -1)
                    except Exception:
                        pass
                except Exception:
                    try:
                        img.set_from_file(normal_decor)
                    except Exception:
                        pass
            else:
                img = Gtk.Image.new_from_icon_name('video-x-generic', Gtk.IconSize.DIALOG)
                try:
                    try:
                        img.set_size_request(-1, -1)
                    except Exception:
                        pass
                except Exception:
                    pass
                # generate thumbnail frame and decorated images in background if ffmpeg available
                if shutil.which('ffmpeg'):
                    def gen(p=path, tp=thumb_path, nb=decor_base, widget=img, tw=target_w, th=target_h, ih=img_h):
                        try:
                            # extract a frame sized to target
                            subprocess.run(['ffmpeg', '-y', '-ss', '00:00:01', '-i', p, '-vframes', '1', '-q:v', '2', '-s', f'{tw}x{ih}', tp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            # render decorated normal + hover
                            try:
                                render_decorated_thumbnail(tp, nb, size=(tw, ih), radius=12)
                            except Exception:
                                pass
                            # prefer decorated normal if created, else fallback to raw frame
                            if os.path.exists(nb + '.png'):
                                GLib.idle_add(self._set_image_cover, widget, nb + '.png', tw, ih, 12)
                                # store paths for hover
                                widget._normal = nb + '.png'
                                widget._hover = nb + '_hover.png' if os.path.exists(nb + '_hover.png') else nb + '.png'
                            else:
                                GLib.idle_add(self._set_image_cover, widget, tp, tw, ih, 12)
                                widget._normal = tp
                                widget._hover = tp
                        except Exception:
                            pass
                    threading.Thread(target=gen, daemon=True).start()
            tile_box.pack_start(img, True, True, 0)
            # lower info area below the image (label + actions)
            lower_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            try:
                # avoid forcing lower area height; let content and scrolling dictate height
                lower_box.set_size_request(-1, -1)
                lower_box.get_style_context().add_class('tile-lower')
            except Exception:
                pass
            # filename label removed from thumbnail view (keeps lower area for actions only)

            # clicking selects and updates preview
            def on_click(ev, p=path, widget=tile):
                try:
                    # use select_clip to update preview and duration/trim
                    self.select_clip(p)
                except Exception:
                    # fallback to simple assign
                    try:
                        self.selected_clip = p
                    except Exception:
                        pass
                try:
                    for c in self.flow.get_children():
                        c.get_style_context().remove_class('selected-tile')
                    widget.get_style_context().add_class('selected-tile')
                except Exception:
                    pass
            # hover enter/leave to swap to hover image
            def on_enter(w, event, widget_img=img):
                try:
                    if hasattr(widget_img, '_hover') and widget_img._hover:
                        GLib.idle_add(self._set_image_cover, widget_img, widget_img._hover, target_w, target_h, 12)
                except Exception:
                    pass
                return False

            def on_leave(w, event, widget_img=img):
                try:
                    if hasattr(widget_img, '_normal') and widget_img._normal:
                        GLib.idle_add(self._set_image_cover, widget_img, widget_img._normal, target_w, target_h, 12)
                except Exception:
                    pass
                return False

            tile.connect('enter-notify-event', on_enter)
            tile.connect('leave-notify-event', on_leave)
            tile.connect('button-press-event', on_click)

            # context buttons below
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            # Play button (image-based, transparent)
            play = Gtk.Button()
            try:
                play_path = self.get_img_file('play.png')
                play_hover_path = self.get_img_file('play1.png')
                play_img = Gtk.Image()
                if os.path.exists(play_path):
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(play_path, 20, 20, True)
                    play_img.set_from_pixbuf(pb)
                    play_img._normal = play_path
                    play_img._hover = play_hover_path if os.path.exists(play_hover_path) else None
                else:
                    play_img = Gtk.Image.new_from_icon_name('media-playback-start', Gtk.IconSize.MENU)
                play.add(play_img)
                play.set_tooltip_text('Play')
                try:
                    play.set_relief(Gtk.ReliefStyle.NONE)
                    play.get_style_context().add_class('icon-button')
                    try:
                        play.set_size_request(-1, -1)
                    except Exception:
                        pass
                except Exception:
                    pass
                def _on_play_enter(w, ev, img=play_img):
                    try:
                        if getattr(img, '_hover', None):
                            pb2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(img._hover, 20, 20, True)
                            img.set_from_pixbuf(pb2)
                    except Exception:
                        pass
                    return False
                def _on_play_leave(w, ev, img=play_img):
                    try:
                        if getattr(img, '_normal', None):
                            pb2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(img._normal, 20, 20, True)
                            img.set_from_pixbuf(pb2)
                    except Exception:
                        pass
                    return False
                play.connect('enter-notify-event', _on_play_enter)
                play.connect('leave-notify-event', _on_play_leave)
                play.connect('clicked', lambda w, p=path: subprocess.Popen(['mpv', p]) if shutil.which('mpv') else subprocess.Popen(['xdg-open', p]))
            except Exception:
                play = Gtk.Button()
                play.add(Gtk.Image.new_from_icon_name('media-playback-start', Gtk.IconSize.MENU))
                play.connect('clicked', lambda w, p=path: subprocess.Popen(['mpv', p]) if shutil.which('mpv') else subprocess.Popen(['xdg-open', p]))

            # Delete (trash) button (image-based, transparent)
            delb = Gtk.Button()
            try:
                trash_path = self.get_img_file('trash.png')
                trash_hover = self.get_img_file('trash1.png')
                del_img = Gtk.Image()
                if os.path.exists(trash_path):
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(trash_path, 20, 20, True)
                    del_img.set_from_pixbuf(pb)
                    del_img._normal = trash_path
                    del_img._hover = trash_hover if os.path.exists(trash_hover) else None
                else:
                    del_img = Gtk.Image.new_from_icon_name('user-trash', Gtk.IconSize.MENU)
                delb.add(del_img)
                delb.set_tooltip_text('Delete')
                try:
                    delb.set_relief(Gtk.ReliefStyle.NONE)
                    delb.get_style_context().add_class('icon-button')
                    try:
                        delb.set_size_request(-1, -1)
                    except Exception:
                        pass
                except Exception:
                    pass
                def _on_del_enter(w, ev, img=del_img):
                    try:
                        if getattr(img, '_hover', None):
                            pb2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(img._hover, 20, 20, True)
                            img.set_from_pixbuf(pb2)
                    except Exception:
                        pass
                    return False
                def _on_del_leave(w, ev, img=del_img):
                    try:
                        if getattr(img, '_normal', None):
                            pb2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(img._normal, 20, 20, True)
                            img.set_from_pixbuf(pb2)
                    except Exception:
                        pass
                    return False
                delb.connect('enter-notify-event', _on_del_enter)
                delb.connect('leave-notify-event', _on_del_leave)
            except Exception:
                delb = Gtk.Button()
                delb.add(Gtk.Image.new_from_icon_name('user-trash', Gtk.IconSize.MENU))
            # Folder button (image-based, transparent) to the left of favorite
            folderb = Gtk.Button()
            try:
                folder_path = self.get_img_file('folder.png')
                folder_hover = self.get_img_file('folder1.png')
                folder_img = Gtk.Image()
                if os.path.exists(folder_path):
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(folder_path, 20, 20, True)
                    folder_img.set_from_pixbuf(pb)
                    folder_img._normal = folder_path
                    folder_img._hover = folder_hover if os.path.exists(folder_hover) else None
                else:
                    folder_img = Gtk.Image.new_from_icon_name('folder', Gtk.IconSize.MENU)
                folderb.add(folder_img)
                folderb.set_tooltip_text('Open Folder')
                try:
                    folderb.set_relief(Gtk.ReliefStyle.NONE)
                    folderb.get_style_context().add_class('icon-button')
                    try:
                        folderb.set_size_request(-1, -1)
                    except Exception:
                        pass
                except Exception:
                    pass
                def _on_folder_enter(w, ev, img=folder_img):
                    try:
                        if getattr(img, '_hover', None):
                            pb2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(img._hover, 20, 20, True)
                            img.set_from_pixbuf(pb2)
                    except Exception:
                        pass
                    return False
                def _on_folder_leave(w, ev, img=folder_img):
                    try:
                        if getattr(img, '_normal', None):
                            pb2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(img._normal, 20, 20, True)
                            img.set_from_pixbuf(pb2)
                    except Exception:
                        pass
                    return False
                folderb.connect('enter-notify-event', _on_folder_enter)
                folderb.connect('leave-notify-event', _on_folder_leave)
                def _on_folder_clicked(w, p=path):
                    try:
                        # open containing folder in file manager
                        subprocess.Popen(['xdg-open', os.path.dirname(p)])
                    except Exception:
                        pass
                folderb.connect('clicked', _on_folder_clicked)
            except Exception:
                folderb = Gtk.Button()
                folderb.add(Gtk.Image.new_from_icon_name('folder', Gtk.IconSize.MENU))

            # Favorited button (image-based, transparent) placed beside delete
            favb = Gtk.Button()
            try:
                fav_path = self.get_img_file('favorite.png')
                fav_hover = self.get_img_file('favorite1.png')
                fav_img = Gtk.Image()
                if os.path.exists(fav_path):
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(fav_path, 20, 20, True)
                    fav_img.set_from_pixbuf(pb)
                    fav_img._normal = fav_path
                    fav_img._hover = fav_hover if os.path.exists(fav_hover) else None
                else:
                    fav_img = Gtk.Image.new_from_icon_name('emblem-favorite', Gtk.IconSize.MENU)
                favb.add(fav_img)
                favb.set_tooltip_text('Favorite')
                try:
                    favb.set_relief(Gtk.ReliefStyle.NONE)
                    favb.get_style_context().add_class('icon-button')
                    try:
                        favb.set_size_request(-1, -1)
                    except Exception:
                        pass
                except Exception:
                    pass
                # initialize fav state from persisted settings
                try:
                    fav_state = False
                    favs = self.settings.get('favorites', {}) if getattr(self, 'settings', None) else {}
                    fav_state = favs.get(f, False)
                    fav_img._fav = fav_state
                    if fav_state and getattr(fav_img, '_hover', None):
                        pb_init = GdkPixbuf.Pixbuf.new_from_file_at_scale(fav_img._hover, 20, 20, True)
                        fav_img.set_from_pixbuf(pb_init)
                except Exception:
                    pass

                def _on_fav_enter(w, ev, img=fav_img):
                    try:
                        if getattr(img, '_hover', None):
                            pb2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(img._hover, 20, 20, True)
                            img.set_from_pixbuf(pb2)
                    except Exception:
                        pass
                    return False
                def _on_fav_leave(w, ev, img=fav_img):
                    try:
                        # If this item is favorited, keep the hover image visible
                        if getattr(img, '_fav', False):
                            if getattr(img, '_hover', None):
                                pb2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(img._hover, 20, 20, True)
                                img.set_from_pixbuf(pb2)
                        else:
                            if getattr(img, '_normal', None):
                                pb2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(img._normal, 20, 20, True)
                                img.set_from_pixbuf(pb2)
                    except Exception:
                        pass
                    return False
                favb.connect('enter-notify-event', _on_fav_enter)
                favb.connect('leave-notify-event', _on_fav_leave)
                # toggle favorited state on click
                def _on_fav_clicked(w, img=fav_img, filename=f):
                    try:
                        cur = getattr(img, '_fav', False)
                        new = not cur
                        img._fav = new
                        # visual feedback: when favorited, show hover image if available
                        if new and getattr(img, '_hover', None):
                            pb2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(img._hover, 20, 20, True)
                            img.set_from_pixbuf(pb2)
                        else:
                            if getattr(img, '_normal', None):
                                pb2 = GdkPixbuf.Pixbuf.new_from_file_at_scale(img._normal, 20, 20, True)
                                img.set_from_pixbuf(pb2)
                        # persist change
                        try:
                            if not getattr(self, 'settings', None):
                                self.settings = {}
                            if 'favorites' not in self.settings:
                                self.settings['favorites'] = {}
                            self.settings['favorites'][filename] = new
                            try:
                                self.save_settings()
                            except Exception:
                                pass
                        except Exception:
                            pass
                    except Exception:
                        pass
                favb.connect('clicked', _on_fav_clicked)
            except Exception:
                favb = Gtk.Button()
                favb.add(Gtk.Image.new_from_icon_name('emblem-favorite', Gtk.IconSize.MENU))
            def on_del(_, p=path, tp=thumb_path, widget=tile, filename=f):
                # ask for confirmation, then move to Trash instead of permanent delete
                try:
                    dlg = Gtk.MessageDialog(transient_for=self, flags=0, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL, text=f"Move '{os.path.basename(p)}' to Trash?")
                    res = dlg.run()
                    dlg.destroy()
                    if res != Gtk.ResponseType.OK:
                        return
                except Exception:
                    # fallback: require confirmation via simple alert
                    try:
                        self._alert('Confirm delete')
                    except Exception:
                        pass
                    return

                try:
                    moved = move_to_trash(p)
                except Exception as e:
                    try:
                        self._alert(f"Failed to move to Trash: {e}")
                    except Exception:
                        pass
                    return

                # remove any generated thumbnails/decorations for this entry
                try:
                    base_noext = os.path.splitext(filename)[0]
                    for fn in os.listdir(self.thumbs_dir):
                        if fn.startswith(base_noext):
                            try:
                                os.remove(os.path.join(self.thumbs_dir, fn))
                            except Exception:
                                pass
                except Exception:
                    pass

                try:
                    # remove tile from UI
                    self.flow.remove(widget)
                except Exception:
                    pass

            # expose split action for this tile (use timeline slider value)
            def _on_split(_, p=path):
                try:
                    if not shutil.which('ffmpeg'):
                        self._alert('ffmpeg required for split operation')
                        return
                    # use current timeline value as split point (seconds)
                    split_at = 0
                    try:
                        split_at = float(getattr(self, 'timeline', None).get_value() if getattr(self, 'timeline', None) else 0)
                    except Exception:
                        split_at = 0
                    if split_at <= 0:
                        self._alert('Choose a split position > 0 seconds')
                        return
                    base = os.path.splitext(p)[0]
                    ext = os.path.splitext(p)[1]
                    out1 = f"{base}_part1{ext}"
                    out2 = f"{base}_part2{ext}"
                    # handle name conflicts
                    idx = 1
                    while os.path.exists(out1) or os.path.exists(out2):
                        out1 = f"{base}_part1_{idx}{ext}"
                        out2 = f"{base}_part2_{idx}{ext}"
                        idx += 1
                    # run ffmpeg split (fast copy)
                    try:
                        cmd1 = ['ffmpeg', '-y', '-i', p, '-t', str(split_at), '-c', 'copy', out1]
                        cmd2 = ['ffmpeg', '-y', '-i', p, '-ss', str(split_at), '-c', 'copy', out2]
                        subprocess.check_call(cmd1)
                        subprocess.check_call(cmd2)
                    except Exception as e:
                        self._alert(f'Split failed: {e}')
                        # cleanup partial outputs
                        try:
                            if os.path.exists(out1): os.remove(out1)
                            if os.path.exists(out2): os.remove(out2)
                        except Exception:
                            pass
                        return
                    # add new files to recordings dir (they are alongside original)
                    # persist action to undo stack
                    try:
                        act = {'type': 'split', 'original': p, 'out1': out1, 'out2': out2}
                        self._undo_stack.append(act)
                        self._redo_stack.clear()
                    except Exception:
                        pass
                    # refresh UI
                    self.refresh_clips()
                    self._populate_editor_list()
                    self._alert(f'Split created: {os.path.basename(out1)}, {os.path.basename(out2)}')
                except Exception:
                    pass

            # attach a small edit button to actions (opens editor and selects this clip)
            try:
                edit_btn = Gtk.Button()
                # create image widget and load default/hover variants if available
                edit_icon_path = self.get_img_file('edit.png')
                edit_icon_hover = self.get_img_file('edit1.png')
                try:
                    if os.path.exists(edit_icon_path):
                        pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(edit_icon_path, 20, 20, True)
                        edit_img = Gtk.Image.new_from_pixbuf(pix)
                    else:
                        edit_img = Gtk.Image.new_from_icon_name('document-edit', Gtk.IconSize.MENU)
                except Exception:
                    try:
                        edit_img = Gtk.Image.new_from_file(edit_icon_path)
                    except Exception:
                        edit_img = Gtk.Image.new_from_icon_name('document-edit', Gtk.IconSize.MENU)
                edit_btn.add(edit_img)
                edit_btn.set_tooltip_text('Edit in timeline / editor')
                edit_btn.set_relief(Gtk.ReliefStyle.NONE)
                edit_btn.get_style_context().add_class('icon-button')
                # wire hover-swap if helper exists
                try:
                    _connect_hover_swap(edit_btn, edit_img, edit_icon_path, edit_icon_hover, 20)
                except Exception:
                    pass

                def _on_edit(w, p=path):
                    try:
                        self.content_stack.set_visible_child_name('editor')
                    except Exception:
                        pass
                    try:
                        # select this clip in the editor/preview
                        self.select_clip(p)
                    except Exception:
                        pass

                edit_btn.connect('clicked', _on_edit)
                actions.pack_start(edit_btn, False, False, 0)
            except Exception:
                pass

            delb.connect('clicked', on_del)
            actions.pack_start(play, False, False, 0)
            # spacer to push the delete button to the right
            try:
                spacer = Gtk.Box()
                actions.pack_start(spacer, True, True, 0)
            except Exception:
                pass
            # pack folder, favorite, then delete buttons
            try:
                actions.pack_start(folderb, False, False, 0)
            except Exception:
                pass
            try:
                actions.pack_start(favb, False, False, 0)
            except Exception:
                pass
            actions.pack_start(delb, False, False, 0)
            lower_box.pack_start(actions, False, False, 0)
            tile_box.pack_start(lower_box, False, False, 0)

            tile.add(tile_box)
            try:
                tile.set_hexpand(False)
                tile.set_vexpand(False)
            except Exception:
                pass
            tile.get_style_context().add_class('clip-row')
            tile._filename = path
            self.flow.add(tile)
        # if only one row, limit scroller height so tiles don't span full window
        try:
            # Allow the scroller to expand/shrink naturally during window resize.
            # Previously we set a fixed size_request for small row counts which
            # prevented shrinking; remove that constraint so the window can be made smaller.
            self.clip_scrolled.set_vexpand(True)
        except Exception:
            pass
        # ensure minimum grid slots by adding placeholders so layout stays organized
        try:
            real_count = len(files)
            cols = max(1, getattr(self, 'flow_cols', 6))
            total_slots = int(self.grid_rows) * int(cols)
            need = max(0, total_slots - real_count)
            for i in range(need):
                ph = Gtk.EventBox()
                ph_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                try:
                    tw, th = getattr(self, 'thumb_size', (240, 135))
                    extra_h = 48
                    ph_box.set_size_request(tw, th + extra_h)
                    ph_box.set_hexpand(False)
                    ph_box.set_vexpand(False)
                except Exception:
                    pass
                try:
                    empty_img = Gtk.Box()
                    empty_img.set_size_request(tw, th)
                    ph_box.pack_start(empty_img, True, True, 0)
                except Exception:
                    pass
                # keep placeholder transparent; do not add a colored lower area
                ph.add(ph_box)
                # make placeholder invisible (reserve layout space only)
                try:
                    ph.get_style_context().add_class('placeholder')
                    ph.set_sensitive(False)
                except Exception:
                    pass
                self.flow.add(ph)
        except Exception:
            pass
        self.flow.show_all()

    def _do_refresh_thumbs(self):
        """Called by the timeout; trigger a single refresh and clear timer."""
        try:
            self.refresh_clips()
        except Exception:
            pass
        try:
            # clear stored timer id
            self._thumb_resize_timer = None
        except Exception:
            pass
        return False

    def _probe_duration(self, path):
        """Return duration in seconds for `path`, using ffprobe or GStreamer as fallback."""
        try:
            if shutil.which('ffprobe'):
                out = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', path], text=True, errors='ignore')
                try:
                    return float(out.strip())
                except Exception:
                    pass
            if Gst:
                try:
                    pb = Gst.ElementFactory.make('playbin', 'probe')
                    pb.set_property('uri', Gst.filename_to_uri(path))
                    pb.set_state(Gst.State.PAUSED)
                    ok, dur = pb.query_duration(Gst.Format.TIME)
                    if ok and dur > 0:
                        val = dur / Gst.SECOND
                        pb.set_state(Gst.State.NULL)
                        return val
                    pb.set_state(Gst.State.NULL)
                except Exception:
                    pass
        except Exception:
            pass
        return 0.0

    # --- Undo / Redo support for basic edit actions ---
    def _undo(self):
        try:
            if not self._undo_stack:
                self._alert('Nothing to undo')
                return
            act = self._undo_stack.pop()
            if act['type'] == 'split':
                # remove generated parts and restore original if possible
                try:
                    if os.path.exists(act.get('out1')):
                        os.remove(act.get('out1'))
                    if os.path.exists(act.get('out2')):
                        os.remove(act.get('out2'))
                    # nothing to restore for original file since it remains
                except Exception:
                    pass
            # push to redo stack
            try:
                self._redo_stack.append(act)
            except Exception:
                pass
            self.refresh_clips()
            self._populate_editor_list()
        except Exception:
            pass

    def _redo(self):
        try:
            if not self._redo_stack:
                self._alert('Nothing to redo')
                return
            act = self._redo_stack.pop()
            if act['type'] == 'split':
                # attempt to re-create split outputs by re-running ffmpeg
                try:
                    p = act.get('original')
                    # best-effort: if originals exist, re-split at same point by comparing filenames
                    if os.path.exists(p) and shutil.which('ffmpeg'):
                        # no stored split time; attempt a simple re-split by creating parts of equal length
                        dur = None
                        try:
                            meta = self._media_meta.get(os.path.basename(p), {})
                            dur = float(meta.get('duration') or 0)
                        except Exception:
                            dur = None
                        if dur and dur > 1:
                            t = dur / 2.0
                            cmd1 = ['ffmpeg', '-y', '-i', p, '-t', str(t), '-c', 'copy', act.get('out1')]
                            cmd2 = ['ffmpeg', '-y', '-i', p, '-ss', str(t), '-c', 'copy', act.get('out2')]
                            subprocess.check_call(cmd1)
                            subprocess.check_call(cmd2)
                except Exception:
                    pass
            # push back to undo
            try:
                self._undo_stack.append(act)
            except Exception:
                pass
            self.refresh_clips()
            self._populate_editor_list()
        except Exception:
            pass

    def _on_key_press(self, widget, event):
        # simple keyboard shortcuts
        try:
            key = Gdk.keyval_name(event.keyval).lower() if event.keyval else ''
        except Exception:
            key = ''
        state = event.state
        # Space toggles play/pause
        if key == 'space':
            try:
                if getattr(self, 'play_toggle', None):
                    self.play_toggle.set_active(not self.play_toggle.get_active())
            except Exception:
                pass
            return True
        # 's' split
        if key == 's':
            try:
                sel = self.get_selected_clip()
                if sel:
                    # find the on-screen split button and invoke
                    # simply call split logic using timeline slider value
                    # reuse split logic by constructing a fake event
                    # call per-tile split by invoking a helper: create one here
                    if not shutil.which('ffmpeg'):
                        self._alert('ffmpeg required for split')
                        return True
                    split_at = 0
                    try:
                        split_at = float(getattr(self, 'timeline', None).get_value() if getattr(self, 'timeline', None) else 0)
                    except Exception:
                        split_at = 0
                    if split_at <= 0:
                        self._alert('Choose a split position > 0 seconds')
                        return True
                    # create the split using temporary calls similar to tile split
                    base = os.path.splitext(sel)[0]
                    ext = os.path.splitext(sel)[1]
                    out1 = f"{base}_part1{ext}"
                    out2 = f"{base}_part2{ext}"
                    idx = 1
                    while os.path.exists(out1) or os.path.exists(out2):
                        out1 = f"{base}_part1_{idx}{ext}"
                        out2 = f"{base}_part2_{idx}{ext}"
                        idx += 1
                    try:
                        cmd1 = ['ffmpeg', '-y', '-i', sel, '-t', str(split_at), '-c', 'copy', out1]
                        cmd2 = ['ffmpeg', '-y', '-i', sel, '-ss', str(split_at), '-c', 'copy', out2]
                        subprocess.check_call(cmd1)
                        subprocess.check_call(cmd2)
                        act = {'type': 'split', 'original': sel, 'out1': out1, 'out2': out2}
                        self._undo_stack.append(act)
                        self._redo_stack.clear()
                        self.refresh_clips()
                        self._populate_editor_list()
                        self._alert(f'Split created: {os.path.basename(out1)}, {os.path.basename(out2)}')
                    except Exception as e:
                        self._alert(f'Split failed: {e}')
            except Exception:
                pass
            return True
        # Ctrl+Z undo
        if ('control-mask' in dir(Gdk) and (state & Gdk.ModifierType.CONTROL_MASK)) or (state & Gdk.ModifierType.CONTROL_MASK):
            if key == 'z':
                try:
                    self._undo()
                except Exception:
                    pass
                return True
            if key == 'y':
                try:
                    self._redo()
                except Exception:
                    pass
                return True
        # Delete -> remove selected
        if key in ('delete', 'backspace'):
            try:
                sel = self.get_selected_clip()
                if sel:
                    # find matching tile's delete handler by invoking on_del via removing file
                    try:
                        move_to_trash(sel)
                        self.refresh_clips()
                        self._populate_editor_list()
                    except Exception:
                        pass
            except Exception:
                pass
            return True
        return False

    def get_selected_clip(self):
        return self.selected_clip

    def select_clip(self, path: str):
        """Select a clip for preview; if currently playing, switch playback to this clip."""
        try:
            self.selected_clip = path
            # update preview: use thumbnail if present
            thumb = os.path.join(self.thumbs_dir, os.path.basename(path) + '.png')
            if os.path.exists(thumb):
                try:
                    self._set_image_cover(self.preview_widget, thumb, 640, 360, overfill=0)
                except Exception:
                    pass
            else:
                # clear preview widget and add placeholder image as main child
                try:
                    # if currently playing with an embedded sink, do not replace it
                    if getattr(self, 'playbin', None) and getattr(self, 'play_toggle', None) and self.play_toggle.get_active() and getattr(self, '_gst_sink', None):
                        # leave the embedded sink widget in place
                        pass
                    else:
                        for c in list(self.preview_container.get_children()):
                            try:
                                if c is self._spinner:
                                    continue
                                self.preview_container.remove(c)
                            except Exception:
                                pass
                        try:
                            self.preview_container.add(self.preview_widget)
                        except Exception:
                            try:
                                self.preview_container.add(self.preview_widget)
                            except Exception:
                                pass
                        self.preview_widget.show()
                except Exception:
                    pass

            # If already playing, switch the playbin to the new clip immediately
            try:
                if Gst and getattr(self, 'playbin', None) and getattr(self, 'play_toggle', None) and self.play_toggle.get_active():
                    try:
                        self._show_spinner()
                    except Exception:
                        pass
                    pb = self._create_player(path)
                    if pb:
                        self.playbin_uri = path
                        GLib.idle_add(self._embed_sink_widget)
                        try:
                            self.playbin.set_state(Gst.State.PLAYING)
                        except Exception:
                            pass
                    else:
                        # failed to create player: hide spinner
                        try:
                            self._hide_spinner()
                        except Exception:
                            pass
            except Exception:
                pass
        except Exception:
            pass
        # Ensure we have an accurate duration cached and update trim tool
        try:
            base = os.path.basename(path)
            meta = self._media_meta.get(base, {})
            dur = float(meta.get('duration') or 0)
            if not dur:
                # probe duration now
                try:
                    d = self._probe_duration(path)
                    if d and d > 0:
                        dur = d
                        self._media_meta[base] = meta = meta or {}
                        self._media_meta[base]['duration'] = dur
                except Exception:
                    pass
            if getattr(self, 'trim_tool', None):
                try:
                    # old TrimTool.set_range signature was (duration, start, end)
                    # new Timeline.set_range expects (start, end)
                    # set timeline/project range and initialize trim to full duration
                    try:
                        self.trim_tool.set_range(0, dur or 0)
                    except Exception:
                        try:
                            # fallback for safety
                            self.trim_tool.set_trim(0, dur or 0)
                        except Exception:
                            pass
                    try:
                        self.trim_tool.set_trim(0, dur or 0)
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception:
            pass

    def on_trim(self, _):
        sel = self.get_selected_clip()
        if not sel:
            self._alert("Select a clip first")
            return
        try:
            if getattr(self, 'trim_tool', None):
                start, end = self.trim_tool.get_trim()
            else:
                # fallback to previous entries if trim_tool unavailable
                try:
                    start = float(self.start_entry.get_text())
                    end = float(self.end_entry.get_text())
                except Exception:
                    self._alert("Enter valid start/end times in seconds")
                    return
        except Exception:
            self._alert("Enter valid start/end times in seconds")
            return
        out = recorder.trim_clip(sel, start, end)
        self._alert(f"Trimmed -> {out}")
        self.refresh_clips()

    def on_save_as(self, _):
        sel = self.get_selected_clip()
        if not sel:
            self._alert("Select a clip first")
            return
        dialog = Gtk.FileChooserDialog(title="Save As", parent=self, action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dialog.set_do_overwrite_confirmation(False)
        dialog.set_current_name(os.path.basename(sel))
        res = dialog.run()
        if res == Gtk.ResponseType.OK:
            dest = dialog.get_filename()
            # if dest exists, append timestamp
            if os.path.exists(dest):
                base, ext = os.path.splitext(dest)
                dest = f"{base}_{int(time.time())}{ext}"
            try:
                # if trim_tool present, export trimmed section instead of raw copy
                if getattr(self, 'trim_tool', None):
                    s, e = self.trim_tool.get_trim()
                    # use ffmpeg to export trimmed area
                    if shutil.which('ffmpeg'):
                        cmd = ['ffmpeg', '-y', '-ss', str(s), '-to', str(e), '-i', sel, '-c', 'copy', dest]
                        subprocess.Popen(cmd)
                    else:
                        shutil.copy2(sel, dest)
                else:
                    shutil.copy2(sel, dest)
                self._alert(f"Saved -> {dest}")
            except Exception as e:
                self._alert(f"Save failed: {e}")
        dialog.destroy()

    def on_play(self, _):
        sel = self.get_selected_clip()
        if not sel:
            self._alert("Select a clip first")
            return
        # Use embedded GStreamer player when available
        if Gst:
            try:
                # toggle behavior: if play_toggle is active -> play, else pause
                active = getattr(self, 'play_toggle', None) and self.play_toggle.get_active()
                if not getattr(self, 'playbin', None) or getattr(self, 'playbin_uri', None) != sel:
                    # create new player for selected clip
                    pb = self._create_player(sel)
                    if not pb:
                        # embedded sink (gtksink) not available — inform user once
                        if not getattr(self, '_gst_warn_shown', False):
                            try:
                                self._alert('Embedded GStreamer sink (gtksink) not available. Install the gstreamer GTK sink plugin (e.g. gstreamer1.0-gtk) to enable in-app preview.')
                            except Exception:
                                pass
                            self._gst_warn_shown = True
                        return
                    self.playbin_uri = sel
                    # embed sink widget if gtksink available or appsink fallback is in use
                    if getattr(self, '_gst_sink', None) or getattr(self, '_appsink', None):
                        GLib.idle_add(self._embed_sink_widget)
                # set desired state
                if active:
                    # If Play Range is active, ensure we start at trim start
                    try:
                        if getattr(self, 'play_selection_toggle', None) and getattr(self.play_selection_toggle, 'get_active', None) and self.play_selection_toggle.get_active():
                            try:
                                ts = getattr(self.timeline, 'trim_start', None)
                                te = getattr(self.timeline, 'trim_end', None)
                                cur_ph = getattr(self.timeline, 'playhead', None)
                                # only seek if playhead is outside the trim range
                                if ts is not None and (cur_ph is None or cur_ph < ts or (te is not None and cur_ph > te)):
                                    try:
                                        self.playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, int(float(ts) * Gst.SECOND))
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        self.playbin.set_state(Gst.State.PLAYING)
                    except Exception:
                        pass
                    # start periodic update
                    try:
                        if not getattr(self, '_pos_update_id', None):
                            self._pos_update_id = GLib.timeout_add(300, self._update_position)
                    except Exception:
                        pass
                else:
                    try:
                        if getattr(self, 'playbin', None):
                            self.playbin.set_state(Gst.State.PAUSED)
                    except Exception:
                        pass
                    try:
                        if getattr(self, '_pos_update_id', None):
                            GLib.source_remove(self._pos_update_id)
                            self._pos_update_id = None
                    except Exception:
                        pass
                # update play/pause icon
                try:
                    if getattr(self, 'play_img', None):
                        if active:
                            try:
                                self.play_img.set_from_icon_name('media-playback-pause', Gtk.IconSize.MENU)
                            except Exception:
                                pass
                        else:
                            try:
                                self.play_img.set_from_icon_name('media-playback-start', Gtk.IconSize.MENU)
                            except Exception:
                                pass
                except Exception:
                    pass
                return
            except Exception:
                # if something unexpected happened, do not fall back to external popout
                return

        # fallback: external player
        if shutil.which("mpv"):
            subprocess.Popen(["mpv", sel])
        else:
            subprocess.Popen(["xdg-open", sel])

    def _update_position(self):
        try:
            if not Gst or not getattr(self, 'playbin', None):
                return False
            ok, pos = self.playbin.query_position(Gst.Format.TIME)
            ok2, dur = self.playbin.query_duration(Gst.Format.TIME)
            if ok and ok2 and dur > 0:
                cur = pos / Gst.SECOND
                total = dur / Gst.SECOND
                GLib.idle_add(self._update_position_ui, cur, total)
            return True
        except Exception:
            return False

    def _format_hms(self, secs: float) -> str:
        try:
            s = int(secs)
            h = s // 3600
            m = (s % 3600) // 60
            sec = s % 60
            return f"{h}:{m:02d}:{sec:02d}"
        except Exception:
            return "0:00:00"

    def _update_position_ui(self, cur, total):
        try:
            # update label
            try:
                self.time_label.set_text(f"{self._format_hms(cur)} / {self._format_hms(total)}")
            except Exception:
                pass
                # update timeline range only when duration changes
            try:
                last_total = getattr(self, '_last_timeline_total', None)
                if last_total is None or abs((total or 0) - (last_total or 0)) > 0.1:
                    try:
                        self.timeline.set_range(0, max(1, total))
                    except Exception:
                        pass
                    self._last_timeline_total = total
                # throttle playhead set to avoid flooding UI
                last = getattr(self, '_last_playhead_update', None)
                if last is None or abs((cur or 0) - (last or 0)) > 0.1:
                    try:
                        # update without forcing an immediate redraw every time
                        try:
                            self.timeline.set_value_silent(cur)
                        except Exception:
                            self.timeline.set_value(cur)
                    except Exception:
                        pass
                    self._last_playhead_update = cur
                # if play-selection mode, stop at trim end
                try:
                    if getattr(self, 'play_selection_toggle', None) and getattr(self.play_selection_toggle, 'get_active', None) and self.play_selection_toggle.get_active():
                        te = getattr(self.timeline, 'trim_end', None)
                        if te is not None and cur >= te:
                            try:
                                if getattr(self, 'playbin', None):
                                    self.playbin.set_state(Gst.State.PAUSED)
                            except Exception:
                                pass
                            try:
                                if getattr(self, 'play_toggle', None):
                                    self.play_toggle.set_active(False)
                            except Exception:
                                pass
                except Exception:
                    pass
            except Exception:
                pass
            # finished UI update
        except Exception:
            pass
        return False

    def _alert(self, msg):
        # Prefer a non-blocking desktop notification for informational alerts.
        try:
            if Notify:
                try:
                    n = Notify.Notification.new("Nova Replay", str(msg), None)
                    n.show()
                    return
                except Exception:
                    pass
        except Exception:
            pass
        # Fallback to `notify-send` command if available
        try:
            subprocess.Popen(["notify-send", "Nova Replay", str(msg)])
            return
        except Exception:
            pass

        # Final fallback: modal GTK message dialog
        try:
            dialog = Gtk.MessageDialog(transient_for=self, flags=0, message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.OK, text=str(msg))
            dialog.run()
            dialog.destroy()
        except Exception:
            # last resort: print to stderr
            try:
                sys.stderr.write(str(msg) + "\n")
            except Exception:
                pass

    def _fill_editor_list(self, on_activate_cb):
        try:
            # clear existing
            for c in list(getattr(self, 'editor_flow', []).get_children() if hasattr(self, 'editor_flow') else []):
                try:
                    self.editor_flow.remove(c)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            files = sorted([f for f in os.listdir(recorder.RECORDINGS_DIR) if os.path.isfile(os.path.join(recorder.RECORDINGS_DIR, f))]) if os.path.exists(recorder.RECORDINGS_DIR) else []
            # populate flow with thumbnails
            for f in files:
                path = os.path.join(recorder.RECORDINGS_DIR, f)
                tile = Gtk.EventBox()
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                # thumbnail image if available
                thumb = os.path.join(self.thumbs_dir, f + '.png')
                if os.path.exists(thumb):
                    try:
                        pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(thumb, 160, 90, True)
                        img = Gtk.Image.new_from_pixbuf(pix)
                    except Exception:
                        img = Gtk.Image.new_from_icon_name('video-x-generic', Gtk.IconSize.DIALOG)
                else:
                    img = Gtk.Image.new_from_icon_name('video-x-generic', Gtk.IconSize.DIALOG)
                try:
                    img.set_size_request(160, 90)
                except Exception:
                    pass
                lbl = Gtk.Label(label=os.path.basename(f))
                lbl.set_ellipsize(Pango.EllipsizeMode.END)
                lbl.set_max_width_chars(18)
                lbl.get_style_context().add_class('dim-label')
                box.pack_start(img, False, False, 0)
                box.pack_start(lbl, False, False, 0)
                tile.add(box)
                # clickable: create a lightweight row-like object for callback
                row_like = type('R', (), {})()
                row_like._path = path
                tile.connect('button-press-event', lambda w, e, r=row_like: on_activate_cb(None, r))
                try:
                    self.editor_flow.add(tile)
                except Exception:
                    pass
            try:
                # ensure flow is visible
                self.editor_flow.show_all()
            except Exception:
                pass
        except Exception:
            pass
        try:
            if hasattr(self, 'editor_flow'):
                self.editor_flow.show_all()
        except Exception:
            pass
        try:
            if hasattr(self, 'editor_flow'):
                self.editor_flow.show_all()
        except Exception:
            pass

    # Timeline helpers
    def _refresh_timeline(self):
        try:
            # convert project_timeline into items for the single timeline widget
            items = []
            for it in self.project_timeline:
                items.append({'filename': it.get('filename'), 'start': float(it.get('start', 0)), 'duration': float(it.get('duration', 0)), 'label': it.get('label')})
            if getattr(self, 'timeline', None):
                try:
                    self.timeline.set_items(items)
                except Exception:
                    pass
        except Exception:
            pass

    def _on_timeline_select(self, idx):
        try:
            self.timeline_selected_idx = idx
            if idx is None:
                return
            # store snapshot for undo before edits begin
            try:
                self._timeline_edit_snapshot = copy.deepcopy(self.project_timeline)
            except Exception:
                self._timeline_edit_snapshot = None
            # center selection: select corresponding clip in editor list if present
            try:
                sel_item = self.project_timeline[idx]
                path = sel_item.get('filename')
                if path:
                    self.select_clip(path)
            except Exception:
                pass
        except Exception:
            pass

    def _on_timeline_changed(self, items):
        """Called when the timeline reports a finalized change (after drag).

        `items` is a list of item dicts with keys `filename`, `start`, `duration`, `label`.
        """
        try:
            # before snapshot may have been stored when drag started
            before = getattr(self, '_timeline_edit_snapshot', None)
            if before is None:
                try:
                    before = copy.deepcopy(self.project_timeline)
                except Exception:
                    before = []
            # build new project timeline from items
            new_proj = []
            for it in items:
                try:
                    new_proj.append({'filename': it.get('filename'), 'start': float(it.get('start', 0)), 'duration': float(it.get('duration', 0)), 'label': it.get('label')})
                except Exception:
                    pass
            # record undo action
            try:
                act = {'type': 'timeline_edit', 'before': before, 'after': copy.deepcopy(new_proj)}
                self._undo_stack.append(act)
                self._redo_stack.clear()
            except Exception:
                pass
            # update model and redraw
            try:
                self.project_timeline = new_proj
                self._refresh_timeline()
            except Exception:
                pass
            # clear transient snapshot
            try:
                self._timeline_edit_snapshot = None
            except Exception:
                pass
        except Exception:
            pass

    # --- GStreamer player integration for preview ---
    def _create_player(self, path):
        if not Gst:
            return None
        try:
            # stop existing
            try:
                if hasattr(self, 'playbin') and self.playbin:
                    self.playbin.set_state(Gst.State.NULL)
            except Exception:
                pass
            pb = Gst.ElementFactory.make('playbin', 'playbin')
            if not pb:
                return None
            # prefer gtksink first (embeddable), then try glimagesink only if embeddable,
            # then vaapisink as a last resort. This avoids sinks that pop out their own window.
            sink = None
            try:
                sink = Gst.ElementFactory.make('gtksink', 'gtksink')
            except Exception:
                sink = None
            if not sink:
                try:
                    tmp = Gst.ElementFactory.make('glimagesink', 'glimagesink')
                    # ensure the sink provides an embeddable widget property (some builds pop out)
                    embeddable = False
                    try:
                        _ = tmp.props.widget
                        embeddable = True
                    except Exception:
                        embeddable = False
                    if embeddable:
                        sink = tmp
                    else:
                        try:
                            tmp.set_state(Gst.State.NULL)
                        except Exception:
                            pass
                        sink = None
                except Exception:
                    sink = None
            # last resort: vaapisink
            if not sink:
                try:
                    sink = Gst.ElementFactory.make('vaapisink', 'vaapisink')
                except Exception:
                    sink = None
            if sink:
                try:
                    pb.set_property('video-sink', sink)
                    self._gst_sink = sink
                    self._appsink = None
                    try:
                        fac = sink.get_factory()
                        self._chosen_video_sink = fac.get_name() if fac else sink.get_name()
                    except Exception:
                        self._chosen_video_sink = getattr(sink, 'name', str(sink))
                except Exception:
                    self._gst_sink = None
                    self._appsink = None
                    self._chosen_video_sink = None
            else:
                # gtksink not available: create an appsink pipeline and render into a Gtk.DrawingArea
                try:
                    conv = Gst.ElementFactory.make('videoconvert', 'conv')
                    appsink = Gst.ElementFactory.make('appsink', 'appsink')
                    if not conv or not appsink:
                        return None
                    # request packed RGB frames from the convert element
                    try:
                        caps = Gst.Caps.from_string('video/x-raw,format=RGB')
                        appsink.set_property('caps', caps)
                    except Exception:
                        pass
                    appsink.set_property('emit-signals', True)
                    appsink.set_property('max-buffers', 2)
                    appsink.set_property('drop', True)

                    sink_bin = Gst.Bin.new('appsink-bin')
                    sink_bin.add(conv)
                    sink_bin.add(appsink)
                    conv.link(appsink)

                    # expose a ghost pad so playbin can link to this bin
                    pad = conv.get_static_pad('sink')
                    ghost = Gst.GhostPad.new('sink', pad)
                    sink_bin.add_pad(ghost)

                    pb.set_property('video-sink', sink_bin)
                    self._appsink = appsink
                    self._gst_sink = None
                except Exception:
                    return None
            # set uri
            uri = Gst.filename_to_uri(path)
            pb.set_property('uri', uri)
            # store
            self.playbin = pb
            try:
                # record chosen sink name for diagnostics
                if getattr(self, '_chosen_video_sink', None):
                    GLib.idle_add(lambda: setattr(self, '_last_used_sink', self._chosen_video_sink))
            except Exception:
                pass
            # attach bus
            bus = pb.get_bus()
            bus.add_signal_watch()
            bus.connect('message', self._on_bus_message)

            # if using appsink, hook new-sample
            try:
                if getattr(self, '_appsink', None):
                    self._appsink.connect('new-sample', self._on_appsink_new_sample)
            except Exception:
                pass
            return pb
        except Exception:
            return None

    def _embed_sink_widget(self):
        try:
            # if using gtksink, embed its widget
            if getattr(self, '_gst_sink', None):
                widget = self._gst_sink.props.widget
                # if the sink widget is already present, ensure it's sized and return
                try:
                    for c in list(self.preview_container.get_children()):
                        if c is widget:
                            try:
                                widget.set_hexpand(True)
                                widget.set_vexpand(True)
                                widget.set_halign(Gtk.Align.FILL)
                                widget.set_valign(Gtk.Align.FILL)
                                widget.set_size_request(640, 360)
                            except Exception:
                                pass
                            widget.show()
                            self.preview_container.show()
                            return
                except Exception:
                    pass
                # remove previous non-spinner children
                for c in list(self.preview_container.get_children()):
                    try:
                        if c is self._spinner:
                            continue
                        self.preview_container.remove(c)
                    except Exception:
                        pass
                # add sink widget as main child
                try:
                    # ensure sink widget can expand to fill the preview area
                    try:
                        widget.set_hexpand(True)
                        widget.set_vexpand(True)
                        widget.set_halign(Gtk.Align.FILL)
                        widget.set_valign(Gtk.Align.FILL)
                        widget.set_size_request(640, 360)
                    except Exception:
                        pass
                    self.preview_container.add(widget)
                except Exception:
                    try:
                        self.preview_container.add(widget)
                    except Exception:
                        pass
                widget.show()
                self.preview_container.show()
                return

            # if using appsink fallback, create/attach a drawing area and render frames
            if getattr(self, '_appsink', None):
                # remove previous non-spinner children
                for c in list(self.preview_container.get_children()):
                    try:
                        if c is self._spinner:
                            continue
                        self.preview_container.remove(c)
                    except Exception:
                        pass
                # create drawing area if needed
                if not getattr(self, '_appsink_draw', None):
                    da = Gtk.DrawingArea()
                    # reduce drawing area min size so the window can shrink
                    da.set_size_request(640, 360)
                    da.connect('draw', self._on_draw_appsink)
                    self._appsink_draw = da
                else:
                    da = self._appsink_draw
                # add drawing area as main child
                self.preview_container.add(da)
                da.show()
                self.preview_container.show()
                return
        except Exception:
            pass

    def _show_spinner(self):
        try:
            if getattr(self, '_spinner', None):
                try:
                    self._spinner.show()
                    self._spinner.start()
                except Exception:
                    pass
        except Exception:
            pass

    def _hide_spinner(self):
        try:
            if getattr(self, '_spinner', None):
                try:
                    self._spinner.stop()
                    self._spinner.hide()
                except Exception:
                    pass
        except Exception:
            pass

    def _on_appsink_new_sample(self, appsink):
        try:
            sample = appsink.emit('pull-sample')
            if not sample:
                return Gst.FlowReturn.ERROR
            buf = sample.get_buffer()
            caps = sample.get_caps()
            try:
                s = caps.get_structure(0)
                w = s.get_value('width')
                h = s.get_value('height')
            except Exception:
                w = None; h = None
            # map buffer and copy data
            success, mapinfo = buf.map(Gst.MapFlags.READ)
            if not success:
                return Gst.FlowReturn.ERROR
            try:
                raw = bytes(mapinfo.data)
            except Exception:
                raw = None
            finally:
                try:
                    buf.unmap(mapinfo)
                except Exception:
                    pass
            if raw and w and h:
                # basic diagnostics
                try:
                    # detect likely pixel format by buffer size
                    expected_rgb = int(w) * int(h) * 3
                    expected_rgba = int(w) * int(h) * 4
                    pb = None
                    if len(raw) == expected_rgb:
                        try:
                            rowstride = int(w) * 3
                            pb = GdkPixbuf.Pixbuf.new_from_data(raw, GdkPixbuf.Colorspace.RGB, False, 8, int(w), int(h), int(rowstride))
                        except Exception:
                            pb = None
                    elif len(raw) == expected_rgba:
                        try:
                            rowstride = int(w) * 4
                            pb = GdkPixbuf.Pixbuf.new_from_data(raw, GdkPixbuf.Colorspace.RGB, True, 8, int(w), int(h), int(rowstride))
                        except Exception:
                            pb = None
                    else:
                        # try best-effort with RGB rowstride, may still work if stride includes padding
                        try:
                            rowstride = int(w) * 3
                            pb = GdkPixbuf.Pixbuf.new_from_data(raw, GdkPixbuf.Colorspace.RGB, False, 8, int(w), int(h), int(rowstride))
                        except Exception:
                            pb = None

                    if not pb:
                        pass
                    else:
                        # keep a reference to raw data to prevent GC
                        self._appsink_frame_ref = raw
                        # store original pixbuf and invalidate any cached scaled version
                        self._appsink_pixbuf_orig = pb
                        try:
                            if hasattr(self, '_appsink_scaled_pixbuf'):
                                del self._appsink_scaled_pixbuf
                            self._appsink_last_alloc = None
                        except Exception:
                            pass
                        # ensure drawing area exists and queue a redraw
                        if getattr(self, '_appsink_draw', None):
                            GLib.idle_add(self._appsink_draw.queue_draw)
                        # ensure spinner hidden once first frame arrives
                        try:
                            GLib.idle_add(self._hide_spinner)
                        except Exception:
                            pass
                except Exception:
                    pass
            return Gst.FlowReturn.OK
        except Exception:
            return Gst.FlowReturn.ERROR

    def _appsink_fade_step(self):
        # fade logic removed; no-op
        return False

    def _on_draw_appsink(self, widget, cr):
        try:
            if getattr(self, '_appsink_pixbuf_orig', None):
                pb = self._appsink_pixbuf_orig
                # scale pixbuf to widget allocation while preserving aspect
                alloc = widget.get_allocation()
                iw = pb.get_width(); ih = pb.get_height()
                if iw > 0 and ih > 0:
                    # compute scale
                    scale = min(alloc.width / iw, alloc.height / ih)
                    nw = int(iw * scale)
                    nh = int(ih * scale)
                    # reuse cached scaled pixbuf when possible
                    need_rescale = True
                    try:
                        last = getattr(self, '_appsink_last_alloc', None)
                        if last and last[0] == alloc.width and last[1] == alloc.height and getattr(self, '_appsink_scaled_pixbuf', None):
                            need_rescale = False
                    except Exception:
                        need_rescale = True
                    if need_rescale:
                        try:
                            self._appsink_scaled_pixbuf = pb.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
                            self._appsink_last_alloc = (alloc.width, alloc.height)
                        except Exception:
                            try:
                                self._appsink_scaled_pixbuf = pb.scale_simple(nw, nh, GdkPixbuf.InterpType.NEAREST)
                                self._appsink_last_alloc = (alloc.width, alloc.height)
                            except Exception:
                                self._appsink_scaled_pixbuf = None
                    scaled = getattr(self, '_appsink_scaled_pixbuf', None)
                    if scaled:
                        x = int((alloc.width - nw) / 2)
                        y = int((alloc.height - nh) / 2)
                        Gdk.cairo_set_source_pixbuf(cr, scaled, x, y)
                        try:
                            cr.paint()
                        except Exception:
                            pass
                        return False
        except Exception:
            pass
        # draw placeholder background if no frame
        try:
            cr.set_source_rgb(0.06, 0.06, 0.06)
            alloc = widget.get_allocation()
            cr.rectangle(0, 0, alloc.width, alloc.height)
            cr.fill()
        except Exception:
            pass
        return False

    def _draw_background(self, widget, cr):
        try:
            if not getattr(self, '_bg_pixbuf_orig', None):
                return False
            alloc = widget.get_allocation()
            aw, ah = alloc.width, alloc.height
            if aw <= 0 or ah <= 0:
                return False
            ow = self._bg_pixbuf_orig.get_width()
            oh = self._bg_pixbuf_orig.get_height()
            try:
                scale = max(float(aw) / float(ow), float(ah) / float(oh)) if ow and oh else 1.0
            except Exception:
                scale = 1.0
            new_w = max(1, int(ow * scale))
            new_h = max(1, int(oh * scale))
            scaled = self._bg_pixbuf_orig.scale_simple(new_w, new_h, GdkPixbuf.InterpType.BILINEAR)
            x = int((aw - new_w) / 2)
            y = int((ah - new_h) / 2)
            try:
                Gdk.cairo_set_source_pixbuf(cr, scaled, x, y)
                cr.paint()
            except Exception:
                pass
        except Exception:
            pass
        return False

    def _start_fade_out(self):
        # fade-out removed; no-op
        return

    def _appsink_fade_out_step(self):
        # fade-out removed; no-op
        return False

    def _on_bus_message(self, bus, msg):
        t = msg.type
        try:
            if t == Gst.MessageType.EOS:
                try:
                    self.playbin.set_state(Gst.State.PAUSED)
                except Exception:
                    pass
                GLib.idle_add(lambda: self.play_toggle.set_active(False))
            elif t == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                GLib.idle_add(lambda: self._alert(f'GStreamer error: {err}'))
            elif t == Gst.MessageType.DURATION_CHANGED:
                self._update_duration()
        except Exception:
            pass

    def _update_duration(self):
        try:
            if not Gst or not getattr(self, 'playbin', None):
                return
            success, duration = self.playbin.query_duration(Gst.Format.TIME)
            if success and duration > 0:
                secs = duration / Gst.SECOND
                GLib.idle_add(self._set_time_label_duration, secs)
        except Exception:
            pass

    def _set_time_label_duration(self, secs):
        try:
            cur = 0
            if Gst and getattr(self, 'playbin', None):
                ok, pos = self.playbin.query_position(Gst.Format.TIME)
                if ok:
                    cur = pos / Gst.SECOND
            try:
                self.time_label.set_text(f"{self._format_hms(cur)} / {self._format_hms(secs)}")
            except Exception:
                pass
            # set timeline range
            try:
                self.timeline.set_range(0, max(1, secs))
            except Exception:
                pass
        except Exception:
            pass
    def _on_timeline_changed(self, widget):
        try:
            if not Gst or not getattr(self, 'playbin', None):
                return
            val = widget.get_value()
            seek_ns = int(val * Gst.SECOND)
            try:
                self.playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, seek_ns)
            except Exception:
                pass
        except Exception:
            pass

    def _on_timeline_seek(self, sec):
        try:
            if not Gst or not getattr(self, 'playbin', None):
                return
            seek_ns = int(float(sec) * Gst.SECOND)
            try:
                self.playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, seek_ns)
            except Exception:
                pass
        except Exception:
            pass


def _create_splash_window():
    # Use a normal top-level window but remove decorations (titlebar)
    splash = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
    # name the window so we can target it with CSS specifically
    try:
        splash.set_name('splash-window')
    except Exception:
        pass
    try:
        splash.set_app_paintable(True)
        splash.set_opacity(1.0)
    except Exception:
        pass
    # Use client-side decorations: let the window be decorated so we can
    # replace the titlebar with our own header via `set_titlebar()`
    splash.set_decorated(True)
    splash.set_keep_above(True)
    splash.set_position(Gtk.WindowPosition.CENTER)
    splash.set_default_size(480, 240)
    splash.set_resizable(False)

    # Apply a small, local CSS theme for the splash so it's jet-black and
    # uses client-side decorations for the headerbar.
    css = b"""
    /* Force a true jet-black background and remove any themed backgrounds */
    window#splash-window, #splash-window, .splash-header, .splash-box {
        background-color: #000000;
        background-image: none;
        background-repeat: no-repeat;
        color: #ffffff;
        box-shadow: none;
        border: none;
    }
    .splash-label { color: #ffffff; font-weight: 600; }
    """
    try:
        style_provider = Gtk.CssProvider()
        style_provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), style_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    except Exception:
        pass

    # client-side headerbar (CSD) — acts as the splash header and is fully themed by our CSS
    header = Gtk.HeaderBar()
    header.set_show_close_button(False)
    header.set_has_subtitle(False)
    header.get_style_context().add_class('splash-header')
    try:
        header.set_size_request(-1, 28)
    except Exception:
        pass

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_border_width(16)
    box.get_style_context().add_class('splash-box')
    try:
        box.set_hexpand(True)
        box.set_vexpand(True)
    except Exception:
        pass

    # Prepare container that will hold header/content. If set_titlebar
    # succeeds we won't pack the header into the container; if it fails
    # we will fall back to packing the header so the UI remains consistent.
    container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    try:
        splash.set_titlebar(header)
    except Exception:
        try:
            container.pack_start(header, False, False, 0)
        except Exception:
            pass
    try:
        container.set_hexpand(True)
        container.set_vexpand(True)
    except Exception:
        pass

    # logo if available
    try:
        logo_path = os.path.join(os.path.dirname(__file__), 'img', 'logo2.png')
        if os.path.exists(logo_path):
            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(logo_path, 128, 128, True)
            img = Gtk.Image.new_from_pixbuf(pix)
            box.pack_start(img, False, False, 0)
    except Exception:
        pass

    lbl = Gtk.Label(label="Loading Nova…")
    lbl.set_margin_top(6)
    lbl.get_style_context().add_class('splash-label')
    box.pack_start(lbl, False, False, 0)
    spinner = Gtk.Spinner()
    spinner.start()
    box.pack_start(spinner, False, False, 6)
    container.pack_start(box, True, True, 0)
    # add a drawing area behind the container to ensure a true opaque black background
    overlay = Gtk.Overlay()
    bg_draw = Gtk.DrawingArea()
    try:
        bg_draw.set_hexpand(True)
        bg_draw.set_vexpand(True)
    except Exception:
        pass

    def _draw_bg(widget, cr):
        try:
            cr.set_source_rgb(0.0, 0.0, 0.0)
            a = widget.get_allocation()
            cr.rectangle(0, 0, a.width, a.height)
            cr.fill()
        except Exception:
            pass
        return False

    try:
        bg_draw.connect('draw', _draw_bg)
    except Exception:
        pass
    overlay.add(bg_draw)
    overlay.add_overlay(container)
    splash.add(overlay)

    return splash, spinner


def main():
    # show a lightweight splash so the app appears responsive while we build UI
    splash, spinner = _create_splash_window()
    splash.show_all()

    app_holder = {'app': None}
    start_ts = time.time()

    def create_app_cb():
        # Create the main app on the main loop (GTK main thread) so widgets are safe
        try:
            app_holder['app'] = NovaReplayWindow()
            app_holder['app'].connect("destroy", app_holder['app'].on_destroy)
        except Exception:
            # if construction fails, ensure splash is removed and re-raise
            try:
                spinner.stop()
            except Exception:
                pass
            try:
                splash.destroy()
            except Exception:
                pass
            raise
        return False

    # schedule creation after splash rendered
    GLib.idle_add(create_app_cb)

    def checker():
        app = app_holder.get('app')
        elapsed = time.time() - start_ts
        # Enforce a minimum splash duration of 3 seconds.
        # Do not show the main window before `elapsed >= 3.0` even if loading finished earlier.
        if elapsed < 3.0:
            return True

        # elapsed >= 5s: create app synchronously if not yet created, then show it.
        if app is None:
            try:
                app_holder['app'] = NovaReplayWindow()
                app_holder['app'].connect("destroy", app_holder['app'].on_destroy)
            except Exception:
                pass

        try:
            spinner.stop()
        except Exception:
            pass
        try:
            splash.destroy()
        except Exception:
            pass
        try:
            if app_holder.get('app') is not None:
                app_holder['app'].show_all()
                try:
                    app_holder['app'].present()
                except Exception:
                    pass
        except Exception:
            pass
        return False

    GLib.timeout_add(200, checker)
    try:
        Gtk.main()
    except TypeError as e:
        msg = str(e)
        if "Couldn't find foreign struct converter for" in msg or 'cairo.Context' in msg:
            sys.stderr.write('\nFatal error: GTK cannot convert cairo.Context for draw callbacks.\n')
            sys.stderr.write('This usually means the pycairo bindings are missing or were not imported before GTK\n')
            sys.stderr.write('Please install pycairo (Debian/Ubuntu: sudo apt install python3-cairo python3-gi-cairo; or pip3 install pycairo)\n')
            sys.stderr.write('Then restart the application.\n')
        raise

if __name__ == '__main__':
    main()
