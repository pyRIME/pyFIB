"""pyFIB logo in the Qt GUI (v0.54.1): window / taskbar icon and the nav-bar lockup.

The logo files live in smartfib_executor/assets/logo (see its README). Everything here
falls back quietly -- a missing file never stops the GUI: no icon, or the plain "pyFIB"
text in the nav bar.
"""

from __future__ import annotations

import sys
from pathlib import Path

LOGO_DIR = Path(__file__).resolve().parent.parent / "assets" / "logo"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
NAV_LOCKUP = "pyfib_logo_on_dark.png"
WINDOWS_APP_ID = "pyFIB.GUI"


def app_icon():
    """QIcon holding the mark at every icon size (16 and 24 px are pixel-fitted)."""
    from qtpy.QtCore import QSize
    from qtpy.QtGui import QIcon
    icon = QIcon()
    for n in ICON_SIZES:
        p = LOGO_DIR / f"pyfib_mark_{n}.png"
        if p.exists():
            icon.addFile(str(p), QSize(n, n))
    return icon


def brand_label(title: str, width: int = 180, tooltip: str = "", height: int = 0):
    """QLabel showing the logo lockup `width` px wide -- or `height` px tall when
    height is given (v0.54.2) -- sharp on high-DPI screens; the plain bold title if the
    image is missing."""
    from qtpy.QtCore import Qt
    from qtpy.QtGui import QGuiApplication, QPixmap
    from qtpy.QtWidgets import QLabel
    lbl = QLabel()
    lbl.setAccessibleName(title)
    if tooltip:
        lbl.setToolTip(tooltip)
    pix = QPixmap(str(LOGO_DIR / NAV_LOCKUP))
    if pix.isNull():
        lbl.setText(title)
        f = lbl.font(); f.setPointSize(20); f.setBold(True); lbl.setFont(f)
        return lbl
    dpr = 1.0
    try:
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            dpr = max(1.0, float(screen.devicePixelRatio()))
    except Exception:  # noqa: BLE001
        dpr = 1.0
    if height:
        scaled = pix.scaledToHeight(int(round(height * dpr)), Qt.SmoothTransformation)
    else:
        scaled = pix.scaledToWidth(int(round(width * dpr)), Qt.SmoothTransformation)
    scaled.setDevicePixelRatio(dpr)
    lbl.setPixmap(scaled)
    lbl.setObjectName("brandLogo")
    return lbl


def set_windows_app_id() -> None:
    """On Windows, give the process its own taskbar identity so the taskbar shows the
    pyFIB icon instead of python.exe's. Call before the first window is shown."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except Exception:  # noqa: BLE001
        pass
