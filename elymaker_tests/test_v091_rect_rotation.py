"""Rotated RECTs as SmartFIB places them (pyFIB patch 0.9.1+pyfib.1, 2026-10-20).

MEASURED in SmartFIB (Crossbeam 550): ``fixtures_pyfib/pyFIB_rotation_test.ely`` holds two
12 x 2 um bars at 30 deg (test A: anchor (-19, -1), height +2; test B: anchor (7, 1),
height -2) plus small unrotated marker squares at each bar's unrotated corners and
centre. Opened in SmartFIB, both bars pass through their CENTRE markers and rise to
the right: rotation is about the box centre, counter-clockwise for a positive angle.
``fixtures_pyfib/pyFIB_waffle.ely``: the two 270 deg hinge slots were milled (8 sites,
2026-10-16) at x -9.49..-7.49 um, y -0.45..-0.25 and +0.39..+0.59 um.

Before the patch elymaker drew rotated RECTs clockwise about the anchor (test A's far
end ~10 um from where SmartFIB puts it).
"""

import math
import os

import pytest

from elymaker.geometry import rect_center, rect_corners
from elymaker.model import Layer, LayerReference, Layout, Rect, Structure
from elymaker.reader import read_ely

HERE = os.path.dirname(__file__)
# kept out of fixtures/ (test_roundtrip counts the 12 example files there)
ROT_TEST = os.path.join(HERE, "fixtures_pyfib", "pyFIB_rotation_test.ely")
WAFFLE = os.path.join(HERE, "fixtures_pyfib", "pyFIB_waffle.ely")


def _R(**kw):
    kw.setdefault("dose_factor", 1.0)
    kw.setdefault("z_extent_m", 1.0e-6)
    return Rect(**kw)


def _rects(path):
    lay = read_ely(path)
    return [s for st in lay.structures for lr in st.layer_refs for s in lr.shapes
            if isinstance(s, Rect)]


def _rotated(path):
    return [r for r in _rects(path) if r.angle_deg % 360]


def _close(a, b, tol=1e-9):
    return all(abs(p - q) <= tol for pa, pb in zip(a, b) for p, q in zip(pa, pb))


# ------------------------------------------------------------------ the rule
def test_rotation_test_bars_turn_ccw_about_their_centre():
    bars = _rotated(ROT_TEST)
    assert len(bars) == 2
    for r in bars:
        cx, cy = rect_center(r.x, r.y, r.width, r.height)
        pts = rect_corners(r.x, r.y, r.width, r.height, r.angle_deg)
        # the pivot is the centre: the corners average to it
        assert abs(sum(p[0] for p in pts) / 4 - cx) < 1e-9
        assert abs(sum(p[1] for p in pts) / 4 - cy) < 1e-9
        # counter-clockwise: the right-hand end of the bar is ABOVE the centre
        right = max(pts, key=lambda p: p[0])
        assert right[1] > cy + 2.0
        # the long axis runs at +30 deg
        p0, p1 = pts[0], pts[1]
        assert abs(math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0])) - 30.0) < 1e-9


def test_rotation_test_exact_corners():
    a, b = sorted(_rotated(ROT_TEST), key=lambda r: r.x)
    # test A: anchor (-19,-1), w 12, h +2 -> centre (-13, 0)
    want_a = [(-17.696152, -3.866025), (-7.303848, 2.133975),
              (-8.303848, 3.866025), (-18.696152, -2.133975)]
    # test B: anchor (7,1), w 12, h -2 -> centre (13, 0)
    want_b = [(7.303848, -2.133975), (17.696152, 3.866025),
              (18.696152, 2.133975), (8.303848, -3.866025)]
    assert _close(rect_corners(a.x, a.y, a.width, a.height, a.angle_deg), want_a, 1e-5)
    assert _close(rect_corners(b.x, b.y, b.width, b.height, b.angle_deg), want_b, 1e-5)


def test_zero_angle_is_the_plain_box():
    assert _close(rect_corners(2.0, 3.0, 4.0, -1.5, 0.0),
                  [(2.0, 3.0), (6.0, 3.0), (6.0, 1.5), (2.0, 1.5)])


def test_waffle_hinge_slots_land_where_they_were_milled():
    slots = sorted(_rotated(WAFFLE), key=lambda r: r.y)
    assert len(slots) == 2 and all(r.angle_deg == 270.0 for r in slots)
    measured = [(-9.49, -7.49, -0.45, -0.25), (-9.48, -7.48, 0.39, 0.59)]
    for r, (x0, x1, y0, y1) in zip(slots, measured):
        pts = rect_corners(r.x, r.y, r.width, r.height, r.angle_deg)
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        assert abs(min(xs) - x0) < 0.01 and abs(max(xs) - x1) < 0.01
        assert abs(min(ys) - y0) < 0.01 and abs(max(ys) - y1) < 0.01


# ------------------------------------------------------------------ users of the rule
def test_validate_extent_and_preview_bounds_use_the_rotated_corners():
    from elymaker.preview import _bounds
    from elymaker.validate import _extent
    r = _rotated(ROT_TEST)[0]
    pts = rect_corners(r.x, r.y, r.width, r.height, r.angle_deg)
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    assert _close([_extent(r)], [(min(xs), max(xs), min(ys), max(ys))])
    lay = Layout(name="t", layers=[Layer(name="L")],
                 structures=[Structure(name="S", layer_refs=[
                     LayerReference(ref="L", shapes=[r])])])
    assert _close([_bounds(lay)], [(min(xs), min(ys), max(xs), max(ys))])


def test_preview_draws_the_bar_rising_to_the_right():
    import re
    from elymaker.preview import to_svg
    r = _rotated(ROT_TEST)[0]
    lay = Layout(name="t", layers=[Layer(name="L")],
                 structures=[Structure(name="S", layer_refs=[
                     LayerReference(ref="L", shapes=[r])])])
    svg = to_svg(lay)
    pts = [tuple(float(v) for v in xy.split(","))
           for xy in re.search(r'<polygon points="([^"]+)"', svg).group(1).split()]
    assert len(pts) == 4
    right, left = max(pts), min(pts)
    assert right[1] < left[1]          # SVG y is down: the right end is higher


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("angle", [0.0, 30.0, 90.0, 145.0, 270.0, -20.0])
@pytest.mark.parametrize("h", [2.0, -2.0])
def test_mirror_is_exact_for_rotated_rects(axis, angle, h):
    from elymaker.gui.items import mirror_shape_model
    r = _R(x=-19.0, y=-1.0, width=12.0, height=h, angle_deg=angle)
    m = mirror_shape_model(r, axis)
    fx, fy = (-1.0, 1.0) if axis == "y" else (1.0, -1.0)
    want = sorted((round(px * fx, 6), round(py * fy, 6))
                  for px, py in rect_corners(r.x, r.y, r.width, r.height, r.angle_deg))
    got = sorted((round(px, 6), round(py, 6))
                 for px, py in rect_corners(m.x, m.y, m.width, m.height, m.angle_deg))
    assert got == want


# ------------------------------------------------------------------ the GUI
def test_coords_angle_sign():
    from elymaker.gui.coords import layout_angle_to_scene, scene_angle_to_layout
    assert layout_angle_to_scene(30.0) == -30.0
    assert scene_angle_to_layout(layout_angle_to_scene(270.0)) == 270.0


def _gui_corners(it):
    from elymaker.gui.coords import scene_to_layout
    return [scene_to_layout(p.x(), p.y())
            for p in (it.mapToScene(q) for q in it._edge_corners_local())]


@pytest.mark.parametrize("angle", [0.0, 30.0, 90.0, 180.0, 270.0, -45.0, 400.0])
@pytest.mark.parametrize("h", [2.0, -2.0])
def test_gui_rect_draws_where_smartfib_does_and_round_trips(qapp, angle, h):
    from elymaker.gui.items import RectItem
    r = _R(x=-19.0, y=-1.0, width=12.0, height=h, angle_deg=angle)
    it = RectItem.from_model(r)
    assert _close(_gui_corners(it), rect_corners(r.x, r.y, r.width, r.height, angle), 1e-6)
    m = it.to_model()
    assert (m.x, m.y, m.width, m.height) == (r.x, r.y, r.width, r.height)
    assert m.angle_deg == angle                       # the file value is unchanged
    c = it.center_layout()
    assert abs(c[0] - (-13.0)) < 1e-6 and abs(c[1] - (-1.0 + h / 2)) < 1e-6


def test_gui_resizing_a_rotated_rect_keeps_the_anchor_and_pivots_about_the_new_centre(qapp):
    from elymaker.gui.items import RectItem
    it = RectItem.from_model(_R(x=1.0, y=2.0, width=6.0, height=-2.0, angle_deg=30.0))
    it.width = 10.0
    it.height = -4.0
    m = it.to_model()
    assert (m.x, m.y, m.width, m.height, m.angle_deg) == (1.0, 2.0, 10.0, -4.0, 30.0)
    assert _close(_gui_corners(it), rect_corners(1.0, 2.0, 10.0, -4.0, 30.0), 1e-6)


def test_gui_scene_round_trip_of_the_rotation_test_file(qapp):
    from elymaker.gui.items import RectItem
    for r in _rects(ROT_TEST):
        m = RectItem.from_model(r).to_model()
        assert (m.x, m.y, m.width, m.height, m.angle_deg) == \
            (r.x, r.y, r.width, r.height, r.angle_deg)


def test_properties_panel_angle_turns_the_box_ccw_and_is_saved_as_typed(qapp):
    from elymaker.gui.items import RectItem
    from elymaker.gui.properties import PropertiesPanel
    it = RectItem.from_model(_R(x=-19.0, y=-1.0, width=12.0, height=2.0, angle_deg=0.0))
    panel = PropertiesPanel()
    panel.set_item(it)
    panel.angle.setValue(30.0)
    m = it.to_model()
    assert m.angle_deg == pytest.approx(30.0)
    assert _close(_gui_corners(it), rect_corners(-19.0, -1.0, 12.0, 2.0, 30.0), 1e-6)
    panel.set_item(it)                                   # re-read: shows what was typed
    assert panel.angle.value() == pytest.approx(30.0)
