"""End-to-end orchestration: images + landmarks -> warped confocal + overlay + QC.

This is the headless entry point the GUI (and CLI) drive. It does NOT resample
the FIB image; only the confocal is warped into the FIB grid.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional
import numpy as np

from .model import RegistrationSession, TransformKind
from .reader import load_image, to_gray, LoadedImage
from .transform import fit_registration, Registration, leave_one_out_rms, RegistrationError
from .warp import apply_prealign, warp_moving
from .composite import pseudocolor, blend_over

log = logging.getLogger("pymap.pipeline")


@dataclass
class RegistrationResult:
    fixed: LoadedImage
    moving_prealigned: np.ndarray     # confocal after rot90/flip
    registration: Registration
    warped: np.ndarray                # confocal resampled into FIB grid (H,W[,3])
    alpha: np.ndarray                 # coverage mask (H,W)
    loo_rms: Optional[float]          # leave-one-out RMS (px), None if too few pts

    def overlay(self, cmap: str = "green", opacity: float = 0.5,
                contrast=(0.0, 1.0)) -> np.ndarray:
        """Compose the blended overlay RGB (FIB background + pseudocolored confocal)."""
        moving_rgb = pseudocolor(self.warped, cmap=cmap, contrast=contrast)
        # zero out colour outside coverage so the LUT floor doesn't tint the FIB
        moving_rgb = moving_rgb * self.alpha[..., None]
        return blend_over(to_gray(self.fixed.data), moving_rgb, self.alpha, opacity)

    def warped_rgba(self, cmap: str = "green", contrast=(0.0, 1.0)) -> np.ndarray:
        """The confocal alone, in FIB space, as RGBA (alpha = coverage).

        This is the hand-off artifact for elymaker: same pixel dimensions as the
        FIB image, so it lines up 1:1 with the milling reference.
        """
        rgb = pseudocolor(self.warped, cmap=cmap, contrast=contrast)
        rgba = np.concatenate([rgb, self.alpha[..., None]], axis=-1)
        return np.clip(rgba, 0.0, 1.0).astype(np.float32)


def register_and_warp(session: RegistrationSession,
                      fixed_img: Optional[LoadedImage] = None,
                      moving_img: Optional[LoadedImage] = None,
                      warp_order: int = 1) -> RegistrationResult:
    """Run a session. Images may be passed in (already loaded) or loaded from the
    session's paths."""
    if fixed_img is None:
        fixed_img = load_image(session.fixed_path)
    if moving_img is None:
        moving_img = load_image(session.moving_path)

    if not session.is_solvable():
        raise RegistrationError(
            f"{session.transform} needs >= {session.min_pairs()} pairs, "
            f"have {len(session.pairs)}")

    moving_pre = apply_prealign(moving_img.data, session.prealign)

    moving_pts = np.array([p.moving for p in session.pairs], dtype=float)
    fixed_pts = np.array([p.fixed for p in session.pairs], dtype=float)
    t0 = time.perf_counter()
    reg = fit_registration(moving_pts, fixed_pts, session.transform)
    t1 = time.perf_counter()
    warped, alpha = warp_moving(moving_pre, reg, fixed_img.shape_hw, order=warp_order)
    t2 = time.perf_counter()
    loo = None
    try:
        loo = leave_one_out_rms(moving_pts, fixed_pts, session.transform)
    except RegistrationError:
        loo = None  # not enough points for LOO
    log.info("%s %d pairs: fit %.3f s, warp %s -> %s %.3f s, leave-one-out %.3f s",
             session.transform, len(session.pairs), t1 - t0, moving_pre.shape,
             tuple(fixed_img.shape_hw), t2 - t1, time.perf_counter() - t2)

    return RegistrationResult(fixed=fixed_img, moving_prealigned=moving_pre,
                              registration=reg, warped=warped, alpha=alpha,
                              loo_rms=loo)
