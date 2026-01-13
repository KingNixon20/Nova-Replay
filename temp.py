#!/usr/bin/env python3
from gi.repository import Gio, GLib
import sys, traceback

def dbg(msg, *a):
    print(msg % a if a else msg)

try:
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    proxy = Gio.DBusProxy.new_sync(conn, Gio.DBusProxyFlags.NONE, None,
                                   'org.freedesktop.portal.Desktop',
                                   '/org/freedesktop/portal/desktop',
                                   'org.freedesktop.portal.ScreenCast', None)
    dbg('Calling CreateSession with (a{sv}) empty dict...')
    opts = GLib.Variant('(a{sv})', ({},))
    try:
        res = proxy.call_sync('CreateSession', opts, Gio.DBusCallFlags.NONE, -1, None)
        dbg('CreateSession returned: %r (type=%s)', res, type(res))
        try:
            unpacked = res.unpack() if isinstance(res, GLib.Variant) else res
            dbg('Unpacked: %r', unpacked)
        except Exception:
            dbg('Could not unpack result')
    except Exception as e:
        dbg('CreateSession call raised:')
        traceback.print_exc()

    # If we received an object path request, listen for Response
    try:
        maybe_path = None
        if isinstance(res, GLib.Variant):
            u = res.unpack()
            if isinstance(u, tuple) and len(u) and isinstance(u[0], str) and u[0].startswith('/'):
                maybe_path = u[0]
        elif isinstance(res, str) and res.startswith('/'):
            maybe_path = res

        if maybe_path:
            dbg('Received request path: %s — waiting for Response signal...', maybe_path)
            req_proxy = Gio.DBusProxy.new_sync(conn, Gio.DBusProxyFlags.NONE, None,
                                               'org.freedesktop.portal.Desktop',
                                               maybe_path,
                                               'org.freedesktop.portal.Request', None)
            loop = GLib.MainLoop()
            def on_gsignal(proxy, sender, signal, params):
                dbg('g-signal %s params=%r', signal, params)
                if signal == 'Response':
                    try:
                        tup = params.unpack() if isinstance(params, GLib.Variant) else params
                        dbg('Response unpacked: %r', tup)
                    except Exception:
                        dbg('Could not unpack Response params')
                    loop.quit()
            req_proxy.connect('g-signal', on_gsignal)
            GLib.timeout_add_seconds(15, lambda: (loop.quit(), False)[1])
            loop.run()
    except Exception:
        dbg('Error while waiting for Response')
        traceback.print_exc()

except Exception:
    print('Fatal error:')
    traceback.print_exc()
