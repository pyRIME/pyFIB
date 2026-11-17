"""Registration quality in micrometres: on the image, and on the grid.

pyFIB applies its limits (warn > 1 um, refuse > 3 um unless overridden) to the
worst-axis leave-one-out error ON THE GRID. On the FIB at +30 deg the grid is seen
at 36 deg, so a vertical image error is ~1.7x larger on the grid; on the SEM (grid
normal to the beam) image and grid agree.

How the grid-plane error is found, without assuming any angle: the confocal is a
top-down image with square pixels, so the fitted transform's own singular values
say how much each image direction is squashed. Residuals are split along the
transform's most- and least-stretched image directions; the least-stretched
component is scaled up by s_max / s_min. ``squash_ratio`` = s_min / s_max is the
same number shown as the foreshortening readout (about 0.59 for the FIB at +30 deg,
about 1 for the SEM). For TPS the global affine part of the spline is used.
"""

from __future__ import annotations

from typing import Optional
import numpy as np

from .model import TransformKind
from .transform import Registration, loo_residual_vectors, RegistrationError


def _rms(a) -> float:
    a = np.asarray(a, dtype=float)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def _grid_components(vectors: np.ndarray, U: np.ndarray, stretch: float) -> np.ndarray:
    """(N, 2) residuals expressed on the grid: along the unsquashed image direction
    (u1) unchanged, along the squashed one (u2) multiplied by ``stretch``."""
    c = vectors @ U                    # components along u1, u2
    c[:, 1] *= stretch
    return c


def registration_qc(registration: Registration, moving_pts, fixed_pts,
                    fixed_pixel_size_m: Optional[float]) -> dict:
    """QC for one registration step (moving -> fixed). Values in px always; in um
    only when the fixed image's pixel size is known. Leave-one-out values are None
    when there are too few pairs (need one more than the transform's minimum)."""
    moving = np.asarray(moving_pts, dtype=float)
    fixed = np.asarray(fixed_pts, dtype=float)
    n = len(moving)
    A = registration.linear if registration.linear is not None else np.eye(2)
    U, S, _vt = np.linalg.svd(A)
    smax, smin = float(S[0]), float(S[1])
    squash = (smin / smax) if smax > 0 else None
    stretch = (smax / smin) if smin > 0 else float("inf")

    fit_vec = registration.forward(moving) - fixed
    loo_vec = None
    loo_error = None
    if n > TransformKind.MIN_PAIRS[registration.kind]:
        try:
            loo_vec = loo_residual_vectors(moving, fixed, registration.kind)
        except RegistrationError as e:
            loo_vec = None
            loo_error = str(e)

    px_um = None if fixed_pixel_size_m is None else float(fixed_pixel_size_m) * 1e6
    out = {
        "kind": registration.kind, "n_pairs": n,
        "rms_px": _rms(np.linalg.norm(fit_vec, axis=1)),
        "loo_rms_px": None if loo_vec is None else _rms(np.linalg.norm(loo_vec, axis=1)),
        "squash_ratio": squash,
        "rms_um": None, "loo_rms_um": None, "rms_grid_um": None, "loo_rms_grid_um": None,
        "loo_error": loo_error,
    }
    if px_um is not None:
        out["rms_um"] = out["rms_px"] * px_um
        g = _grid_components(fit_vec, U, stretch)
        out["rms_grid_um"] = max(_rms(g[:, 0]), _rms(g[:, 1])) * px_um
        if loo_vec is not None:
            out["loo_rms_um"] = out["loo_rms_px"] * px_um
            gl = _grid_components(loo_vec, U, stretch)
            out["loo_rms_grid_um"] = max(_rms(gl[:, 0]), _rms(gl[:, 1])) * px_um
    return out


def combine_steps(steps) -> dict:
    """Root-sum-square of per-step errors along a registration chain
    (independent errors). None if any step lacks the value."""
    def rss(key):
        vals = [s.get(key) for s in steps]
        if not vals or any(v is None for v in vals):
            return None
        return float(np.sqrt(sum(v * v for v in vals)))
    return {"combined_loo_rms_um": rss("loo_rms_um"),
            "combined_loo_rms_grid_um": rss("loo_rms_grid_um")}
