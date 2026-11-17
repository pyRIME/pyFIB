"""Convert float [0,1] numpy arrays to QImage/QPixmap (owning their buffers)."""

from __future__ import annotations

import numpy as np
from qtpy.QtGui import QImage, QPixmap


def ndarray_to_qimage(arr: np.ndarray) -> QImage:
    a = np.clip(np.asarray(arr, dtype=np.float32), 0.0, 1.0)
    if a.ndim == 2:
        buf = (a * 255.0 + 0.5).astype(np.uint8, copy=True)
        h, w = buf.shape
        qi = QImage(buf.data, w, h, w, QImage.Format_Grayscale8)
        return qi.copy()
    if a.ndim == 3 and a.shape[2] == 3:
        buf = np.ascontiguousarray((a * 255.0 + 0.5).astype(np.uint8))
        h, w, _ = buf.shape
        qi = QImage(buf.data, w, h, 3 * w, QImage.Format_RGB888)
        return qi.copy()
    if a.ndim == 3 and a.shape[2] == 4:
        buf = np.ascontiguousarray((a * 255.0 + 0.5).astype(np.uint8))
        h, w, _ = buf.shape
        qi = QImage(buf.data, w, h, 4 * w, QImage.Format_RGBA8888)
        return qi.copy()
    raise ValueError(f"cannot convert array of shape {a.shape} to QImage")


def ndarray_to_qpixmap(arr: np.ndarray) -> QPixmap:
    return QPixmap.fromImage(ndarray_to_qimage(arr))
