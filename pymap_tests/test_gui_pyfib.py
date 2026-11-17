"""GUI paths used by pyFIB: overview from a positions file, targets file, embedding."""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyqt5")

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("qtpy")
pytest.importorskip("PyQt5")

from qtpy.QtWidgets import QApplication, QWidget
from qtpy.QtCore import Qt

from pymap import api
from pymap.gui.controller import LandmarkController
from pymap.transform import RegistrationError

MARKS = [(20, 15), (70, 18), (25, 60), (80, 55), (50, 35)]
SHIFT = (8.0, -4.0)
PX = 2.7905e-07


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pyfib_session(tmp_path):
    session = tmp_path / "session_20261010"
    clem = session / "clem"
    (clem / "images").mkdir(parents=True)
    fib = np.zeros((80, 100), np.uint8)
    conf = np.zeros((80, 100), np.uint8)
    for x, y in MARKS:
        fib[y, x] = 255
        conf[int(y - SHIFT[1]), int(x - SHIFT[0])] = 255
    Image.fromarray(fib).save(clem / "images" / "clem_ov_grid1_1.bmp")
    Image.fromarray(conf).save(tmp_path / "confocal.png")

    def rec(name, rot=0.0):
        return {"name": name, "captured_unix_s": 1791580000.0, "fib_magnification_x": 400.0,
                "fib_objective_potential_v": 15000.0,
                "stage_at": {"x_m": 0.0446, "y_m": 0.0512, "z_m": 0.0295, "r_deg": 12.0,
                             "m_m": 0.013, "t_deg": 30.0},
                "image_bmp": "images\\clem_ov_grid1_1.bmp", "image_width_px": 100,
                "image_height_px": 80, "fov_width_m": PX * 100, "fov_height_m": PX * 80,
                "fib_pixel_size_m": PX, "fib_scan_rotation_deg": rot,
                "fib_beam_shift_x_m": 0.0, "fib_beam_shift_y_m": 0.0}
    pf = clem / "overview_positions.json"
    pf.write_text(json.dumps({"schema": "smartfib_executor.positions.v2", "created_unix_s": 0,
                              "positions": {"clem_ov_grid1_1": rec("clem_ov_grid1_1"),
                                            "rotated": rec("rotated", rot=0.2)}}))
    return session, pf, str(tmp_path / "confocal.png")


def _solved(app, pf, conf, n_pairs=5, session=None):
    c = LandmarkController()
    c.session_dir = str(session) if session else None
    c.load_fixed_from_record(str(pf), "clem_ov_grid1_1")
    c.load_moving(conf)
    for x, y in MARKS[:n_pairs]:
        c.place_fixed(float(x), float(y))
        c.place_moving(float(x - SHIFT[0]), float(y - SHIFT[1]))
    return c


def test_overview_from_record_uses_its_geometry(app, pyfib_session):
    session, pf, conf = pyfib_session
    c = _solved(app, pf, conf)
    assert c.meta.fib_pixel_size_m == pytest.approx(PX)
    assert c.fixed_source["name"] == "clem_ov_grid1_1"
    q = c.qc()
    assert q["loo_rms_grid_um"] is not None and q["loo_rms_grid_um"] < 0.01
    assert q["squash_ratio"] == pytest.approx(1.0, abs=1e-6)     # pure shift


def test_overview_record_refusals(app, pyfib_session):
    session, pf, conf = pyfib_session
    c = LandmarkController()
    with pytest.raises(RegistrationError, match="scan rotation"):
        c.load_fixed_from_record(str(pf), "rotated")
    with pytest.raises(RegistrationError, match="no position"):
        c.load_fixed_from_record(str(pf), "missing")


def test_write_targets_file_round_trip(app, pyfib_session):
    session, pf, conf = pyfib_session
    c = _solved(app, pf, conf, session=session)
    c.set_mode("target")
    c.place_target(30.0 - SHIFT[0], 40.0 - SHIFT[1])      # confocal px -> FIB (30, 40)
    c.place_target(60.0 - SHIFT[0], 20.0 - SHIFT[1])
    written = []
    c.targetsWritten.connect(written.append)
    path = c.write_targets_file()
    assert written == [path]
    assert path == str(session / "clem" / "targets_clem_ov_grid1_1.json")
    doc = api.read_targets(path)                        # ready marker checked
    assert [t["id"] for t in doc["targets"]] == ["T1", "T2"]
    assert doc["targets"][0]["fib_px"] == pytest.approx([30.0, 40.0], abs=1e-3)
    assert doc["targets"][0]["layout_um"] == pytest.approx(
        list(api.layout_um((30.0, 40.0), 100, 80, PX)), abs=1e-3)
    src = doc["sources"][0]
    assert src["image"] == "images/clem_ov_grid1_1.bmp"
    assert src["positions_file"] == "overview_positions.json"
    rec = api.load_positions(pf)["clem_ov_grid1_1"]
    assert api.check_source_against_record(src, rec, targets_dir=session / "clem") == []
    step = doc["registration"]["chain"][0]
    assert step["from"] == "confocal" and step["to"] == "fib:clem_ov_grid1_1"
    assert doc["registration"]["combined_loo_rms_grid_um"] is not None
    assert (session / "clem" / "pymap_session_clem_ov_grid1_1.json").exists()


def test_write_targets_refusals(app, pyfib_session, tmp_path):
    session, pf, conf = pyfib_session
    # no pyFIB record behind the fixed image
    c = LandmarkController()
    c.load_fixed(str(session / "clem" / "images" / "clem_ov_grid1_1.bmp"))
    c.load_moving(conf)
    with pytest.raises(RegistrationError, match="pyFIB positions file"):
        c.build_targets_doc(str(session))
    # too few pairs to measure the error (affine needs 4 for leave-one-out)
    c = _solved(app, pf, conf, n_pairs=3, session=session)
    c.place_target(10.0, 10.0)
    with pytest.raises(RegistrationError, match="leave-one-out"):
        c.write_targets_file()
    # no targets
    c = _solved(app, pf, conf, session=session)
    with pytest.raises(RegistrationError, match="no targets"):
        c.write_targets_file()
    # more than 30
    for k in range(31):
        c.place_target(5.0 + k, 5.0)
    with pytest.raises(RegistrationError, match="at most 30"):
        c.write_targets_file()


def test_large_registration_error_is_flagged(app, pyfib_session):
    session, pf, conf = pyfib_session
    c = LandmarkController()
    c.session_dir = str(session)
    c.load_fixed_from_record(str(pf), "clem_ov_grid1_1")
    c.meta.fib_pixel_size_m = 1e-6                       # 1 um/px to make errors large
    c.load_moving(conf)
    rng = np.random.default_rng(4)
    for x, y in MARKS:
        c.place_fixed(float(x), float(y))
        jx, jy = rng.normal(0, 3.0, 2)
        c.place_moving(float(x - SHIFT[0] + jx), float(y - SHIFT[1] + jy))
    c.place_target(30.0, 30.0)
    doc = c.build_targets_doc()
    worst = doc["registration"]["combined_loo_rms_grid_um"]
    assert worst > 1.0
    assert any("exceeds" in w for w in doc["targets"][0]["warnings"])


def test_build_widget_embeds_and_calls_back(app, pyfib_session):
    from pymap.gui.embed import build_widget
    session, pf, conf = pyfib_session
    got = []
    host = QWidget()
    w = build_widget(host, session_dir=str(session), on_targets_written=got.append)
    assert isinstance(w, QWidget) and w.parent() is host
    assert not w.isWindow()                    # a child widget, not a top-level window
    assert w.positions_file == str(session / "clem" / "overview_positions.json")
    c = w.c
    c.load_fixed_from_record(w.positions_file, "clem_ov_grid1_1")
    c.load_moving(conf)
    for x, y in MARKS:
        c.place_fixed(float(x), float(y))
        c.place_moving(float(x - SHIFT[0]), float(y - SHIFT[1]))
    c.place_target(20.0, 20.0)
    w.write_targets()                                    # the toolbar action
    assert got == [str(session / "clem" / "targets_clem_ov_grid1_1.json")]
    assert api.ready_marker_ok(got[0]) is True


def test_qc_panel_shows_um_and_squash(app, pyfib_session):
    from pymap.gui.main_window import MainWindow
    session, pf, conf = pyfib_session
    win = MainWindow(session_dir=str(session))
    win.c.load_fixed_from_record(str(pf), "clem_ov_grid1_1")
    win.c.load_moving(conf)
    for x, y in MARKS:
        win.c.place_fixed(float(x), float(y))
        win.c.place_moving(float(x - SHIFT[0]), float(y - SHIFT[1]))
    assert "um" in win.panel.lbl_loo.text()
    assert win.panel.lbl_grid.text().startswith("Grid error:") and "um" in win.panel.lbl_grid.text()
    assert win.panel.lbl_squash.text() == "Squash: 1.00"


# ---- v0.9.1: robustness for pyFIB (review revision 6) ---------------------- #
def _solve_in(c, conf):
    c.load_moving(conf)
    for x, y in MARKS:
        c.place_fixed(float(x), float(y))
        c.place_moving(float(x - SHIFT[0]), float(y - SHIFT[1]))


def test_file_handlers_report_errors_instead_of_raising(app, pyfib_session, tmp_path,
                                                        monkeypatch):
    from qtpy.QtWidgets import QFileDialog, QMessageBox
    from pymap.gui.main_window import MainWindow
    session, pf, conf = pyfib_session
    bad = tmp_path / "broken.tif"
    bad.write_bytes(b"not an image at all")
    badjson = tmp_path / "broken.json"
    badjson.write_text("{ not json")
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[1]))
    win = MainWindow(session_dir=str(session))
    for slot, path in [(win.open_moving, bad), (win.open_fixed, bad),
                       (win.load_meta, badjson), (win.load_session, badjson)]:
        monkeypatch.setattr(QFileDialog, "getOpenFileName",
                            lambda *a, _p=str(path), **k: (_p, ""))
        slot()                                          # must not raise
    assert shown == ["Open confocal", "Open FIB", "Load FIB meta", "Load session"]
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(tmp_path / "no_such_dir" / "s.json"), ""))
    win.save_session()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path))
    win.export()                                        # nothing solved -> refusal
    assert shown[-2:] == ["Save session", "Export"]


def test_widget_survives_reparenting_to_own_window_and_back(app, pyfib_session):
    from qtpy.QtWidgets import QVBoxLayout
    from pymap.gui.embed import build_widget
    session, pf, conf = pyfib_session
    got = []
    host = QWidget()
    lay = QVBoxLayout(host)
    w = build_widget(host, session_dir=str(session), on_targets_written=got.append)
    lay.addWidget(w)
    w.c.load_fixed_from_record(w.positions_file, "clem_ov_grid1_1")
    w.setParent(None)                                   # "Open pyMap in own window"
    w.show()
    assert w.isWindow()
    _solve_in(w.c, conf)
    w.c.place_target(20.0, 20.0)
    lay.addWidget(w)                                    # back into the tab
    assert w.parent() is host and not w.isWindow()
    w.write_targets()
    assert got and api.ready_marker_ok(got[0]) is True
    assert w.dock.widget().minimumWidth() >= 260
    assert any(a.text() == "Controls" for a in w.findChildren(type(w.dock.toggleViewAction())))


def test_set_session_unlinks_overview_from_other_session(app, pyfib_session, tmp_path):
    from pymap.gui.main_window import MainWindow
    session, pf, conf = pyfib_session
    win = MainWindow(session_dir=str(session))
    win.c.load_fixed_from_record(str(pf), "clem_ov_grid1_1")
    win.set_session(str(session))                       # same session: link kept
    assert win.c.fixed_source is not None
    new = tmp_path / "session_new"
    win.set_session(str(new))
    assert win.positions_file == str(new / "clem" / "overview_positions.json")
    assert win.c.fixed_source is None
    _solve_in(win.c, conf)
    win.c.place_target(20.0, 20.0)
    with pytest.raises(RegistrationError, match="pyFIB positions file"):
        win.c.write_targets_file()
    assert not (new / "clem").exists()


def test_targets_refused_for_overview_of_another_session(app, pyfib_session, tmp_path):
    session, pf, conf = pyfib_session
    c = _solved(app, pf, conf, session=tmp_path / "other_session")
    c.place_target(20.0, 20.0)
    with pytest.raises(RegistrationError, match="not part of this session"):
        c.write_targets_file()


def test_session_file_relinks_pyfib_overview(app, pyfib_session, tmp_path):
    session, pf, conf = pyfib_session
    c = _solved(app, pf, conf, session=session)
    sp = tmp_path / "s.json"
    c.save_session_to(str(sp))
    c2 = LandmarkController()
    c2.session_dir = str(session)
    assert c2.load_session_from(str(sp)) == []
    assert c2.fixed_source["name"] == "clem_ov_grid1_1"
    c2.place_target(20.0, 20.0)
    assert api.read_targets(c2.write_targets_file())["targets"]
    # a session without the link clears a stale link instead of keeping it
    d = json.loads(sp.read_text())
    d.pop("pyfib_source"); d["fixed_path"] = ""
    sp.write_text(json.dumps(d))
    notes = c2.load_session_from(str(sp))
    assert c2.fixed_source is None and any("link cleared" in n for n in notes)


def test_stale_channel_row_is_ignored(app, pyfib_session):
    session, pf, conf = pyfib_session
    c = _solved(app, pf, conf)
    c.set_channel_param(5, low=0.5)                     # only 1 channel: no IndexError
    c.set_channel_param(-1, low=0.5)
    assert c._confocal.channels[0].low != 0.5


def test_standalone_excepthook_shows_a_message(app, monkeypatch, capsys):
    import sys
    from qtpy.QtWidgets import QMessageBox
    from pymap.gui import app as gui_app
    shown = []
    monkeypatch.setattr(QMessageBox, "exec_", lambda self: shown.append(self.text()))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)      # restored afterwards
    hook = gui_app.install_excepthook()
    try:
        raise ValueError("boom")
    except ValueError:
        hook(*sys.exc_info())
    assert shown == ["ValueError: boom"]
    assert "Traceback" in capsys.readouterr().err
