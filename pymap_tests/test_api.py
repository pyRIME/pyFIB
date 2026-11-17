"""pymap.api: the file-level contract with pyFIB (no Qt, no scikit-image)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pymap import api

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# fixtures: a fake pyFIB session
# --------------------------------------------------------------------------- #
def _texture(h=768, w=1024, seed=0):
    rng = np.random.default_rng(seed)
    small = rng.random((h // 8, w // 8)).astype(np.float32)
    big = Image.fromarray(small).resize((w, h), Image.Resampling.BICUBIC)
    a = np.asarray(big, dtype=np.float64)
    a = (a - a.min()) / (a.max() - a.min())
    return (a * 200 + 20).astype(np.uint8)


def _record(name, image_rel, *, w=1024, h=768, px=2.7905e-07, t=30.0, r=12.0,
            rot=0.0, bsx=0.0, bsy=0.0, cap=1791580000.0):
    return {"name": name, "captured_unix_s": cap, "fib_magnification_x": 400.0,
            "fib_objective_potential_v": 15000.0,
            "stage_at": {"x_m": 0.0446, "y_m": 0.0512, "z_m": 0.0295, "r_deg": r,
                         "m_m": 0.013, "t_deg": t},
            "image_bmp": image_rel, "image_width_px": w, "image_height_px": h,
            "fov_width_m": px * w, "fov_height_m": px * h, "fib_pixel_size_m": px,
            "fib_scan_rotation_deg": rot, "fib_beam_shift_x_m": bsx,
            "fib_beam_shift_y_m": bsy}


@pytest.fixture
def session(tmp_path):
    clem = tmp_path / "clem"
    (clem / "images").mkdir(parents=True)
    Image.fromarray(_texture()).save(clem / "images" / "clem_ov_grid1_1.bmp")
    rec = _record("clem_ov_grid1_1", "images\\clem_ov_grid1_1.bmp")   # Windows-style
    pf = clem / "overview_positions.json"
    pf.write_text(json.dumps({"schema": "smartfib_executor.positions.v2",
                              "created_unix_s": 0, "microscope_serial": None,
                              "positions": {rec["name"]: rec}}), encoding="utf-8")
    return tmp_path, clem, pf, rec


# --------------------------------------------------------------------------- #
# import isolation
# --------------------------------------------------------------------------- #
def test_import_pymap_api_pulls_in_nothing_heavy():
    code = ("import sys; sys.path.insert(0, %r); import pymap.api, pymap; "
            "heavy=[m for m in ('numpy','PIL','skimage','scipy','qtpy','PyQt5','tifffile',"
            "'readlif') if m in sys.modules]; print(','.join(heavy) or 'none')" % str(ROOT))
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == "none"


def test_lazy_top_level_names_still_work():
    import pymap
    assert pymap.TransformKind.AFFINE == "affine"
    assert callable(pymap.fit_registration)
    assert pymap.__version__ == "0.9.2"
    with pytest.raises(AttributeError):
        pymap.no_such_name


# --------------------------------------------------------------------------- #
# records and paths
# --------------------------------------------------------------------------- #
def test_windows_paths_normalised(session):
    _root, clem, pf, rec = session
    assert api.normalise_rel_path("images\\position_10.bmp") == "images/position_10.bmp"
    assert api.normalise_rel_path("./a/b.bmp") == "a/b.bmp"
    p = api.record_image_path(rec, pf)
    assert p.exists() and p.name == "clem_ov_grid1_1.bmp"


def test_load_positions_refuses_other_schemas(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"schema": "something.else", "positions": {}}))
    with pytest.raises(api.PyMapApiError):
        api.load_positions(f)


def test_check_overview_record_limits():
    ok_err, ok_warn = api.check_overview_record(_record("a", "x.bmp"))
    assert ok_err == [] and ok_warn == []
    err, _ = api.check_overview_record(_record("a", "x.bmp", rot=0.1))
    assert any("scan rotation" in e for e in err)
    err, _ = api.check_overview_record(_record("a", "x.bmp", bsx=0.02e-6))
    assert any("beam shift X" in e for e in err)
    v1 = _record("a", "x.bmp"); v1["fib_pixel_size_m"] = None
    err, _ = api.check_overview_record(v1)
    assert any("v1 record" in e for e in err)
    _, warn = api.check_overview_record(_record("a", "x.bmp", t=2.0))
    assert any("not SEM normal" in w for w in warn)
    _, warn = api.check_overview_record(_record("a", "x.bmp", r=40.0))
    assert any("rotation" in w for w in warn)


def test_fib_source_paths_relative_to_targets_dir(session):
    _root, clem, pf, rec = session
    src = api.fib_source_from_record(rec, positions_file=pf, targets_dir=clem)
    assert src["kind"] == "fib_position" and src["id"] == "ov1"
    assert src["image"] == "images/clem_ov_grid1_1.bmp"
    assert src["positions_file"] == "overview_positions.json"
    assert src["image_sha256"].startswith("sha256:")
    assert src["stage_at"]["t_deg"] == 30.0 and set(src["stage_at"]) == {
        "x_m", "y_m", "z_m", "t_deg", "r_deg"}
    bad = dict(rec, fib_scan_rotation_deg=1.0)
    with pytest.raises(api.PyMapApiError):
        api.fib_source_from_record(bad, positions_file=pf, targets_dir=clem)


def test_layout_um_matches_meta_convention():
    from pymap.meta import FibImageMeta
    m = FibImageMeta(image_width=1024, image_height=768, fib_pixel_size_m=2.7905e-07)
    for px in [(612.4, 301.8), (0, 0), (1024, 768), (512, 384)]:
        xu, yu = api.layout_um(px, 1024, 768, 2.7905e-07)
        xm, ym = m.px_to_fov_m(*px)
        assert xu == pytest.approx(xm * 1e6) and yu == pytest.approx(ym * 1e6)
    assert api.layout_um((612.4, 301.8), 1024, 768, 2.7905e-07) == pytest.approx((28.02, 22.94), abs=0.01)


# --------------------------------------------------------------------------- #
# targets files
# --------------------------------------------------------------------------- #
def _doc(clem, pf, rec, n=2):
    src = api.fib_source_from_record(rec, positions_file=pf, targets_dir=clem)
    targets = [{"id": "T%d" % (i + 1), "label": str(i + 1), "source": "ov1",
                "fib_px": [100.0 + 10 * i, 200.0], "layout_um": [0.0, 0.0],
                "warnings": [], "note": ""} for i in range(n)]
    reg = {"chain": [{"from": "confocal", "to": "fib:" + rec["name"], "kind": "affine",
                      "n_pairs": 5, "rms_um": 0.3, "loo_rms_um": 0.5, "loo_rms_grid_um": 0.8}],
           "combined_loo_rms_um": 0.5, "combined_loo_rms_grid_um": 0.8}
    return api.new_targets_doc(sources=[src], targets=targets,
                               confocal={"file": "c.tif", "channels": ["Ch1"]},
                               registration=reg)


def test_targets_round_trip_with_ready_marker(session):
    _root, clem, pf, rec = session
    path = clem / api.targets_filename(rec["name"])
    assert path.name == "targets_clem_ov_grid1_1.json"
    api.write_targets(path, _doc(clem, pf, rec))
    assert api.ready_marker_path(path).exists()
    assert api.ready_marker_ok(path) is True
    doc = api.read_targets(path)
    assert doc["schema"] == "pymap.targets.v2" and len(doc["targets"]) == 2
    assert doc["pixel_convention"].startswith("corner")


def test_read_targets_refuses_edited_or_unmarked_files(session):
    _root, clem, pf, rec = session
    path = api.write_targets(clem / "t.json", _doc(clem, pf, rec))
    path.write_text(path.read_text().replace("100.0", "101.0"))
    assert api.ready_marker_ok(path) is False
    with pytest.raises(api.PyMapApiError):
        api.read_targets(path)
    os.remove(api.ready_marker_path(path))
    with pytest.raises(api.PyMapApiError):
        api.read_targets(path)
    assert api.read_targets(path, require_ready=False)["targets"][0]["fib_px"][0] == 101.0


def test_validation_catches_bad_documents(session):
    _root, clem, pf, rec = session
    d = _doc(clem, pf, rec, n=31)
    assert any("at most 30" in e for e in api.validate_targets_doc(d))
    d = _doc(clem, pf, rec); d["targets"][0]["source"] = "nope"
    assert any("unknown source" in e for e in api.validate_targets_doc(d))
    d = _doc(clem, pf, rec); d["targets"][1]["fib_px"] = [1.0]
    assert any("fib_px" in e for e in api.validate_targets_doc(d))
    d = _doc(clem, pf, rec); d["sources"][0]["kind"] = "sem_tiff"
    assert any("unknown kind" in e for e in api.validate_targets_doc(d))
    with pytest.raises(api.PyMapApiError):
        api.write_targets(clem / "bad.json", _doc(clem, pf, rec, n=31))


def test_source_identity_check(session):
    _root, clem, pf, rec = session
    src = api.fib_source_from_record(rec, positions_file=pf, targets_dir=clem)
    assert api.check_source_against_record(src, rec, targets_dir=clem) == []
    recap = dict(rec, captured_unix_s=rec["captured_unix_s"] + 60)
    assert any("captured" in m for m in api.check_source_against_record(src, recap, targets_dir=clem))
    Image.fromarray(_texture(seed=5)).save(clem / "images" / "clem_ov_grid1_1.bmp")
    assert any("hash" in m for m in api.check_source_against_record(src, rec, targets_dir=clem))


# --------------------------------------------------------------------------- #
# SEM overviews (pyFIB-owned file)
# --------------------------------------------------------------------------- #
def test_load_sem_overviews(tmp_path):
    f = tmp_path / "sem_overviews.json"
    f.write_text(json.dumps({"schema": "pyfib.sem_overviews.v1", "overviews": [
        {"name": "sem_grid1_200x", "image": "images\\sem_grid1_200x.bmp",
         "image_width_px": 1024, "image_height_px": 768, "field_width_m": 571.5e-6}]}))
    e = api.load_sem_overviews(f)[0]
    assert e["pixel_size_m"] == pytest.approx(571.5e-6 / 1024)
    assert e["pixel_size_nominal"] is True
    assert e["image"] == "images/sem_grid1_200x.bmp"
    f.write_text(json.dumps({"schema": "nope"}))
    with pytest.raises(api.PyMapApiError):
        api.load_sem_overviews(f)


# --------------------------------------------------------------------------- #
# locate_targets: pyMap finds, pyFIB moves
# --------------------------------------------------------------------------- #
OV_PX = 2.7905e-07          # 400x, 1024 px
SITE_PX = OV_PX / 5.0       # ~2000x


def _site_from_overview(ov, x0, y0, w=1024, h=768, scale=0.2, databar=False):
    box = (x0, y0, x0 + w * scale, y0 + h * scale)
    im = Image.fromarray(ov.astype(np.float32)).resize((w, h), Image.Resampling.BICUBIC, box=box)
    a = np.clip(np.asarray(im), 0, 255)
    rng = np.random.default_rng(1)
    a = np.clip(a + rng.normal(0, 4, a.shape), 0, 255)
    if databar:
        a[h - 60:h - 58, :] = 255          # bright separator
        a[h - 58:, :] = 30                 # bar body
    return a


@pytest.mark.parametrize("databar", [False, True])
def test_locate_targets_on_textured_overview(databar):
    ov = _texture().astype(np.float64)
    t1 = (530.0, 410.0)                    # target pyFIB centred on
    t2 = (560.0, 380.0)                    # a neighbour in the same field
    err = (9.3, -6.1)                      # stage landed ~2.6 um off
    x0 = t1[0] + err[0] - 1024 * 0.2 / 2
    y0 = t1[1] + err[1] - 768 * 0.2 / 2
    site = _site_from_overview(ov, x0, y0, databar=databar)
    src = {"id": "ov1", "fib_pixel_size_m": OV_PX}
    rec = {"fib_pixel_size_m": SITE_PX, "image_width_px": 1024, "image_height_px": 768}
    targets = [{"id": "T1", "source": "ov1", "fib_px": list(t1)},
               {"id": "T2", "source": "ov1", "fib_px": list(t2)},
               {"id": "X", "source": "other", "fib_px": [0, 0]}]
    out = api.locate_targets(src, rec, targets, overview_image=ov, site_image=site)
    assert [o.id for o in out] == ["T1", "T2"]
    for o, t in zip(out, (t1, t2)):
        exp = ((t[0] - x0) / 0.2, (t[1] - y0) / 0.2)
        assert abs(o.x_px - exp[0]) < 2.0 and abs(o.y_px - exp[1]) < 2.0, (o, exp)
        assert o.score > 0.5
    ident, x, y, score = out[0]           # unpacks as a plain 4-tuple
    assert ident == "T1"


def test_locate_flags_a_periodic_grid_as_ambiguous():
    yy, xx = np.mgrid[0:768, 0:1024]
    ov = np.where((xx % 50 < 6) | (yy % 50 < 6), 220.0, 40.0)
    ov += np.random.default_rng(3).normal(0, 2, ov.shape)
    t1 = (525.0, 400.0)
    x0, y0 = t1[0] - 102.4, t1[1] - 76.8
    site = _site_from_overview(ov, x0, y0)
    m = api.locate_site(ov, OV_PX, site, SITE_PX, expected_centre=t1, search_radius_px=102)
    assert m.peak > 0.5
    assert m.score < 0.15                 # the grid matches one square over just as well


def test_locate_refuses_site_larger_than_overview():
    ov = _texture(h=100, w=120).astype(np.float64)
    site = _texture(h=768, w=1024).astype(np.float64)
    with pytest.raises(api.PyMapApiError):
        api.locate_site(ov, OV_PX, site, OV_PX)
