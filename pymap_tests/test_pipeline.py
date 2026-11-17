import json
import numpy as np
import pytest
from PIL import Image

from pymap.model import RegistrationSession, PreAlign, TransformKind
from pymap.session import save_session, load_session
from pymap.reader import load_image, to_gray
from pymap.pipeline import register_and_warp
from pymap.transform import RegistrationError


def _write_png(path, arr_uint8, mode=None):
    Image.fromarray(arr_uint8).save(path)  # mode inferred from dtype/shape


def test_reader_8bit_gray(tmp_path):
    p = tmp_path / "g.png"
    _write_png(str(p), (np.random.rand(16, 24) * 255).astype(np.uint8))
    li = load_image(str(p))
    assert li.shape_hw == (16, 24)
    assert not li.is_rgb and li.bit_depth == 8
    assert li.data.max() <= 1.0 and li.data.min() >= 0.0


def test_reader_16bit(tmp_path):
    p = tmp_path / "g16.png"
    arr = (np.random.rand(10, 10) * 65535).astype(np.uint16)
    Image.fromarray(arr).save(str(p))  # uint16 -> I;16
    li = load_image(str(p))
    assert li.bit_depth == 16
    assert li.data.max() <= 1.0


def test_reader_rgb(tmp_path):
    p = tmp_path / "c.png"
    _write_png(str(p), (np.random.rand(12, 12, 3) * 255).astype(np.uint8), mode="RGB")
    li = load_image(str(p))
    assert li.is_rgb and li.data.shape == (12, 12, 3)


def test_session_roundtrip(tmp_path):
    s = RegistrationSession(fixed_path="fib.bmp", moving_path="c.tif",
                            transform=TransformKind.TPS,
                            prealign=PreAlign(rot90=3, flip_h=True))
    for i in range(4):
        s.add_pair((i, 2 * i), (10 + i, 3 * i))
    p = tmp_path / "sess.json"
    save_session(s, str(p))
    s2 = load_session(str(p))
    assert s2.transform == TransformKind.TPS
    assert s2.prealign.rot90 == 3 and s2.prealign.flip_h
    assert len(s2.pairs) == 4
    assert s2.pairs[2].moving == (12.0, 6.0)


def test_pipeline_end_to_end_known_shift(tmp_path):
    # FIB fixed image with a bright marker; confocal is the same marker shifted.
    fib = np.zeros((80, 100), np.float32)
    conf = np.zeros((80, 100), np.float32)
    marks_fixed = [(20, 15), (70, 18), (25, 60), (80, 55)]
    shift = np.array([8.0, -4.0])
    for (x, y) in marks_fixed:
        fib[y, x] = 1.0
        mx, my = int(x - shift[0]), int(y - shift[1])
        conf[my, mx] = 1.0
    fib_p = tmp_path / "fib.png"; _write_png(str(fib_p), (fib * 255).astype(np.uint8))
    conf_p = tmp_path / "conf.png"; _write_png(str(conf_p), (conf * 255).astype(np.uint8))

    s = RegistrationSession(fixed_path=str(fib_p), moving_path=str(conf_p),
                            transform=TransformKind.AFFINE)
    for (x, y) in marks_fixed:
        s.add_pair((x, y), (x - shift[0], y - shift[1]))

    res = register_and_warp(s)
    assert res.registration.rms < 1e-3
    # warped confocal marker lands on the FIB marker location
    ys, xs = np.where(res.warped > 0.4)
    hits = set(zip(xs.tolist(), ys.tolist()))
    for (x, y) in marks_fixed:
        assert any(abs(hx - x) <= 1 and abs(hy - y) <= 1 for (hx, hy) in hits)

    overlay = res.overlay(cmap="green", opacity=0.6)
    assert overlay.shape == (80, 100, 3)
    rgba = res.warped_rgba(cmap="green")
    assert rgba.shape == (80, 100, 4)


def test_pipeline_requires_enough_points(tmp_path):
    fib = np.zeros((20, 20), np.float32)
    fib_p = tmp_path / "f.png"; _write_png(str(fib_p), (fib).astype(np.uint8))
    s = RegistrationSession(fixed_path=str(fib_p), moving_path=str(fib_p),
                            transform=TransformKind.AFFINE)
    s.add_pair((1, 1), (1, 1))
    with pytest.raises(RegistrationError):
        register_and_warp(s)


def test_prealign_applied_before_warp(tmp_path):
    # moving is rotated 90deg vs fixed; prealign rot90 must undo it so that
    # simple landmarks line up.
    fib = np.zeros((60, 60), np.float32)
    fib[10, 40] = 1.0
    conf = np.rot90(fib, k=1).copy()   # rotate the marker
    fib_p = tmp_path / "f.png"; _write_png(str(fib_p), (fib * 255).astype(np.uint8))
    conf_p = tmp_path / "c.png"; _write_png(str(conf_p), (conf * 255).astype(np.uint8))

    # after prealign rot90=3 (undo the k=1), moving == fib, so identity landmarks
    s = RegistrationSession(fixed_path=str(fib_p), moving_path=str(conf_p),
                            transform=TransformKind.AFFINE,
                            prealign=PreAlign(rot90=3))
    for (x, y) in [(5, 5), (50, 5), (5, 50), (50, 50)]:
        s.add_pair((x, y), (x, y))
    res = register_and_warp(s)
    ys, xs = np.where(res.warped > 0.4)
    assert (int(xs[0]), int(ys[0])) == (40, 10)
