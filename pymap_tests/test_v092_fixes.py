"""Regression tests for the v0.9.2 fixes found by the independent review."""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyqt5")

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("PyQt5")
from qtpy.QtWidgets import QApplication

from pymap import tps as tps_mod
from pymap.tps import ThinPlateSpline, _U
from pymap.gui.controller import LandmarkController, _Slot
from pymap.reader import LoadedImage
from pymap.transform import RegistrationError

MARKS = [(20, 15), (70, 18), (25, 60), (80, 55), (50, 35)]
SHIFT = (8.0, -4.0)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def images(tmp_path):
    fib = np.zeros((80, 100), np.uint8)
    conf = np.zeros((80, 100), np.uint8)
    for x, y in MARKS:
        fib[y, x] = 255
        conf[int(y - SHIFT[1]), int(x - SHIFT[0])] = 255
    Image.fromarray(fib).save(tmp_path / "fib.png")
    Image.fromarray(conf).save(tmp_path / "conf.png")
    return str(tmp_path / "fib.png"), str(tmp_path / "conf.png")


def _solved(c, images, n=5):
    c.load_fixed(images[0])
    c.load_moving(images[1])
    for x, y in MARKS[:n]:
        c.place_fixed(float(x), float(y))
        c.place_moving(float(x - SHIFT[0]), float(y - SHIFT[1]))
    return c


# ---- #1 TPS cost ----------------------------------------------------------- #
def test_tps_in_chunks_matches_one_block(monkeypatch):
    rng = np.random.default_rng(1)
    src = rng.uniform(0, 500, (25, 2))
    dst = src * 1.3 + rng.normal(0, 3, (25, 2))
    t = ThinPlateSpline()
    t.estimate(src, dst)
    pts = rng.uniform(-50, 550, (1000, 2))
    whole = t(pts)
    monkeypatch.setattr(tps_mod, "CHUNK", 7)
    assert np.allclose(t(pts), whole, rtol=0, atol=1e-9)
    assert np.allclose(t(src), dst, atol=1e-6)            # still interpolates
    assert _U(np.array([0.0, 1e-13]))[0] == 0.0 and _U(np.array([1e-13]))[0] == 0.0


def test_warp_evaluates_the_inverse_map_once():
    from pymap.transform import fit_registration
    from pymap.warp import warp_moving
    rng = np.random.default_rng(2)
    mv = rng.uniform(0, 90, (6, 2))
    reg = fit_registration(mv, mv * 1.1 + 2, "tps")
    calls = []
    inner = reg.inverse_map
    reg.inverse_map = lambda c: (calls.append(1), inner(c))[1]
    warp_moving(rng.random((100, 100, 3), dtype=np.float32), reg, (60, 70))
    assert len(calls) == 1


def test_half_pair_click_does_not_rewarp(app, images, monkeypatch):
    import pymap.gui.controller as ctl
    c = _solved(LandmarkController(), images)
    n = []
    real = ctl.register_and_warp
    monkeypatch.setattr(ctl, "register_and_warp", lambda *a, **k: (n.append(1), real(*a, **k))[1])
    c.place_fixed(10.0, 10.0)              # half a pair: complete pairs unchanged
    c.delete_active()                      # removing it: unchanged again
    assert n == []
    c.place_fixed(60.0, 70.0)
    c.place_moving(52.0, 74.0)             # completes a pair
    assert n == [1]
    assert c._result is not None and len(c._complete_session().pairs) == 6


# ---- #2 flicker ------------------------------------------------------------ #
def test_flicker_reuses_frames_and_pauses_when_hidden(app, images, monkeypatch):
    from pymap.gui.main_window import MainWindow
    win = MainWindow()
    _solved(win.c, images)
    win.c.set_compare(mode="flicker")
    renders = []
    real = win.c._compare_layers
    monkeypatch.setattr(win.c, "_compare_layers", lambda: (renders.append(1), real())[1])
    win.c._flicker_cache = None
    for _ in range(6):
        win.c.set_compare(flicker_phase=not win.c.flicker_phase)
    assert len(renders) == 1                           # both frames from one render
    phase = win.c.flicker_phase
    assert not win.overlay_view.isVisible()            # window never shown
    win.panel._flicker_tick()
    assert win.c.flicker_phase == phase                # hidden: tick skipped


# ---- #3 NaN TIFF ------------------------------------------------------------ #
def test_float_tiff_with_nan_opens(app, tmp_path, monkeypatch):
    tifffile = pytest.importorskip("tifffile")
    from qtpy.QtWidgets import QFileDialog, QMessageBox
    from pymap.gui.main_window import MainWindow
    a = np.random.default_rng(3).random((2, 40, 50)).astype(np.float32)
    a[0, 5, 5] = np.nan
    a[1, 6, 6] = np.inf
    p = tmp_path / "decon.tif"
    tifffile.imwrite(str(p), a, imagej=True, metadata={"axes": "CYX"})
    boxes = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *x, **k: boxes.append(x[1:3]))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *x, **k: (str(p), ""))
    win = MainWindow()
    win.open_moving()
    app.processEvents()
    assert boxes == []
    chans = win.c._confocal.channels
    assert len(chans) == 2 and all(np.isfinite(ch.data).all() for ch in chans)
    assert all(np.isfinite([ch.low, ch.high]).all() for ch in chans)
    assert win.panel.chan_layout.count() == 2


# ---- #4 recompute never leaves the views half-updated ------------------------ #
def test_unexpected_registration_error_is_shown_and_views_refresh(app, images, monkeypatch):
    import pymap.gui.controller as ctl
    c = _solved(LandmarkController(), images, n=4)
    got = {"qc": [], "overlay": 0, "targets": 0}
    c.qcChanged.connect(lambda q: got["qc"].append(q["message"]))
    c.overlayChanged.connect(lambda *_: got.__setitem__("overlay", got["overlay"] + 1))
    c.targetsChanged.connect(lambda: got.__setitem__("targets", got["targets"] + 1))
    monkeypatch.setattr(ctl, "register_and_warp", lambda *a, **k: 1 / 0)
    c.set_transform("tps")
    assert c._result is None
    assert got["qc"] and got["qc"][-1].startswith("registration failed: ZeroDivisionError")
    assert got["overlay"] == 1 and got["targets"] == 1


# ---- #5 session transform / atomic load ------------------------------------- #
def test_session_transform_checked_and_bad_load_changes_nothing(app, images, tmp_path):
    c = _solved(LandmarkController(), images, n=4)
    sp = tmp_path / "s.json"
    c.save_session_to(str(sp))
    d = json.loads(sp.read_text())
    d["transform"] = "TPS"                               # case is forgiven
    sp.write_text(json.dumps(d))
    c2 = LandmarkController()
    c2.load_session_from(str(sp))
    assert c2.transform == "tps"
    before = [(s.fixed, s.moving) for s in c2.slots]
    d["transform"] = "bspline"
    d["pairs"] = []
    sp.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="unknown transform"):
        c2.load_session_from(str(sp))
    d["transform"] = "affine"
    bad = tmp_path / "broken.tif"
    bad.write_bytes(b"not an image")
    d["moving_path"] = str(bad)
    sp.write_text(json.dumps(d))
    with pytest.raises(Exception):
        c2.load_session_from(str(sp))
    assert [(s.fixed, s.moving) for s in c2.slots] == before and c2.transform == "tps"
    with pytest.raises(RegistrationError):
        c2.set_transform("TPS ")


# ---- #6 panel follows a loaded session -------------------------------------- #
def test_load_session_resyncs_panel(app, images, tmp_path, monkeypatch):
    from qtpy.QtWidgets import QFileDialog
    from pymap.gui.main_window import MainWindow
    from pymap.model import PreAlign
    c = _solved(LandmarkController(), images, n=4)
    c.set_prealign(PreAlign(rot90=1, flip_h=True, flip_v=False))
    c.set_transform("tps")
    sp = tmp_path / "s.json"
    c.save_session_to(str(sp))
    win = MainWindow()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(sp), ""))
    win.load_session()
    p = win.panel
    assert p.cmb_tf.currentText() == "tps" and p._rot == 1 and p.cb_h.isChecked()
    p.cb_v.setChecked(True)                    # used to reset the loaded rotation
    assert win.c.prealign.rot90 == 1 and win.c.prealign.flip_h and win.c.prealign.flip_v


# ---- #8 channel sliders always reach the overlay ---------------------------- #
def test_keyboard_channel_change_is_committed(app, tmp_path, monkeypatch):
    from qtpy.QtTest import QTest
    from qtpy.QtWidgets import QSlider
    from pymap.gui.main_window import MainWindow
    tifffile = pytest.importorskip("tifffile")
    a = (np.random.default_rng(4).random((2, 30, 40)) * 4000).astype(np.uint16)
    p = tmp_path / "two.tif"
    tifffile.imwrite(str(p), a, imagej=True, metadata={"axes": "CYX"})
    win = MainWindow()
    win.c.load_moving(str(p))
    rewarps = []
    monkeypatch.setattr(win.c, "recompute", lambda: rewarps.append(1))
    before = win.c._moving
    sl = win.panel.chan_box.findChildren(QSlider)[0]
    sl.setValue(500)                       # keyboard / wheel / groove: no sliderReleased
    sl.setValue(520)
    assert rewarps == []                   # debounced, not on every tick
    QTest.qWait(600)
    assert win.c._confocal.channels[0].low == pytest.approx(0.52)
    assert win.c._moving is not before     # moving view re-merged
    assert rewarps == [1]                  # and the overlay re-warped once


# ---- #9 stale FIB metadata -------------------------------------------------- #
def test_open_fib_drops_metadata_of_another_image(app, images, tmp_path):
    from pymap.meta import FibImageMeta
    c = LandmarkController()
    c.meta, c._meta_source = FibImageMeta(image_width=1024, image_height=768,
                                          fib_pixel_size_m=1e-7), "record"
    c.load_fixed(images[0])
    assert c.meta is None
    mp = tmp_path / "m.json"
    FibImageMeta(image_width=100, image_height=80, fib_pixel_size_m=2e-7).save(str(mp))
    c.load_meta(str(mp))
    c.load_fixed(images[0])                    # same size, from a file: kept
    assert c.meta is not None and c.meta.fib_pixel_size_m == 2e-7


# ---- #10 degenerate leave-one-out names the pair --------------------------- #
def test_degenerate_loo_names_the_pair():
    from pymap.transform import loo_residual_vectors
    mv = [(0, 0), (10, 0), (20, 0), (5, 9)]            # 3 collinear + 1
    with pytest.raises(RegistrationError, match="without pair 4"):
        loo_residual_vectors(mv, [(x + 1, y + 2) for x, y in mv], "affine")


# ---- #11 zoom limits -------------------------------------------------------- #
def test_wheel_zoom_is_clamped(app):
    from pymap.gui.views import ZoomPanView, ZOOM_MIN, ZOOM_MAX

    class Ev:
        def __init__(self, y):
            self._y = y

        def angleDelta(self):
            return self

        def y(self):
            return self._y
    v = ZoomPanView()
    v.set_image(np.zeros((50, 60), np.float32))
    for _ in range(200):
        v.wheelEvent(Ev(120 * 5))
    assert v.transform().m11() == pytest.approx(ZOOM_MAX)
    for _ in range(400):
        v.wheelEvent(Ev(-120 * 5))
    assert v.transform().m11() == pytest.approx(ZOOM_MIN)
    v.wheelEvent(Ev(0))                                  # touchpad: no-op


# ---- #12 multi-image .lif --------------------------------------------------- #
class _Dims:
    def __init__(self, x, y, z=1, t=1, m=1):
        self.x, self.y, self.z, self.t, self.m = x, y, z, t, m

    def __repr__(self):
        return "Dims(%d,%d,%d,%d,%d)" % (self.x, self.y, self.z, self.t, self.m)


class _FakeImage:
    def __init__(self, name, dims, channels, value):
        self.name, self.dims, self.channels, self.scale = name, dims, channels, (2.0,)
        self._v = value

    def get_frame(self, z=0, t=0, c=0, m=0):
        return Image.fromarray(np.full((self.dims.y, self.dims.x), self._v + c, np.uint8))


class _FakeLif:
    IMAGES = [("preview", _Dims(16, 12), 1, 10), ("Series005", _Dims(40, 30, 3), 2, 50),
              ("TileScan", _Dims(20, 20, 1, 1, 4), 1, 90)]

    def __init__(self, path):
        self.image_list = [{"name": n, "dims": d, "channels": c} for n, d, c, _ in self.IMAGES]

    def get_image(self, i):
        n, d, c, v = self.IMAGES[i]
        return _FakeImage(n, d, c, v)


def test_lif_lists_images_and_opens_the_chosen_one(app, tmp_path, monkeypatch):
    pytest.importorskip("readlif")
    import readlif.reader
    from qtpy.QtWidgets import QFileDialog, QInputDialog
    from pymap.confocal import list_lif_images, import_confocal
    monkeypatch.setattr(readlif.reader, "LifFile", _FakeLif)
    p = str(tmp_path / "grid.lif")
    imgs = list_lif_images(p)
    assert [(i["name"], i["x"], i["z"], i["channels"], i["m"]) for i in imgs] == [
        ("preview", 16, 1, 1, 1), ("Series005", 40, 3, 2, 1), ("TileScan", 20, 1, 1, 4)]
    ci = import_confocal(p, series=2)
    assert "tile scan of 4 tiles" in ci.summary() and "image 3 of 3" in ci.summary()
    from pymap.gui.main_window import MainWindow
    win = MainWindow()
    offered = []

    def pick(parent, title, label, items, current, editable):
        offered.append((list(items), current))
        return items[1], True
    monkeypatch.setattr(QInputDialog, "getItem", pick)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (p, ""))
    win.open_moving()
    assert offered and offered[0][1] == 1                # the largest image is preselected
    assert win.c.moving_series == 1 and win.c._confocal.shape_hw() == (30, 40)
    assert len(win.c._confocal.channels) == 2
    sp = tmp_path / "s.json"
    win.c.save_session_to(str(sp))
    assert json.loads(sp.read_text())["moving_series"] == 1


def test_degenerate_landmarks_are_refused_clearly():
    from pymap.transform import fit_registration
    line = [(0, 0), (10, 0), (20, 0)]
    for kind in ("affine", "tps"):
        with pytest.raises(RegistrationError, match="degenerate"):
            fit_registration(line, [(x + 1, y) for x, y in line], kind)
    with pytest.raises(RegistrationError, match="same point"):
        fit_registration([(0, 0), (0, 0), (9, 1), (3, 8)],
                         [(1, 0), (2, 0), (10, 1), (4, 8)], "tps")
    with pytest.raises(RegistrationError, match="degenerate"):
        fit_registration([(5, 5)] * 3, [(6, 5)] * 3, "rigid")
    fit_registration(line, [(x + 1, y) for x, y in line], "similarity")   # fine: 2D not needed


# ---- second review (v0.9.2 verification) ------------------------------------ #
def test_only_the_overlay_view_keeps_images(app, images):
    from pymap.gui.main_window import MainWindow
    win = MainWindow()
    _solved(win.c, images)
    assert win.moving_view._pm_cache == [] and win.fixed_view._pm_cache == []
    assert win.overlay_view._pm_cache


def test_held_slider_rewarps_once_on_release(app, tmp_path, monkeypatch):
    from qtpy.QtTest import QTest
    from qtpy.QtWidgets import QSlider
    from pymap.gui.main_window import MainWindow
    tifffile = pytest.importorskip("tifffile")
    a = (np.random.default_rng(5).random((2, 30, 40)) * 4000).astype(np.uint16)
    p = tmp_path / "two.tif"
    tifffile.imwrite(str(p), a, imagej=True, metadata={"axes": "CYX"})
    win = MainWindow()
    win.c.load_moving(str(p))
    rewarps = []
    monkeypatch.setattr(win.c, "recompute", lambda: rewarps.append(1))
    sl = win.panel.chan_box.findChildren(QSlider)[0]
    sl.setSliderDown(True)                     # mouse held
    sl.setValue(300)
    QTest.qWait(500)                           # a pause mid-drag
    assert rewarps == []
    sl.setSliderDown(False)                    # emits sliderReleased
    assert rewarps == [1]
    sl.sliderReleased.emit()                   # a second release, nothing changed
    assert rewarps == [1]


def test_load_meta_refreshes_um_without_rewarp(app, images, tmp_path, monkeypatch):
    import pymap.gui.controller as ctl
    from pymap.meta import FibImageMeta
    c = _solved(LandmarkController(), images)
    assert c.qc()["rms_um"] is None
    mp = tmp_path / "m.json"
    FibImageMeta(image_width=100, image_height=80, fib_pixel_size_m=2e-7).save(str(mp))
    n = []
    real = ctl.register_and_warp
    monkeypatch.setattr(ctl, "register_and_warp", lambda *a, **k: (n.append(1), real(*a, **k))[1])
    c.load_meta(str(mp))
    assert c.qc()["rms_um"] is not None and n == []
    c.place_fixed(5.0, 5.0)                    # half a pair: still no re-warp
    assert n == []


def test_dropped_metadata_is_announced(app, images):
    from pymap.meta import FibImageMeta
    c = LandmarkController()
    c.meta, c._meta_source = FibImageMeta(image_width=1024, image_height=768,
                                          fib_pixel_size_m=1e-7), "record"
    c.load_fixed(images[0])
    notes = c.pop_notices()
    assert notes and "pyFIB record" in notes[0] and c.pop_notices() == []
