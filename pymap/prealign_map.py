"""Map points through a PreAlign (rot90 + flips), in pure numpy-free logic.

Used by the GUI so that when the user rotates/flips the moving image *after*
placing landmarks, the moving landmarks stay pinned to the same image feature
(remap = forward(new, inverse(old, pt))).

Pixel convention matches warp.apply_prealign exactly: rot90 CCW (np.rot90) first,
then flip_h (reverse columns, x -> w-1-x), then flip_v (reverse rows, y -> h-1-y).
Coordinates are (x=col, y=row) integer/float pixel indices.
"""

from __future__ import annotations

from typing import Tuple
from .model import PreAlign


def _rot90_once(x, y, w, h):
    """One CCW rotation (np.rot90 k=1) on an image of size (w,h).

    (x,y) -> (y, w-1-x); new size becomes (w'=h, h'=w).
    """
    return y, (w - 1) - x, h, w


def prealign_forward(pt: Tuple[float, float], pa: PreAlign,
                     w: int, h: int) -> Tuple[float, float, int, int]:
    """Map an original-image point to pre-aligned space. Returns (x',y',w',h')."""
    x, y = pt
    cw, ch = w, h
    for _ in range(pa.rot90 % 4):
        x, y, cw, ch = _rot90_once(x, y, cw, ch)
    if pa.flip_h:
        x = (cw - 1) - x
    if pa.flip_v:
        y = (ch - 1) - y
    return x, y, cw, ch


def prealign_inverse(pt: Tuple[float, float], pa: PreAlign,
                     w: int, h: int) -> Tuple[float, float, int, int]:
    """Map a pre-aligned-space point back to the original image. Returns (x',y',w',h')."""
    k = pa.rot90 % 4
    rw, rh = (h, w) if (k % 2 == 1) else (w, h)   # dims after rotation
    x, y = pt
    if pa.flip_v:
        y = (rh - 1) - y
    if pa.flip_h:
        x = (rw - 1) - x
    cw, ch = rw, rh
    for _ in range((4 - k) % 4):
        x, y, cw, ch = _rot90_once(x, y, cw, ch)
    return x, y, cw, ch


def remap_point(pt: Tuple[float, float], old_pa: PreAlign, new_pa: PreAlign,
                w: int, h: int) -> Tuple[float, float]:
    """Move a point picked in old_pa's space into new_pa's space (same feature).

    ``pt`` is a CORNER-convention coordinate (pixel (0,0) spans [0,1)), as clicked in
    the Qt view. prealign_forward/inverse work on pixel indices (x -> w-1-x on a
    flip), so convert to indices and back; applying the index formulas directly to a
    clicked coordinate moved the point by one pixel on every flip or rotation."""
    ox, oy, _, _ = prealign_inverse((pt[0] - 0.5, pt[1] - 0.5), old_pa, w, h)
    nx, ny, _, _ = prealign_forward((ox, oy), new_pa, w, h)
    return nx + 0.5, ny + 0.5
