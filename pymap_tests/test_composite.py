import numpy as np
import pytest

from pymap.composite import pseudocolor, COLORMAPS


def test_named_lut_colors_grayscale():
    g = np.full((3, 3), 0.5, np.float32)
    r = pseudocolor(g, "red").mean(axis=(0, 1))
    assert r[0] > 0.4 and r[1] < 1e-6 and r[2] < 1e-6


def test_named_lut_recolors_rgb_confocal():
    # single-channel fluorescence stored as R=G=B must still obey the LUT
    g = np.full((3, 3), 0.5, np.float32)
    rgb = np.stack([g, g, g], axis=-1)
    red = pseudocolor(rgb, "red").mean(axis=(0, 1))
    green = pseudocolor(rgb, "green").mean(axis=(0, 1))
    assert red[0] > 0.4 and red[1] < 1e-6 and red[2] < 1e-6
    assert green[1] > 0.4 and green[0] < 1e-6 and green[2] < 1e-6


def test_rgb_passthrough_preserves_channels():
    img = np.zeros((2, 2, 3), np.float32)
    img[..., 0] = 0.9  # pure red source
    out = pseudocolor(img, "rgb")
    np.testing.assert_allclose(out, img, atol=1e-6)


def test_rgb_option_on_grayscale_gives_gray():
    g = np.full((2, 2), 0.3, np.float32)
    out = pseudocolor(g, "rgb").mean(axis=(0, 1))
    assert abs(out[0] - out[1]) < 1e-6 and abs(out[1] - out[2]) < 1e-6


def test_unknown_lut_raises():
    with pytest.raises(ValueError):
        pseudocolor(np.zeros((2, 2), np.float32), "chartreuse")


def _layers():
    fib = np.zeros((10, 10, 3), np.float32)      # black FIB
    conf = np.ones((10, 10, 3), np.float32)      # white confocal
    alpha = np.ones((10, 10), np.float32)        # fully covered
    return fib, conf, alpha


def test_swipe_splits_left_fib_right_confocal():
    from pymap.composite import compare_swipe
    fib, conf, alpha = _layers()
    out = compare_swipe(fib, conf, alpha, split_frac=0.5, divider=False)
    split = int(round(0.5 * 9))
    assert out[:, :split].max() < 1e-6          # left = FIB (black)
    assert out[:, split:].min() > 0.99          # right = confocal (white)


def test_swipe_divider_drawn():
    from pymap.composite import compare_swipe
    fib, conf, alpha = _layers()
    out = compare_swipe(fib, conf, alpha, split_frac=0.5, divider=True)
    split = int(round(0.5 * 9))
    assert not np.allclose(out[:, split], 0.0)  # divider column colored


def test_checkerboard_alternates():
    from pymap.composite import compare_checkerboard
    fib, conf, alpha = _layers()
    out = compare_checkerboard(fib, conf, alpha, tile=1)
    # (0,0) tile -> FIB (black); (0,1) -> confocal (white)
    assert out[0, 0].max() < 1e-6
    assert out[0, 1].min() > 0.99


def test_checkerboard_uncovered_shows_fib():
    from pymap.composite import compare_checkerboard
    fib = np.zeros((8, 8, 3), np.float32)
    conf = np.ones((8, 8, 3), np.float32)
    alpha = np.zeros((8, 8), np.float32)   # nothing covered
    out = compare_checkerboard(fib, conf, alpha, tile=2)
    assert out.max() < 1e-6                # all FIB, no confocal leaks in
