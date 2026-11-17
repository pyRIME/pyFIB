"""Pseudocolor LUTs and overlay compositing.

A single-channel confocal is pseudocolored (green/magenta/etc.) and alpha-blended
over the grayscale FIB image. RGB confocals are used as-is. The FIB image is only
used as the background - it is never modified.
"""

from __future__ import annotations

from typing import Tuple
import numpy as np

from .reader import to_gray


def _stretch(gray: np.ndarray, contrast: Tuple[float, float] = (0.0, 1.0)) -> np.ndarray:
    lo, hi = contrast
    if hi <= lo:
        return np.clip(gray, 0.0, 1.0)
    return np.clip((gray - lo) / (hi - lo), 0.0, 1.0)


def _lut_gray(g):    return np.stack([g, g, g], axis=-1)
def _lut_green(g):   return np.stack([np.zeros_like(g), g, np.zeros_like(g)], axis=-1)
def _lut_red(g):     return np.stack([g, np.zeros_like(g), np.zeros_like(g)], axis=-1)
def _lut_blue(g):    return np.stack([np.zeros_like(g), np.zeros_like(g), g], axis=-1)
def _lut_cyan(g):    return np.stack([np.zeros_like(g), g, g], axis=-1)
def _lut_magenta(g): return np.stack([g, np.zeros_like(g), g], axis=-1)
def _lut_yellow(g):  return np.stack([g, g, np.zeros_like(g)], axis=-1)


def _lut_hot(g):
    r = np.clip(g / 0.365, 0, 1)
    gg = np.clip((g - 0.365) / (0.746 - 0.365), 0, 1)
    b = np.clip((g - 0.746) / (1.0 - 0.746), 0, 1)
    return np.stack([r, gg, b], axis=-1)


COLORMAPS = {
    "gray": _lut_gray,
    "green": _lut_green,
    "red": _lut_red,
    "blue": _lut_blue,
    "cyan": _lut_cyan,
    "magenta": _lut_magenta,
    "yellow": _lut_yellow,
    "hot": _lut_hot,
}


def pseudocolor(data: np.ndarray, cmap: str = "green",
                contrast: Tuple[float, float] = (0.0, 1.0)) -> np.ndarray:
    """Map an image to an RGB float array in [0,1].

    Named LUTs (green/magenta/...) always collapse the input to luminance first,
    so they recolor RGB confocals (e.g. single-channel fluorescence stored as
    R=G=B) the same as true grayscale. Use cmap="rgb" for true-color passthrough
    of a genuinely multi-color (merged-channel) confocal.
    """
    if cmap in ("rgb", "raw"):
        if data.ndim == 3 and data.shape[2] >= 3:
            return np.clip(data[..., :3].astype(np.float32), 0.0, 1.0)
        g = _stretch(to_gray(data), contrast)
        return _lut_gray(g).astype(np.float32)
    if cmap not in COLORMAPS:
        raise ValueError(f"unknown colormap {cmap!r}; "
                         f"choices: {sorted(COLORMAPS) + ['rgb']}")
    g = _stretch(to_gray(data), contrast)
    return COLORMAPS[cmap](g).astype(np.float32)


def blend_over(fixed: np.ndarray,
               moving_rgb: np.ndarray,
               alpha: np.ndarray,
               opacity: float = 0.5) -> np.ndarray:
    """Alpha-blend moving_rgb over the fixed image.

    fixed     : (H,W) or (H,W,3) FIB background (unmodified).
    moving_rgb: (H,W,3) pseudocolored confocal in fixed grid.
    alpha     : (H,W) coverage mask in [0,1].
    opacity   : global multiplier on the moving layer.
    """
    bg = fixed if (fixed.ndim == 3 and fixed.shape[2] == 3) \
        else np.stack([fixed] * 3, axis=-1)
    bg = np.clip(bg.astype(np.float32), 0.0, 1.0)
    a = (np.clip(alpha, 0.0, 1.0) * float(opacity))[..., None]
    out = bg * (1.0 - a) + np.clip(moving_rgb, 0.0, 1.0) * a
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def to_uint8(rgb: np.ndarray) -> np.ndarray:
    return (np.clip(rgb, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


_DIVIDER = (0.09, 0.76, 0.70)   # teal seam, matches the ecosystem accent


def _full_confocal(fib_rgb, conf_rgb, alpha):
    """Confocal at full strength where covered, FIB elsewhere."""
    a = np.clip(alpha, 0.0, 1.0)[..., None]
    return np.clip(fib_rgb * (1.0 - a) + conf_rgb * a, 0.0, 1.0)


def compare_swipe(fib_rgb, conf_rgb, alpha, split_frac=0.5, divider=True):
    """FIB left of the split, confocal(over FIB) right of it. For edge alignment."""
    h, w, _ = fib_rgb.shape
    blended = _full_confocal(fib_rgb, conf_rgb, alpha)
    split_x = int(round(min(max(split_frac, 0.0), 1.0) * (w - 1)))
    out = np.array(fib_rgb, dtype=np.float32, copy=True)
    out[:, split_x:] = blended[:, split_x:]
    if divider and 0 <= split_x < w:
        out[:, split_x] = _DIVIDER
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def compare_checkerboard(fib_rgb, conf_rgb, alpha, tile=32):
    """Alternating tiles of FIB and confocal(over FIB). Classic registration QC."""
    h, w, _ = fib_rgb.shape
    tile = max(int(tile), 1)
    blended = _full_confocal(fib_rgb, conf_rgb, alpha)
    yy, xx = np.mgrid[0:h, 0:w]
    fib_tile = (((xx // tile) + (yy // tile)) % 2) == 0
    out = np.where(fib_tile[..., None], fib_rgb, blended)
    return np.clip(out, 0.0, 1.0).astype(np.float32)
