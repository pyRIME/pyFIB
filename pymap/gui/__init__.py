"""pyMap GUI (PyQt5 via qtpy).

Standalone landmark-registration tool: two-image canvas, BigWarp-style point
picking, live overlay preview, fit-quality readout, session + RGBA export.

Applies the shared fib_dark_theme when available (see theme.py), with a bundled
Fusion-dark fallback for standalone offline use.
"""

from .app import main

__all__ = ["main"]
