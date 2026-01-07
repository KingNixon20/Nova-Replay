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
        super().__init__(title="Nova Replay")
        self.set_default_size(1400, 900)
        # load persisted settings (recordings dir, backend, hotkey)
        try:
            self.load_settings()
        except Exception:
            # safe no-op if loading fails
            self.settings = {}

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
        .segmented { background: #222426; border: 1px solid #2e3134; padding: 6px; border-radius: 999px; }
        .segmented .seg-tab { background: transparent; color: #94a3b8; border-radius: 999px; padding: 6px 14px; margin: 2px; }
        .segmented .seg-tab.active { background: #2d3336; color: #ffffff; }
        .segmented .seg-tab GtkLabel { color: inherit; }
        .segmented-container { background: transparent; border-bottom: 1px solid #2e3134; padding-bottom: 8px; margin-bottom: 8px; }
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
            sidebar.set_vexpand(True)
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

        sidebar.pack_start(btn_clips, False, False, 2)
        # keep settings anchored to the bottom
        sidebar.pack_end(btn_settings, False, False, 6)

        main_box.pack_start(sidebar, False, False, 0)
        # thin black divider between sidebar and content
        divider = Gtk.Box()
        try:
            divider.set_size_request(1, -1)
            divider.get_style_context().add_class('side-divider')
        except Exception:
            pass
        main_box.pack_start(divider, False, False, 0)

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
            clips_box.set_margin_top(100)
        except Exception:
            pass
        # segmented control above the thumbnail browser
        self.clip_filter_mode = 'all'
        seg_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        seg_box.get_style_context().add_class('segmented')
        # container provides transparent background + dividing line
        seg_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        seg_container.get_style_context().add_class('segmented-container')
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
        clips_box.pack_start(seg_container, False, False, 6)
        self.clip_scrolled = Gtk.ScrolledWindow()
        self.flow = Gtk.FlowBox()
        self.flow_cols = 4
        self.flow.set_max_children_per_line(self.flow_cols)
        self.flow.set_min_children_per_line(2)
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_row_spacing(6)
        self.flow.set_column_spacing(6)
        # default thumb size (will be updated on resize)
        self.thumb_size = (160, 90)
        # react to scroller resize to compute thumb sizes that fit container width
        def on_scrolled_alloc(w, allocation):
            try:
                width = allocation.width
                cols = max(1, getattr(self, 'flow_cols', 4))
                spacing = self.flow.get_column_spacing() or 6
                total_spacing = spacing * (cols - 1)
                # compute per-tile width, cap at 480
                per = max(64, min(480, (width - total_spacing) // cols - 8))
                target_w = int(per)
                target_h = int(target_w * 9 / 16)
                if (target_w, target_h) != getattr(self, 'thumb_size', (0, 0)):
                    self.thumb_size = (target_w, target_h)
                    GLib.idle_add(self.refresh_clips)
            except Exception:
                pass

        self.clip_scrolled.connect('size-allocate', on_scrolled_alloc)
        self.clip_scrolled.add(self.flow)
        clips_box.pack_start(self.clip_scrolled, True, True, 6)

        # (Trim controls removed — user will add trimming UI later)

        self.content_stack.add_titled(clips_box, 'clips', 'Clips')

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
        self.refresh_clips()

        # Optional global hotkey manager (best-effort)
        self.hotkeys = HotkeyManager(self.toggle_recording)
        self.hotkeys.start()

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
        for f in files:
            path = os.path.join(recorder.RECORDINGS_DIR, f)
            # build a tile
            tile = Gtk.EventBox()
            tile_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            try:
                tile_box.set_size_request(96, 96)
                tile_box.set_hexpand(False)
                tile_box.set_vexpand(False)
            except Exception:
                pass
            thumb_path = os.path.join(self.thumbs_dir, f + '.png')
            target_w, target_h = getattr(self, 'thumb_size', (160, 90))
            decor_base = os.path.splitext(thumb_path)[0] + f'_{target_w}x{target_h}_decor'
            normal_decor = decor_base + '.png'
            hover_decor = decor_base + '_hover.png'
            if os.path.exists(normal_decor):
                img = Gtk.Image.new_from_file(normal_decor)
                img._normal = normal_decor
                img._hover = hover_decor if os.path.exists(hover_decor) else normal_decor
                try:
                    img.set_size_request(target_w, target_h)
                except Exception:
                    pass
            else:
                img = Gtk.Image.new_from_icon_name('video-x-generic', Gtk.IconSize.DIALOG)
                try:
                    img.set_size_request(target_w, target_h)
                except Exception:
                    pass
                # generate thumbnail frame and decorated images in background if ffmpeg available
                if shutil.which('ffmpeg'):
                    def gen(p=path, tp=thumb_path, nb=decor_base, widget=img, tw=target_w, th=target_h):
                        try:
                            # extract a frame sized to target
                            subprocess.call(['ffmpeg', '-y', '-ss', '00:00:01', '-i', p, '-vframes', '1', '-q:v', '2', '-s', f'{tw}x{th}', tp])
                            # render decorated normal + hover
                            try:
                                render_decorated_thumbnail(tp, nb, size=(tw, th), radius=12)
                            except Exception:
                                pass
                            # prefer decorated normal if created, else fallback to raw frame
                            if os.path.exists(nb + '.png'):
                                GLib.idle_add(widget.set_from_file, nb + '.png')
                                # store paths for hover
                                widget._normal = nb + '.png'
                                widget._hover = nb + '_hover.png' if os.path.exists(nb + '_hover.png') else nb + '.png'
                            else:
                                GLib.idle_add(widget.set_from_file, tp)
                                widget._normal = tp
                                widget._hover = tp
                        except Exception:
                            pass
                    threading.Thread(target=gen, daemon=True).start()
            tile_box.pack_start(img, True, True, 0)
            lbl = Gtk.Label(label=f)
            lbl.set_max_width_chars(24)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_xalign(0)
            tile_box.pack_start(lbl, False, False, 0)

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
                        widget_img.set_from_file(widget_img._hover)
                except Exception:
                    pass
                return False

            def on_leave(w, event, widget_img=img):
                try:
                    if hasattr(widget_img, '_normal') and widget_img._normal:
                        widget_img.set_from_file(widget_img._normal)
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
            def on_del(_, p=path, tp=thumb_path, widget=tile):
                try:
                    os.remove(p)
                except Exception:
                    pass
                try:
                    if os.path.exists(tp):
                        os.remove(tp)
                except Exception:
                    pass
                self.flow.remove(widget)
            delb.connect('clicked', on_del)
            actions.pack_start(play, False, False, 0)
            actions.pack_start(delb, False, False, 0)
            tile_box.pack_start(actions, False, False, 0)

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
            import math
            cols = max(1, getattr(self, 'flow_cols', 8))
            rows = math.ceil(len(files) / cols) if cols > 0 else 1
            tile_h = 96
            row_spacing = 6
            padding = 40
            max_h = rows * (tile_h + row_spacing) + padding
            if rows <= 1:
                self.clip_scrolled.set_vexpand(False)
                try:
                    self.clip_scrolled.set_size_request(0, int(max_h))
                except Exception:
                    pass
            else:
                self.clip_scrolled.set_vexpand(True)
                try:
                    # reset size request to allow normal expansion
                    self.clip_scrolled.set_size_request(0, 0)
                except Exception:
                    pass
        except Exception:
            pass
        self.flow.show_all()
        # if nothing matched, show a gentle empty-state message
        if len(self.flow.get_children()) == 0:
            msg = ''
            if getattr(self, 'clip_filter_mode', 'all') == 'full':
                msg = 'No full sessions found.'
            elif getattr(self, 'clip_filter_mode', 'all') == 'clips':
                msg = 'No edited clips found.'
            else:
                msg = 'No videos found.'
            lbl = Gtk.Label(label=msg)
            lbl.get_style_context().add_class('dim-label')
            self.flow.add(lbl)
            self.flow.show_all()

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


def main():
    app = NovaReplayWindow()
    app.connect("destroy", app.on_destroy)
    app.show_all()
    Gtk.main()

if __name__ == '__main__':
    main()
