"""Theme application.

pyMap matches the elymaker / pyFIB look. The canonical theme lives in elymaker
(elymaker/gui/style.py) and is vendored here verbatim as `style.py`. By default
pyMap applies that vendored theme so the standalone tool matches exactly.

If the deployed ecosystem exposes a shared `fib_dark_theme` module, it is
preferred (so a future single source of truth wins over the vendored copy);
otherwise the vendored elymaker theme is used.
"""

from __future__ import annotations

import importlib
import os

from . import style

_PROBE_FUNCS = ("apply_theme", "apply", "set_theme", "install", "setup")
_PROBE_CONSTS = ("STYLESHEET", "QSS", "stylesheet", "DARK_QSS")


def _try_external(app):
    try:
        mod = importlib.import_module("fib_dark_theme")
    except Exception:
        return None
    for fn in _PROBE_FUNCS:
        f = getattr(mod, fn, None)
        if callable(f):
            try:
                f(app)
                return "fib_dark_theme.%s()" % fn
            except Exception:
                pass
    for const in _PROBE_CONSTS:
        s = getattr(mod, const, None)
        if isinstance(s, str) and s.strip():
            app.setStyle("Fusion")
            app.setStyleSheet(s)
            return "fib_dark_theme.%s" % const
    modfile = getattr(mod, "__file__", "") or ""
    qss = os.path.join(os.path.dirname(modfile), "fib_dark_theme.qss")
    if os.path.isfile(qss):
        app.setStyle("Fusion")
        with open(qss, "r") as f:
            app.setStyleSheet(f.read())
        return qss
    return None


def apply_theme(app):
    """Apply the shared theme. Returns a tag describing what was applied."""
    external = _try_external(app)
    if external:
        return external
    style.apply_theme(app)
    return "elymaker-style (vendored)"
