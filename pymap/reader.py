"""Image loading for pyMap.

Loads the FIB bitmap (fixed) and the confocal 2D projection (moving) as float
arrays in [0, 1]. Handles 8-bit and 16-bit, grayscale and RGB. 16-bit data is
normalised by its dtype maximum (65535) so the absolute range is preserved;
per-image contrast stretching is a display concern handled in composite.py.
"""

from __future__ import annotations

from dataclasses import dataclass
import numpy as np
from PIL import Image


@dataclass
class LoadedImage:
    data: np.ndarray     # float32 in [0,1], shape (H,W) or (H,W,3)
    is_rgb: bool
    bit_depth: int       # 8 or 16
    path: str = ""

    @property
    def shape_hw(self):
        return self.data.shape[0], self.data.shape[1]


def load_image(path: str) -> LoadedImage:
    img = Image.open(path)
    mode = img.mode

    if mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        arr = np.asarray(img, dtype=np.float32)
        bad = ~np.isfinite(arr)
        if bad.any():                    # float TIFF with NaN/inf: as the confocal importer
            top = float(arr[~bad].max()) if (~bad).any() else 0.0
            arr = np.nan_to_num(arr, nan=0.0, posinf=top, neginf=0.0)
        bit_depth = 16
        denom = 65535.0 if arr.max() > 255 else 255.0
        data = np.clip(arr / denom, 0.0, 1.0)
        return LoadedImage(data=data, is_rgb=False, bit_depth=bit_depth, path=path)

    if mode in ("RGB", "RGBA", "P"):
        rgb = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
        return LoadedImage(data=rgb, is_rgb=True, bit_depth=8, path=path)

    # L, LA, 1, or anything else -> 8-bit grayscale
    gray = np.asarray(img.convert("L"), dtype=np.float32) / 255.0
    return LoadedImage(data=gray, is_rgb=False, bit_depth=8, path=path)


def to_gray(data: np.ndarray) -> np.ndarray:
    """Collapse an (H,W,3) array to grayscale using Rec.601 luma; passthrough (H,W)."""
    if data.ndim == 2:
        return data.astype(np.float32, copy=False)
    if data.ndim == 3 and data.shape[2] >= 3:
        r, g, b = data[..., 0], data[..., 1], data[..., 2]
        return (0.299 * r + 0.587 * g + 0.114 * b).astype(np.float32)
    raise ValueError(f"Unexpected image shape {data.shape}")
