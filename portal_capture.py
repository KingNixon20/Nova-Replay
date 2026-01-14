"""
PortalCapture scaffold for screen capture via xdg-desktop-portal + GStreamer.

Provides:
- PortalCapture: high-level scaffold with start()/stop().
- build_pipeline_from_fd(fd, appsink_name='appsink'): builds an appsink-based Gst.Pipeline
  that reads from a file descriptor (fdsrc -> decodebin -> videoconvert -> appsink).
- build_pipeline_from_node_id(node_id, appsink_name='appsink'): attempts to build a pipeline
  using `pipewiresrc` if available (platform/plugin dependent).

Notes:
- This file is defensive and intentionally does not implement portal FD extraction.
  If you call `PortalCapture.start()` without providing `fd`, it will attempt basic
  portal DBus interactions and then raise NotImplementedError with a clear message
  pointing to where to implement.
  
Requires PyGObject with Gio/Gst available.
"""

from gi.repository import Gio, Gst, GLib
import typing
import logging
import os
import re

# Initialize basic logging if not configured by the application
if not logging.getLogger().handlers:
    if os.environ.get('NOVA_REPLAY_DEBUG'):
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

Gst.init(None)


def build_pipeline_from_fd(fd: int, appsink_name: str = "appsink") -> Gst.Pipeline:
    """Build an appsink-based Gst.Pipeline that consumes data from a supplied FD.

    Parameters:
    - fd: file descriptor (int) open for reading (e.g. a PipeWire fd).
    - appsink_name: name assigned to the appsink element.

    Returns:
    - Gst.Pipeline ready to be set to PLAYING by the caller.
    """
    if not isinstance(fd, int):
        raise TypeError("fd must be an integer file descriptor")
    logger.debug('Building pipeline from fd=%s appsink=%s', fd, appsink_name)

    pipeline = Gst.Pipeline.new("portal-capture-pipeline")
    if pipeline is None:
        raise RuntimeError("Failed to create Gst.Pipeline")

    fdsrc = Gst.ElementFactory.make("fdsrc", "fdsrc")
    if fdsrc is None:
        raise RuntimeError("fdsrc element not available in GStreamer installation")
    fdsrc.set_property("fd", int(fd))
    try:
        fdsrc.set_property("close", False)
    except Exception:
        pass

    decode = Gst.ElementFactory.make("decodebin", "decodebin")
    videoconvert = Gst.ElementFactory.make("videoconvert", "videoconvert")
    appsink = Gst.ElementFactory.make("appsink", appsink_name)
    if appsink is None:
        raise RuntimeError("appsink element not available in GStreamer installation")
    appsink.set_property("emit-signals", False)
    appsink.set_property("max-buffers", 1)
    appsink.set_property("drop", True)

    for el in (fdsrc, decode, videoconvert, appsink):
        pipeline.add(el)

    if not Gst.Element.link(fdsrc, decode):
        raise RuntimeError("Failed to link fdsrc -> decodebin")

    if not Gst.Element.link(videoconvert, appsink):
        raise RuntimeError("Failed to link videoconvert -> appsink")

    def _on_pad_added(decodebin, pad):
        sinkpad = videoconvert.get_static_pad("sink")
        if not sinkpad:
            return
        if sinkpad.is_linked():
            return
        try:
            pad.link(sinkpad)
        except Exception:
            pass

    decode.connect("pad-added", _on_pad_added)
    logger.debug('Pipeline constructed (fdsrc -> decodebin -> videoconvert -> appsink)')

    return pipeline


def build_pipeline_from_node_id(node_id: int, appsink_name: str = "appsink") -> Gst.Pipeline:
    """Attempt to build a pipeline using `pipewiresrc` targeted at a PipeWire node id."""
    if not isinstance(node_id, int):
        raise TypeError("node_id must be an integer")
    logger.debug('Building pipeline from node_id=%s appsink=%s', node_id, appsink_name)

    pipeline = Gst.Pipeline.new("portal-capture-pw-node-pipeline")
    if pipeline is None:
        raise RuntimeError("Failed to create Gst.Pipeline")

    pwsrc = Gst.ElementFactory.make("pipewiresrc", "pipewiresrc")
    if pwsrc is None:
        raise RuntimeError(
            "pipewiresrc element not available. Install GStreamer PipeWire plugin."
        )

    set_success = False
    for prop in ("node", "node-id", "device"):
        try:
            pwsrc.set_property(prop, int(node_id))
            set_success = True
            break
        except Exception:
            continue
    if not set_success:
        raise RuntimeError(
            "Unable to set node id property on pipewiresrc; inspect available properties."
        )

    convert = Gst.ElementFactory.make("videoconvert", "videoconvert")
    appsink = Gst.ElementFactory.make("appsink", appsink_name)
    appsink.set_property("emit-signals", False)
    appsink.set_property("max-buffers", 1)
    appsink.set_property("drop", True)

    for el in (pwsrc, convert, appsink):
        if el is None:
            raise RuntimeError("Required GStreamer element missing")
        pipeline.add(el)

    if not Gst.Element.link(pwsrc, convert):
        raise RuntimeError("Failed to link pipewiresrc -> videoconvert")
    if not Gst.Element.link(convert, appsink):
        raise RuntimeError("Failed to link videoconvert -> appsink")

    logger.debug('Pipeline constructed (pipewiresrc -> videoconvert -> appsink)')

    return pipeline


class PortalCapture:
    """High-level scaffold for capturing via portal or direct FD/node."""

    def __init__(self, appsink_name: str = "appsink"):
        self.appsink_name = appsink_name
        self.pipeline: typing.Optional[Gst.Pipeline] = None
        self._dbus_proxy: typing.Optional[Gio.DBusProxy] = None
        self._portal_session_handle: typing.Optional[str] = None

    def _ensure_portal_proxy(self):
        if self._dbus_proxy is not None:
            return self._dbus_proxy
        try:
            # Obtain a session bus connection object and create a proxy for the ScreenCast interface.
            logger.debug('Obtaining session bus connection for portal proxy')
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            # Basic preflight: ensure the portal service appears owned on the session bus
            try:
                has_owner = conn.call_sync(
                    'org.freedesktop.DBus',
                    '/org/freedesktop/DBus',
                    'org.freedesktop.DBus',
                    'NameHasOwner',
                    GLib.Variant('(s)', ('org.freedesktop.portal.Desktop',)),
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
                if isinstance(has_owner, GLib.Variant):
                    try:
                        owned = bool(has_owner.unpack())
                    except Exception:
                        owned = bool(has_owner)
                else:
                    owned = bool(has_owner)
                if not owned:
                    raise RuntimeError('xdg-desktop-portal does not appear to be registered on the session bus')
            except Exception as ie:
                logger.debug('Portal preflight check failed: %s', ie)
                # Continue — we'll let later calls return a clearer error, but fail early if bus unavailable
                pass
            self._dbus_proxy = Gio.DBusProxy.new_sync(
                conn,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.ScreenCast",
                None,
            )
            logger.debug('Created portal proxy object %r', self._dbus_proxy)
        except Exception as exc:
            logger.exception('Failed to create portal DBus proxy')
            raise RuntimeError(f"Failed to create portal DBus proxy: {exc}")
        return self._dbus_proxy

    def start(self, fd: typing.Optional[int] = None, node_id: typing.Optional[int] = None):
        if fd is not None:
            self.pipeline = build_pipeline_from_fd(fd, appsink_name=self.appsink_name)
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Failed to set pipeline to PLAYING (fd pipeline)")
            return

        if node_id is not None:
            self.pipeline = build_pipeline_from_node_id(node_id, appsink_name=self.appsink_name)
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Failed to set pipeline to PLAYING (node pipeline)")
            return

        proxy = self._ensure_portal_proxy()

        # Helper: call a portal method and if it returns a request object path,
        # wait for the Request.Response signal to obtain the async reply.
        class PortalNegotiationError(RuntimeError):
            pass

        def _is_valid_object_path(p: str) -> bool:
            # Basic validation for DBus object path
            return bool(re.match(r'^(/[_A-Za-z0-9]+)+$', p))


        def _call_method_and_wait(method: str, param_opts: typing.Optional[typing.Union[dict, tuple]]) -> typing.Tuple[int, typing.Optional[GLib.Variant]]:
            """Call `method` on the portal proxy with `param_variant` and wait for Response.

            Returns tuple (response_code, results_variant_or_None)
            """
            logger.debug('Calling portal method %s with opts %r', method, param_opts)

            def _try_call(p):
                try:
                    return proxy.call_sync(method, p, Gio.DBusCallFlags.NONE, -1, None)
                except Exception as e:
                    # Map common portal failure modes to clearer errors so the caller can act.
                    msg = str(e)
                    logger.debug('portal call attempt failed for %r: %s', p, msg)
                    low = msg.lower()
                    if 'missing token' in low or 'invalid token' in low:
                        raise RuntimeError(
                            'xdg-desktop-portal rejected the call: Missing/invalid token. '
                            'This often indicates the portal backend is not accepting requests from this process or the call parameters are malformed. '
                            'Ensure you run from a desktop session (HOME, XDG_RUNTIME_DIR, DBUS session set) and that xdg-desktop-portal and its backend are healthy.'
                        )
                    if 'no reply' in low or 'remote peer disconnected' in low or 'segv' in low:
                        raise RuntimeError('xdg-desktop-portal appears to have disconnected or crashed during the call')
                    # generic re-raise for other errors
                    raise

            # Build only the canonical parameter form. Trying many shapes triggered instability
            # in some portal backends; be conservative and only send (a{sv}).
            candidates = []
            try:
                if isinstance(param_opts, dict):
                    candidates.append(GLib.Variant('(a{sv})', (param_opts,)))
                else:
                    candidates.append(GLib.Variant('(a{sv})', ({},)))
            except Exception as e:
                logger.debug('Failed to build canonical parameter variant: %s', e)
                candidates.append(None)

            res = None
            last_exc = None
            for cand in candidates:
                try:
                    res = _try_call(cand)
                    break
                except Exception as e:
                    last_exc = e
                    continue
            if res is None:
                logger.exception('All portal call invocation attempts failed for %s', method)
                raise RuntimeError(f"Portal call {method} failed: {last_exc}")

            logger.debug('Portal method %s returned raw response: %r', method, res)

            # The method may return an object path (request path) or a direct response variant.
            req_path = None
            try:
                # Attempt to extract an object path string from the result variant
                if isinstance(res, GLib.Variant):
                    try:
                        # Unpack may yield a tuple containing an object path
                        unpacked = res.unpack()
                    except Exception:
                        unpacked = None
                    if isinstance(unpacked, tuple) and len(unpacked) > 0 and isinstance(unpacked[0], str) and unpacked[0].startswith('/'):
                        candidate = unpacked[0]
                        if _is_valid_object_path(candidate):
                            req_path = candidate
                        else:
                            logger.debug('Portal returned object path-like string that is not a valid object path: %r', candidate)
                elif isinstance(res, str) and res.startswith('/'):
                    if _is_valid_object_path(res):
                        req_path = res
                    else:
                        logger.debug('Portal returned string that is not a valid object path: %r', res)
            except Exception:
                req_path = None

            if not req_path:
                # No async request path returned; some portals may return results directly
                try:
                    if isinstance(res, GLib.Variant):
                        logger.debug('Portal method %s returned direct variant results', method)
                        return (0, res)
                except Exception:
                    pass
                return (0, None)

            # Create a proxy for the request object to listen for the Response signal
            logger.debug('Portal method %s returned request path %s; creating Request proxy', method, req_path)
            if not req_path:
                # No valid request path returned; treat as no-async and return direct results
                try:
                    if isinstance(res, GLib.Variant):
                        return (0, res)
                except Exception:
                    pass
                return (0, None)

            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            try:
                request_proxy = Gio.DBusProxy.new_sync(conn, Gio.DBusProxyFlags.NONE, None, "org.freedesktop.portal.Desktop", req_path, "org.freedesktop.portal.Request", None)
            except Exception as e:
                logger.exception('Failed to create Request proxy for path %s: %s', req_path, e)
                raise PortalNegotiationError('Portal returned an invalid Request path or the portal backend crashed while creating the Request proxy')

            loop = GLib.MainLoop()
            result_container = {'code': None, 'results': None}

            def _on_response(proxy_obj, sender_name, response, results):
                # signature commonly: (u a{sv}) but bindings may vary; capture what we get
                try:
                    result_container['code'] = response
                    result_container['results'] = results
                except Exception:
                    result_container['code'] = int(response) if response is not None else -1
                    result_container['results'] = results
                try:
                    loop.quit()
                except Exception:
                    pass

            # Connect to the Response signal
            try:
                request_proxy.connect('g-signal', lambda *a, **k: _on_response(*a[2:]))
            except Exception:
                # older versions: connect to 'Response' specifically
                try:
                    request_proxy.connect('Response', _on_response)
                except Exception:
                    pass
            logger.debug('Waiting for portal response on %s (timeout 10s)', req_path)
            # Run the loop until the response arrives or timeout
            timeout_id = GLib.timeout_add_seconds(10, lambda: (loop.quit(), False)[1])
            try:
                loop.run()
            finally:
                try:
                    GLib.source_remove(timeout_id)
                except Exception:
                    pass

            logger.debug('Portal response received: code=%r results=%r', result_container.get('code'), result_container.get('results'))
            return (result_container['code'] or 0, result_container['results'])

        # Step 1: CreateSession
        opts = {}
        logger.debug('Step 1: CreateSession')
        code, res = _call_method_and_wait('CreateSession', opts)
        if code != 0:
            logger.error('CreateSession failed with code %s', code)
            raise RuntimeError(f'CreateSession failed with code {code}')

        # Step 2: SelectSources (ask for any source) — many portals accept empty options
        # Use the returned session handle if available (res may contain it). We'll try both patterns.
        session_handle = None
        try:
            if isinstance(res, GLib.Variant):
                val = res.unpack()
                if isinstance(val, tuple) and len(val) > 0 and isinstance(val[0], str) and val[0].startswith('/'):
                    session_handle = val[0]
        except Exception:
            session_handle = None

        select_opts = {}
        logger.debug('Step 2: SelectSources (session_handle=%r)', session_handle)
        if session_handle:
            # Some portals expect the session object path wrapper
            try:
                code2, res2 = _call_method_and_wait('SelectSources', (session_handle, select_opts))
            except Exception:
                code2, res2 = _call_method_and_wait('SelectSources', select_opts)
        else:
            code2, res2 = _call_method_and_wait('SelectSources', select_opts)

        if code2 != 0:
            logger.error('SelectSources failed with code %s', code2)
            raise RuntimeError(f'SelectSources failed with code {code2}')

        # Step 3: Start the session (this is the call that typically yields PipeWire info)
        start_opts = {}
        logger.debug('Step 3: Start (session_handle=%r)', session_handle)
        if session_handle:
            code3, res3 = _call_method_and_wait('Start', (session_handle, start_opts))
        else:
            code3, res3 = _call_method_and_wait('Start', start_opts)

        # If Start returned results directly, try to parse for pipewire fd/node
        portal_response = res3
        logger.debug('Portal Start returned variant: %r', portal_response)

        # Attempt to extract a pipewire node id or fd from the portal response
        found_fd = None
        found_node = None
        try:
            if isinstance(portal_response, GLib.Variant):
                unpacked = portal_response.unpack()
                # walk tuple/dict structures trying to find integers/paths
                def _walk(o):
                    nonlocal found_fd, found_node
                    if found_fd or found_node:
                        return
                    if isinstance(o, dict):
                        for k, v in o.items():
                            _walk(v)
                    elif isinstance(o, (list, tuple)):
                        for it in o:
                            _walk(it)
                    elif isinstance(o, int):
                        # heuristics: treat integers > 0 as node ids
                        if o > 0:
                            found_node = o
                    elif isinstance(o, str):
                        # ignore
                        pass
                _walk(unpacked)
                logger.debug('Parsed unpacked portal response for fd/node discovery')
        except Exception:
            pass

        # As a fallback, if results were provided via the earlier request handler stored in _call_method_and_wait,
        # try to use that (we returned results there as well).
        if not found_fd and not found_node and portal_response is None:
            # nothing to work with — raise with debugging info
            raise RuntimeError('Portal Start did not return PipeWire fd/node; portal response variants unavailable.\n' +
                               'Inspect system logs or run a manual portal test. The raw response objects were not captured.')

        # Prefer node id if found
        if found_node:
            logger.info('Starting pipeline from PipeWire node id %s', found_node)
            try:
                self.pipeline = build_pipeline_from_node_id(int(found_node), appsink_name=self.appsink_name)
            except Exception:
                logger.exception('Failed to build pipeline from node id %s', found_node)
                raise
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error('Failed to set pipeline to PLAYING (node pipeline)')
                raise RuntimeError('Failed to set pipeline to PLAYING (node pipeline)')
            # attach bus listener
            try:
                bus = self.pipeline.get_bus()
                bus.add_signal_watch()
                def _on_msg(bus, msg):
                    try:
                        logger.debug('GStreamer message: %s', msg.type)
                    except Exception:
                        logger.debug('GStreamer message (raw): %r', msg)
                bus.connect('message', _on_msg)
            except Exception:
                logger.exception('Failed to attach bus handler to node pipeline')
            return

        if found_fd:
            logger.info('Starting pipeline from PipeWire fd %s', found_fd)
            try:
                self.pipeline = build_pipeline_from_fd(int(found_fd), appsink_name=self.appsink_name)
            except Exception:
                logger.exception('Failed to build pipeline from fd %s', found_fd)
                raise
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error('Failed to set pipeline to PLAYING (fd pipeline)')
                raise RuntimeError('Failed to set pipeline to PLAYING (fd pipeline)')
            try:
                bus = self.pipeline.get_bus()
                bus.add_signal_watch()
                def _on_msg2(bus, msg):
                    try:
                        logger.debug('GStreamer message: %s', msg.type)
                    except Exception:
                        logger.debug('GStreamer message (raw): %r', msg)
                bus.connect('message', _on_msg2)
            except Exception:
                logger.exception('Failed to attach bus handler to fd pipeline')
            return

        # If we reach here, we couldn't auto-start a pipeline — provide helpful debugging info
        raise RuntimeError('Portal negotiation completed but no usable PipeWire fd/node found in response.\n'
                           'The raw portal Start response was: %r' % (portal_response,))

    def stop(self):
        if self.pipeline is not None:
            try:
                self.pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
            self.pipeline = None

        self._portal_session_handle = None
        self._dbus_proxy = None
