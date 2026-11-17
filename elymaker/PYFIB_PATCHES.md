# Local pyFIB patches to the bundled ELYMaker

This copy of ELYMaker is bundled inside pyFIB. It is ELYMaker 0.9.1 plus the
patches below; its version reads `0.9.1+pyfib.1`. Each patch should also be made
upstream, after which this copy can be replaced by the next ELYMaker release.

## 0.9.1+pyfib.1 (pyFIB v0.54.3, 2026-10-20): rotated rectangles

**Problem.** SmartFIB rotates a RECT about the **centre** of its box,
**counter-clockwise** for a positive angle (layout y up, as SmartFIB displays it).
ELYMaker 0.9.1 drew rotated RECTs **clockwise about the anchor**. Its SVG preview
turned the right way, but also about the anchor. Files are written unchanged; only
how ELYMaker *shows* rotated RECTs was wrong, but that matters when people place
shapes by eye.

**Evidence**
- `pyFIB_rotation_test.ely` (in `elymaker_tests/fixtures_pyfib/`) opened in SmartFIB
  on the Crossbeam 550 (Evan, 2026-10-20). Both 30° bars pass through their centre
  markers and rise to the right, for height +2 and for height −2.
- `pyFIB_waffle.ely`: the two 270° hinge slots were milled at the centre-pivot
  positions on 8 sites (2026-10-16).

**Size of the error before the patch**
- At 30°, test A's far end was drawn about 10 µm from where SmartFIB puts it.
- At **180°** (common in SmartFIB-saved waffle files), the box was drawn mirrored
  through its anchor, a full box away. For example, `Waffle_bulk.ely`: drawn at
  x 68.4…86.5, but SmartFIB mills it at x 86.5…104.7.

**Change** (`elymaker_rect_rotation.patch`)
- `geometry.rect_center`, `geometry.rect_corners`: the one rule. The box runs from
  the anchor by the signed width and height, then rotates counter-clockwise about
  its centre. Vertex order is SmartFIB's.
- `gui/coords.layout_angle_to_scene` and `scene_angle_to_layout` negate the angle,
  because Qt's item rotation is clockwise on screen.
- `gui/items.RectItem`:
  - The rotation pivot (Qt transform origin) is the box centre, kept there when the
    width or height changes.
  - `pos()` stays the unrotated anchor, so `x`, `y` and `angle` are saved exactly as
    read.
  - `layout_bbox` uses the rotated corners.
  - `mirror_shape_model` is unchanged: it is now exact for rotated rectangles too
    (docstring updated).
- `gui/properties`: the angle box goes through `layout_angle_to_scene`.
- `preview`: rectangle outlines and bounds use `rect_corners`.
- `validate._extent`: uses `rect_corners`. It used to treat (x, y) as the centre and
  ignore the angle.

**Tests**: `elymaker_tests/test_v091_rect_rotation.py` (48). The two files it reads
are in `elymaker_tests/fixtures_pyfib/`, kept out of `fixtures/` because
`test_roundtrip` counts the 12 example files there.

**Not changed (open):**
- TEXT has an angle too; its pivot has not been measured.
- The automatic marked-edge estimate (`_auto_marked_index`) is untouched. In the
  SmartFIB screenshot the blue edge on both 30° bars was edge 1 (P0→P1), while
  ELYMaker's estimate gives edge 2 (test A) and edge 4 (test B). What SmartFIB's blue
  line means is not confirmed.
