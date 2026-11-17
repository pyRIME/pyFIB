import numpy as np
import pytest

from pymap.transform import fit_registration, leave_one_out_rms, RegistrationError
from pymap.model import TransformKind


def _apply_affine(pts, A):
    h = np.hstack([pts, np.ones((len(pts), 1))])
    return (h @ A.T)[:, :2]


def test_affine_recovers_known_transform():
    A = np.array([[1.2, -0.1, 30.0],
                  [0.05, 0.9, -15.0],
                  [0.0, 0.0, 1.0]])
    moving = np.array([[0, 0], [100, 0], [0, 80], [120, 90], [40, 200]], float)
    fixed = _apply_affine(moving, A)
    reg = fit_registration(moving, fixed, TransformKind.AFFINE)
    assert reg.rms < 1e-6
    # forward maps moving landmarks onto fixed
    np.testing.assert_allclose(reg.forward(moving), fixed, atol=1e-6)


def test_similarity_is_rigid_plus_scale():
    # rotate 30 deg, scale 1.5, translate
    th = np.deg2rad(30.0)
    s = 1.5
    R = s * np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    moving = np.array([[0, 0], [50, 0], [0, 70], [90, 20]], float)
    fixed = moving @ R.T + np.array([10.0, -5.0])
    reg = fit_registration(moving, fixed, TransformKind.SIMILARITY)
    assert reg.rms < 1e-6


def test_rigid_rejects_scale():
    th = np.deg2rad(20.0)
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    moving = np.array([[0, 0], [50, 0], [0, 70], [90, 20]], float)
    fixed_pure = moving @ R.T + np.array([3.0, 4.0])
    fixed_scaled = moving @ (2.0 * R).T + np.array([3.0, 4.0])
    assert fit_registration(moving, fixed_pure, TransformKind.RIGID).rms < 1e-6
    # a rigid fit cannot absorb a 2x scale -> large residual
    assert fit_registration(moving, fixed_scaled, TransformKind.RIGID).rms > 10.0


def test_tps_interpolates_control_points_exactly():
    rng = np.random.default_rng(0)
    moving = rng.uniform(0, 200, size=(8, 2))
    fixed = moving + rng.uniform(-5, 5, size=(8, 2))  # arbitrary warp
    reg = fit_registration(moving, fixed, TransformKind.TPS)
    # TPS passes through its control points -> residual ~ 0 by construction
    np.testing.assert_allclose(reg.forward(moving), fixed, atol=1e-6)
    assert reg.rms < 1e-6


def test_inverse_map_composes_to_identity_for_affine():
    A = np.array([[1.1, 0.2, 12.0], [-0.05, 0.95, -8.0], [0, 0, 1.0]])
    moving = np.array([[0, 0], [100, 0], [0, 80], [120, 90]], float)
    fixed = _apply_affine(moving, A)
    reg = fit_registration(moving, fixed, TransformKind.AFFINE)
    pts_fixed = np.array([[10, 10], [55, 33], [200, 5]], float)
    back = reg.forward(reg.inverse_map(pts_fixed))
    np.testing.assert_allclose(back, pts_fixed, atol=1e-6)


def test_too_few_points_raises():
    with pytest.raises(RegistrationError):
        fit_registration([[0, 0], [1, 1]], [[0, 0], [1, 1]], TransformKind.AFFINE)


def test_leave_one_out_zero_for_exact_affine():
    A = np.array([[1.2, -0.1, 30.0], [0.05, 0.9, -15.0], [0, 0, 1.0]])
    moving = np.array([[0, 0], [100, 0], [0, 80], [120, 90], [40, 200]], float)
    fixed = _apply_affine(moving, A)
    # affine data -> LOO error effectively zero
    assert leave_one_out_rms(moving, fixed, TransformKind.AFFINE) < 1e-6
