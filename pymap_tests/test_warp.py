import numpy as np

from pymap.model import PreAlign, TransformKind
from pymap.warp import apply_prealign, warp_moving
from pymap.transform import fit_registration


def test_prealign_identity():
    img = np.arange(12, dtype=float).reshape(3, 4)
    out = apply_prealign(img, PreAlign())
    np.testing.assert_array_equal(out, img)


def test_prealign_rot90_matches_numpy():
    img = np.arange(12, dtype=float).reshape(3, 4)
    out = apply_prealign(img, PreAlign(rot90=1))
    np.testing.assert_array_equal(out, np.rot90(img, 1))


def test_prealign_flip_order_rot_then_flip():
    img = np.arange(12, dtype=float).reshape(3, 4)
    out = apply_prealign(img, PreAlign(rot90=1, flip_h=True, flip_v=True))
    expect = np.rot90(img, 1)
    expect = expect[:, ::-1][::-1, :]
    np.testing.assert_array_equal(out, expect)


def test_warp_identity_recovers_image():
    rng = np.random.default_rng(1)
    moving = rng.random((40, 50)).astype(np.float32)
    # identity landmarks -> transform is identity
    pts = np.array([[5, 5], [45, 5], [5, 35], [45, 35]], float)
    reg = fit_registration(pts, pts, TransformKind.AFFINE)
    warped, alpha = warp_moving(moving, reg, moving.shape, order=1)
    np.testing.assert_allclose(warped, moving, atol=1e-4)
    assert alpha.mean() > 0.99


def test_warp_translation_shifts_content_and_alpha():
    moving = np.zeros((60, 60), np.float32)
    moving[30, 20] = 1.0
    # fixed = moving shifted +10 in x, +5 in y  => forward maps moving->fixed
    src = np.array([[0, 0], [40, 0], [0, 40], [40, 40]], float)
    dst = src + np.array([10.0, 5.0])
    reg = fit_registration(src, dst, TransformKind.AFFINE)
    warped, alpha = warp_moving(moving, reg, (60, 60), order=0)
    ys, xs = np.where(warped > 0.5)
    assert (int(xs[0]), int(ys[0])) == (30, 35)   # (20+10, 30+5)
    # translation pushes part of the source off-grid -> alpha not fully covered
    assert alpha.mean() < 1.0


def test_warp_into_larger_fixed_grid():
    moving = np.ones((20, 20), np.float32)
    pts = np.array([[0, 0], [20, 0], [0, 20], [20, 20]], float)
    reg = fit_registration(pts, pts, TransformKind.AFFINE)
    warped, alpha = warp_moving(moving, reg, (40, 40), order=0)
    assert warped.shape == (40, 40)
    # only the top-left 20x20 region is covered
    assert alpha[:20, :20].mean() > 0.95
    assert alpha[25:, 25:].mean() == 0.0
