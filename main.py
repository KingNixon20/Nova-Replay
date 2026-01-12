#Bambaclatttttttt
#!/usr/bin/env python3
import os
import sys
import subprocess
import threading
import gi
gi.require_version('Gtk', '3.0')
try:
    gi.require_version('Gst', '1.0')
except Exception:
    pass
from gi.repository import Gtk, GLib, Gdk, Pango, GdkPixbuf
try:
    from gi.repository import Gst
    Gst.init(None)
except Exception:
    Gst = None
import time
import recorder
import json
from thumbnail_renderer import render_decorated_thumbnail
import shutil

from datetime import datetime
#newline

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
        # Smaller default size so the window opens compactly
        self.set_default_size(1200, 800)
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
        .sidebar { background: #000000; color: #cfd8e3; padding: 2px; min-width: 48px; }
        .nav-button { background: transparent; color: #cfd8e3; border: none; padding: 0; margin: 2px; border-radius: 4px; min-width: 32px; min-height: 32px; }
        .nav-button GtkImage { margin: 4px; }
        .nav-button GtkLabel { color: #05021b; }
        /* thin, unobtrusive divider between sidebar and content */
        .side-divider {
            /* Match the sidebar background so any seam is invisible */
            background: #000000;
            min-width: 1px;
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
        .tile-lower { background: #1F1F1F; border-bottom-left-radius: 6px; border-bottom-right-radius: 6px; padding: 6px; }
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
            min-height: 20px;
            min-width: 20px;
        }
        #min-button GtkImage {
            min-height: 18px;
            min-width: 18px;
        }
        """
        style_provider = Gtk.CssProvider()
        style_provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), style_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Custom client-side header bar (CSD) — spans full window width
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        try:
            top_bar.set_size_request(-1, 36)
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
            exit_event.set_size_request(28, 28)
            exit_event.set_halign(Gtk.Align.END)
            exit_event.set_margin_end(6)
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

        top_bar.pack_end(exit_event, False, False, 8)

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
            # make minimize slightly smaller than the exit button
            min_event.set_size_request(24, 24)
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
        # keep overlay reference for dynamic background updates
        self.content_overlay = content_overlay
        bg_path = self.get_img_file('bg.png')
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

        # helper to update background when user changes selection
        def _update_bg():
            try:
                if not getattr(self, '_bg_pixbuf_orig', None):
                    GLib.idle_add(self._bg_img.hide)
                    return False
                allocation = self.content_overlay.get_allocation()
                if allocation.width <= 0:
                    return False
                ow = self._bg_pixbuf_orig.get_width()
                oh = self._bg_pixbuf_orig.get_height()
                aw = allocation.width
                scale = float(aw) / float(ow) if ow else 1.0
                new_w = max(1, int(ow * scale))
                new_h = max(1, int(oh * scale))
                scaled = self._bg_pixbuf_orig.scale_simple(new_w, new_h, GdkPixbuf.InterpType.BILINEAR)
                GLib.idle_add(self._bg_img.set_from_pixbuf, scaled)
                try:
                    self._bg_img.show()
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
                self.record_btn.set_size_request(40, 32)
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
                refresh_spinner.set_size_request(18, 18)
                refresh_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
                refresh_box.pack_start(ref_img, False, False, 0)
                refresh_box.pack_start(refresh_spinner, False, False, 0)
                refresh_spinner.hide()
                refresh_btn.add(refresh_box)
                refresh_btn.set_tooltip_text('Refresh Thumbnails')
                refresh_btn.set_size_request(40, 32)
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

        # Editor workspace
        editor_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        editor_box.get_style_context().add_class('editor-panel')

        # Left: media list / project panel
        left_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        # left panel default minimum width
        left_panel.set_size_request(220, -1)
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
        self.editor_flow.set_max_children_per_line(2)
        self.editor_flow.set_min_children_per_line(1)
        # allow single selection so users can pick another clip while one is playing
        try:
            self.editor_flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        except Exception:
            # older GTK versions may not support selection; ignore
            pass
        self.editor_flow_scrolled.add(self.editor_flow)
        left_panel.pack_start(self.editor_flow_scrolled, True, True, 0)

        media_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_btn = Gtk.Button(label='Add')
        def _on_add_media(_):
            dlg = Gtk.FileChooserDialog(title='Add Media', parent=self, action=Gtk.FileChooserAction.OPEN)
            dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, 'Add', Gtk.ResponseType.OK)
            dlg.set_select_multiple(True)
            res = dlg.run()
            if res == Gtk.ResponseType.OK:
                for fp in dlg.get_filenames():
                    try:
                        dest = os.path.join(recorder.RECORDINGS_DIR, os.path.basename(fp))
                        os.makedirs(recorder.RECORDINGS_DIR, exist_ok=True)
                        shutil.copy2(fp, dest)
                    except Exception:
                        pass
                self.refresh_clips()
                self._populate_editor_list()
            dlg.destroy()
        add_btn.connect('clicked', _on_add_media)
        rm_btn = Gtk.Button(label='Remove')
        def _on_remove_media(_):
            sel = self.get_selected_clip()
            if not sel:
                return
            try:
                move_to_trash(sel)
                self.refresh_clips()
                self._populate_editor_list()
            except Exception:
                pass
        rm_btn.connect('clicked', _on_remove_media)
        media_btn_box.pack_start(add_btn, True, True, 0)
        media_btn_box.pack_start(rm_btn, True, True, 0)
        left_panel.pack_start(media_btn_box, False, False, 0)

        # use a resizable paned layout: left media panel resizable, center+right fixed
        center_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        center_panel.get_style_context().add_class('video-preview')
        # preview container: use an Overlay so we can place a spinner above the video
        self.preview_container = Gtk.Overlay()
        self.preview_container.set_size_request(640, 360)
        self.preview_container.get_style_context().add_class('preview-area')
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
        self.play_toggle.add(Gtk.Image.new_from_icon_name('media-playback-start', Gtk.IconSize.MENU))
        self.play_toggle.set_tooltip_text('Play / Pause')
        self.play_toggle.connect('toggled', lambda w: self.on_play(w))
        ctrl_box.pack_start(self.play_toggle, False, False, 0)

        self.time_label = Gtk.Label(label='00:00 / 00:00')
        self.time_label.set_xalign(0)
        ctrl_box.pack_start(self.time_label, False, False, 6)

        # timeline slider for seeking
        self.timeline = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.01)
        self.timeline.set_hexpand(True)
        self.timeline.set_value_pos(Gtk.PositionType.RIGHT)
        self.timeline.connect('value-changed', self._on_timeline_changed)
        ctrl_box.pack_start(self.timeline, True, True, 0)
        center_panel.pack_start(ctrl_box, False, False, 0)

        # trim controls (start / end) and actions
        trim_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.start_entry = Gtk.Entry(); self.start_entry.set_text('0')
        self.end_entry = Gtk.Entry(); self.end_entry.set_text('10')
        trim_box.pack_start(Gtk.Label(label='Start(s):'), False, False, 0)
        trim_box.pack_start(self.start_entry, False, False, 0)
        trim_box.pack_start(Gtk.Label(label='End(s):'), False, False, 0)
        trim_box.pack_start(self.end_entry, False, False, 0)
        trim_btn = Gtk.Button(label='Trim')
        trim_btn.connect('clicked', self.on_trim)
        trim_box.pack_start(trim_btn, False, False, 6)
        save_btn = Gtk.Button(label='Save As...')
        save_btn.connect('clicked', self.on_save_as)
        trim_box.pack_start(save_btn, False, False, 0)
        center_panel.pack_start(trim_box, False, False, 0)

        # build center+right composite so paned can contain them as a single child
        center_right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        center_right.pack_start(center_panel, True, True, 6)

        # Right: tools / properties
        right_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right_panel.set_size_request(260, -1)
        tools_lbl = Gtk.Label(label='Tools')
        tools_lbl.get_style_context().add_class('dim-label')
        right_panel.pack_start(tools_lbl, False, False, 6)

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
            try:
                start = float(self.start_entry.get_text())
                end = float(self.end_entry.get_text())
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
                # use ffmpeg to trim (fast copy)
                try:
                    cmd = ['ffmpeg', '-y', '-ss', str(start), '-to', str(end), '-i', sel, '-c', 'copy', out]
                    threading.Thread(target=subprocess.call, args=(cmd,), daemon=True).start()
                    self._alert(f'Exporting -> {out}')
                except Exception as e:
                    self._alert(f'Export failed: {e}')
            else:
                dlg.destroy()

        export_btn.connect('clicked', _on_export)
        export_box.pack_start(export_btn, False, False, 0)
        right_panel.pack_start(export_box, False, False, 0)

        center_right.pack_start(right_panel, False, False, 6)

        # horizontal paned: left panel resizable, right side contains center+right
        pan = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        try:
            pan.pack1(left_panel, resize=True, shrink=False)
            pan.pack2(center_right, resize=True, shrink=False)
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

        # Opacity slider
        opacity_label = Gtk.Label(label="Window transparency:")
        opacity_label.set_xalign(0)
        settings_box.pack_start(opacity_label, False, False, 0)
        opacity_slider = Gtk.HScale.new_with_range(0.1, 1.0, 0.1)
        opacity_slider.set_value(1.0)
        opacity_slider.connect("value-changed", self.on_opacity_changed)
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
                        GLib.idle_add(self._bg_img.hide)
                    except Exception:
                        pass
            except Exception:
                pass

        self.bg_combo.connect('changed', _on_bg_changed)
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

        crf_lbl = Gtk.Label(label='CRF:')
        crf_lbl.set_xalign(0)
        row2.pack_start(crf_lbl, False, False, 8)
        adj_crf = Gtk.Adjustment(value=self.settings.get('encoder', {}).get('crf', 23), lower=0, upper=51, step_increment=1, page_increment=5, page_size=0)
        self.crf_spin = Gtk.SpinButton.new(adj_crf, 1, 0)
        row2.pack_start(self.crf_spin, False, False, 0)
        enc_box.pack_start(row2, False, False, 0)

        row3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        br_lbl = Gtk.Label(label='Video kbps:')
        br_lbl.set_xalign(0)
        row3.pack_start(br_lbl, False, False, 0)
        adj_br = Gtk.Adjustment(value=self.settings.get('encoder', {}).get('bitrate_kbps', 4000), lower=0, upper=20000, step_increment=100, page_increment=500, page_size=0)
        self.bitrate_spin = Gtk.SpinButton.new(adj_br, 100, 0)
        row3.pack_start(self.bitrate_spin, False, False, 0)

        fps_lbl = Gtk.Label(label='FPS:')
        fps_lbl.set_xalign(0)
        row3.pack_start(fps_lbl, False, False, 8)
        adj_fps = Gtk.Adjustment(value=self.settings.get('encoder', {}).get('fps', 60), lower=1, upper=240, step_increment=1, page_increment=5, page_size=0)
        self.fps_spin = Gtk.SpinButton.new(adj_fps, 1, 0)
        row3.pack_start(self.fps_spin, False, False, 0)
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

        ab_lbl = Gtk.Label(label='Audio kbps:')
        ab_lbl.set_xalign(0)
        row4.pack_start(ab_lbl, False, False, 8)
        adj_ab = Gtk.Adjustment(value=self.settings.get('encoder', {}).get('audio_bitrate_kbps', 128), lower=16, upper=512, step_increment=16, page_increment=64, page_size=0)
        self.audio_bitrate_spin = Gtk.SpinButton.new(adj_ab, 1, 0)
        row4.pack_start(self.audio_bitrate_spin, False, False, 0)
        enc_box.pack_start(row4, False, False, 0)

        # Threads / hwaccel
        row5 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        th_lbl = Gtk.Label(label='Threads:')
        th_lbl.set_xalign(0)
        row5.pack_start(th_lbl, False, False, 0)
        adj_th = Gtk.Adjustment(value=self.settings.get('encoder', {}).get('threads', 0), lower=0, upper=64, step_increment=1, page_increment=2, page_size=0)
        self.threads_spin = Gtk.SpinButton.new(adj_th, 1, 0)
        row5.pack_start(self.threads_spin, False, False, 0)

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
        enc_box.pack_start(row5, False, False, 0)

        enc_frame.add(enc_box)
        settings_box.pack_start(enc_frame, False, False, 6)


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
        # gather encoder ui state if present
        enc = self.settings.get('encoder', {})
        try:
            # derive engine from Recording backend selection
            try:
                be_text = getattr(self, 'backend_combo', None).get_active_text() if getattr(self, 'backend_combo', None) else None
                if be_text and 'wf-recorder' in be_text:
                    engine_sel = 'wf-recorder'
                elif be_text and 'pipewire' in be_text:
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
        # apply favorites filter
        try:
            if getattr(self, 'clip_filter_mode', 'all') == 'favorites':
                favs = self.settings.get('favorites', {}) if getattr(self, 'settings', None) else {}
                files = [f for f in files if favs.get(f, False)]
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
                # allow tile width to be flexible so the grid can shrink horizontally
                tile_box.set_size_request(0, total_h)
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
                        img.set_size_request(0, img_h)
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
                        img.set_size_request(0, img_h)
                    except Exception:
                        pass
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
                lower_box.set_size_request(0, extra_h)
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
                    play.set_size_request(28, 28)
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
                    delb.set_size_request(28, 28)
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
                    folderb.set_size_request(28, 28)
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
                    favb.set_size_request(28, 28)
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
                    try:
                        self.playbin.set_state(Gst.State.PLAYING)
                    except Exception:
                        pass
                    # start periodic update
                    try:
                        if not getattr(self, '_pos_update_id', None):
                            self._pos_update_id = GLib.timeout_add(250, self._update_position)
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
                # update timeline and label in main loop
                def _upd():
                    try:
                        self.time_label.set_text(f"{int(cur):02d}:{int(cur%60):02d} / {int(total):02d}:{int(total%60):02d}")
                        try:
                            self.timeline.handler_block_by_func(self._on_timeline_changed)
                        except Exception:
                            pass
                        try:
                            self.timeline.set_range(0, max(1, total))
                            self.timeline.set_value(cur)
                        except Exception:
                            pass
                        try:
                            self.timeline.handler_unblock_by_func(self._on_timeline_changed)
                        except Exception:
                            pass
                    except Exception:
                        pass
                    return False
                GLib.idle_add(_upd)
            return True
        except Exception:
            return False

    def _alert(self, msg):
        dialog = Gtk.MessageDialog(self, 0, Gtk.MessageType.INFO, Gtk.ButtonsType.OK, msg)
        dialog.run()
        dialog.destroy()

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
            # prefer gtksink if available for easy GTK embedding
            sink = Gst.ElementFactory.make('gtksink', 'gtksink')
            if sink:
                pb.set_property('video-sink', sink)
                self._gst_sink = sink
                self._appsink = None
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
                        self._appsink_pixbuf = pb
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
            if getattr(self, '_appsink_pixbuf', None):
                pb = self._appsink_pixbuf
                # scale pixbuf to widget allocation while preserving aspect
                alloc = widget.get_allocation()
                iw = pb.get_width(); ih = pb.get_height()
                if iw > 0 and ih > 0:
                    # compute scale
                    scale = min(alloc.width / iw, alloc.height / ih)
                    nw = int(iw * scale)
                    nh = int(ih * scale)
                    scaled = pb.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
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
            self.time_label.set_text(f"{int(cur):02d}:{int(cur%60):02d} / {int(secs):02d}:{int(secs%60):02d}")
            # set slider range
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


def _create_splash_window():
    # Use a normal top-level window but remove decorations (titlebar)
    splash = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
    splash.set_decorated(False)
    splash.set_keep_above(True)
    splash.set_position(Gtk.WindowPosition.CENTER)
    splash.set_default_size(480, 240)
    splash.set_resizable(False)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_border_width(16)
    box.get_style_context().add_class('splash-box')

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
