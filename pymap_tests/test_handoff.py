import json
import numpy as np
import pytest
from PIL import Image

from pymap.meta import FibImageMeta
from pymap.handoff import (write_handoff, HandoffJob, HANDOFF_SCHEMA,
                            target_layout_um)


def _meta():
    return FibImageMeta(image_width=100, image_height=80, fov_width_m=20e-6)


def test_job_schema_and_fields(tmp_path):
    meta = _meta()
    underlay = np.zeros((80, 100, 3), np.float32)
    rep = write_handoff(str(tmp_path), underlay, meta, ely_out="lam.ely",
                        targets_fib_px=[(50, 40), (10, 20)])
    job = json.loads((tmp_path / "job.json").read_text())
    assert job["schema"] == HANDOFF_SCHEMA
    assert job["center"] == "origin"
    assert job["pixel_size_m"] == pytest.approx(20e-6 / 100)
    assert job["image_width_px"] == 100 and job["image_height_px"] == 80
    assert job["out"] == "lam.ely"
    assert job["image"] == "overlay.png"
    assert (tmp_path / "overlay.png").exists()
    assert rep["n_targets"] == 2


def test_handoff_matches_elymaker_pixel_formula():
    # elymaker: layout_x_um = (px - W/2) * pixel_size_m * 1e6 ; y up
    meta = _meta()
    pix = meta.fib_pixel_size_m
    for (x, y) in [(50, 40), (0, 0), (100, 80), (73, 12)]:
        xu, yu = target_layout_um((x, y), meta)
        assert xu == pytest.approx((x - 100 / 2) * pix * 1e6)
        assert yu == pytest.approx((80 / 2 - y) * pix * 1e6)


def test_center_target_is_origin():
    meta = _meta()
    xu, yu = target_layout_um((50, 40), meta)
    assert xu == pytest.approx(0.0) and yu == pytest.approx(0.0)


def test_targets_sidecar_written(tmp_path):
    meta = _meta()
    underlay = np.zeros((80, 100, 3), np.float32)
    write_handoff(str(tmp_path), underlay, meta, targets_fib_px=[(60, 40)])
    side = json.loads((tmp_path / "targets.json").read_text())
    assert side["center"] == "origin"
    assert side["targets"][0]["fib_px"] == [60.0, 40.0]
    # 10 px right of centre -> +10*pix um
    assert side["targets"][0]["layout_um"][0] == pytest.approx(10 * meta.fib_pixel_size_m * 1e6)


def test_no_targets_no_sidecar(tmp_path):
    write_handoff(str(tmp_path), np.zeros((80, 100, 3), np.float32), _meta())
    assert not (tmp_path / "targets.json").exists()
    assert (tmp_path / "job.json").exists()
