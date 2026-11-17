"""Standalone application entry point: ``python -m pymap.gui``.

Optional arguments tie the window to a pyFIB session, as the embedded widget is:
    python -m pymap.gui --session-dir D:\\sessions\\2026-10-10
"""

from __future__ import annotations

import argparse
import sys
import traceback
from qtpy.QtWidgets import QApplication, QMessageBox

from .theme import apply_theme
from .controller import LandmarkController
from .main_window import MainWindow
from .. import diag


def install_excepthook(parent=None):
    """Standalone only: show an unexpected error instead of letting PyQt5 end the
    program (it aborts on an exception raised in a slot when no hook is set).
    Embedded in pyFIB the host's own hook applies, so this is not installed."""
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        sys.stderr.write(text)
        try:
            diag.log.error("UNCAUGHT exception (standalone hook):\n%s", text.rstrip())
        except Exception:  # noqa: BLE001
            pass
        try:
            box = QMessageBox(QMessageBox.Warning, "pyMap - unexpected error",
                              "%s: %s" % (exc_type.__name__, exc), parent=parent)
            box.setDetailedText(text)
            box.exec_()
        except Exception:  # noqa: BLE001  (never fail inside the hook)
            previous(exc_type, exc, tb)

    sys.excepthook = hook
    return hook


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    ap = argparse.ArgumentParser(prog="python -m pymap.gui")
    ap.add_argument("--session-dir", help="pyFIB session folder (targets go to <dir>/clem/)")
    ap.add_argument("--positions-file", help="pyFIB positions file holding the FIB overview")
    ap.add_argument("--log-dir", help="folder for pymap.log (default: <pyMap folder>/logs)")
    args, qt_args = ap.parse_known_args(argv[1:])
    app = QApplication.instance() or QApplication([argv[0]] + qt_args)
    diag.setup(args.log_dir, embedded=False)
    tag = apply_theme(app)
    controller = LandmarkController()
    win = MainWindow(controller, session_dir=args.session_dir,
                     positions_file=args.positions_file, log_dir=args.log_dir)
    install_excepthook(win)
    win.resize(1400, 800)
    if not diag.previous_unclean():
        win.statusBar().showMessage("theme: %s   log: %s" % (tag, diag.log_path() or "off"))
    win.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
