import numpy as np
import pytest

from pymap.meta import FibImageMeta


def test_pixel_size_derived_from_fov():
    m = FibImageMeta(image_width=1024, image_height=768, fov_width_m=20e-6)
    assert m.fib_pixel_size_m == pytest.approx(20e-6 / 1024)
    assert m.fov_height_m == pytest.approx(20e-6 / 1024 * 768)


def test_fov_derived_from_pixel_size():
    m = FibImageMeta(image_width=1000, image_height=1000, fib_pixel_size_m=1e-8)
    assert m.fov_width_m == pytest.approx(1e-5)


def test_inconsistent_metadata_rejected():
    with pytest.raises(ValueError):
        FibImageMeta(image_width=1024, image_height=768,
                     fov_width_m=20e-6, fib_pixel_size_m=5e-8)


def test_center_maps_to_origin():
    m = FibImageMeta(image_width=1024, image_height=768, fov_width_m=20e-6)
    x_m, y_m = m.px_to_fov_m(512, 384)
    assert x_m == pytest.approx(0.0)
    assert y_m == pytest.approx(0.0)


def test_y_axis_is_flipped_up_positive():
    m = FibImageMeta(image_width=100, image_height=100, fib_pixel_size_m=1.0)
    # a pixel ABOVE centre (smaller row) must have +Y
    _, y_top = m.px_to_fov_m(50, 10)
    _, y_bot = m.px_to_fov_m(50, 90)
    assert y_top > 0 and y_bot < 0


def test_px_fov_roundtrip():
    m = FibImageMeta(image_width=1024, image_height=768, fov_width_m=20e-6)
    for px in [(0, 0), (1023, 767), (100.5, 640.25), (512, 384)]:
        xm, ym = m.px_to_fov_m(*px)
        back = m.fov_m_to_px(xm, ym)
        np.testing.assert_allclose(back, px, atol=1e-9)


def test_extra_fields_carry_through():
    d = {"image_width": 512, "image_height": 512, "fib_pixel_size_m": 1e-8,
         "fib_objective_potential_v": 30000.0, "scan_rotation_deg": 12.0}
    m = FibImageMeta.from_dict(d)
    assert m.extra["fib_objective_potential_v"] == 30000.0
    assert m.extra["scan_rotation_deg"] == 12.0
