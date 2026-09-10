"""Keep X11 protocol errors from killing the process.

libX11's DEFAULT error handler prints "X Error of failed request: BadWindow"
and then calls exit(1). GStreamer's ximagesrc asks the X server about its
window on every frame, so the moment a captured game closes (or replaces) its
window there is a BadWindow in flight -- and with the default handler that
took the whole server down (2026-09-08 16:45, exit status 1). Installing our
own handler turns that into a logged warning; ximagesrc then fails its frame,
the pipeline errors, and only that session ends. Process-wide: libX11 is one
shared library, so this covers every X client in the process.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging

log = logging.getLogger("x11")

_installed = False
_handler_ref = None      # keep the ctypes callback alive for the process lifetime


class _XErrorEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("display", ctypes.c_void_p),
                ("resourceid", ctypes.c_ulong), ("serial", ctypes.c_ulong),
                ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte),
                ("minor_code", ctypes.c_ubyte)]


_HANDLER_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_XErrorEvent))


def install() -> bool:
    """Install a non-fatal X error handler (idempotent). False if no libX11."""
    global _installed, _handler_ref
    if _installed:
        return True
    name = ctypes.util.find_library("X11") or "libX11.so.6"
    try:
        lib = ctypes.CDLL(name)
    except OSError as e:
        log.info("libX11 not available (%s); X error handler not installed", e)
        return False

    def on_error(_display, ev) -> int:
        e = ev.contents
        log.warning("X error ignored: code %d on request %d.%d, resource 0x%x "
                    "(a captured window probably went away)",
                    e.error_code, e.request_code, e.minor_code, e.resourceid)
        return 0

    _handler_ref = _HANDLER_T(on_error)
    lib.XSetErrorHandler.restype = ctypes.c_void_p
    lib.XSetErrorHandler.argtypes = [_HANDLER_T]
    lib.XSetErrorHandler(_handler_ref)
    _installed = True
    log.info("non-fatal X error handler installed")
    return True
