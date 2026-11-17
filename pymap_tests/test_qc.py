"""Overlay pixel convention (M4) and registration QC in micrometres."""

import math

import numpy as np
import pytest

from pymap.model import TransformKind
from pymap.transform import fit_registration, loo_residual_vectors, leave_one_out_rms
from pymap.warp import warp_moving
from pymap.qc import registration_qc, combine_steps


# --------------------------------------------------------------------------- #
# M4: the overlay lands where the targets do
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scale", [2.0, 0.5, 3.0])
def test_overlay_pixel_lands_where_its_corner_coords_say(scale):
    # Landmarks are corner-convention (Qt scene): fixed = scale * moving.
    mv = np.array([[0, 0], [40, 0], [0, 40], [40, 40], [17, 9]], float)
    reg = fit_registration(mv, scale * mv, TransformKind.AFFINE)
    img = np.zeros((40, 40), np.float32)
    img[10, 10] = 1.0                       # spans corner coords [10, 11)
    out_n = int(round(40 * scale))
    w, _a = warp_moving(img, reg, (out_n, out_n), order=0)
    ys, xs = np.nonzero(w > 0.5)
    lo, hi = 10 * scale, 11 * scale         # where the pixel's extent maps
    expected = [i for i in range(out_n) if lo <= i + 0.5 < hi]
    assert sorted(set(xs.tolist())) == expected
    assert sorted(set(ys.tolist())) == expected


def test_identity_and_translation_unchanged_by_the_fix():
    rng = np.random.default_rng(0)
    img = rng.random((30, 40)).astype(np.float32)
    pts = np.array([[0, 0], [40, 0], [0, 30], [40, 30]], float)
    w, a = warp_moving(img, fit_registration(pts, pts, TransformKind.AFFINE), (30, 40))
    np.testing.assert_allclose(w, img, atol=1e-5)
    w2, _ = warp_moving(img, fit_registration(pts, pts + [3, 2], TransformKind.AFFINE),
                        (30, 40), order=0)
    np.testing.assert_allclose(w2[2:, 3:], img[:-2, :-3], atol=1e-6)


# --------------------------------------------------------------------------- #
# QC in micrometres
# --------------------------------------------------------------------------- #
SQUASH = math.sin(math.radians(36.0))       # FIB at +30 deg sees the grid at 36 deg


def _fib_like_transform():
    """confocal (top-down, square px) -> FIB image: rotate 20 deg, scale 2, squash Y."""
    th = math.radians(20.0)
    R = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
    return np.diag([1.0, SQUASH]) @ (2.0 * R)


def _pairs(A, n=9, noise=None, seed=0):
    rng = np.random.default_rng(seed)
    mv = rng.uniform(0, 300, size=(n, 2))
    fx = mv @ A.T + [40.0, 25.0]
    if noise is not None:
        fx = fx + noise(rng, n)
    return mv, fx


def test_linear_part_is_recorded_for_every_kind():
    A = _fib_like_transform()
    mv, fx = _pairs(A)
    reg = fit_registration(mv, fx, TransformKind.AFFINE)
    np.testing.assert_allclose(reg.linear, A, atol=1e-9)
    tps = fit_registration(mv, fx, TransformKind.TPS)
    np.testing.assert_allclose(tps.linear, A, atol=1e-6)     # affine data: no bending
    sim = fit_registration(mv, mv @ (2.0 * np.eye(2)).T, TransformKind.SIMILARITY)
    np.testing.assert_allclose(sim.linear, 2.0 * np.eye(2), atol=1e-9)


def test_loo_vectors_agree_with_loo_rms():
    A = _fib_like_transform()
    mv, fx = _pairs(A, noise=lambda r, n: r.normal(0, 0.7, (n, 2)))
    v = loo_residual_vectors(mv, fx, TransformKind.AFFINE)
    assert v.shape == (9, 2)
    assert math.sqrt(np.mean(np.sum(v * v, axis=1))) == pytest.approx(
        leave_one_out_rms(mv, fx, TransformKind.AFFINE))


def test_squash_ratio_reads_the_foreshortening():
    mv, fx = _pairs(_fib_like_transform())
    q = registration_qc(fit_registration(mv, fx, TransformKind.AFFINE), mv, fx, 2.79e-7)
    assert q["squash_ratio"] == pytest.approx(SQUASH, abs=1e-6)
    mv2, fx2 = _pairs(2.0 * np.eye(2))                       # top-down (SEM-like)
    q2 = registration_qc(fit_registration(mv2, fx2, TransformKind.AFFINE), mv2, fx2, 1e-6)
    assert q2["squash_ratio"] == pytest.approx(1.0, abs=1e-6)


def test_vertical_image_error_is_larger_on_the_grid():
    """Errors only along the squashed (vertical) image axis: grid error = image error
    / sin 36 deg. Errors only along the unsquashed axis: grid error = image error."""
    A = _fib_like_transform()
    px = 2.7905e-7
    vert = lambda r, n: np.c_[np.zeros(n), r.normal(0, 1.0, n)]
    horiz = lambda r, n: np.c_[r.normal(0, 1.0, n), np.zeros(n)]
    mv, fx = _pairs(A, n=25, noise=vert, seed=2)
    qv = registration_qc(fit_registration(mv, fx, TransformKind.AFFINE), mv, fx, px)
    assert qv["loo_rms_grid_um"] == pytest.approx(qv["loo_rms_um"] / SQUASH, rel=0.03)
    mv, fx = _pairs(A, n=25, noise=horiz, seed=2)
    qh = registration_qc(fit_registration(mv, fx, TransformKind.AFFINE), mv, fx, px)
    assert qh["loo_rms_grid_um"] == pytest.approx(qh["loo_rms_um"], rel=0.03)


def test_qc_without_pixel_size_or_enough_pairs():
    A = _fib_like_transform()
    mv, fx = _pairs(A, n=3)
    q = registration_qc(fit_registration(mv, fx, TransformKind.AFFINE), mv, fx, None)
    assert q["loo_rms_px"] is None and q["rms_um"] is None and q["loo_rms_grid_um"] is None
    assert q["n_pairs"] == 3 and q["squash_ratio"] == pytest.approx(SQUASH, abs=1e-6)


def test_combine_steps_root_sum_square():
    c = combine_steps([{"loo_rms_um": 0.3, "loo_rms_grid_um": 0.4},
                       {"loo_rms_um": 0.4, "loo_rms_grid_um": 0.3}])
    assert c["combined_loo_rms_um"] == pytest.approx(0.5)
    assert c["combined_loo_rms_grid_um"] == pytest.approx(0.5)
    assert combine_steps([{"loo_rms_um": None, "loo_rms_grid_um": 1.0}])["combined_loo_rms_um"] is None
