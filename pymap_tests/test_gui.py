import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyqt5")

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("qtpy")
pytest.importorskip("PyQt5")

from qtpy.QtWidgets import QApplication
from qtpy.QtCore import QPointF

from pymap.gui.theme import apply_theme
from pymap.gui.controller import LandmarkController
from pymap.gui.main_window import MainWindow
from pymap.gui.imageconv import ndarray_to_qimage
from pymap.model import PreAlign, TransformKind


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    yield a


@pytest.fixture
def imgs(tmp_path):
    fib = np.zeros((80, 100), np.float32)
    conf = np.zeros((80, 100), np.float32)
    marks = [(20, 15), (70, 18), (25, 60), (80, 55)]
    sh = (8.0, -4.0)
    for x, y in marks:
        fib[y, x] = 1.0
        conf[int(y - sh[1]), int(x - sh[0])] = 1.0
    fp = tmp_path / "fib.png"; Image.fromarray((fib * 255).astype(np.uint8)).save(fp)
    cp = tmp_path / "conf.png"; Image.fromarray((conf * 255).astype(np.uint8)).save(cp)
    return str(fp), str(cp), marks, sh


def test_imageconv_formats(app):
    assert ndarray_to_qimage(np.zeros((4, 5), np.float32)).width() == 5
    assert ndarray_to_qimage(np.zeros((4, 5, 3), np.float32)).height() == 4
    assert not ndarray_to_qimage(np.zeros((4, 5, 4), np.float32)).isNull()


def test_theme_applies(app):
    tag = apply_theme(app)
    # no external fib_dark_theme on the dev box -> vendored elymaker style
    assert tag == "elymaker-style (vendored)"
    assert app.styleSheet()  # QSS actually installed


def test_window_builds_and_registers(app, imgs):
    fp, cp, marks, sh = imgs
    overlays = []
    qcs = []
    win = MainWindow()
    win.c.overlayChanged.connect(lambda a: overlays.append(a))
    win.c.qcChanged.connect(lambda d: qcs.append(d))

    win.c.load_fixed(fp)
    win.c.load_moving(cp)

    # place 4 complete pairs (fixed then moving each), as the UI would
    for (x, y) in marks:
        win.c.place_fixed(float(x), float(y))
        win.c.place_moving(float(x - sh[0]), float(y - sh[1]))

    assert win.c._result is not None
    assert win.c._result.registration.rms < 1e-3
    # views received markers
    win._refresh_markers()
    assert len(win.fixed_view._markers) == 4
    assert len(win.moving_view._markers) == 4
    # overlay is RGB at FIB size
    assert overlays[-1].shape == (80, 100, 3)
    assert qcs[-1]["n"] == 4


def test_prealign_remap_keeps_registration(app, imgs):
    fp, cp, marks, sh = imgs
    c = LandmarkController()
    c.load_fixed(fp)
    c.load_moving(cp)
    for (x, y) in marks:
        c.place_fixed(float(x), float(y))
        c.place_moving(float(x - sh[0]), float(y - sh[1]))
    rms_before = c._result.registration.rms
    # rotate the moving image 90; points should remap and the fit stay valid
    c.set_prealign(PreAlign(rot90=1))
    assert c._result is not None
    assert c._result.registration.rms < max(1e-2, rms_before + 1e-2)
    # moving markers now live in the rotated frame (dims swapped: 100x80 -> 80x100)
    mm = c.moving_markers()
    assert all(0 <= m["x"] < 80 and 0 <= m["y"] < 100 for m in mm)


def test_transform_switch_and_export(app, imgs, tmp_path):
    fp, cp, marks, sh = imgs
    c = LandmarkController()
    c.load_fixed(fp)
    c.load_moving(cp)
    for (x, y) in marks:
        c.place_fixed(float(x), float(y))
        c.place_moving(float(x - sh[0]), float(y - sh[1]))
    c.set_transform(TransformKind.TPS)
    assert c._result.registration.kind == "tps"
    out = tmp_path / "export"
    rep = c.export_to(str(out))
    assert (out / "overlay.png").exists()
    assert (out / "confocal_in_fib.png").exists()
    assert (out / "session.json").exists()
    assert (out / "report.json").exists()
    # exported RGBA has an alpha channel at FIB size
    rgba = np.asarray(Image.open(out / "confocal_in_fib.png"))
    assert rgba.shape == (80, 100, 4)
    assert rep["transform"] == "tps"


def test_view_signals_drive_controller(app, imgs):
    fp, cp, marks, sh = imgs
    win = MainWindow()
    win.c.load_fixed(fp)
    win.c.load_moving(cp)
    # emulate clicks via the view signals the UI relies on
    for (x, y) in marks:
        win.fixed_view.pointAdded.emit(float(x), float(y))
        win.moving_view.pointAdded.emit(float(x - sh[0]), float(y - sh[1]))
    assert win.c._result is not None and win.c._result.registration.n == 4
    # emulate a drag on fixed point 0 then release-commit
    win.fixed_view.pointMoved.emit(0, 21.0, 16.0)
    win.fixed_view.dragFinished.emit()
    assert win.c.slots[0].fixed == (21.0, 16.0)


def _solved_controller(fp, cp, marks, sh):
    c = LandmarkController()
    c.load_fixed(fp)
    c.load_moving(cp)
    for (x, y) in marks:
        c.place_fixed(float(x), float(y))
        c.place_moving(float(x - sh[0]), float(y - sh[1]))
    return c


def test_target_maps_to_fib_and_um(app, imgs, tmp_path):
    from pymap.meta import FibImageMeta
    fp, cp, marks, sh = imgs
    c = _solved_controller(fp, cp, marks, sh)
    # a target at confocal feature (12,19) should map onto FIB (20,15)
    c.place_target(float(20 - sh[0]), float(15 - sh[1]))
    sol = c.target_solutions()[0]
    assert sol["fixed"] is not None
    assert abs(sol["fixed"][0] - 20) < 0.5 and abs(sol["fixed"][1] - 15) < 0.5
    assert sol["um"] is None   # no meta yet
    meta = FibImageMeta(image_width=100, image_height=80, fov_width_m=20e-6)
    m = tmp_path / "meta.json"; meta.save(str(m))
    c.load_meta(str(m))
    sol = c.target_solutions()[0]
    assert sol["um"] is not None


def test_mode_dispatch_targets_vs_landmarks(app, imgs):
    fp, cp, marks, sh = imgs
    win = MainWindow()
    win.c.load_fixed(fp)
    win.c.load_moving(cp)
    for (x, y) in marks:
        win.fixed_view.pointAdded.emit(float(x), float(y))
        win.moving_view.pointAdded.emit(float(x - sh[0]), float(y - sh[1]))
    win.c.set_mode("target")
    win.rb_check = None
    win.moving_view.pointAdded.emit(30.0, 30.0)   # should become a target now
    assert len(win.c.targets) == 1
    assert len(win.c.slots) == 4                  # unchanged
    # in target mode a fixed-pane click is ignored
    win.fixed_view.pointAdded.emit(5.0, 5.0)
    assert len(win.c.slots) == 4


def test_controller_export_handoff(app, imgs, tmp_path):
    import json
    from pymap.meta import FibImageMeta
    fp, cp, marks, sh = imgs
    c = _solved_controller(fp, cp, marks, sh)
    c.place_target(float(20 - sh[0]), float(15 - sh[1]))
    # no meta -> refuses
    with pytest.raises(Exception):
        c.export_handoff(str(tmp_path / "h1"))
    meta = FibImageMeta(image_width=100, image_height=80, fov_width_m=20e-6)
    mp = tmp_path / "meta.json"; meta.save(str(mp))
    c.load_meta(str(mp))
    rep = c.export_handoff(str(tmp_path / "h2"), ely_out="mill.ely")
    job = json.loads((tmp_path / "h2" / "job.json").read_text())
    assert job["out"] == "mill.ely" and job["center"] == "origin"
    assert rep["n_targets"] == 1
    assert (tmp_path / "h2" / "targets.json").exists()


def test_prealign_remaps_targets_too(app, imgs):
    from pymap.model import PreAlign
    fp, cp, marks, sh = imgs
    c = _solved_controller(fp, cp, marks, sh)
    c.place_target(float(20 - sh[0]), float(15 - sh[1]))
    fib_before = c.target_solutions()[0]["fixed"]
    c.set_prealign(PreAlign(rot90=1))
    fib_after = c.target_solutions()[0]["fixed"]
    # target still lands on the same FIB location after a prealign change
    assert abs(fib_after[0] - fib_before[0]) < 1.0
    assert abs(fib_after[1] - fib_before[1]) < 1.0


def test_compare_modes_render(app, imgs):
    fp, cp, marks, sh = imgs
    c = _solved_controller(fp, cp, marks, sh)
    seen = []
    c.overlayChanged.connect(lambda a: seen.append(a))
    for mode in ("swipe", "checker", "flicker", "blend"):
        c.set_compare(mode=mode)
        assert seen[-1].shape == (80, 100, 3)
    # swipe split changes the image
    c.set_compare(mode="swipe", split=0.2)
    a1 = seen[-1].copy()
    c.set_compare(split=0.8)
    assert not np.allclose(a1, seen[-1])


def test_residual_vectors_gated_and_populated(app, imgs):
    fp, cp, marks, sh = imgs
    c = _solved_controller(fp, cp, marks, sh)
    assert c.residual_vectors() == []          # off by default
    c.set_residuals(visible=True)
    rv = c.residual_vectors()
    assert len(rv) == 4
    for v in rv:                               # exact-affine data -> ~zero residual
        assert abs(v["mx"] - v["fx"]) < 1e-3 and abs(v["my"] - v["fy"]) < 1e-3


def test_overlay_view_draws_residual_items(app, imgs):
    fp, cp, marks, sh = imgs
    win = MainWindow()
    win.c.load_fixed(fp)
    win.c.load_moving(cp)
    for (x, y) in marks:
        win.fixed_view.pointAdded.emit(float(x), float(y))
        win.moving_view.pointAdded.emit(float(x - sh[0]), float(y - sh[1]))
    assert len(win.overlay_view._residual_items) == 0
    win.c.set_residuals(visible=True, scale=10.0)
    # 4 pairs -> line + dot each = 8 items
    assert len(win.overlay_view._residual_items) == 8
    win.c.set_residuals(visible=False)
    assert len(win.overlay_view._residual_items) == 0


def test_load_multichannel_confocal_sets_rgb(app, imgs, tmp_path):
    tiff = pytest.importorskip("tifffile")
    fp, cp, marks, sh = imgs
    # a 3-channel z-stack OME-TIFF at the FIB's dimensions (80x100)
    p = tmp_path / "conf.ome.tif"
    arr = (np.random.rand(5, 3, 80, 100) * 4000).astype(np.uint16)
    tiff.imwrite(str(p), arr, photometric="minisblack", metadata={"axes": "ZCYX"})
    win = MainWindow()
    win.c.load_fixed(fp)
    win.c.load_moving(str(p))
    assert win.c._confocal.is_multicolor()
    assert win.c.cmap == "rgb"                     # multicolor preserved
    assert win.c._moving.data.ndim == 3            # merged RGB moving image
    assert win.c._moving.data.shape == (80, 100, 3)
    assert "3 channel" in win.c._confocal.summary()


def test_per_channel_controls_and_edit(app, imgs, tmp_path):
    tiff = pytest.importorskip("tifffile")
    fp, cp, marks, sh = imgs
    p = tmp_path / "conf.ome.tif"
    arr = (np.random.rand(3, 2, 80, 100) * 3000).astype(np.uint16)
    tiff.imwrite(str(p), arr, photometric="minisblack", metadata={"axes": "ZCYX"})
    win = MainWindow()
    win.c.load_fixed(fp)
    win.c.load_moving(str(p))
    # channel panel built two rows
    assert not win.panel.chan_box.isHidden()
    assert win.panel.chan_layout.count() == 2
    infos = win.c.channel_infos()
    assert len(infos) == 2 and "low" in infos[0] and "high" in infos[0]
    # editing a channel's white point changes the merged moving image
    before = win.c._moving.data.copy()
    win.c.set_channel_param(1, high=0.1, commit=True)
    assert not np.allclose(before, win.c._moving.data)
    # excluding a channel drops it from the merge
    win.c.set_channel_param(1, include=False, commit=True)
    assert not win.c._confocal.is_multicolor()
