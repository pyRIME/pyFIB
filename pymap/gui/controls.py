"""Right-hand control panel: mode, pre-align, transform, display, targets, QC."""

from __future__ import annotations

from qtpy.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                            QPushButton, QCheckBox, QComboBox, QLabel, QSlider,
                            QGridLayout, QTableWidget, QTableWidgetItem,
                            QHeaderView, QRadioButton, QButtonGroup)
from qtpy.QtCore import Qt, Signal, QTimer

from ..model import TransformKind, PreAlign
from ..composite import COLORMAPS
from .. import diag
from .style import DANGER
from .controller import (MODE_LANDMARK, MODE_TARGET, COMPARE_MODES,
                         CMP_FLICKER, CMP_SWIPE, CMP_CHECKER)

CH_COLORS = {
    "green": (0.0, 1.0, 0.0), "magenta": (1.0, 0.0, 1.0),
    "cyan": (0.0, 1.0, 1.0), "red": (1.0, 0.0, 0.0),
    "blue": (0.35, 0.55, 1.0), "yellow": (1.0, 1.0, 0.0),
    "gray": (1.0, 1.0, 1.0),
}
CH_COLOR_NAMES = list(CH_COLORS.keys())


def _nearest_color_name(rgb):
    best, bd = "gray", 1e9
    for nm, c in CH_COLORS.items():
        d = sum((a - b) ** 2 for a, b in zip(rgb, c))
        if d < bd:
            best, bd = nm, d
    return best


def _slider_int(v, default=0) -> int:
    """A channel window value (0..1) as a 0..1000 slider position; NaN/inf safe."""
    try:
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return default
        return int(round(min(max(f, 0.0), 1.0) * 1000))
    except (TypeError, ValueError):
        return default


class ControlPanel(QWidget):
    exportHandoffRequested = Signal()

    def __init__(self, controller, parent=None, error_owner=None):
        super().__init__(parent)
        self.c = controller
        self._rot = 0
        self._error_owner = error_owner          # MainWindow: report_error / diag_state
        # the flicker timer skips its tick while this returns False (MainWindow sets
        # it to the overlay view's visibility: no rendering while pyMap is hidden)
        self.flicker_visible = None
        # channel sliders: values are stored at once; the moving view is re-merged
        # 60 ms after the last change and the overlay re-warped 350 ms after it, so
        # keyboard / wheel / groove-click changes reach the overlay too (v0.9.2)
        self._chan_refresh = QTimer(self)
        self._chan_refresh.setSingleShot(True)
        self._chan_refresh.setInterval(60)
        self._chan_commit = QTimer(self)
        self._chan_commit.setSingleShot(True)
        self._chan_commit.setInterval(350)
        self._chan_dirty = False                 # stored values not yet re-warped
        self._build()
        self._chan_refresh.timeout.connect(self._g("channel display", self.c.refresh_moving_display))
        self._chan_commit.timeout.connect(self._g("apply channel display", self._channel_commit))
        # controller -> panel: PyQt sends an exception raised in these to the host's
        # excepthook, not back to the emitter, so they are guarded too (quiet: frequent)
        self.c.qcChanged.connect(self._g("show fit quality", self._on_qc, quiet=True))
        self.c.targetsChanged.connect(self._g("show targets", self._on_targets, quiet=True))
        self.c.channelsChanged.connect(self._g("rebuild channel rows", self._rebuild_channels))

    def _build(self):
        root = QVBoxLayout(self)

        # --- Mode -----------------------------------------------------------
        mode = QGroupBox("Pick mode")
        ml = QHBoxLayout(mode)
        self.rb_lm = QRadioButton("Landmarks")
        self.rb_tg = QRadioButton("Targets")
        self.rb_lm.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_lm)
        grp.addButton(self.rb_tg)
        self.rb_lm.toggled.connect(self._g(
            "pick mode landmarks", lambda on: on and self.c.set_mode(MODE_LANDMARK), quiet=True))
        self.rb_tg.toggled.connect(self._g(
            "pick mode targets", lambda on: on and self.c.set_mode(MODE_TARGET), quiet=True))
        ml.addWidget(self.rb_lm)
        ml.addWidget(self.rb_tg)
        root.addWidget(mode)

        # --- Pre-align ------------------------------------------------------
        pa = QGroupBox("Pre-align (moving)")
        g = QGridLayout(pa)
        b_ccw = QPushButton("Rotate CCW")
        b_cw = QPushButton("Rotate CW")
        b_ccw.clicked.connect(self._g("rotate CCW", lambda: self._rotate(+1)))
        b_cw.clicked.connect(self._g("rotate CW", lambda: self._rotate(-1)))
        self.cb_h = QCheckBox("Flip H")
        self.cb_v = QCheckBox("Flip V")
        self.cb_h.toggled.connect(self._g("flip H", self._prealign_changed))
        self.cb_v.toggled.connect(self._g("flip V", self._prealign_changed))
        self.lbl_rot = QLabel("rot90 = 0")
        g.addWidget(b_ccw, 0, 0)
        g.addWidget(b_cw, 0, 1)
        g.addWidget(self.cb_h, 1, 0)
        g.addWidget(self.cb_v, 1, 1)
        g.addWidget(self.lbl_rot, 2, 0, 1, 2)
        root.addWidget(pa)

        # --- Transform ------------------------------------------------------
        tf = QGroupBox("Transform")
        tl = QVBoxLayout(tf)
        self.cmb_tf = QComboBox()
        self.cmb_tf.addItems(list(TransformKind.ALL))
        self.cmb_tf.setCurrentText(self.c.transform)
        self.cmb_tf.currentTextChanged.connect(self._g("set transform", self.c.set_transform))
        tl.addWidget(self.cmb_tf)
        root.addWidget(tf)

        # --- Display --------------------------------------------------------
        dp = QGroupBox("Display")
        dg = QGridLayout(dp)
        self.cmb_cmap = QComboBox()
        self.cmb_cmap.addItems(sorted(COLORMAPS.keys()) + ["rgb"])
        self.cmb_cmap.setCurrentText(self.c.cmap)
        self.cmb_cmap.currentTextChanged.connect(self._g(
            "set LUT", lambda v: self.c.set_display(cmap=v)))
        self.sl_op = self._slider(int(self.c.opacity * 100))
        self.sl_op.valueChanged.connect(self._g(
            "opacity slider", lambda v: self.c.set_display(opacity=v / 100.0), quiet=True))
        self.sl_lo = self._slider(0)
        self.sl_hi = self._slider(100)
        self.sl_lo.valueChanged.connect(self._g("contrast slider", self._contrast_changed, quiet=True))
        self.sl_hi.valueChanged.connect(self._g("contrast slider", self._contrast_changed, quiet=True))
        dg.addWidget(QLabel("LUT"), 0, 0)
        dg.addWidget(self.cmb_cmap, 0, 1)
        dg.addWidget(QLabel("Opacity"), 1, 0)
        dg.addWidget(self.sl_op, 1, 1)
        dg.addWidget(QLabel("Contrast lo"), 2, 0)
        dg.addWidget(self.sl_lo, 2, 1)
        dg.addWidget(QLabel("Contrast hi"), 3, 0)
        dg.addWidget(self.sl_hi, 3, 1)
        root.addWidget(dp)

        # --- Confocal channels (dynamic) ------------------------------------
        self.chan_box = QGroupBox("Confocal channels")
        self.chan_layout = QVBoxLayout(self.chan_box)
        self.chan_box.setVisible(False)
        root.addWidget(self.chan_box)

        # --- Landmarks ------------------------------------------------------
        pt = QGroupBox("Landmarks")
        pg = QHBoxLayout(pt)
        b_del = QPushButton("Delete selected")
        b_clr = QPushButton("Clear all")
        b_del.clicked.connect(self._g("delete selected landmark", self.c.delete_active))
        b_clr.clicked.connect(self._g("clear all landmarks", self.c.clear))
        pg.addWidget(b_del)
        pg.addWidget(b_clr)
        root.addWidget(pt)

        # --- QC -------------------------------------------------------------
        qc = QGroupBox("Fit quality")
        qg = QVBoxLayout(qc)
        self.lbl_n = QLabel("pairs: 0")
        self.lbl_rms = QLabel("RMS: -")
        self.lbl_loo = QLabel("LOO RMS: -")
        self.lbl_grid = QLabel("Grid error: -")
        self.lbl_grid.setToolTip(
            "Worst-axis leave-one-out error on the grid (um). pyFIB warns above "
            "1 um and refuses above 3 um unless overridden.")
        self.lbl_squash = QLabel("Squash: -")
        self.lbl_squash.setToolTip(
            "How much the fit squashes the image vertically (smallest / largest "
            "scale). About 0.59 for the FIB at +30 deg, about 1 for the SEM. A very "
            "different value suggests a wrong flip/rotation or tilt correction.")
        self.lbl_msg = QLabel("")
        self.lbl_msg.setWordWrap(True)
        self.lbl_msg.setStyleSheet("color:%s;" % DANGER)
        for w in (self.lbl_n, self.lbl_rms, self.lbl_loo, self.lbl_grid,
                  self.lbl_squash, self.lbl_msg):
            qg.addWidget(w)
        self.tbl = QTableWidget(0, 2)
        self.tbl.setHorizontalHeaderLabels(["#", "residual px"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        qg.addWidget(self.tbl)
        root.addWidget(qc)

        # --- Verify (overlay) ----------------------------------------------
        vf = QGroupBox("Verify alignment (overlay)")
        vg = QGridLayout(vf)
        self.cmb_cmp = QComboBox()
        self.cmb_cmp.addItems(list(COMPARE_MODES))
        self.cmb_cmp.currentTextChanged.connect(self._g("compare mode", self._compare_mode_changed))
        self.sl_split = self._slider(50)
        self.sl_split.valueChanged.connect(self._g(
            "swipe slider", lambda v: self.c.set_compare(split=v / 100.0), quiet=True))
        self.sl_tile = QSlider(Qt.Horizontal)
        self.sl_tile.setRange(8, 128)
        self.sl_tile.setValue(32)
        self.sl_tile.valueChanged.connect(self._g(
            "checker slider", lambda v: self.c.set_compare(tile=v), quiet=True))
        self.cb_resid = QCheckBox("Residual arrows")
        self.cb_resid.toggled.connect(self._g(
            "residual arrows", lambda on: self.c.set_residuals(visible=on)))
        self.sl_rscale = QSlider(Qt.Horizontal)
        self.sl_rscale.setRange(1, 50)
        self.sl_rscale.setValue(1)
        self.sl_rscale.valueChanged.connect(self._g(
            "residual scale slider", lambda v: self.c.set_residuals(scale=float(v)), quiet=True))
        vg.addWidget(QLabel("Mode"), 0, 0)
        vg.addWidget(self.cmb_cmp, 0, 1)
        vg.addWidget(QLabel("Swipe %"), 1, 0)
        vg.addWidget(self.sl_split, 1, 1)
        vg.addWidget(QLabel("Checker tile"), 2, 0)
        vg.addWidget(self.sl_tile, 2, 1)
        vg.addWidget(self.cb_resid, 3, 0)
        vg.addWidget(self.sl_rscale, 3, 1)
        root.addWidget(vf)

        self._flicker = QTimer(self)
        self._flicker.setInterval(450)
        self._flicker.timeout.connect(self._g("flicker tick", self._flicker_tick, quiet=True))

        # --- Targets --------------------------------------------------------
        tg = QGroupBox("Targets -> elymaker")
        tgl = QVBoxLayout(tg)
        self.ttbl = QTableWidget(0, 5)
        self.ttbl.setHorizontalHeaderLabels(["#", "FIB x", "FIB y", "X um", "Y um"])
        self.ttbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.ttbl.verticalHeader().setVisible(False)
        self.ttbl.setEditTriggers(QTableWidget.NoEditTriggers)
        tgl.addWidget(self.ttbl)
        trow = QHBoxLayout()
        b_tdel = QPushButton("Delete target")
        b_tclr = QPushButton("Clear targets")
        b_tdel.clicked.connect(self._g("delete target", self.c.delete_active_target))
        b_tclr.clicked.connect(self._g("clear targets", self.c.clear_targets))
        trow.addWidget(b_tdel)
        trow.addWidget(b_tclr)
        tgl.addLayout(trow)
        b_hand = QPushButton("Export elymaker handoff...")
        b_hand.clicked.connect(lambda *_: self.exportHandoffRequested.emit())
        tgl.addWidget(b_hand)
        root.addWidget(tg)

        root.addStretch(1)

    def _g(self, name, fn, quiet=False):
        """A guarded slot: an error is logged and reported, never passed to Qt."""
        return diag.slot(name, fn, owner=self._error_owner, quiet=quiet)

    def _slider(self, val):
        s = QSlider(Qt.Horizontal)
        s.setRange(0, 100)
        s.setValue(val)
        return s

    # ---- handlers ----------------------------------------------------------
    def _rotate(self, d):
        self._rot = (self._rot + d) % 4
        self.lbl_rot.setText("rot90 = %d" % self._rot)
        self._prealign_changed()

    def _prealign_changed(self, *_):
        self.c.set_prealign(PreAlign(rot90=self._rot,
                                     flip_h=self.cb_h.isChecked(),
                                     flip_v=self.cb_v.isChecked()))

    def _contrast_changed(self, *_):
        lo = self.sl_lo.value() / 100.0
        hi = self.sl_hi.value() / 100.0
        if hi <= lo:
            hi = min(1.0, lo + 0.01)
        self.c.set_display(contrast=(lo, hi))

    def _compare_mode_changed(self, mode):
        self.c.set_compare(mode=mode)
        if mode == CMP_FLICKER:
            self._flicker.start()
        else:
            self._flicker.stop()

    def _flicker_tick(self):
        vis = self.flicker_visible
        if vis is not None and not vis():
            return
        self.c.set_compare(flicker_phase=not self.c.flicker_phase)

    def sync_from_controller(self):
        """Show the controller's transform, pre-align, LUT and compare mode without
        re-triggering them (v0.9.2: after Load Session the panel still showed the
        old values, and ticking Flip H then reset the loaded rotation)."""
        c = self.c
        widgets = (self.cmb_tf, self.cb_h, self.cb_v, self.cmb_cmap, self.cmb_cmp,
                   self.rb_lm, self.rb_tg)
        for w in widgets:
            w.blockSignals(True)
        try:
            self.cmb_tf.setCurrentText(c.transform)
            self._rot = int(c.prealign.rot90) % 4
            self.lbl_rot.setText("rot90 = %d" % self._rot)
            self.cb_h.setChecked(bool(c.prealign.flip_h))
            self.cb_v.setChecked(bool(c.prealign.flip_v))
            self.cmb_cmap.setCurrentText(c.cmap)
            self.cmb_cmp.setCurrentText(c.compare_mode)
            (self.rb_tg if c.mode == MODE_TARGET else self.rb_lm).setChecked(True)
        finally:
            for w in widgets:
                w.blockSignals(False)
        if c.compare_mode == CMP_FLICKER:
            self._flicker.start()
        else:
            self._flicker.stop()

    def _channel_slider(self, idx, which, value, slider=None):
        if which == "low":
            self.c.set_channel_param(idx, low=value / 1000.0, refresh=False)
        else:
            self.c.set_channel_param(idx, high=value / 1000.0, refresh=False)
        self._chan_dirty = True
        self._chan_refresh.start()
        # while the mouse holds the slider only the moving view follows; the overlay is
        # re-warped once on release (v0.9.2 review: a pause mid-drag re-warped too)
        if slider is None or not slider.isSliderDown():
            self._chan_commit.start()

    def _channel_commit(self):
        if self._chan_dirty:
            self._chan_dirty = False
            self.c.commit_channels()

    def _channel_released(self):
        self._chan_refresh.stop()
        self._chan_commit.stop()
        self._channel_commit()

    # ---- confocal channels -------------------------------------------------
    def _rebuild_channels(self):
        while self.chan_layout.count():
            item = self.chan_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        infos = self.c.channel_infos()
        self.chan_box.setVisible(len(infos) > 0)
        for info in infos:
            self.chan_layout.addWidget(self._channel_row(info))

    def _channel_row(self, info):
        i = info["index"]
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(2, 2, 2, 2)
        cb = QCheckBox(info["name"])
        cb.setChecked(info["include"])
        cb.toggled.connect(self._g(
            "channel %d include" % (i + 1),
            lambda on, idx=i: self.c.set_channel_param(idx, include=on, commit=True)))
        cc = QComboBox()
        cc.addItems(CH_COLOR_NAMES)
        cc.setCurrentText(_nearest_color_name(info["color"]))
        cc.currentTextChanged.connect(self._g(
            "channel %d colour" % (i + 1),
            lambda nm, idx=i: self.c.set_channel_param(idx, color=CH_COLORS[nm], commit=True)))
        black = QSlider(Qt.Horizontal)
        black.setRange(0, 1000)
        black.setValue(_slider_int(info["low"]))
        white = QSlider(Qt.Horizontal)
        white.setRange(0, 1000)
        white.setValue(_slider_int(info["high"], 1000))
        black.valueChanged.connect(self._g(
            "channel %d black slider" % (i + 1),
            lambda v, idx=i, s=black: self._channel_slider(idx, "low", v, s), quiet=True))
        white.valueChanged.connect(self._g(
            "channel %d white slider" % (i + 1),
            lambda v, idx=i, s=white: self._channel_slider(idx, "high", v, s), quiet=True))
        black.sliderReleased.connect(self._g("apply channel display", self._channel_released))
        white.sliderReleased.connect(self._g("apply channel display", self._channel_released))
        g.addWidget(cb, 0, 0)
        g.addWidget(cc, 0, 1)
        g.addWidget(QLabel("black"), 1, 0)
        g.addWidget(black, 1, 1)
        g.addWidget(QLabel("white"), 2, 0)
        g.addWidget(white, 2, 1)
        return w

    def _on_qc(self, qc: dict):
        self.lbl_n.setText("pairs: %d" % qc["n"])
        def px_um(px, um):
            if px is None:
                return "-"
            return "%.3f px" % px + ("" if um is None else " (%.2f um)" % um)
        self.lbl_rms.setText("RMS: " + px_um(qc["rms"], qc.get("rms_um")))
        self.lbl_loo.setText("LOO RMS: " + px_um(qc["loo"], qc.get("loo_um")))
        g = qc.get("loo_grid_um")
        self.lbl_grid.setText("Grid error: -" if g is None else "Grid error: %.2f um" % g)
        colour = "" if g is None else ("color:%s;" % DANGER if g > 3.0 else
                                       "color:#e0a84a;" if g > 1.0 else "")
        self.lbl_grid.setStyleSheet(colour)
        sq = qc.get("squash")
        self.lbl_squash.setText("Squash: -" if sq is None else "Squash: %.2f" % sq)
        self.lbl_msg.setText(qc.get("message", "") or "")
        res = qc.get("residuals", []) or []
        self.tbl.setRowCount(len(res))
        for i, r in enumerate(res):
            self.tbl.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.tbl.setItem(i, 1, QTableWidgetItem("%.3f" % r))

    def _on_targets(self):
        sols = self.c.target_solutions()
        self.ttbl.setRowCount(len(sols))
        for row, s in enumerate(sols):
            fib = s["fixed"]
            um = s["um"]
            vals = [str(s["index"] + 1),
                    "-" if fib is None else "%.1f" % fib[0],
                    "-" if fib is None else "%.1f" % fib[1],
                    "-" if um is None else "%.2f" % um[0],
                    "-" if um is None else "%.2f" % um[1]]
            for col, v in enumerate(vals):
                self.ttbl.setItem(row, col, QTableWidgetItem(v))
