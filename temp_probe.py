#!/usr/bin/env python3
from gi.repository import Gio, GLib
import sys, traceback

def dbg(msg, *a):
    print(msg % a if a else msg)

candidates = []
# Common candidate shapes to try
candidates.append(("(a{sv}) empty tuple", lambda: GLib.Variant('(a{sv})', ({},))))
candidates.append(("a{sv} empty map", lambda: GLib.Variant('a{sv}', {})))
candidates.append(("(a{sv}u) empty map + 0", lambda: GLib.Variant('(a{sv}u)', ({}, 0))))
candidates.append(("a{sv} with session_handle_token empty string", lambda: GLib.Variant('a{sv}', {'session_handle_token': GLib.Variant('s','')})))
candidates.append(("a{sv} with handle_token empty string", lambda: GLib.Variant('a{sv}', {'handle_token': GLib.Variant('s','')})))
candidates.append(("() empty tuple", lambda: GLib.Variant('()', ())))

try:
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    proxy = Gio.DBusProxy.new_sync(conn, Gio.DBusProxyFlags.NONE, None,
                                   'org.freedesktop.portal.Desktop',
                                   '/org/freedesktop/portal/desktop',
                                   'org.freedesktop.portal.ScreenCast', None)

    for desc, make_variant in candidates:
        dbg('\n=== Trying variant: %s ===', desc)
        try:
            v = make_variant()
        except Exception:
            dbg('Could not build variant for %s', desc)
            traceback.print_exc()
            continue

        try:
            dbg('Calling CreateSession with variant type: %s', v.get_type_string() if isinstance(v, GLib.Variant) else type(v))
            res = proxy.call_sync('CreateSession', v, Gio.DBusCallFlags.NONE, -1, None)
            dbg('CreateSession returned: %r (type=%s)', res, type(res))
            try:
                if isinstance(res, GLib.Variant):
                    unpacked = res.unpack()
                    dbg('Unpacked: %r', unpacked)
                else:
                    dbg('Non-Variant response: %r', res)
            except Exception:
                dbg('Could not unpack result')
        except Exception as e:
            dbg('CreateSession call raised: %s', e)
            traceback.print_exc()
            continue

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
                dbg('Received request path: %s — waiting for Response...', maybe_path)
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
