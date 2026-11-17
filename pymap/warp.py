"""Pre-align the moving image and warp it into the fixed (FIB) pixel grid.

The FIB image is never touched. warp_moving resamples the (pre-aligned) confocal
image into the FIB grid using the registration's inverse map, and returns an
(H,W) or (H,W,3) float array plus a coverage alpha (1 where the FIB pixel was
covered by the moving image, 0 outside it).
"""

from __future__ import annotations

from typing import Tuple
import numpy as np
from skimage.transform import warp as _sk_warp

from .model import PreAlign
from .transform import Registration


def apply_prealign(img: np.ndarray, prealign: PreAlign) -> np.ndarray:
    """Apply rot90 (CCW) then flips. Matches model.PreAlign semantics."""
    out = np.rot90(img, k=prealign.rot90)
    if prealign.flip_h:
        out = out[:, ::-1]
    if prealign.flip_v:
        out = out[::-1, :]
    return np.ascontiguousarray(out)


def warp_moving(moving_prealigned: np.ndarray,
                registration: Registration,
                out_shape_hw: Tuple[int, int],
                order: int = 1) -> Tuple[np.ndarray, np.ndarray]:
    """Warp moving -> fixed grid.

    Returns (warped, alpha) where warped has the same channel layout as
    moving_prealigned and alpha is (H,W) coverage in [0,1].
    """
    H, W = out_shape_hw
    # Landmarks (and so the registration) use the CORNER convention: pixel (0,0)
    # spans [0,1), the convention of the Qt scene, elymaker and pyFIB. skimage.warp
    # works in the CENTRE convention (pixel (0,0) centred on 0). Shift by half a pixel
    # on the way in and out, so the overlay lands where the targets do (M4: without
    # this the overlay sat ~0.5 * (scale - 1) px off).
    inv_corner = registration.inverse_map   # fixed px -> moving px (corner convention)
    cache = {}

    def inv(coords):
        # The image and the coverage mask are warped onto the same output grid, so the
        # inverse map (the costly part for TPS) is evaluated once and reused (v0.9.2).
        coords = np.asarray(coords, dtype=float)
        hit = cache.get("in")
        if hit is not None and hit.shape == coords.shape and np.array_equal(hit, coords):
            return cache["out"]
        out = inv_corner(coords + 0.5) - 0.5
        cache["in"], cache["out"] = coords.copy(), out
        return out

    warped = _sk_warp(moving_prealigned, inv, output_shape=(H, W),
                      order=order, mode="constant", cval=0.0,
                      preserve_range=True).astype(np.float32)

    # Coverage: warp an all-ones mask; anything < 0.5 fell outside the source.
    src_mask = np.ones(moving_prealigned.shape[:2], dtype=np.float32)
    cov = _sk_warp(src_mask, inv, output_shape=(H, W),
                   order=0, mode="constant", cval=0.0,
                   preserve_range=True)
    alpha = (cov >= 0.5).astype(np.float32)
    return warped, alpha
