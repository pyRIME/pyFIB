"""Hosting the embedded tools (ELYMaker, pyMap) inside pyFIB (v0.53.2).

Evan, 2026-10-12: in ELYMaker, selecting a shape pushed the maximised pyFIB window past
the bottom of the screen (probe legend, Validation box and status bar hidden; Pan could
not reach them); Restore Down + Maximize fixed it until the next selection. Cause: the
embedded tool's right-hand dock column (Tools + Properties + New-shape defaults) cannot
scroll, so its minimum height is the sum of every button and field (~1100 px with
nothing selected, ~1600 px with a rectangle selected) and that minimum became pyFIB's.
Qt grows a window to a new minimum even when maximised and never shrinks it back.

Three things here, used by the ELYMaker tab and the Correlation tab's pyMap pane:

  contain(widget)        the embedded window's minimum size no longer propagates to
                         pyFIB (size policy Ignored): nothing inside a tool can resize
                         the pyFIB window again;
  scroll_docks(win, ...) wrap the named dock panels in scroll areas: when space is short
                         a panel scrolls instead of growing the window or overlapping
                         its neighbour (done from outside; the bundled tool is unchanged);
  Detachable             "Open in own window": moves the SAME widget (all its state)
                         into its own top-level window -- e.g. a second monitor,
                         maximised -- and back into the tab when that window is closed.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (QDockWidget, QFrame, QHBoxLayout, QLabel, QPushButton,
                            QScrollArea, QSizePolicy, QVBoxLayout, QWidget)


def contain(widget: QWidget) -> None:
    """The host layout ignores this widget's size hints (its minimum included)."""
    widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)


def scroll_docks(win, object_names: Iterable[str]) -> int:
    """Put the panel of each named QDockWidget of ``win`` in a scroll area (once).
    Returns how many were wrapped. The dock keeps the panel's natural width."""
    n = 0
    for d in win.findChildren(QDockWidget):
        if d.objectName() not in object_names:
            continue
        panel = d.widget()
        if panel is None or isinstance(panel, QScrollArea):
            continue
        sa = QScrollArea()
        sa.setObjectName(d.objectName() + "_scroll")
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        width = panel.sizeHint().width()
        d.setWidget(sa)
        sa.setWidget(panel)
        sa.setMinimumWidth(width + sa.verticalScrollBar().sizeHint().width())
        n += 1
    return n


def share_dock_column(win, upper: str, tabbed: Iterable[str], upper_frac: float = 0.5,
                      raise_on_selection=None) -> bool:
    """Give a tall dock column room: the ``tabbed`` docks share ONE slot as tabs (the
    first one raised) below the ``upper`` dock, which gets ``upper_frac`` of the height.
    ``raise_on_selection``: a signal; when it fires the first tabbed dock is raised
    (ELYMaker: selecting a shape shows Properties)."""
    docks = {d.objectName(): d for d in win.findChildren(QDockWidget)}
    names = [n for n in tabbed if n in docks]
    if upper not in docks or len(names) < 2:
        return False
    first = docks[names[0]]
    for n in names[1:]:
        win.tabifyDockWidget(first, docks[n])
    first.raise_()
    h = max(win.height(), 600)
    try:
        win.resizeDocks([docks[upper], first],
                        [int(h * upper_frac), int(h * (1 - upper_frac))], Qt.Vertical)
    except Exception:  # noqa: BLE001
        pass
    if raise_on_selection is not None:
        raise_on_selection.connect(lambda *_a: first.raise_())
    return True


class _ToolWindow(QWidget):
    """The top-level window a detached tool lives in; closing it docks the tool back."""

    def __init__(self, title: str, on_close: Callable[[], None]):
        super().__init__(None, Qt.Window)
        self.setWindowTitle(title)
        self._on_close = on_close
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.body = lay

    def closeEvent(self, ev):  # noqa: N802 (Qt)
        cb, self._on_close = self._on_close, None
        if cb is not None:
            cb()
        super().closeEvent(ev)


class Detachable:
    """Moves one embedded widget between a slot in a tab and its own window.

    ``slot_layout``: the layout the widget lives in inside the tab;
    ``placeholder``: shown in the tab while the widget is detached (it gets a "Bring
    back" button). The widget object is never rebuilt: everything in it survives."""

    def __init__(self, title: str, slot_layout, placeholder_text: str):
        self.title = title
        self._slot = slot_layout
        self.widget: Optional[QWidget] = None
        self.window: Optional[_ToolWindow] = None
        self._geometry = None
        self.placeholder = QWidget()
        pl = QVBoxLayout(self.placeholder)
        lab = QLabel(placeholder_text)
        lab.setWordWrap(True)
        lab.setAlignment(Qt.AlignCenter)
        lab.setStyleSheet("color:#9aa3b2;")
        row = QHBoxLayout()
        self.raise_btn = QPushButton("Show its window")
        self.raise_btn.clicked.connect(self.raise_window)
        self.back_btn = QPushButton("Bring back into this tab")
        self.back_btn.clicked.connect(self.reattach)
        row.addStretch(1)
        row.addWidget(self.raise_btn)
        row.addWidget(self.back_btn)
        row.addStretch(1)
        pl.addStretch(1)
        pl.addWidget(lab)
        pl.addLayout(row)
        pl.addStretch(1)
        self.placeholder.hide()
        self._slot.addWidget(self.placeholder, 1)
        self.listeners = []          # callables(detached: bool)

    @property
    def detached(self) -> bool:
        return self.window is not None

    def set_widget(self, w: Optional[QWidget]) -> None:
        """The widget now in the slot (after a build / rebuild); None when removed."""
        self.widget = w

    def detach(self) -> None:
        w = self.widget
        if w is None or self.window is not None:
            return
        self._slot.removeWidget(w)
        win = _ToolWindow(self.title, self.reattach)
        win.body.addWidget(w)
        w.show()
        self.window = win
        self.placeholder.show()
        if self._geometry is not None:
            win.restoreGeometry(self._geometry)
            win.show()
        else:
            scr = self.placeholder.screen() if hasattr(self.placeholder, "screen") else None
            if scr is not None:
                g = scr.availableGeometry()
                win.resize(int(g.width() * 0.9), int(g.height() * 0.9))
                win.move(g.x() + int(g.width() * 0.05), g.y() + int(g.height() * 0.05))
            win.show()
        win.raise_()
        win.activateWindow()
        for cb in self.listeners:
            cb(True)

    def raise_window(self) -> None:
        if self.window is not None:
            if self.window.isMinimized():
                self.window.showNormal()
            self.window.raise_()
            self.window.activateWindow()

    def reattach(self) -> None:
        win, self.window = self.window, None
        if win is None:
            return
        try:
            self._geometry = win.saveGeometry()
        except Exception:  # noqa: BLE001
            pass
        w = self.widget
        if w is not None:
            win.body.removeWidget(w)
            self._slot.addWidget(w, 1)
            w.show()
        self.placeholder.hide()
        win._on_close = None
        win.hide()
        win.deleteLater()
        for cb in self.listeners:
            cb(False)
