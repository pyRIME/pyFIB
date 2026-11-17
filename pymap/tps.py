"""Thin-plate spline in pure numpy.

Implemented here (rather than skimage.transform.ThinPlateSplineTransform) because
that class requires scikit-image >= 0.24, which requires Python >= 3.10 and will
not install on the instrument's 3.9.13 pin. This implementation depends only on
numpy and works identically on 3.9.

Convention: works in (x, y) coordinates to match skimage.transform.warp callable
inverse-map convention. estimate(src, dst) fits a spline mapping src -> dst that
interpolates the control points exactly.
"""

from __future__ import annotations

import numpy as np


# Points evaluated per block in __call__: bounds memory to ~CHUNK x N x 8 bytes per
# temporary (v0.9.2; a 1024x768 TPS with 40 pairs took 1.3 GB in one block).
CHUNK = 65536


def _U(r2: np.ndarray) -> np.ndarray:
    """Radial basis U(r) = r^2 * log(r), expressed via squared distance r2.

    r^2 log(r) = 0.5 * r2 * log(r2); zero at r2 == 0.
    """
    r2 = np.asarray(r2, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 0.5 * r2 * np.log(r2)
    out[r2 <= 1e-12] = 0.0
    return out


class ThinPlateSpline:
    def __init__(self):
        self.src = None          # (N,2)
        self.coeffs = None       # (N+3, 2)

    def estimate(self, src, dst) -> bool:
        src = np.asarray(src, dtype=float)
        dst = np.asarray(dst, dtype=float)
        if src.shape != dst.shape or src.ndim != 2 or src.shape[1] != 2:
            raise ValueError("src and dst must both be (N,2)")
        n = src.shape[0]
        if n < 3:
            raise ValueError("TPS needs at least 3 control points")

        d2 = np.sum((src[:, None, :] - src[None, :, :]) ** 2, axis=2)   # (N,N)
        K = _U(d2)
        P = np.hstack([np.ones((n, 1)), src])                          # (N,3)

        L = np.zeros((n + 3, n + 3))
        L[:n, :n] = K
        L[:n, n:] = P
        L[n:, :n] = P.T

        Y = np.vstack([dst, np.zeros((3, 2))])
        try:
            self.coeffs = np.linalg.solve(L, Y)
        except np.linalg.LinAlgError:
            self.coeffs, *_ = np.linalg.lstsq(L, Y, rcond=None)
        self.src = src
        return True

    def __call__(self, coords) -> np.ndarray:
        coords = np.asarray(coords, dtype=float)
        single = coords.ndim == 1
        if single:
            coords = coords[None, :]
        n = self.src.shape[0]
        w = self.coeffs[:n]     # (N,2)
        a = self.coeffs[n:]     # (3,2)
        out = np.empty((coords.shape[0], 2))
        for i0 in range(0, coords.shape[0], CHUNK):
            c = coords[i0:i0 + CHUNK]
            d2 = ((c[:, None, 0] - self.src[None, :, 0]) ** 2
                  + (c[:, None, 1] - self.src[None, :, 1]) ** 2)              # (m,N)
            out[i0:i0 + CHUNK] = _U(d2) @ w + c @ a[1:] + a[0]
        return out[0] if single else out
