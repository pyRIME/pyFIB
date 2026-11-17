"""Correlation tab (v0.52.0): CLEM targeting with pyMap, part 1 -- nothing moves to a target.

Evan 2026-10-09: one tab after GISDEP with two panes.

  Targets (pyFIB, left)
    1. Grid at SEM normal: Go to grid (the existing setup move via CLEM SAFE to Grid1 /
       Grid2 GIS Normal; ARM-gated + confirmed) and Coincidence check (the existing
       measure-and-recommend tool; coincidence stays manual for now).
    2. Overviews: Capture FIB overview (400x, beam shift zeroed first, no 800x frame)
       and Capture SEM overview (pyFIB grabs it and records the stage), both into
       <session>/clem/ through `clem-overview` -- ARM-gated, confirmed, never move the
       stage, refuse unless at SEM normal.
    3. Targets: pyMap's newest targets file in <session>/clem, checked in-process (files
       only) and shown with each target's stage X/Y, offset from the overview centre,
       status and reasons. "Check targets" writes the plan (`clem-targets`).
    4. Mark targets (v0.53.0, Evan 2026-10-10): tick targets; ONE SmartFIB layout with
       the markup pattern (default Markup_Standard centred on its tick = the lamella) on
       every ticked target, milled at the FIB overview's position and magnification
       (`clem-markup`; the stage never moves). Edit in ELYMaker opens the layout over
       the overview with the targets drawn in; a saved edit is milled as saved; Reset
       regenerates it. Visiting targets comes later, after the 400x stage calibration.

  Register (pyMap, right)
    pyMap embedded when it is installed next to pyFIB and provides
    pymap.gui.embed.build_widget(...); otherwise a note (or "Open pyMap" for a pyMap
    without the embed entry point). pyMap only reads/writes files; it never touches the
    instrument.

    v0.52.1 (pyMap v0.9.0 bundled at the pyFIB root): build_widget's positions_file is
    the file pyMap offers FIB overviews from, so pyFIB passes
    <session>/clem/overview_positions.json (never the milling positions file). pyMap's
    only interface is build_widget + the callback, so a session change REBUILDS the
    widget instead of calling anything on it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Optional

from qtpy.QtCore import Qt, QTimer
from qtpy.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .pages import _card, _section, _title


def find_pymap():
    """(package dir, has_embed) for a pyMap that is OURS, else (None, False). Uses
    find_spec on the top package only (no import): an unrelated PyPI 'pymap' (an IMAP
    server) has no gui/ folder and is ignored."""
    try:
        spec = importlib.util.find_spec("pymap")
    except Exception:  # noqa: BLE001
        return None, False
    locs = list(getattr(spec, "submodule_search_locations", None) or []) if spec else []
    if not locs or not (Path(locs[0]) / "gui").is_dir():
        return None, False
    root = Path(locs[0])
    return root, (root / "gui" / "embed.py").exists()


class RegisterPane(QWidget):
    def __init__(self, page):
        super().__init__()
        self._page = page
        self._widget = None
        self._built_for = None
        self._declined_for = None    # v0.55.0: a session whose pyMap restart was declined
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 0)
        self.wide_btn = QPushButton("Hide targets pane")
        self.wide_btn.setCheckable(True)
        self.wide_btn.setToolTip("Give pyMap the whole tab while registering; click again "
                                 "to bring the targets pane back.")
        self.wide_btn.toggled.connect(self._toggle_wide)
        # v0.53.2 (Evan: the panels are too small): pyMap in its own window.
        self.detach_btn = QPushButton("Open pyMap in own window")
        self.detach_btn.setToolTip("Move pyMap (with its images, landmarks and targets) "
                                   "into its own window, e.g. a second monitor, maximised. "
                                   "Close that window to bring it back here.")
        self.detach_btn.clicked.connect(self.detach)
        self.detach_btn.setEnabled(False)
        bar.addStretch(1)
        bar.addWidget(self.detach_btn)
        bar.addWidget(self.wide_btn)
        self._lay.addLayout(bar)
        # The note + button live in their own placeholder so its spacers never compete
        # with the embedded pyMap widget for height.
        self._placeholder = QWidget()
        ph = QVBoxLayout(self._placeholder)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setAlignment(Qt.AlignCenter)
        self.note.setStyleSheet("color:#9aa3b2;")
        self.open_btn = QPushButton("Open pyMap")
        self.open_btn.clicked.connect(self._open_separately)
        self.open_btn.hide()
        ph.addStretch(1)
        ph.addWidget(self.note)
        ph.addWidget(self.open_btn, alignment=Qt.AlignCenter)
        ph.addStretch(1)
        self._lay.addWidget(self._placeholder, 1)
        from .embedding import Detachable
        self.host = Detachable("pyMap (pyFIB Correlation)", self._lay,
                               "pyMap is open in its own window. Targets it writes still "
                               "appear on the left.")
        self.host.listeners.append(self._on_detached)

    def _on_detached(self, on: bool) -> None:
        self.detach_btn.setEnabled(not on and self._widget is not None)
        self.wide_btn.setEnabled(not on)
        if on and self.wide_btn.isChecked():
            self.wide_btn.setChecked(False)

    @property
    def detached(self) -> bool:
        return self.host.detached

    def detach(self) -> None:
        self.host.detach()

    def _log(self, text: str) -> None:
        """One line in pyFIB's Log tab (best effort; never raises)."""
        try:
            self._page._win.log_page.append(text)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _overview_positions(sd: str) -> str:
        from ..clem import OVERVIEW_POSITIONS, clem_dir
        return str(clem_dir(sd) / OVERVIEW_POSITIONS)

    def _n_landmarks(self) -> int:
        try:
            return len(self._widget.c.slots)       # pyMap internals: count only, best effort
        except Exception:  # noqa: BLE001
            return 0

    def ensure_built(self, session_dir, positions_file=None) -> None:
        sd = str(session_dir) if session_dir else None
        was_detached = False
        if self._widget is not None:
            if sd == self._built_for:
                return
            # v0.55.0 (pyMap v0.9.2 review, request 1): a new session no longer destroys
            # pyMap. Rebuilding silently lost the operator's images, landmarks and targets
            # ("pyMap crashed and reset"). pyMap >= 0.9.1 takes the new session in place
            # and unlinks an overview from the old one, so targets can't be written into
            # the new session against it.
            if hasattr(self._widget, "set_session"):
                old, self._built_for = self._built_for, sd
                # set_session logs and shows its own errors; it never raises into pyFIB.
                self._widget.set_session(
                    sd, positions_file=self._overview_positions(sd) if sd else None)
                now = getattr(getattr(self._widget, "c", None), "session_dir", sd)
                if now != sd:
                    self._log(f"[pyMap] session change to {sd} FAILED inside pyMap (it "
                              f"still reports {now}); see logs/pymap.log. Do not write "
                              f"targets until pyMap shows the new session.")
                    return
                n = self._n_landmarks()
                self._log(f"[pyMap] session changed: {old} -> {sd}. pyMap kept its images "
                          f"and {n} landmark pair(s); open the FIB overview from this "
                          f"session before writing targets.")
                if n:
                    # Review (v0.55.0): the FIB points were picked on the OLD overview.
                    # If the new overview is a recapture or another grid they no longer
                    # match it, yet the fit still looks good (the pairs agree with each
                    # other) and the targets would be shifted. Say so, plainly, once.
                    msg = (f"The session changed while pyMap holds {n} landmark pair(s).\n\n"
                           f"Their FIB points were picked on the previous session's "
                           f"overview. Before writing targets for this session, open its "
                           f"own FIB overview and re-pick or clear those pairs: points "
                           f"carried over from another image give shifted targets even "
                           f"when the fit looks good.")
                    self._log("[pyMap] " + msg.replace("\n\n", " "))
                    try:
                        from qtpy.QtWidgets import QMessageBox
                        QMessageBox.warning(self, "pyMap: landmarks from the old overview", msg)
                    except Exception:  # noqa: BLE001
                        pass
                return
            # Fallback (a pyMap without set_session): rebuild -- but ask first when
            # landmarks would be lost, and only once per session.
            n = self._n_landmarks()
            if n:
                if self._declined_for == sd:
                    return
                if not self._page._win.confirm(
                        f"The session changed. This pyMap cannot follow it, so it must be "
                        f"restarted, which discards its {n} landmark pair(s), images and "
                        f"targets.\n\nRestart pyMap now? (No: keep working in it; it still "
                        f"writes to {self._built_for})"):
                    self._declined_for = sd
                    self._log(f"[pyMap] session changed to {sd}; restart declined, pyMap "
                              f"still writes to {self._built_for}.")
                    return
            self._log(f"[pyMap] rebuilding for the new session: {self._built_for} -> {sd} "
                      f"({n} landmark pair(s) discarded).")
            was_detached = self.host.detached          # rebuild in the tab, then move
            self.host.reattach()                        # it back out (v0.53.2)
            self.host.set_widget(None)
            self.detach_btn.setEnabled(False)
            self._lay.removeWidget(self._widget)       # new session: rebuild
            self._widget.setParent(None)
            self._widget.deleteLater()
            self._widget = None
            self._built_for = None
            self._placeholder.show()
        if sd is None:
            self.note.setText("Set a positions file (session) first.")
            return
        root, has_embed = find_pymap()
        if root is None:
            self.note.setText("pyMap is not installed next to pyFIB.\n\nPut the pymap "
                              "folder beside smartfib_executor (the pyFIB folder) to register "
                              "the confocal here. Targets written by pyMap into "
                              "<session>/clem still appear on the left.")
            return
        if not has_embed:
            self.note.setText(f"pyMap found ({root}) but it has no embed entry point "
                              f"(pymap.gui.embed). Open it as its own window; its targets "
                              f"file still appears on the left.")
            self.open_btn.show()
            return
        try:
            from pymap.gui.embed import build_widget        # type: ignore
            self._widget = build_widget(
                parent=self, session_dir=sd,
                positions_file=self._overview_positions(sd),
                on_targets_written=lambda *_a: self._page.refresh_targets())
        except Exception as e:  # noqa: BLE001
            self.note.setText(f"pyMap could not start: {e}\n\nIt needs scikit-image, "
                              f"numpy and Pillow: python -m pip install -r "
                              f"requirements-pymap.txt")
            self._widget = None
            return
        self._built_for = sd
        self._declined_for = None
        self._placeholder.hide()
        from .embedding import contain
        contain(self._widget)          # v0.53.2: pyMap's minimum size stays out of pyFIB's
        self._lay.addWidget(self._widget, 1)
        self.host.set_widget(self._widget)
        self.detach_btn.setEnabled(True)
        self._page.give_pymap_room()
        if was_detached:
            self.host.detach()

    def _toggle_wide(self, on: bool) -> None:
        self._page.left_pane.setVisible(not on)
        self.wide_btn.setText("Show targets pane" if on else "Hide targets pane")

    def _open_separately(self) -> None:
        # instrument=False (v0.55.1): pyMap only reads and writes files
        self._page._win.run_module("pymap.gui", [], show_log=False, instrument=False)


class CorrelationPage(QWidget):
    (T_MARK, T_LABEL, T_ID, T_STATUS, T_X, T_Y, T_DX, T_DY, T_OFF, T_MARKUP,
     T_NOTES) = range(11)
    HEADERS = ["Mark", "Label", "ID", "Status", "Stage X (mm)", "Stage Y (mm)", "ΔX (µm)",
               "ΔY (µm)", "From centre (µm)", "Markup", "Notes"]

    def __init__(self, window):
        super().__init__()
        self._win = window
        self._sig = None
        self.plan = None
        self.mplan = None
        self._mark_sel = {}          # target id -> ticked (kept across refreshes)
        self._filling = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        outer.addWidget(split)

        # ---------------------------------------------------------------- left
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        left = QWidget()
        lay = QVBoxLayout(left)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.addWidget(_title("Correlation"))

        c1 = _card(); l1 = QVBoxLayout(c1)
        l1.addWidget(_section("1 · Grid at SEM normal (T 30°, R 12°)"))
        r = QHBoxLayout()
        r.addWidget(QLabel("Grid:"))
        self.grid_combo = QComboBox()
        self.grid_combo.addItems(["Grid1", "Grid2"])
        r.addWidget(self.grid_combo)
        self.goto_grid_btn = QPushButton("Go to grid")
        self.goto_grid_btn.setToolTip("Setup move to the grid's GIS Normal fixture via CLEM "
                                      "SAFE (Z first). ARM-gated and confirmed.")
        self.goto_grid_btn.clicked.connect(self._on_goto_grid)
        r.addWidget(self.goto_grid_btn)
        self.coinc_btn = QPushButton("Coincidence check")
        self.coinc_btn.setToolTip("SEM autofocus + WD read; RECOMMENDS a Z move toward 5 mm. "
                                  "Moves nothing (coincidence stays manual for now).")
        self.coinc_btn.clicked.connect(self._on_coincidence)
        r.addWidget(self.coinc_btn)
        r.addStretch(1)
        l1.addLayout(r)
        lay.addWidget(c1)

        c2 = _card(); l2 = QVBoxLayout(c2)
        l2.addWidget(_section("2 · Overviews"))
        from ..clem import limits as L
        r = QHBoxLayout()
        r.addWidget(QLabel("FIB mag:"))
        self.fib_mag = QDoubleSpinBox(); self.fib_mag.setRange(50, 5000)
        self.fib_mag.setDecimals(0); self.fib_mag.setValue(L.FIB_OVERVIEW_MAG)
        self.fib_mag.setSuffix(" x")
        r.addWidget(self.fib_mag)
        self.fib_btn = QPushButton("Capture FIB overview")
        self.fib_btn.clicked.connect(lambda: self._on_capture("fib"))
        r.addWidget(self.fib_btn)
        r.addSpacing(16)
        r.addWidget(QLabel("SEM mag:"))
        self.sem_mag = QDoubleSpinBox(); self.sem_mag.setRange(10, 20000)
        self.sem_mag.setDecimals(0); self.sem_mag.setValue(L.SEM_OVERVIEW_MAG)
        self.sem_mag.setSuffix(" x")
        r.addWidget(self.sem_mag)
        self.sem_btn = QPushButton("Capture SEM overview")
        self.sem_btn.clicked.connect(lambda: self._on_capture("sem"))
        r.addWidget(self.sem_btn)
        r.addStretch(1)
        l2.addLayout(r)
        self.ov_label = QLabel("No overviews yet.")
        self.ov_label.setWordWrap(True)
        self.ov_label.setStyleSheet("color:#9aa3b2;")
        l2.addWidget(self.ov_label)
        lay.addWidget(c2)

        c3 = _card(); l3 = QVBoxLayout(c3)
        l3.addWidget(_section("3 · Targets from pyMap"))
        r = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_targets)
        r.addWidget(self.refresh_btn)
        self.check_btn = QPushButton("Check targets (write plan)")
        self.check_btn.clicked.connect(self._on_check)
        r.addWidget(self.check_btn)
        self.accept_qc = QCheckBox(f"Accept registration error above {L.QC_REFUSE_UM:g} µm")
        self.accept_qc.setToolTip("Operator override, recorded in the plan.")
        self.accept_qc.toggled.connect(lambda _c: self.refresh_targets())
        r.addWidget(self.accept_qc)
        r.addStretch(1)
        l3.addLayout(r)
        self.file_label = QLabel("")
        self.file_label.setWordWrap(True)
        l3.addWidget(self.file_label)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color:#9aa3b2;")
        l3.addWidget(self.summary)
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.T_NOTES, QHeaderView.Stretch)
        self.table.setMinimumHeight(220)
        self.table.itemChanged.connect(self._on_item_changed)
        l3.addWidget(self.table, 1)
        lay.addWidget(c3, 1)

        # v0.53.0: mark targets with the markup trenches, at the overview position.
        from ..clem.markup import DEFAULT_MARKUP
        c4 = _card(); l4 = QVBoxLayout(c4)
        l4.addWidget(_section("4 · Mark targets (markup trenches at SEM normal)"))
        hint = QLabel("One SmartFIB layout with the markup on every ticked target, milled "
                      "at the FIB overview's position and magnification: the stage does "
                      "not move. The lamella goes midway between the trenches, at the "
                      "tick's height.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa3b2;")
        l4.addWidget(hint)
        r = QHBoxLayout()
        r.addWidget(QLabel("Markup pattern (.ely):"))
        self.markup_edit = QLineEdit(str(DEFAULT_MARKUP))
        self.markup_edit.setToolTip("Its origin is where the lamella goes (Markup_Standard "
                                    "centred on the tick, bundled).")
        self.markup_edit.editingFinished.connect(self.refresh_markup)
        r.addWidget(self.markup_edit, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse_markup)
        r.addWidget(b)
        l4.addLayout(r)
        r = QHBoxLayout()
        r.addWidget(QLabel("Mark:"))
        for text, val in (("All OK", True), ("None", False)):
            bb = QPushButton(text)
            bb.clicked.connect(lambda _=False, v=val: self._set_marks(v))
            r.addWidget(bb)
        r.addStretch(1)
        l4.addLayout(r)
        r = QHBoxLayout()
        self.mk_check_btn = QPushButton("Check markup")
        self.mk_check_btn.setToolTip("Dry run: writes the layout and prints the plan in the "
                                     "Log. Nothing is sent to the instrument.")
        self.mk_check_btn.clicked.connect(self._on_check_markup)
        r.addWidget(self.mk_check_btn)
        self.mk_edit_btn = QPushButton("Edit in ELYMaker")
        self.mk_edit_btn.setToolTip("Open the layout over the FIB overview (targets drawn "
                                    "in) to move, add or delete markups; Save there. The "
                                    "edit is milled as saved.")
        self.mk_edit_btn.clicked.connect(self._on_edit_markup)
        r.addWidget(self.mk_edit_btn)
        self.mk_reset_btn = QPushButton("Reset markup")
        self.mk_reset_btn.setToolTip("Regenerate the layout from the pattern and the ticked "
                                     "targets (discards an ELYMaker edit).")
        self.mk_reset_btn.clicked.connect(self._on_reset_markup)
        r.addWidget(self.mk_reset_btn)
        r.addStretch(1)
        l4.addLayout(r)
        r = QHBoxLayout()
        self.mk_mill_btn = QPushButton("Mill markup")
        self.mk_mill_btn.setObjectName("toolButton")
        self.mk_mill_btn.setToolTip("ARM on: checks the stage is still at the overview "
                                    "position, zeroes the beam shift, sets the overview "
                                    "magnification, mills the layout, grabs one after image. "
                                    "Uses the Rough Mill tab's Confirm done value.")
        self.mk_mill_btn.clicked.connect(self._on_mill_markup)
        r.addWidget(self.mk_mill_btn)
        r.addStretch(1)
        l4.addLayout(r)
        self.markup_status = QLabel("")
        self.markup_status.setWordWrap(True)
        l4.addWidget(self.markup_status)
        visit = QLabel("Visiting targets (go to, site image, centre) comes later, after the "
                       "overview magnification's stage scale is measured.")
        visit.setWordWrap(True)
        visit.setStyleSheet("color:#9aa3b2;")
        l4.addWidget(visit)
        lay.addWidget(c4)
        scroll.setWidget(left)
        split.addWidget(scroll)
        self.left_pane = scroll
        self._split = split

        # ---------------------------------------------------------------- right
        self.register = RegisterPane(self)
        split.addWidget(self.register)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([900, 600])          # the Register pane needs room from the start

        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._poll)

    # ------------------------------------------------------------------ helpers
    def give_pymap_room(self) -> None:
        """pyMap shows three images and its controls: give it ~55 % once embedded."""
        total = max(self._split.width(), 1400)
        self._split.setSizes([int(total * 0.45), int(total * 0.55)])

    def session_dir(self) -> Optional[Path]:
        pf = self._win.positions_file()
        return Path(pf).parent if pf else None

    def clem_dir(self) -> Optional[Path]:
        s = self.session_dir()
        if s is None:
            return None
        from ..clem import clem_dir
        return clem_dir(s)

    def on_show(self) -> None:
        self.refresh_overviews()
        self.refresh_targets()
        self.register.ensure_built(self.session_dir(), self._win.positions_file())
        self._timer.start()

    def hideEvent(self, ev):  # noqa: N802 (Qt)
        self._timer.stop()
        super().hideEvent(ev)

    def reload(self) -> None:
        """New session: forget the old one's files (and rebuild pyMap for it if built)."""
        self._sig = None
        self._mark_sel = {}
        self.refresh_overviews()
        self.refresh_targets()
        if self.register._widget is not None:
            self.register.ensure_built(self.session_dir())

    # ------------------------------------------------------------------ actions
    def _armed(self) -> bool:
        return bool(self._win.is_armed())

    def _on_goto_grid(self) -> None:
        from ..clem import GRID_FIXTURES
        name = GRID_FIXTURES[self.grid_combo.currentText()]
        if self._armed() and not self._win.confirm(
                f"Move the stage to {name} (T 30°, R 12°) via CLEM SAFE?\n\nZ goes to the "
                f"CLEM SAFE height first, then X/Y/T/R; arrival is confirmed at each."):
            self._win.status("Cancelled; nothing moved.")
            return
        self._win.run_module("smartfib_executor.setup_move",
                             [name] + (["--arm"] if self._armed() else []))

    def _on_coincidence(self) -> None:
        args = ["--dry-run"] + ([] if self._armed() else ["--simulate"])
        self._win.run_module("smartfib_executor.coincidence", args)

    def capture_argv(self, kind: str):
        pf = self._win.positions_file()
        if not pf:
            self._win.status("Set a positions file (session) first.")
            return None
        mag = (self.fib_mag if kind == "fib" else self.sem_mag).value()
        args = ["clem-overview", "--kind", kind, "--grid", self.grid_combo.currentText(),
                "--mag", f"{mag:g}", "--positions-file", pf]
        if self._armed():
            args.append("--arm")
        return args

    def _on_capture(self, kind: str) -> None:
        args = self.capture_argv(kind)
        if args is None:
            return
        grid = self.grid_combo.currentText()
        if self._armed():
            if kind == "fib":
                q = (f"Capture a FIB overview of {grid} at {self.fib_mag.value():g}x?\n\nOne "
                     f"FIB frame over the whole field (ion dose on every target). The FIB "
                     f"beam shift is zeroed first. The stage does not move.")
            else:
                q = (f"Capture an SEM overview of {grid} at {self.sem_mag.value():g}x?\n\n"
                     f"Switches to SEM imaging and grabs one frame. Detector, scan speed, "
                     f"brightness and contrast stay as set. The stage does not move.")
            if not self._win.confirm(q):
                self._win.status("Cancelled; nothing changed.")
                return
        self._win.run_cli(args, on_done=lambda rc: self.refresh_overviews(),
                          show_log=False)

    def _on_check(self) -> None:
        pf = self._win.positions_file()
        path = self._targets_path()
        if not pf or path is None:
            self._win.status("No pyMap targets file in this session's clem folder.")
            return
        args = ["clem-targets", str(path), "--positions-file", pf]
        if self.accept_qc.isChecked():
            args.append("--accept-qc")
        self._win.run_cli(args, on_done=lambda rc: self.refresh_targets(), show_log=False)

    # ------------------------------------------------------------------ views
    def refresh_overviews(self) -> None:
        c = self.clem_dir()
        if c is None:
            self.ov_label.setText("Set a positions file (session) first.")
            return
        parts = []
        try:
            from ..clem import OVERVIEW_POSITIONS
            from ..positions import PositionDatabase
            pf = c / OVERVIEW_POSITIONS
            if pf.exists():
                for rec in PositionDatabase.load(pf).positions.values():
                    parts.append(f"FIB {rec.name} ({rec.fib_magnification_x:g}x)")
        except Exception as e:  # noqa: BLE001
            parts.append(f"FIB overviews unreadable: {e}")
        try:
            from ..clem.overview import load_sem_overviews
            for e in load_sem_overviews(self.session_dir())["overviews"]:
                parts.append(f"SEM {e.get('name')} ({float(e.get('sem_magnification_x') or 0):g}x)")
        except Exception as e:  # noqa: BLE001
            parts.append(f"SEM overviews unreadable: {e}")
        self.ov_label.setText(" · ".join(parts) if parts else "No overviews yet.")

    def _targets_path(self) -> Optional[Path]:
        c = self.clem_dir()
        if c is None:
            return None
        from ..clem.targets import find_targets_file
        return find_targets_file(c)

    def _signature(self):
        p = self._targets_path()
        if p is None:
            return None
        from ..clem.targets import ready_marker_path
        m = ready_marker_path(p)
        return (str(p), p.stat().st_mtime, m.stat().st_mtime if m.exists() else None)

    def _poll(self) -> None:
        # v0.55.0: runs every 3 s from a timer -- an error here is logged to the Log tab
        # only when its text changes (review: a share that keeps failing would otherwise
        # write a line every 3 s and push the command output out of the Log tab), never
        # raised into a warning box that would come back every 3 s.
        try:
            sig = self._signature()
            if sig != self._sig:
                self.refresh_targets()
            self._poll_error = None
        except Exception as e:  # noqa: BLE001
            text = f"{type(e).__name__}: {e}"
            if text != getattr(self, "_poll_error", None):
                self._poll_error = text
                try:
                    self._win.log_page.append(f"[correlation] targets refresh failed: {text}"
                                              f" (logged once until it changes)")
                except Exception:  # noqa: BLE001
                    pass

    def refresh_targets(self) -> None:
        self.table.setRowCount(0)
        path = self._targets_path()
        try:
            self._sig = self._signature()
        except Exception:  # noqa: BLE001
            self._sig = None
        if path is None:
            self.plan = self.mplan = None
            self.file_label.setText("No pyMap targets file in this session's clem folder yet.")
            self.summary.setText("")
            self.markup_status.setText("")
            return
        from ..cli import _project_root
        from ..clem.targets import plan_targets
        s = self.session_dir()
        try:
            self.plan = plan = plan_targets(path, accept_qc=self.accept_qc.isChecked(),
                                            cal_dirs=[path.parent, s, _project_root()])
        except Exception as e:  # noqa: BLE001
            # v0.55.0: e.g. a targets file read while pyMap is still writing it, or a
            # malformed file -- shown here (nothing can be marked), not an error box.
            self.plan = self.mplan = None
            import html
            self.file_label.setText(f"<b>{html.escape(path.name)}</b> — could not be read: "
                                    f"{html.escape(f'{type(e).__name__}: {e}')}")
            self.summary.setText("")
            self.markup_status.setText("")
            return
        c = plan.counts()
        import html
        self.file_label.setText(
            f"<b>{html.escape(path.name)}</b> — {c['ok']} ok, {c['warn']} with warnings, "
            f"{c['refused']} refused" + ("" if plan.ok else " · <b>file refused</b>"))
        bits = []
        if plan.overview is not None:
            bits.append(f"overview {plan.overview.name} ({plan.overview.fib_magnification_x:g}x)")
        if plan.qc_um is not None:
            bits.append(f"registration {plan.qc_um:.2f} µm ({plan.qc_kind})"
                        + (" · overridden" if plan.qc_override else ""))
        bits.append(f"scale: {plan.scale.get('text')}")
        self.summary.setText(" · ".join(bits) + "".join(f"\n✕ {e}" for e in plan.errors)
                             + "".join(f"\n! {w}" for w in plan.warnings))
        # v0.55.0 (pyMap review, request 2): try/finally -- an error part-way through used
        # to leave _filling True, after which the Mark ticks silently stopped working.
        self._filling = True
        try:
            self.table.setRowCount(len(plan.rows))
            for i, row in enumerate(plan.rows):
                def cell(col, text, tip=None):
                    it = QTableWidgetItem(text)
                    if tip:
                        it.setToolTip(tip)
                    self.table.setItem(i, col, it)
                mk = QTableWidgetItem("")
                if row.status == "refused":
                    mk.setFlags(Qt.ItemIsEnabled)
                    mk.setToolTip("refused targets cannot be marked")
                else:
                    mk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
                    on = self._mark_sel.setdefault(row.id, row.status == "ok")
                    mk.setCheckState(Qt.Checked if on else Qt.Unchecked)
                self.table.setItem(i, self.T_MARK, mk)
                cell(self.T_LABEL, row.label)
                cell(self.T_ID, row.id)
                cell(self.T_STATUS, {"ok": "OK", "warn": "Warning",
                                     "refused": "Refused"}.get(row.status, str(row.status)))
                if row.stage_x_m is not None:
                    cell(self.T_X, f"{row.stage_x_m * 1e3:.4f}")
                    cell(self.T_Y, f"{row.stage_y_m * 1e3:.4f}")
                    cell(self.T_DX, f"{row.stage_dx_m * 1e6:+.1f}")
                    cell(self.T_DY, f"{row.stage_dy_m * 1e6:+.1f}")
                    cell(self.T_OFF, f"{row.offset_um:.0f}")
                notes = "; ".join(row.reasons + row.warnings + ([row.note] if row.note else []))
                cell(self.T_NOTES, notes, notes)
        finally:
            self._filling = False
        self.refresh_markup()

    # ------------------------------------------------------------------ markup (v0.53.0)
    def selected_ids(self):
        if self.plan is None:
            return []
        return [r.id for r in self.plan.rows
                if r.status != "refused" and self._mark_sel.get(r.id)]

    def _on_item_changed(self, it) -> None:
        if self._filling or it.column() != self.T_MARK or self.plan is None:
            return
        rid = self.table.item(it.row(), self.T_ID)
        if rid is not None:
            self._mark_sel[rid.text()] = it.checkState() == Qt.Checked
            self.refresh_markup()

    def _set_marks(self, on: bool) -> None:
        if self.plan is None:
            return
        for r in self.plan.rows:
            self._mark_sel[r.id] = bool(on) and r.status == "ok"
        self.refresh_targets()

    def _browse_markup(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Markup pattern", self.markup_edit.text(),
                                              "SmartFIB layout (*.ely)")
        if path:
            self.markup_edit.setText(path)
            self.refresh_markup()

    def markup_plan(self, *, write: bool = False, reset: bool = False):
        """The markup plan for the ticked targets (files only)."""
        if self.plan is None or self.session_dir() is None:
            return None
        from ..clem.markup import MarkupError, plan_markup, prepare_file
        try:
            mp = plan_markup(self.plan, base=self.markup_edit.text().strip() or None,
                             selected=self.selected_ids())
            return prepare_file(mp, self.session_dir(), reset=reset, write=write)
        except (MarkupError, OSError) as e:
            self.markup_status.setText(f"✕ {e}")
            return None

    def refresh_markup(self) -> None:
        mp = self.mplan = self.markup_plan(write=False)
        if mp is None:
            if self.plan is None:
                self.markup_status.setText("")
            return
        from ..clem.markup import load_markups_log
        milled = {}
        for run in load_markups_log(self.session_dir()):
            if run.get("status") != "milled" or \
                    (run.get("overview") or {}).get("position") != getattr(mp.overview, "name", None):
                continue
            for t in run.get("targets") or []:
                milled[str(t.get("id"))] = str(run.get("finished") or run.get("started"))[11:16]
        edited = mp.file_state == "edited"
        in_file = set(mp.milled_ids())
        self._filling = True
        try:                         # v0.55.0: never leave _filling stuck True
            for i, r in enumerate(mp.rows):
                if edited:
                    txt = "in edited layout" if r.id in in_file else ""
                elif r.status == "marked":
                    txt = "will mark"
                elif r.status == "refused":
                    txt = "refused: " + "; ".join(r.reasons)
                else:
                    txt = ""
                if r.id in milled:
                    txt = f"milled {milled[r.id]}" + (f" · {txt}" if txt else "")
                it = QTableWidgetItem(txt)
                it.setToolTip(txt)
                self.table.setItem(i, self.T_MARKUP, it)
                mk = self.table.item(i, self.T_MARK)
                if mk is not None and mk.flags() & Qt.ItemIsUserCheckable:
                    mk.setFlags((mk.flags() | Qt.ItemIsEnabled) if not edited
                                else (mk.flags() & ~Qt.ItemIsEnabled))
        finally:
            self._filling = False
        n = len(mp.milled_ids()) if mp.ok else 0
        if mp.ok:
            state = {"generated": "", "edited": " · edited in ELYMaker: milled as saved (Reset "
                                                 "markup to use the ticks again)"}.get(
                mp.file_state, "")
            text = (f"{n} markup(s) on {mp.overview.name} at {mp.overview.fib_magnification_x:g}x"
                    + (f" · probe {', '.join(mp.probes)}" if mp.probes else "") + state)
        else:
            text = "✕ " + "; ".join(mp.errors)
        text += "".join(f"\n! {w}" for w in mp.warnings)
        self.markup_status.setText(text)

    def markup_argv(self, mp):
        pf = self._win.positions_file()
        if not pf or mp is None or self.plan is None:
            return None
        ids = mp.milled_ids() if mp.file_state == "edited" else self.selected_ids()
        sel = ["--targets", ",".join(ids)] if ids else ["--all"]   # edited: milled as saved
        try:
            confirm_s = self._win.mill_page.done_confirm_spin.value()
        except Exception:  # noqa: BLE001
            confirm_s = 20
        args = ["clem-markup", str(self.plan.targets_file), "--positions-file", pf,
                *sel, "--markup", self.markup_edit.text().strip(),
                "--wait", "auto", "--done-confirm-s", str(confirm_s)]
        if mp.file_sha256:
            args += ["--expect-sha256", mp.file_sha256]
        if self.accept_qc.isChecked():
            args.append("--accept-qc")
        if self._armed():
            args.append("--arm")
        return args

    def _set_markup_busy(self, busy: bool) -> None:
        for b in (self.mk_check_btn, self.mk_edit_btn, self.mk_reset_btn, self.mk_mill_btn):
            b.setEnabled(not busy)

    def _markup_done(self, rc) -> None:
        self._set_markup_busy(False)
        self.refresh_markup()

    def _on_check_markup(self) -> None:
        mp = self.markup_plan(write=True)
        args = self.markup_argv(mp)
        if args is None:
            self._win.status("Load the targets and tick at least one first.")
            return
        args = [a for a in args if a != "--arm"]
        self._win.run_cli(args, on_done=lambda rc: self.refresh_markup(), show_log=False)

    def _on_edit_markup(self) -> None:
        mp = self.markup_plan(write=True)
        if mp is None or not mp.ok or not mp.file_path:
            self.refresh_markup()
            self._win.status("Nothing to edit: " + ("; ".join(mp.errors) if mp else
                                                     "no targets loaded"))
            return
        from ..clem.markup import annotate_overview
        img = (annotate_overview(mp, Path(mp.file_path).with_name(
            f"{mp.overview.name}_targets.png")) or mp.overview_image)
        if not img:
            self._win.status("The overview image is missing.")
            return
        self._win.elymaker_page.open_handoff(
            image_path=str(img), pixel_size_um=float(mp.pixel_size_um),
            out_path=str(mp.file_path), load_path=str(mp.file_path),
            fov_width_um=float(mp.field_um[0]), site=f"{mp.overview.name} markup")
        self._win.show_page("elymaker")
        self._win.status("ELYMaker: the markup layout over the overview (targets drawn in). "
                         "Save there, then come back to Correlation.")

    def _on_reset_markup(self) -> None:
        mp = self.markup_plan(write=False)
        if mp is not None and mp.file_state in ("edited", "refused") and not self._win.confirm(
                "Discard the markup edits made in ELYMaker and regenerate the layout from "
                "the pattern and the ticked targets?"):
            return
        self.markup_plan(write=True, reset=True)
        self.refresh_markup()

    def _on_mill_markup(self) -> None:
        mp = self.markup_plan(write=True)
        self.refresh_markup()
        if mp is None or not mp.ok:
            self._win.status("Markup not milled: " + ("; ".join(mp.errors) if mp else
                                                       "no targets loaded"))
            return
        args = self.markup_argv(mp)
        if args is None:
            return
        if self._armed():
            labels = ", ".join(r.label for r in mp.rows if r.id in set(mp.milled_ids()))
            mag = mp.overview.fib_magnification_x
            extra = (f" plus {mp.extra_shapes} other shape(s)" if mp.extra_shapes else "")
            if not self._win.confirm(
                    f"Mill the markup on {len(mp.milled_ids())} target(s) ({labels}){extra} "
                    f"at {mag:g}x?\n\nOne SmartFIB layout: {Path(mp.file_path).name} "
                    f"({'edited in ELYMaker, milled as saved' if mp.file_state == 'edited' else 'generated'})"
                    + (f", probe {', '.join(mp.probes)}" if mp.probes else "") + ".\n"
                    f"The stage stays at {mp.overview.name}: refused if it moved since the "
                    f"overview. FIB beam shift zeroed, {mag:g}x, objective as at the overview; "
                    f"then one after image at {mag:g}x."):
                self._win.status("Cancelled; nothing milled.")
                return
            self._set_markup_busy(True)
        if not self._win.run_cli(args, on_done=self._markup_done, show_log=False):
            self._set_markup_busy(False)
