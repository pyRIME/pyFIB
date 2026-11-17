"""Landmark-based transform fitting with fit-quality QC.

Fits a transform mapping  moving px -> fixed px  from corresponding landmark
pairs. Exposes:
    forward(coords)      moving px -> fixed px   (map a confocal target into FIB)
    inverse_map(coords)  fixed px  -> moving px   (used by skimage.warp)
    residuals / rms      fit-quality QC

For rigid/similarity/affine, residuals are the true fit error. TPS interpolates
its control points exactly, so plain residuals are ~0 and meaningless as a QC
number; use leave_one_out_rms() instead - it is the honest measure of how well
the deformable fit generalises between control points.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Tuple
import numpy as np

from skimage.transform import EuclideanTransform, SimilarityTransform, AffineTransform

from .model import TransformKind
from .tps import ThinPlateSpline


class RegistrationError(ValueError):
    pass


_LINEAR = {
    TransformKind.RIGID: EuclideanTransform,
    TransformKind.SIMILARITY: SimilarityTransform,
    TransformKind.AFFINE: AffineTransform,
}


@dataclass
class Registration:
    kind: str
    n: int
    forward: Callable[[np.ndarray], np.ndarray]      # moving -> fixed
    inverse_map: Callable[[np.ndarray], np.ndarray]  # fixed -> moving (for warp)
    residuals: np.ndarray                            # per-point euclidean error (px)
    rms: float                                       # RMS of residuals (px)
    linear: np.ndarray = None                        # 2x2 d(fixed)/d(moving); TPS: affine part

    def map_point(self, x: float, y: float) -> Tuple[float, float]:
        out = self.forward(np.array([[x, y]], dtype=float))[0]
        return (float(out[0]), float(out[1]))


def _as_xy(pts) -> np.ndarray:
    a = np.asarray(pts, dtype=float)
    if a.ndim != 2 or a.shape[1] != 2:
        raise RegistrationError("points must be an (N,2) array of (x,y)")
    return a


def fit_registration(moving_pts, fixed_pts, kind: str = TransformKind.AFFINE) -> Registration:
    moving = _as_xy(moving_pts)
    fixed = _as_xy(fixed_pts)
    if moving.shape != fixed.shape:
        raise RegistrationError("moving and fixed must have the same number of points")
    n = moving.shape[0]
    need = TransformKind.MIN_PAIRS.get(kind)
    if need is None:
        raise RegistrationError(f"unknown transform kind {kind!r}")
    if n < need:
        raise RegistrationError(f"{kind} needs >= {need} pairs, got {n}")

    degenerate = RegistrationError(
        f"{kind}: the landmarks are degenerate (points on one line, or repeated); "
        f"spread them out")
    # v0.9.2: affine and TPS need the points to span both directions, and every model
    # needs two distinct points; otherwise the fit is arbitrary across the line
    for pts in (moving, fixed):
        c = pts - pts.mean(axis=0)
        sv = np.linalg.svd(c, compute_uv=False) if n >= 2 else np.zeros(2)
        if sv[0] < 1e-6:
            raise degenerate
        if kind in (TransformKind.AFFINE, TransformKind.TPS) and \
                (len(sv) < 2 or sv[1] < 1e-6 * sv[0] or sv[1] < 1e-3):
            raise degenerate
        if kind == TransformKind.TPS:      # a spline can't pass one point two ways
            d2 = np.sum((pts[:, None, :] - pts[None, :, :]) ** 2, axis=2)
            np.fill_diagonal(d2, np.inf)
            if d2.min() < 1e-12:
                raise RegistrationError("tps: two landmarks sit on the same point; "
                                        "delete one of them")
    if kind in _LINEAR:
        tform = _LINEAR[kind]()
        try:
            ok = tform.estimate(moving, fixed)   # maps src(moving) -> dst(fixed)
            # v0.9.2: skimage inverts the matrix lazily; a singular one (e.g. three
            # collinear points with affine) raised numpy's LinAlgError, not ours
            inv_t = tform.inverse
        except (np.linalg.LinAlgError, ValueError, ZeroDivisionError) as e:
            raise degenerate from e
        if not ok or not np.all(np.isfinite(tform.params)) \
                or abs(np.linalg.det(tform.params[:2, :2])) < 1e-12:
            raise degenerate
        forward = _wrap_call(tform)
        inverse_map = _wrap_call(inv_t)
        pred = forward(moving)
        linear = np.array(tform.params[:2, :2], dtype=float)
    else:  # TPS
        fwd = ThinPlateSpline(); fwd.estimate(moving, fixed)
        inv = ThinPlateSpline(); inv.estimate(fixed, moving)
        forward = fwd
        inverse_map = inv
        pred = forward(moving)
        # global affine part of the spline: rows of coeffs[n:] are (1, x, y) terms
        a = fwd.coeffs[n:]
        linear = np.array(a[1:3].T, dtype=float)
        if not (np.all(np.isfinite(fwd.coeffs)) and np.all(np.isfinite(inv.coeffs))):
            raise degenerate
    if not np.all(np.isfinite(pred)):
        raise degenerate

    res = np.linalg.norm(pred - fixed, axis=1)
    rms = float(np.sqrt(np.mean(res ** 2))) if n else 0.0
    return Registration(kind=kind, n=n, forward=forward, inverse_map=inverse_map,
                        residuals=res, rms=rms, linear=linear)


def _wrap_call(tform) -> Callable[[np.ndarray], np.ndarray]:
    # skimage transforms are callable on (N,2) (x,y); normalise to a plain fn.
    def f(coords):
        return tform(np.asarray(coords, dtype=float))
    return f


def loo_residual_vectors(moving_pts, fixed_pts, kind: str = TransformKind.TPS) -> np.ndarray:
    """(N, 2) leave-one-out prediction errors in fixed px: for each pair, fit
    without it and record (predicted - actual)."""
    moving = _as_xy(moving_pts)
    fixed = _as_xy(fixed_pts)
    n = moving.shape[0]
    need = TransformKind.MIN_PAIRS[kind]
    if n <= need:
        raise RegistrationError(
            f"leave-one-out for {kind} needs > {need} pairs, got {n}")
    out = np.zeros((n, 2))
    idx = np.arange(n)
    for i in idx:
        try:
            reg = fit_registration(moving[idx != i], fixed[idx != i], kind)
        except RegistrationError as e:
            raise RegistrationError(
                "leave-one-out: without pair %d the other pairs are degenerate (%s); "
                "move or add a landmark pair" % (i + 1, e)) from e
        out[i] = reg.forward(moving[i:i + 1])[0] - fixed[i]
    return out


def leave_one_out_rms(moving_pts, fixed_pts, kind: str = TransformKind.TPS) -> float:
    """Leave-one-out RMS prediction error (px). The honest QC for TPS, and a
    useful generalisation check for the linear models too."""
    v = loo_residual_vectors(moving_pts, fixed_pts, kind)
    return float(np.sqrt(np.mean(np.sum(v * v, axis=1))))
