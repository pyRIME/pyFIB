import numpy as np
import pytest

from pymap.model import PreAlign
from pymap.warp import apply_prealign
from pymap.prealign_map import prealign_forward, prealign_inverse, remap_point


ALL_PREALIGNS = [
    PreAlign(rot90=r, flip_h=fh, flip_v=fv)
    for r in range(4) for fh in (False, True) for fv in (False, True)
]


@pytest.mark.parametrize("pa", ALL_PREALIGNS)
def test_forward_matches_apply_prealign(pa):
    # Put a single marker pixel; where apply_prealign sends it must equal
    # where prealign_forward maps the coordinate.
    H, W = 7, 11
    for (r, c) in [(0, 0), (3, 5), (6, 10), (2, 9)]:
        img = np.zeros((H, W), np.float32)
        img[r, c] = 1.0
        out = apply_prealign(img, pa)
        ys, xs = np.where(out > 0.5)
        got = (int(xs[0]), int(ys[0]))
        fx, fy, fw, fh = prealign_forward((c, r), pa, W, H)
        assert (int(round(fx)), int(round(fy))) == got
        assert (fw, fh) == (out.shape[1], out.shape[0])


@pytest.mark.parametrize("pa", ALL_PREALIGNS)
def test_forward_inverse_roundtrip(pa):
    W, H = 11, 7
    for pt in [(0, 0), (10, 6), (5.5, 3.25), (9, 1)]:
        fx, fy, _, _ = prealign_forward(pt, pa, W, H)
        bx, by, bw, bh = prealign_inverse((fx, fy), pa, W, H)
        assert (round(bx, 6), round(by, 6)) == (round(pt[0], 6), round(pt[1], 6))
        assert (bw, bh) == (W, H)


def test_remap_between_prealigns_keeps_feature():
    # A feature at original pixel (c=8, r=1), clicked at its CENTRE in the Qt view
    # (corner convention: index + 0.5). Change the prealign; the remapped click must
    # sit at the centre of the same feature's pixel under the new prealign.
    W, H = 11, 7
    old = PreAlign(rot90=1, flip_h=True)
    new = PreAlign(rot90=3, flip_v=True)
    orig = (8.0, 1.0)
    ix, iy = prealign_forward(orig, old, W, H)[:2]
    remapped = remap_point((ix + 0.5, iy + 0.5), old, new, W, H)
    ex, ey = prealign_forward(orig, new, W, H)[:2]
    assert (round(remapped[0], 6), round(remapped[1], 6)) == (ex + 0.5, ey + 0.5)


@pytest.mark.parametrize("old", ALL_PREALIGNS[::3])
@pytest.mark.parametrize("new", ALL_PREALIGNS[1::3])
def test_remapped_click_lands_on_the_same_image_pixel(old, new):
    # Pixel-level check against the image transform itself (apply_prealign): a click
    # anywhere inside a marked pixel must still be inside that pixel after remapping.
    H, W = 7, 11
    img = np.zeros((H, W), np.float32)
    img[2, 9] = 1.0
    ys, xs = np.where(apply_prealign(img, old) > 0.5)
    for fx, fy in [(0.5, 0.5), (0.1, 0.9), (0.85, 0.2)]:          # inside the pixel
        click = (xs[0] + fx, ys[0] + fy)
        nx, ny = remap_point(click, old, new, W, H)
        ys2, xs2 = np.where(apply_prealign(img, new) > 0.5)
        assert int(np.floor(nx)) == xs2[0] and int(np.floor(ny)) == ys2[0]
