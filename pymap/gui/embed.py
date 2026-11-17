"""Embed pyMap in a host Qt application (pyFIB's Correlation tab, Register pane).

    from pymap.gui.embed import build_widget
    w = build_widget(parent, session_dir=session, on_targets_written=reload_targets)

The host owns the QApplication and the theme: this never creates a QApplication
and never calls a theme function (pyFIB already applies fib_dark_theme, whose
palette pyMap's own colours match). Importing it needs Qt and scikit-image; a host
that wants to start without them should import it inside a try/except and show a
message instead (pyFIB's plan).

The widget assumes nothing about its parent: the host may reparent it (into its
own top-level window and back). After the host starts a new session it can call
``widget.set_session(session_dir, positions_file=None)``; an overview from the
old session is then unlinked so targets can't be written against it.
"""

from __future__ import annotations

from typing import Callable, Optional


def build_widget(parent=None, *, session_dir, positions_file=None,
                 on_targets_written: Optional[Callable[[str], None]] = None,
                 log_dir=None):
    """pyMap's full registration UI as a child QWidget.

    session_dir        : pyFIB session folder; targets are written to
                         ``<session_dir>/clem/targets_<overview>.json`` + ready marker.
    positions_file     : positions file offered by "Open FIB overview (pyFIB)"
                         (default ``<session_dir>/clem/overview_positions.json``).
    on_targets_written : called with the targets file path after it and its ready
                         marker are written, so the host can reload at once.
    log_dir            : folder for pymap.log (default ``<folder holding pymap>/logs``,
                         which is ``<pyFIB>/logs`` when pyMap is bundled in pyFIB).
    """
    from qtpy.QtWidgets import QApplication
    if QApplication.instance() is None:
        raise RuntimeError("pymap.gui.embed.build_widget needs the host's QApplication "
                           "(create it before building the widget)")
    from .. import diag
    diag.setup(log_dir, embedded=True)
    with diag.action("build_widget", detail="session=%r positions=%r" % (session_dir,
                                                                         positions_file)):
        from .controller import LandmarkController
        from .main_window import MainWindow
        controller = LandmarkController()
        w = MainWindow(controller, parent, session_dir=session_dir,
                       positions_file=positions_file,
                       on_targets_written=on_targets_written, embedded=True,
                       log_dir=log_dir)
        controller.setParent(w)          # lifetime follows the widget
    if not diag.previous_unclean():
        w.statusBar().showMessage(
            "Open the FIB overview (pyFIB) and a confocal, pick landmarks and targets, "
            "then Write targets for pyFIB.")
    return w
