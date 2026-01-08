#!/usr/bin/env python3

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

class ResolutionWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Window Resolution")
        self.set_default_size(800, 600)

        self.label = Gtk.Label()
        self.label.set_margin_top(20)
        self.label.set_margin_bottom(20)

        self.add(self.label)

        self.connect("destroy", Gtk.main_quit)
        self.connect("configure-event", self.on_resize)

        self.update_label()

    def on_resize(self, widget, event):
        self.update_label()

    def update_label(self):
        width, height = self.get_size()
        self.label.set_text(f"{width} x {height}")

win = ResolutionWindow()
win.show_all()
Gtk.main()
