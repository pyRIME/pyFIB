"""pyMap main window: fixed | moving | overlay, with the control dock."""

from __future__ import annotations

import os
import time
import traceback
from qtpy.QtWidgets import (QMainWindow, QWidget, QSplitter, QVBoxLayout,
                            QLabel, QDockWidget, QFileDialog, QMessageBox,
                            QAction, QToolBar, QInputDialog, QScrollArea)
from qtpy.QtCore import Qt, QEvent, QTimer

from .. import diag

from .views import LandmarkView
from .controls import ControlPanel
from .controller import LandmarkController, MODE_TARGET, _inside
from ..transform import RegistrationError
from ..confocal import ConfocalImportError

_IMG_FILTER = "Images (*.bmp *.png *.tif *.tiff *.jpg *.jpeg);;All files (*)"
CONTROLS_MIN_WIDTH = 270     # px; narrower clips the control labels


class _TitledView(QWidget):
    def __init__(self, title, view):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lbl = QLabel(title)
        lbl.setStyleSheet("color:#9aa3b2; padding:2px;")
        lay.addWidget(lbl)
        lay.addWidget(view)


class MainWindow(QMainWindow):
    """pyMap's window. Standalone it is a top-level window; embedded (pyFIB's
    Correlation tab, via pymap.gui.embed.build_widget) it is a plain child widget.

    session_dir      : pyFIB session folder; targets go to <session_dir>/clem/.
    positions_file   : default positions file for "Open FIB overview (pyFIB)"
                       (default <session_dir>/clem/overview_positions.json).
    on_targets_written(path) : called after a targets file + ready marker are written.

    Every toolbar action reports failures in a message box and leaves pyMap running;
    nothing assumes what the window's parent is, so a host may reparent it (e.g. into
    its own top-level window and back).

    Every entry point (toolbar, controls, views, controller signals) is a guarded
    slot (pymap.diag): an error is logged to pymap.log with a traceback and pyMap's
    state, shown once, and never reaches Qt or the host.
    """

    ERROR_BOX_EVERY_S = 10.0       # the same failing action shows at most one box per 10 s

    def __init__(self, controller: LandmarkController = None, parent=None, *,
                 session_dir=None, positions_file=None, on_targets_written=None,
                 embedded: bool = False, log_dir=None):
        diag.setup(log_dir, embedded=embedded)
        super().__init__(parent)
        self._embedded = bool(embedded)
        self._last_box = {}
        self._box_open = False
        self.setWindowTitle("pyMap")
        if embedded:
            self.setWindowFlags(Qt.Widget)
        self.c = controller or LandmarkController(self)
        self.c.session_dir = str(session_dir) if session_dir else None
        if positions_file is None and session_dir:
            positions_file = os.path.join(str(session_dir), "clem", "overview_positions.json")
        self.positions_file = str(positions_file) if positions_file else None
        self._on_targets_written = on_targets_written

        self.fixed_view = LandmarkView()
        self.moving_view = LandmarkView()
        self.overlay_view = LandmarkView(read_only=True)
        for v in (self.fixed_view, self.moving_view, self.overlay_view):
            v.set_error_owner(self)
        self.overlay_view.cache_images = True     # flicker's two frames

        split = QSplitter(Qt.Horizontal)
        split.addWidget(_TitledView("FIB (fixed - reference, not resampled)", self.fixed_view))
        split.addWidget(_TitledView("Confocal (moving - pre-aligned)", self.moving_view))
        split.addWidget(_TitledView("Overlay (confocal -> FIB space)", self.overlay_view))
        split.setSizes([400, 400, 400])
        self.setCentralWidget(split)

        panel = ControlPanel(self.c, error_owner=self)
        self.panel = panel
        panel.flicker_visible = self.overlay_view.isVisible
        panel.exportHandoffRequested.connect(self._slot("Export handoff", self.export_handoff))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setMinimumWidth(CONTROLS_MIN_WIDTH)
        dock = QDockWidget("Controls", self)
        dock.setObjectName("pymap_controls")
        dock.setWidget(scroll)
        dock.setAllowedAreas(Qt.RightDockWidgetArea | Qt.LeftDockWidgetArea)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable
                         | QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self.resizeDocks([dock], [CONTROLS_MIN_WIDTH + 20], Qt.Horizontal)
        self.dock = dock

        self._build_actions()
        self._wire()
        self.statusBar().showMessage("Open a FIB image and a confocal image to begin.")
        self._log_created(session_dir)

    # ---- diagnostics -------------------------------------------------------
    # toolbar actions that open a file dialog: the hang dump watches only their work
    # (diag.hang_watch), not the time spent browsing
    _DIALOG_ACTIONS = {"Open FIB overview (pyFIB)", "Open FIB", "Open Confocal",
                       "Load Session", "Save Session", "Load FIB meta", "Export",
                       "Export handoff", "Diagnostics"}

    def _slot(self, name, fn, quiet=False):
        return diag.slot(name, fn, owner=self, quiet=quiet,
                         hang=name not in self._DIALOG_ACTIONS)

    def _log_created(self, session_dir):
        name = "pyMap window %s" % hex(id(self))
        diag.log.info("%s created (embedded=%s, parent=%s, session=%s, positions=%s, log=%s)",
                      name, self._embedded, type(self.parent()).__name__ if self.parent() else None,
                      session_dir, self.positions_file, diag.log_path())
        self.destroyed.connect(lambda *_a, _n=name: diag.log.info("%s destroyed", _n))
        unclean = diag.previous_unclean()
        if unclean:
            self.statusBar().showMessage(
                "An earlier pyMap run ended without closing (%s). Details: %s"
                % (unclean[-1], diag.log_path()))

    def diag_state(self) -> dict:
        """A one-line summary of pyMap's state, written with every logged failure."""
        c = self.c
        st = {}

        def put(k, fn):
            try:
                st[k] = fn()
            except Exception as e:  # noqa: BLE001
                st[k] = "?(%s)" % type(e).__name__
        put("fixed", lambda: None if c._fixed is None else "%s %s %s" % (
            os.path.basename(c._fixed.path or ""), c._fixed.data.shape, c._fixed.data.dtype))
        put("fixed_source", lambda: (c.fixed_source or {}).get("name"))
        put("moving", lambda: None if c._confocal is None else "%s [%s] %s" % (
            os.path.basename(c._confocal.source or ""), c._confocal.summary(),
            c._confocal.shape_hw()))
        put("moving_pre", lambda: None if c._moving_pre is None else tuple(c._moving_pre.shape))
        put("pairs", lambda: "%d slots, %d complete" % (
            len(c.slots), sum(1 for sl in c.slots if sl.complete())))
        put("active", lambda: c.active)
        put("targets", lambda: len(c.targets))
        put("active_target", lambda: c.active_target)
        put("transform", lambda: c.transform)
        put("prealign", lambda: c.prealign)
        put("mode", lambda: c.mode)
        put("compare", lambda: getattr(c, "compare_mode", None))
        put("solved", lambda: c._result is not None)
        put("session", lambda: c.session_dir)
        put("window", lambda: "embedded=%s isWindow=%s visible=%s" % (
            self._embedded, self.isWindow(), self.isVisible()))
        return st

    def report_error(self, name, exc):
        """Show a failure that a guarded slot caught (already logged). Deferred until
        the current event handler returns; one box per action per 10 s."""
        msg = "%s failed: %s: %s" % (name, type(exc).__name__, exc)
        try:
            self.statusBar().showMessage(msg + "  |  log: %s" % (diag.log_path() or "-"))
        except Exception:  # noqa: BLE001
            pass
        now = time.monotonic()
        if self._box_open or now - self._last_box.get(name, -1e9) < self.ERROR_BOX_EVERY_S:
            return
        self._last_box[name] = now
        text = ("%s\n\npyMap kept running; the action was stopped where it failed.\n"
                "Details are in %s" % (msg, diag.log_path() or "(no log file)"))
        QTimer.singleShot(0, lambda: self._show_error_box(name, text))

    def _show_error_box(self, name, text):
        if self._box_open:
            return
        self._box_open = True
        try:
            QMessageBox.warning(self, "pyMap: %s" % name, text)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._box_open = False

    def show_diagnostics(self):
        env = diag.environment()
        lines = ["Log file: %s" % (diag.log_path() or "(logging is off)"),
                 "Hang dumps: %s" % (os.path.join(diag.log_dir(), diag.HANGS_FILE)
                                     if diag.log_dir() else "-")]
        for s in diag.previous_unclean():
            lines.append("Earlier run ended without closing: %s" % s)
        lines.append("")
        lines += ["%s: %s" % (k, v) for k, v in env.items()]
        box = QMessageBox(QMessageBox.Information, "pyMap diagnostics", "\n".join(lines),
                          parent=self)
        open_btn = box.addButton("Open log folder", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec_()
        if box.clickedButton() is open_btn and diag.log_dir():
            from qtpy.QtGui import QDesktopServices
            from qtpy.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(diag.log_dir()))

    def event(self, e):
        t = e.type()
        if t in (QEvent.ParentChange, QEvent.Close):
            try:
                p = self.parent()
                diag.log.info("pyMap window %s: %s (parent=%s, isWindow=%s)", hex(id(self)),
                              "reparented" if t == QEvent.ParentChange else "close",
                              type(p).__name__ if p is not None else None, self.isWindow())
            except Exception:  # noqa: BLE001
                pass
        return super().event(e)

    # ---- actions -----------------------------------------------------------
    def _build_actions(self):
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(name, slot):
            a = QAction(name, self)
            a.triggered.connect(self._slot(name, slot))
            tb.addAction(a)
            return a

        act("Open FIB overview (pyFIB)", self.open_fixed_record)
        act("Open FIB", self.open_fixed)
        act("Open Confocal", self.open_moving)
        tb.addSeparator()
        act("Load Session", self.load_session)
        act("Save Session", self.save_session)
        act("Load FIB meta", self.load_meta)
        tb.addSeparator()
        act("Write targets for pyFIB", self.write_targets)
        act("Export", self.export)
        act("Export handoff", self.export_handoff)
        tb.addSeparator()
        act("Fit views", self.fit_views)
        ctl = self.dock.toggleViewAction()          # show / hide the Controls dock
        ctl.setText("Controls")
        tb.addAction(ctl)
        act("Diagnostics", self.show_diagnostics)

    # ---- wiring ------------------------------------------------------------
    def _wire(self):
        # Every connection is a guarded slot. PyQt hands an exception raised in a slot
        # to sys.excepthook (pyFIB's error box), never back to the code that emitted
        # the signal, so the controller -> view connections need guarding as well.
        c, g = self.c, self._slot
        c.fixedImageChanged.connect(g("show FIB image", self.fixed_view.set_image, quiet=True))
        c.movingImageChanged.connect(g("show confocal image", self.moving_view.set_image,
                                       quiet=True))
        c.overlayChanged.connect(g("show overlay", self.overlay_view.set_image, quiet=True))
        c.markersChanged.connect(g("draw landmarks", self._refresh_markers, quiet=True))
        c.targetsChanged.connect(g("draw targets", self._refresh_targets, quiet=True))
        c.residualsChanged.connect(g("draw residuals", self._refresh_residuals, quiet=True))

        self.fixed_view.pointAdded.connect(g("add FIB landmark", self._fixed_added))
        self.fixed_view.pointMoved.connect(g("drag FIB landmark", c.move_fixed, quiet=True))
        self.fixed_view.pointSelected.connect(g("select landmark", c.select))
        self.fixed_view.dragFinished.connect(g("drop FIB landmark", c.commit_move))

        self.moving_view.pointAdded.connect(g("add confocal point", self._moving_added))
        self.moving_view.pointMoved.connect(g("drag confocal landmark", c.move_moving,
                                              quiet=True))
        self.moving_view.pointSelected.connect(g("select landmark", c.select))
        self.moving_view.dragFinished.connect(g("drop confocal landmark", c.commit_move))
        self.moving_view.targetClicked.connect(g("select target", c.select_target))
        self.moving_view.cursorMoved.connect(g("cursor readout", self._cursor_readout,
                                               quiet=True))

        self.overlay_view.targetClicked.connect(g("select target", c.select_target))
        c.targetsWritten.connect(g("targets written callback", self._targets_written))

    def _fixed_added(self, x, y):
        if self.c.mode != MODE_TARGET:
            self.c.place_fixed(x, y)

    def _moving_added(self, x, y):
        if self.c.mode == MODE_TARGET:
            self.c.place_target(x, y)
        else:
            self.c.place_moving(x, y)

    def _refresh_markers(self):
        self.fixed_view.set_markers(self.c.fixed_markers(), self.c.active)
        self.moving_view.set_markers(self.c.moving_markers(), self.c.active)

    def _refresh_targets(self):
        sols = self.c.target_solutions()
        moving = [{"index": s["index"], "x": s["moving"][0], "y": s["moving"][1]}
                  for s in sols]
        fibm = [{"index": s["index"], "x": s["fixed"][0], "y": s["fixed"][1]}
                for s in sols if s["fixed"] is not None]
        self.moving_view.set_target_markers(moving, self.c.active_target)
        self.overlay_view.set_target_markers(fibm, self.c.active_target)

    def _refresh_residuals(self):
        self.overlay_view.set_residuals(self.c.residual_vectors(),
                                        self.c.residual_scale)

    def _cursor_readout(self, x, y):
        fib, um = self.c.map_cursor(x, y)
        if fib is None:
            return
        msg = "cursor  FIB px (%.0f, %.0f)" % (fib[0], fib[1])
        if um is not None:
            msg += "   layout (%.2f, %.2f) um" % (um[0], um[1])
        self.statusBar().showMessage(msg)

    # ---- slots -------------------------------------------------------------
    def open_fixed_record(self):
        """Pick a position from a pyFIB positions file (the +30 deg FIB overview)."""
        from .. import api
        pf = self.positions_file
        if not pf or not os.path.isfile(pf):
            pf, _ = QFileDialog.getOpenFileName(
                self, "pyFIB positions file", os.path.dirname(pf) if pf else "",
                "pyFIB positions (*.json);;All files (*)")
            if not pf:
                return
        try:
            recs = api.load_positions(pf)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Open FIB overview", str(e))
            return
        if not recs:
            QMessageBox.information(self, "Open FIB overview", "No positions in %s" % pf)
            return
        labels = []
        for name, r in recs.items():
            r = r if isinstance(r, dict) else {}
            st = r.get("stage_at") or {}
            t = st.get("t_deg")
            labels.append("%s   (%sx, tilt %s)" % (
                name, ("%g" % r["fib_magnification_x"]) if r.get("fib_magnification_x") else "?",
                ("%.1f" % t) if t is not None else "?"))
        item, ok = QInputDialog.getItem(self, "Open FIB overview",
                                        "Position (%s):" % os.path.basename(pf),
                                        labels, len(labels) - 1, False)
        if not ok:
            return
        name = list(recs)[labels.index(item)]
        try:
            with diag.hang_watch("open FIB overview %s" % name):
                warnings = self.c.load_fixed_from_record(pf, name)
        except Exception as e:  # noqa: BLE001
            self._fail("Open FIB overview", e, "Could not open position %r" % name)
            return
        self.positions_file = pf
        msg = "FIB overview: %s" % name
        if warnings:
            msg += "  |  " + "; ".join(warnings)
        self.statusBar().showMessage(msg)

    def write_targets(self):
        try:
            path = self.c.write_targets_file()
        except Exception as e:  # noqa: BLE001  (refusals, api errors, I/O)
            self._fail("Write targets", e, "Could not write targets")
            return
        self.statusBar().showMessage("Targets written: %s" % path)

    def _targets_written(self, path):
        diag.log.info("targets written: %s", path)
        if self._on_targets_written is not None:
            try:
                self._on_targets_written(path)
            except Exception as e:  # noqa: BLE001  (the host's callback must not break pyMap)
                diag.log.error("host callback on_targets_written failed:\n%s",
                               traceback.format_exc().rstrip())
                self.statusBar().showMessage("targets written; host callback failed: %s" % e)

    def _fail(self, title, error, what="Could not complete this"):
        """Report a failed action without letting the exception reach Qt (an
        unhandled exception in a slot ends the program under PyQt5)."""
        refusal = isinstance(error, (RegistrationError, ConfocalImportError))
        try:
            if refusal:
                diag.log.warning("REFUSED %s: %s", title, error)
            else:
                diag.log.error("FAIL  %s: %s: %s\n  state: %s\n%s", title,
                               type(error).__name__, error, diag._state_text(self.diag_state),
                               "".join(traceback.format_exception(
                                   type(error), error, error.__traceback__)).rstrip())
        except Exception:  # noqa: BLE001
            pass
        if refusal:
            text = str(error)
        else:
            text = "%s:\n%s: %s\n\nDetails: %s" % (what, type(error).__name__, error,
                                                  diag.log_path() or "-")
        QMessageBox.warning(self, title, text)
        self.statusBar().showMessage("%s failed" % title)

    def set_session(self, session_dir, positions_file=None):
        """Point pyMap at a (new) pyFIB session. A FIB overview from another session
        is unlinked, so targets can't be written into this session against it.
        Called by the host: an error is logged and shown, never raised into it."""
        try:
            with diag.action("set_session", detail="%r, %r" % (session_dir, positions_file),
                             state=self.diag_state, boundary=True):
                self._set_session(session_dir, positions_file)
        except Exception as e:  # noqa: BLE001
            self.report_error("set_session", e)

    def _set_session(self, session_dir, positions_file=None):
        self.c.session_dir = str(session_dir) if session_dir else None
        if positions_file is None and session_dir:
            positions_file = os.path.join(str(session_dir), "clem", "overview_positions.json")
        self.positions_file = str(positions_file) if positions_file else None
        fs = self.c.fixed_source
        if fs is not None and session_dir and not _inside(fs["positions_file"], session_dir):
            self.c.fixed_source = None
            self.statusBar().showMessage(
                "Session changed: reopen the FIB overview from this session to write targets")
        else:
            self.statusBar().showMessage("Session: %s" % (session_dir or "none"))

    def open_fixed(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open FIB image", "", _IMG_FILTER)
        if not p:
            return
        try:
            with diag.hang_watch("open FIB %s" % os.path.basename(p)):
                self.c.load_fixed(p)
        except Exception as e:  # noqa: BLE001
            self._fail("Open FIB", e, "Could not open %s" % os.path.basename(p))
            return
        msg = "FIB: %s" % os.path.basename(p)
        notes = self.c.pop_notices()
        if notes:
            msg += "  |  " + "; ".join(notes)
        self.statusBar().showMessage(msg)

    def open_moving(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Open confocal image", "",
            "Confocal (*.tif *.tiff *.ome.tif *.lif *.png *.jpg *.bmp);;All files (*)")
        if not p:
            return
        series = None
        if p.lower().endswith(".lif"):
            try:
                series = self._pick_lif_image(p)
            except Exception as e:  # noqa: BLE001
                self._fail("Open confocal", e, "Could not read %s" % os.path.basename(p))
                return
            if series is False:
                return
        try:
            with diag.hang_watch("open confocal %s" % os.path.basename(p)):
                self.c.load_moving(p, series=series)
        except Exception as e:  # noqa: BLE001
            self._fail("Open confocal", e, "Could not open %s" % os.path.basename(p))
            return
        self.panel.sync_from_controller()
        self.statusBar().showMessage(
            "Confocal: %s  [%s]" % (os.path.basename(p), self.c._confocal.summary()))

    def _pick_lif_image(self, path):
        """Which image of a multi-image .lif to open: an index, None for a file with
        one image, or False when the user cancels (v0.9.2: the first image was always
        used, which may be a preview or one tile)."""
        from ..confocal import list_lif_images
        imgs = list_lif_images(path)
        if len(imgs) <= 1:
            return None
        labels = ["%d. %s  (%dx%d, z %d, %d ch%s)" % (
            im["index"] + 1, im["name"], im["x"], im["y"], im["z"], im["channels"],
            ", %d tiles" % im["m"] if im["m"] > 1 else "") for im in imgs]
        biggest = max(range(len(imgs)), key=lambda i: imgs[i]["x"] * imgs[i]["y"])
        item, ok = QInputDialog.getItem(self, "Open confocal",
                                        "%s holds %d images. Open:" % (
                                            os.path.basename(path), len(imgs)),
                                        labels, biggest, False)
        if not ok:
            return False
        return labels.index(item)

    def load_meta(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open FIB metadata (json)", "",
                                           "JSON (*.json);;All files (*)")
        if not p:
            return
        try:
            with diag.hang_watch("load FIB meta"):
                self.c.load_meta(p)
        except Exception as e:  # noqa: BLE001
            self._fail("Load FIB meta", e, "Could not read %s" % os.path.basename(p))
            return
        self.statusBar().showMessage("Loaded FIB metadata: %s" % os.path.basename(p))

    def load_session(self):
        p, _ = QFileDialog.getOpenFileName(self, "Load session", "",
                                           "JSON (*.json);;All files (*)")
        if not p:
            return
        try:
            with diag.hang_watch("load session %s" % os.path.basename(p)):
                notes = self.c.load_session_from(p)
        except Exception as e:  # noqa: BLE001
            self._fail("Load session", e, "Could not load %s" % os.path.basename(p))
            return
        self.panel.sync_from_controller()
        self.fit_views()
        msg = "Loaded session: %s" % os.path.basename(p)
        if notes:
            msg += "  |  " + "; ".join(notes)
        self.statusBar().showMessage(msg)

    def save_session(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save session", "session.json",
                                           "JSON (*.json)")
        if not p:
            return
        try:
            self.c.save_session_to(p)
        except Exception as e:  # noqa: BLE001
            self._fail("Save session", e, "Could not save %s" % os.path.basename(p))
            return
        self.statusBar().showMessage("Saved session: %s" % os.path.basename(p))

    def export(self):
        d = QFileDialog.getExistingDirectory(self, "Export to folder")
        if not d:
            return
        try:
            with diag.hang_watch("export"):
                rep = self.c.export_to(d)
        except Exception as e:  # noqa: BLE001
            self._fail("Export", e, "Could not export to %s" % d)
            return
        self.statusBar().showMessage("Exported (rms=%.3f px) to %s" % (rep["rms_px"], d))

    def export_handoff(self):
        d = QFileDialog.getExistingDirectory(self, "Export elymaker handoff to folder")
        if not d:
            return
        name, ok = QInputDialog.getText(self, "elymaker handoff",
                                        ".ely filename to write:", text="lamella.ely")
        if not ok or not name:
            return
        try:
            with diag.hang_watch("export handoff"):
                rep = self.c.export_handoff(d, ely_out=name)
        except Exception as e:  # noqa: BLE001
            self._fail("Handoff", e, "Could not write the handoff to %s" % d)
            return
        self.statusBar().showMessage(
            "Handoff: job.json + %d target(s) -> %s (open with elymaker-gui --job)"
            % (rep["n_targets"], d))

    def fit_views(self):
        self.fixed_view.fit()
        self.moving_view.fit()
        self.overlay_view.fit()
