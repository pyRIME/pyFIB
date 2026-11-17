# ELYMaker handoff: rotated rectangles (from pyFIB, 2026-10-20)

**For:** the Claude working on ELYMaker upstream.
**From:** the Claude working on pyFIB, which bundles ELYMaker 0.9.1 inside pyFIB.
**What happened:** pyFIB v0.54.3 patched its bundled copy to ELYMaker `0.9.1+pyfib.1`.
Please make the same fix upstream (suggested 0.9.2). pyFIB will then bundle that
release and drop its local patch.

## 1. What SmartFIB does (measured on the instrument)

SmartFIB rotates a RECT about the **centre of its box**, **counter-clockwise** for a
positive `angle` (layout y up, as SmartFIB displays it). The box itself doesn't depend
on the angle: it runs from the anchor `(x, y)` by the signed `width` and `height`.

The rule as code (now `elymaker/geometry.py`):

```python
def rect_corners(x, y, width, height, angle_deg=0.0):
    cx, cy = x + width / 2, y + height / 2          # pivot = box centre
    a = radians(angle_deg); ca, sa = cos(a), sin(a)
    return [(cx + (px - cx) * ca - (py - cy) * sa,  # CCW, y up
             cy + (px - cx) * sa + (py - cy) * ca)
            for px, py in ((x, y), (x + width, y), (x + width, y + height), (x, y + height))]
```

**Evidence**
- `pyFIB_rotation_test.ely` (attached) opened in SmartFIB on the ZEISS Crossbeam 550
  (Evan, 2026-10-20). It holds two 12 × 2 µm bars at 30°, plus small unrotated squares
  marking each bar's unrotated corners and centre:
  - test A: anchor (−19, −1), height **+2**;
  - test B: anchor (7, 1), height **−2**.
  - Both bars pass through their **centre** squares and rise to the right
    (`SmartFIB_rotation_test.png`, attached).
  - Test A's corners: (−17.70, −3.87), (−7.30, 2.13), (−8.30, 3.87), (−18.70, −2.13).
- `pyFIB_waffle.ely` (attached) has two 0.2 × 2 µm hinge slots at 270°. On 8 milled
  sites (Rough after images, 2026-10-16) they were cut at the centre-pivot positions:
  x −9.49…−7.49 µm, y −0.45…−0.25 and +0.39…+0.59 µm.
- The same screenshot confirms the signed-height convention: SmartFIB draws a blue line
  on the edge through the anchor, and the box runs from that edge upward when `height`
  is positive and downward when negative.

## 2. What ELYMaker 0.9.1 did wrong

Written files were never affected. What ELYMaker **showed** was wrong, which matters
because people place shapes by eye.

| Where | 0.9.1 behaviour |
|---|---|
| GUI `RectItem` (`gui/items.py`, `gui/coords.py`) | Rotated **clockwise about the anchor**. `layout_angle_to_scene` returned the angle unchanged; its docstring's "double flip cancels" reasoning was wrong. |
| SVG preview (`preview.py`) | Counter-clockwise (correct direction), but **about the anchor**; bounds ignored the angle. |
| `validate._extent` | Treated `(x, y)` as the box **centre** and ignored the angle (field-of-view check). |
| `gui/items.layout_bbox` (measure label) | Ignored the angle. |

**How big the error was**
- 30°: test A's far end was drawn about 10 µm from where SmartFIB puts it.
- **180°** (common in SmartFIB-saved waffle files): the box was drawn mirrored through
  its anchor, a whole box away. `Waffle_bulk.ely` (in `fixtures/`): drawn at
  x 68.4…86.5, y −44.2…−37.4; SmartFIB mills x 86.5…104.7, y −51.0…−44.2.
  `Waffle_bulk_20mm.ely` is off by 18 × 15 µm.
- 90°/270°: the box swings to the other side, and the direction is reversed too.

**Worth telling users:** anything placed by eye next to a rotated rectangle (any
non-zero angle, including 180°) in ELYMaker ≤ 0.9.1 should be checked in SmartFIB.

## 3. The fix (attached as `elymaker_rect_rotation.patch`)

The patch is against ELYMaker 0.9.1, with paths relative to the folder holding the
`elymaker` package. The test file is at `elymaker_tests/` in pyFIB; upstream it
probably belongs in `tests/`.

- **`geometry.py`:** new `rect_center()` and `rect_corners()`, the one rule above, with
  the evidence in a comment.
- **`gui/coords.py`:** `layout_angle_to_scene(a) = -a` and `scene_angle_to_layout(a) = -a`
  (Qt's item rotation is clockwise on screen).
- **`gui/items.py` `RectItem`:**
  - `width` and `height` are properties; their setter keeps the Qt transform origin at
    the box centre (`setTransformOriginPoint(w/2, -h/2)` in local y-down coordinates).
    So rotation pivots about the centre, and a resize of a rotated box keeps its anchor
    and pivots about the new centre. That's also what SmartFIB does with the same file
    values.
  - `pos()` stays the **unrotated anchor**, so `x`, `y`, `width`, `height` and `angle`
    are written back exactly as read (tested at 0, 30, 90, 180, 270, −45 and 400°, for
    both height signs).
  - The marked-edge helpers use `mapToScene`, so they follow the rotated box
    automatically.
- **`gui/items.py` `layout_bbox`:** uses `rect_corners`.
- **`gui/items.py` `mirror_shape_model`:** no code change. With a centre pivot it is
  now exact for rotated rectangles; the docstring was updated. −angle for an x mirror
  and 180−angle for a y mirror both give the exact mirror footprint.
- **`gui/properties.py`:** the angle box goes through `layout_angle_to_scene`. It used
  to call `setRotation(value)` directly.
- **`preview.py`:** outlines and bounds use `rect_corners`.
- **`validate.py`:** `_extent` uses `rect_corners`.
- **`__init__.py`:** `0.9.1+pyfib.1` (upstream: your next version).

**Tests:** `test_v091_rect_rotation.py`, 48 tests, all passing; the whole ELYMaker suite
passes (292), and so does the full pyFIB suite (1314). They cover:
- the measured bars (exact corners, centre pivot, counter-clockwise);
- the waffle hinge where it was milled;
- GUI corners equal `rect_corners` for many angles and both height signs;
- exact round trip;
- resize of a rotated box;
- the properties-panel angle;
- mirror exact for rotated boxes (24 cases);
- preview direction;
- validate and preview bounds.

The two fixtures sit in a separate `fixtures_pyfib/` folder, because `test_roundtrip`
asserts exactly 12 files in `fixtures/`.

**After the fix:** the GUI draws the rotation test the same way SmartFIB shows it
(`ELYMaker_after_fix.png`, attached).

## 4. Open: please look at these

1. **TEXT** has an `angle` too. Its pivot and direction are unmeasured, and the patch
   doesn't touch it. The same kind of test file (a rotated TEXT with marker squares)
   opened in SmartFIB would settle it.
2. **The auto marked edge** (`_auto_marked_index`, "edge facing the frame centre",
   marked INFERRED in the code) doesn't match what SmartFIB drew on the rotation test.
   Both bars use `edge_sel_auto_scan_angle="true"` with no marked edge. SmartFIB drew
   its blue line on **edge 1** (P0→P1) of both. ELYMaker's estimate gives **edge 2**
   for test A and **edge 4** for test B. What SmartFIB's blue line means (scan start?)
   isn't confirmed. Worth one more SmartFIB screenshot with known marked edges before
   changing the estimate.
3. The validation pane's field-of-view check now uses the true rotated extent (it used
   to treat `(x, y)` as the centre), so field-of-view warnings can change for any file
   with rectangles near the field edge. That's correct, but worth a line in the release
   notes.
4. pyFIB's own report uses an identical copy of the rule
   (`smartfib_executor/report/overlay.py rect_corners_layout`), and a pyFIB test keeps
   the two equal. If the rule ever changes upstream, tell pyFIB.

## Attached

- `ELYMaker_rotated_rect_handoff.md`: this file
- `elymaker_rect_rotation.patch`: the code change plus the new test file
- `test_v091_rect_rotation.py`: the tests on their own
- `pyFIB_rotation_test.ely`, `pyFIB_waffle.ely`: the fixtures
- `SmartFIB_rotation_test.png`: Evan's SmartFIB screenshot (the measurement)
- `ELYMaker_after_fix.png`: patched ELYMaker showing the same file
