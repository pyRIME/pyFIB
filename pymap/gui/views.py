"""Graphics views: a zoom/pan base, and a landmark/target picking view.

Scene coordinates equal image pixel coordinates, so all point coordinates here
are in image pixels. Markers keep a constant on-screen size (ItemIgnoresTransforms).

LandmarkView carries two marker layers: registration landmarks ("lm", cyan/gold)
and targets ("tgt", magenta, "T"-prefixed). read_only=True (used for the overlay
pane) disables adding/dragging but still lets targets be clicked/selected.
"""

from __future__ import annotations

from typing import List, Dict, Optional
import numpy as np

from qtpy.QtWidgets import (QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
                            QGraphicsEllipseItem, QGraphicsSimpleTextItem,
                            QGraphicsLineItem)
from qtpy.QtCore import Qt, Signal, QRectF, QPointF
from qtpy.QtGui import QPixmap, QPen, QBrush, QColor, QPainter

from .imageconv import ndarray_to_qpixmap
from .style import CANVAS_BG
from .. import diag

_MARK_R = 6.0
_DRAG_THRESH = 4
ZOOM_MIN = 0.01     # screen px per image px (v0.9.2: unlimited zoom made the view
ZOOM_MAX = 100.0    # transform non-invertible, so clicks landed at the wrong place)

_COL_LM = QColor(45, 200, 255)
_COL_LM_ACTIVE = QColor(255, 210, 60)
_COL_TGT = QColor(255, 92, 240)
_COL_TGT_ACTIVE = QColor(255, 150, 245)


class ZoomPanView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixitem: Optional[QGraphicsPixmapItem] = None
        self.setRenderHint(QPainter.SmoothPixmapTransform, False)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setBackgroundBrush(QBrush(QColor(CANVAS_BG)))
        self._pan = False
        self._pan_last = QPointF()
        self._owner_ref = None
        self.cache_images = False
        self._pm_cache = []

    @property
    def error_owner(self):
        """The window that shows this view's errors (held weakly)."""
        return self._owner_ref() if self._owner_ref is not None else None

    def set_error_owner(self, owner):
        import weakref
        self._owner_ref = weakref.ref(owner) if owner is not None else None

    def set_image(self, arr: np.ndarray):
        # cache_images (the overlay view): the last two images are kept with their
        # pixmaps, so flicker's two cached frames cost no conversion. Other views keep
        # nothing (holding the previous confocal array cost +234 MB at 3000x3000).
        pm = None
        cache = self._pm_cache if self.cache_images else None
        if cache is not None:
            pm = next((p for a, p in cache if a is arr), None)
        if pm is None:
            pm = ndarray_to_qpixmap(arr)
            if cache is not None:
                cache.append((arr, pm))
                del cache[:-2]
        first = self._pixitem is None
        if self._pixitem is None:
            self._pixitem = self._scene.addPixmap(pm)
            self._pixitem.setZValue(-100)
        else:
            self._pixitem.setPixmap(pm)
        self._scene.setSceneRect(QRectF(pm.rect()))
        if first:
            self.fit()

    def image_size(self):
        if self._pixitem is None:
            return (0, 0)
        r = self._pixitem.pixmap().rect()
        return (r.width(), r.height())

    def fit(self):
        if self._pixitem is not None:
            self.fitInView(self._pixitem, Qt.KeepAspectRatio)

    @diag.guard_event("wheelEvent")
    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120.0
        if not steps:
            return
        factor = 1.25 ** max(min(steps, 10.0), -10.0)
        current = abs(self.transform().m11()) or 1.0
        factor = min(max(current * factor, ZOOM_MIN), ZOOM_MAX) / current
        if abs(factor - 1.0) > 1e-9:
            self.scale(factor, factor)

    @diag.guard_event("mousePressEvent")
    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._pan = True
            self._pan_last = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    @diag.guard_event("mouseMoveEvent")
    def mouseMoveEvent(self, event):
        if self._pan:
            delta = event.pos() - self._pan_last
            self._pan_last = event.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    @diag.guard_event("mouseReleaseEvent")
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._pan:
            self._pan = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _Marker(QGraphicsEllipseItem):
    def __init__(self, index: int, kind: str):
        super().__init__(-_MARK_R, -_MARK_R, 2 * _MARK_R, 2 * _MARK_R)
        self.index = index
        self.kind = kind
        self.setFlag(QGraphicsEllipseItem.ItemIgnoresTransformations, True)
        self.setZValue(10)
        self.setData(0, kind)
        self.setData(1, index)
        label = ("T%d" % (index + 1)) if kind == "tgt" else str(index + 1)
        self._label = QGraphicsSimpleTextItem(label, self)
        self._label.setBrush(QBrush(QColor(255, 255, 255)))
        self._label.setPos(_MARK_R + 1, -_MARK_R - 2)
        self.set_active(False)

    def set_active(self, active: bool):
        if self.kind == "tgt":
            color = _COL_TGT_ACTIVE if active else _COL_TGT
        else:
            color = _COL_LM_ACTIVE if active else _COL_LM
        pen = QPen(color)
        pen.setWidthF(2.0)
        pen.setCosmetic(True)
        self.setPen(pen)
        fill = QColor(color)
        fill.setAlpha(70)
        self.setBrush(QBrush(fill))


class LandmarkView(ZoomPanView):
    pointAdded = Signal(float, float)       # empty-click add (landmark or target)
    pointMoved = Signal(int, float, float)  # landmark drag (live)
    pointSelected = Signal(int)             # landmark clicked
    dragFinished = Signal()                 # landmark drag released
    targetClicked = Signal(int)             # target marker clicked
    cursorMoved = Signal(float, float)      # hover position (image px)

    def __init__(self, parent=None, read_only: bool = False):
        super().__init__(parent)
        self._read_only = read_only
        self._markers: Dict[int, _Marker] = {}
        self._tmarkers: Dict[int, _Marker] = {}
        self._residual_items: list = []
        self._drag_index: Optional[int] = None
        self._press_pos = None
        self._press_hit = (None, None)   # (kind, index)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    # ---- marker sync -------------------------------------------------------
    def _sync(self, store: Dict[int, _Marker], markers: List[dict],
              kind: str, active_index):
        want = {m["index"] for m in markers}
        for idx in list(store):
            if idx not in want:
                self._scene.removeItem(store.pop(idx))
        for m in markers:
            idx = m["index"]
            mk = store.get(idx)
            if mk is None:
                mk = _Marker(idx, kind)
                self._scene.addItem(mk)
                store[idx] = mk
            mk.setPos(QPointF(m["x"], m["y"]))
            mk.set_active(idx == active_index)

    def set_markers(self, markers: List[dict], active_index: Optional[int]):
        self._sync(self._markers, markers, "lm", active_index)

    def set_target_markers(self, markers: List[dict], active_index: Optional[int]):
        self._sync(self._tmarkers, markers, "tgt", active_index)

    def set_residuals(self, vectors: List[dict], scale: float = 1.0):
        """Draw arrows from each fixed landmark to its scaled mapped-moving point.
        vectors: [{'fx','fy','mx','my'}]. Length is exaggerated by `scale`."""
        for it in self._residual_items:
            self._scene.removeItem(it)
        self._residual_items = []
        pen = QPen(QColor(255, 210, 60))
        pen.setWidthF(1.6)
        pen.setCosmetic(True)
        for v in vectors:
            fx, fy = v["fx"], v["fy"]
            dx = (v["mx"] - fx) * scale
            dy = (v["my"] - fy) * scale
            tipx, tipy = fx + dx, fy + dy
            line = QGraphicsLineItem(fx, fy, tipx, tipy)
            line.setPen(pen)
            line.setZValue(20)
            self._scene.addItem(line)
            self._residual_items.append(line)
            # origin dot (constant screen size) so zero-length residuals still show
            dot = QGraphicsEllipseItem(-2.5, -2.5, 5, 5)
            dot.setFlag(QGraphicsEllipseItem.ItemIgnoresTransformations, True)
            dot.setPen(pen)
            dot.setBrush(QBrush(QColor(255, 210, 60)))
            dot.setPos(QPointF(fx, fy))
            dot.setZValue(21)
            self._scene.addItem(dot)
            self._residual_items.append(dot)

    # ---- interaction -------------------------------------------------------
    def _hit(self, pos):
        item = self.itemAt(pos)
        while item is not None:
            d = item.data(0)
            if d in ("lm", "tgt"):
                return d, int(item.data(1))
            item = item.parentItem()
        return None, None

    def _scene_xy(self, pos):
        sp = self.mapToScene(pos)
        w, h = self.image_size()
        x = min(max(sp.x(), 0.0), max(w - 1, 0))
        y = min(max(sp.y(), 0.0), max(h - 1, 0))
        return x, y

    @diag.guard_event("mousePressEvent")
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.pos()
            self._press_hit = self._hit(event.pos())
            kind, idx = self._press_hit
            self._drag_index = idx if (kind == "lm" and not self._read_only) else None
            event.accept()
            return
        super().mousePressEvent(event)

    @diag.guard_event("mouseMoveEvent")
    def mouseMoveEvent(self, event):
        if self._drag_index is not None and (event.buttons() & Qt.LeftButton):
            x, y = self._scene_xy(event.pos())
            mk = self._markers.get(self._drag_index)
            if mk is None:                       # the point went away mid-drag
                self._drag_index = None
                return
            mk.setPos(QPointF(x, y))
            self.pointMoved.emit(self._drag_index, x, y)
            event.accept()
            return
        if not self._pan and event.buttons() == Qt.NoButton and self._pixitem is not None:
            x, y = self._scene_xy(event.pos())
            self.cursorMoved.emit(x, y)
        super().mouseMoveEvent(event)

    @diag.guard_event("mouseReleaseEvent")
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._press_pos is not None:
            moved = (event.pos() - self._press_pos).manhattanLength()
            kind, idx = self._press_hit
            if kind == "lm":
                if moved < _DRAG_THRESH:
                    self.pointSelected.emit(idx)
                elif not self._read_only:
                    self.dragFinished.emit()
            elif kind == "tgt":
                if moved < _DRAG_THRESH:
                    self.targetClicked.emit(idx)
            else:
                if moved < _DRAG_THRESH and not self._read_only and self._pixitem is not None:
                    x, y = self._scene_xy(event.pos())
                    self.pointAdded.emit(x, y)
            self._press_pos = None
            self._drag_index = None
            self._press_hit = (None, None)
            event.accept()
            return
        super().mouseReleaseEvent(event)
