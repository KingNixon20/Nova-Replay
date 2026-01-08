#!/usr/bin/env python3
import os
import sys
import subprocess
import threading
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk, Pango, GdkPixbuf
import time
import recorder
import json
from thumbnail_renderer import render_decorated_thumbnail
import shutil

from datetime import datetime


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
            f.write(f'DeletionDate={datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S") }\n')
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

class NovaReplayWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Nova")
        self.set_default_size(1700, 950)

        # CSS styling for a modern look
        css = b"""
        .headerbar { background: #0f1720; color: #ffffff; }
        .primary-button { background: #ff4b4b; color: #fff; border-radius: 6px; padding: 6px; }
        .primary-button GtkLabel { color: #ffffff; }
        .primary-button.recording { background: #c0392b; }
        .recording { background: #c0392b; }
        .sidebar { background: #000000; color: #cfd8e3; padding: 2px; min-width: 48px; }
        .nav-button { background: transparent; color: #cfd8e3; border: none; padding: 0; margin: 2px; border-radius: 4px; min-width: 32px; min-height: 32px; }
        .nav-button GtkImage { margin: 4px; }
        .nav-button GtkLabel { color: #05021b; }
        .side-divider { background: #000000; }
        .selected-tile { border: 2px solid #2b6cb0; }
        .main-bg { background: #000000; }
        .clip-row { background: #1f1f1f; border-radius: 6px; padding: 6px; }
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
        .tile-lower { background: #00ff00; border-bottom-left-radius: 6px; border-bottom-right-radius: 6px; padding: 6px; }
        """
        style_provider = Gtk.CssProvider()
        style_provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), style_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Header bar
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.props.title = "Nova"
        hb.get_style_context().add_class('headerbar')

        # Primary Record button in header (nicer look)
        self.header_record_btn = Gtk.Button()
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.header_record_icon = Gtk.Image.new_from_icon_name('media-record', Gtk.IconSize.BUTTON)
        self.header_record_label = Gtk.Label(label='Record')
        btn_box.pack_start(self.header_record_icon, False, False, 0)
        btn_box.pack_start(self.header_record_label, False, False, 0)
        self.header_record_btn.add(btn_box)
        self.header_record_btn.set_size_request(110, 36)
        self.header_record_btn.get_style_context().add_class('primary-button')
        self.header_record_btn.connect('clicked', lambda w: self._toggle_recording_gui())
        hb.pack_end(self.header_record_btn)

        self.set_titlebar(hb)

        # Main layout: sidebar + content
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        main_box.get_style_context().add_class('main-bg')
        self.add(main_box)

        # Sidebar (smaller)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sidebar.set_size_request(48, -1)
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
        # Optional logo at top of sidebar
        logo_path = get_img_file('logo2.png')
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

        # Clips button (icon-only, scaled)
        btn_clips = Gtk.Button()
        clip_icon_path = get_img_file('clips.png')
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
        btn_clips.set_size_request(48, 48)
        btn_clips.set_relief(Gtk.ReliefStyle.NONE)
        try:
            btn_clips.set_hexpand(False)
            btn_clips.set_vexpand(False)
            btn_clips.set_halign(Gtk.Align.CENTER)
        except Exception:
            pass
        btn_clips.get_style_context().add_class('nav-button')
        btn_clips.connect("clicked", lambda w: self.content_stack.set_visible_child_name('clips'))
        # pack clips button immediately so subsequent buttons appear below it
        sidebar.pack_start(btn_clips, False, False, 2)

        # Editor button (under Clips)
        btn_editor = Gtk.Button()
        editor_icon_path = get_img_file('editor.png')
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
        btn_editor.set_size_request(48, 48)
        btn_editor.set_relief(Gtk.ReliefStyle.NONE)
        try:
            btn_editor.set_hexpand(False)
            btn_editor.set_vexpand(False)
            btn_editor.set_halign(Gtk.Align.CENTER)
        except Exception:
            pass
        btn_editor.get_style_context().add_class('nav-button')
        btn_editor.connect("clicked", lambda w: self.content_stack.set_visible_child_name('editor'))
        sidebar.pack_start(btn_editor, False, False, 2)

        # Settings button
        btn_settings = Gtk.Button()
        settings_icon_path = get_img_file('settings.png')
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
        btn_settings.set_size_request(48, 48)
        btn_settings.set_relief(Gtk.ReliefStyle.NONE)
        try:
            btn_settings.set_hexpand(False)
            btn_settings.set_vexpand(False)
            btn_settings.set_halign(Gtk.Align.CENTER)
        except Exception:
            pass
        btn_settings.get_style_context().add_class('nav-button')
        btn_settings.connect("clicked", lambda w: self.content_stack.set_visible_child_name('settings'))

        # keep settings anchored to the bottom
        sidebar.pack_end(btn_settings, False, False, 6)

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
                    self._sidebar_current_width = new
                    self.sidebar.set_size_request(new, -1)
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
            divider.set_size_request(1, -1)
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
        bg_path = get_img_file('bg.png')
        self._bg_path = bg_path if os.path.exists(bg_path) else None
        self._bg_img = Gtk.Image()
        if self._bg_path:
            # cache original pixbuf and add background as main child and stack content on top
            try:
                self._bg_pixbuf_orig = GdkPixbuf.Pixbuf.new_from_file(self._bg_path)
            except Exception:
                self._bg_pixbuf_orig = None
            content_overlay.add(self._bg_img)
            content_overlay.add_overlay(self.content_stack)

            def on_overlay_alloc(w, allocation):
                try:
                    if self._bg_pixbuf_orig and allocation.width > 0:
                        ow = self._bg_pixbuf_orig.get_width()
                        oh = self._bg_pixbuf_orig.get_height()
                        aw = allocation.width
                        # scale to fit width, keep aspect ratio
                        scale = float(aw) / float(ow) if ow else 1.0
                        new_w = max(1, int(ow * scale))
                        new_h = max(1, int(oh * scale))
                        scaled = self._bg_pixbuf_orig.scale_simple(new_w, new_h, GdkPixbuf.InterpType.BILINEAR)
                        GLib.idle_add(self._bg_img.set_from_pixbuf, scaled)
                        # keep image anchored to top
                        try:
                            self._bg_img.set_halign(Gtk.Align.CENTER)
                            self._bg_img.set_valign(Gtk.Align.START)
                        except Exception:
                            pass
                except Exception:
                    pass

            content_overlay.connect('size-allocate', on_overlay_alloc)
        else:
            # no bg image; just use stack directly
            content_overlay.add(self.content_stack)

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
            seg_container.pack_start(search_box, False, False, 6)
        except Exception:
            pass
        tabs = [('All Videos', 'all'), ('Full Sessions', 'full'), ('Clips', 'clips')]
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
                    self.clip_scrolled.set_vexpand(False)
                    try:
                        self.clip_scrolled.set_size_request(0, desired_h)
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

        # Editor view (placeholder)
        editor_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        ed_lbl = Gtk.Label(label='Editor')
        ed_lbl.get_style_context().add_class('dim-label')
        ed_lbl.set_xalign(0)
        editor_box.pack_start(ed_lbl, False, False, 8)
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
        self.backend_combo.append_text("wf-recorder (wayland)")
        self.backend_combo.append_text("pipewire (ffmpeg)")
        self.backend_combo.set_active(0)
        settings_box.pack_start(self.backend_combo, False, False, 0)

        # Hotkey entry (informational)
        hotkey_label = Gtk.Label(label="Toggle recording hotkey:")
        hotkey_label.set_xalign(0)
        settings_box.pack_start(hotkey_label, False, False, 0)
        self.hotkey_entry = Gtk.Entry()
        self.hotkey_entry.set_text("Ctrl+Alt+R")
        settings_box.pack_start(self.hotkey_entry, False, False, 0)

        # Output directory open button
        out_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        out_lbl = Gtk.Label(label="Recordings folder:")
        out_lbl.set_xalign(0)
        out_box.pack_start(out_lbl, True, True, 0)
        self.out_entry = Gtk.Entry()
        self.out_entry.set_text(recorder.RECORDINGS_DIR)
        self.out_entry.set_editable(False)
        out_box.pack_start(self.out_entry, True, True, 0)

        open_out = Gtk.Button(label="Open")
        def open_dir(_):
            path = recorder.RECORDINGS_DIR
            if os.path.exists(path):
                subprocess.Popen(["xdg-open", path])
            else:
                self._alert("Recordings folder not found")
        open_out.connect('clicked', open_dir)
        out_box.pack_start(open_out, False, False, 0)

        change_btn = Gtk.Button(label="Change...")
        def change_dir(_):
            dlg = Gtk.FileChooserDialog(title="Select Recordings Folder", parent=self, action=Gtk.FileChooserAction.SELECT_FOLDER)
            dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK)
            res = dlg.run()
            if res == Gtk.ResponseType.OK:
                newp = dlg.get_filename()
                try:
                    recorder.set_recordings_dir(newp)
                    self.out_entry.set_text(recorder.RECORDINGS_DIR)
                    # update thumbnails dir and refresh
                    self.thumbs_dir = os.path.join(recorder.RECORDINGS_DIR, 'thumbnails')
                    os.makedirs(self.thumbs_dir, exist_ok=True)
                    self.refresh_clips()
                    # persist immediately
                    try:
                        self.save_settings()
                    except Exception:
                        pass
                except Exception as e:
                    self._alert(f"Failed to set recordings folder: {e}")
            dlg.destroy()
        change_btn.connect('clicked', change_dir)
        out_box.pack_start(change_btn, False, False, 0)
        settings_box.pack_start(out_box, False, False, 0)

        # Apply/save (session-only)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect('clicked', lambda w: (self.save_settings(), self._alert("Settings saved")))
        settings_box.pack_end(apply_btn, False, False, 6)

        self.content_stack.add_titled(settings_box, 'settings', 'Settings')

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
        self.thumbs_dir = os.path.join(recorder.RECORDINGS_DIR, 'thumbnails')
        os.makedirs(self.thumbs_dir, exist_ok=True)
        # debounce timer id for thumbnail refresh during resize
        self._thumb_resize_timer = None
        self.refresh_clips()

        # Optional global hotkey manager (best-effort)
        self.hotkeys = HotkeyManager(self.toggle_recording)
        self.hotkeys.start()
        # mark that initialization finished (used by splash logic)
        try:
            self._loaded = True
        except Exception:
            self._loaded = True

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
            elif txt and 'wf-recorder' in txt:
                backend = 'wf-recorder'
            elif txt and 'pipewire' in txt:
                backend = 'pipewire'
        except Exception:
            backend = self.settings.get('preferred_backend', 'auto')
        hotkey = self.hotkey_entry.get_text() if hasattr(self, 'hotkey_entry') else self.settings.get('hotkey', 'Ctrl+Alt+R')
        cfg = {
            'recordings_dir': rec_dir,
            'preferred_backend': backend,
            'hotkey': hotkey,
        }
        try:
            with open(self.get_config_path(), 'w') as f:
                json.dump(cfg, f, indent=2)
            self.settings = cfg
        except Exception as e:
            raise

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
                # show stop
                self.header_record_icon.set_from_icon_name('media-playback-stop', Gtk.IconSize.BUTTON)
                self.header_record_label.set_text('Stop')
                self.header_record_btn.get_style_context().add_class('recording')
            else:
                # show record
                self.header_record_icon.set_from_icon_name('media-record', Gtk.IconSize.BUTTON)
                self.header_record_label.set_text('Record')
                try:
                    self.header_record_btn.get_style_context().remove_class('recording')
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
            elif txt and 'wf-recorder' in txt:
                pref = 'wf-recorder'
            elif txt and 'pipewire' in txt:
                pref = 'pipewire'
        except Exception:
            pref = 'auto'
        # warn if chosen backend binary likely missing
        try:
            if pref == 'wf-recorder' and not shutil.which('wf-recorder'):
                self._alert('wf-recorder not found; falling back to available backend')
            if pref in ('ffmpeg-x11', 'pipewire') and not shutil.which('ffmpeg'):
                self._alert('ffmpeg not found; recording will likely fail')
        except Exception:
            pass
        # let Recorder choose a timestamped filename to avoid overwriting
        self.recorder = recorder.Recorder(mode=mode, preferred_backend=pref)
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
            self.recorder.stop()
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
                tile_box.set_size_request(target_w, total_h)
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
                    img.set_size_request(target_w, img_h)
                except Exception:
                    try:
                        img.set_from_file(normal_decor)
                    except Exception:
                        pass
            else:
                img = Gtk.Image.new_from_icon_name('video-x-generic', Gtk.IconSize.DIALOG)
                try:
                    img.set_size_request(target_w, img_h)
                except Exception:
                    pass
                # generate thumbnail frame and decorated images in background if ffmpeg available
                if shutil.which('ffmpeg'):
                    def gen(p=path, tp=thumb_path, nb=decor_base, widget=img, tw=target_w, th=target_h, ih=img_h):
                        try:
                            # extract a frame sized to target
                            subprocess.call(['ffmpeg', '-y', '-ss', '00:00:01', '-i', p, '-vframes', '1', '-q:v', '2', '-s', f'{tw}x{ih}', tp])
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
                lower_box.set_size_request(tw, extra_h)
                lower_box.get_style_context().add_class('tile-lower')
            except Exception:
                pass
            # filename label removed from thumbnail view (keeps lower area for actions only)

            # clicking selects
            def on_click(ev, p=path, widget=tile):
                # simple selection highlight
                self.selected_clip = p
                for c in self.flow.get_children():
                    c.get_style_context().remove_class('selected-tile')
                widget.get_style_context().add_class('selected-tile')
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
            play = Gtk.Button()
            play.add(Gtk.Image.new_from_icon_name('media-playback-start', Gtk.IconSize.MENU))
            play.connect('clicked', lambda w, p=path: subprocess.Popen(['mpv', p]) if shutil.which('mpv') else subprocess.Popen(['xdg-open', p]))
            delb = Gtk.Button()
            delb.add(Gtk.Image.new_from_icon_name('user-trash', Gtk.IconSize.MENU))
            def on_del(_, p=path, tp=thumb_path, widget=tile, filename=f):
                # ask for confirmation, then move to Trash instead of permanent delete
                try:
                    dlg = Gtk.MessageDialog(self, 0, Gtk.MessageType.QUESTION, Gtk.ButtonsType.OK_CANCEL,
                                            f"Move '{os.path.basename(p)}' to Trash?")
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

            delb.connect('clicked', on_del)
            actions.pack_start(play, False, False, 0)
            # spacer to push the delete button to the right
            try:
                spacer = Gtk.Box()
                actions.pack_start(spacer, True, True, 0)
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

    def get_selected_clip(self):
        return self.selected_clip

    def on_trim(self, _):
        sel = self.get_selected_clip()
        if not sel:
            self._alert("Select a clip first")
            return
        try:
            start = float(self.start_entry.get_text())
            end = float(self.end_entry.get_text())
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
        if shutil.which("mpv"):
            subprocess.Popen(["mpv", sel])
        else:
            subprocess.Popen(["xdg-open", sel])

    def _alert(self, msg):
        dialog = Gtk.MessageDialog(self, 0, Gtk.MessageType.INFO, Gtk.ButtonsType.OK, msg)
        dialog.run()
        dialog.destroy()


def _create_splash_window():
    # Use a normal top-level window but remove decorations (titlebar)
    splash = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
    try:
        splash.set_decorated(False)
    except Exception:
        pass
    # keep above so it's visible while loading
    try:
        splash.set_keep_above(True)
    except Exception:
        pass
    # Some window managers ignore set_decorated; force override-redirect once
    # the toplevel is realized so the WM will not draw decorations.
    def _on_splash_realize(w):
        try:
            gw = w.get_window()
            if gw is not None:
                gw.set_override_redirect(True)
        except Exception:
            pass

    try:
        splash.connect('realize', _on_splash_realize)
    except Exception:
        pass
    splash.set_default_size(480, 240)
    splash.set_resizable(False)
    splash.set_position(Gtk.WindowPosition.CENTER)
    splash.set_keep_above(True)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_border_width(16)
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
    box.pack_start(lbl, False, False, 0)
    spinner = Gtk.Spinner()
    spinner.start()
    box.pack_start(spinner, False, False, 6)
    splash.add(box)
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
    Gtk.main()

if __name__ == '__main__':
    main()
