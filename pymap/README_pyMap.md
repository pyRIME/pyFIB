# pyMap v0.9.2

Correlative light/electron microscopy overlay for pyFIB (roadmap item 4-adjacent:
CLEM targeting). Registers a 2D confocal (light) image into a FIB image's pixel
grid using landmark-based transforms, produces a blended overlay with
alignment-verification tools, reads multi-format / multi-channel confocal
(Leica Stellaris: OME-TIFF / ImageJ / `.lif`), and **hands targets off to
elymaker** for lamella placement.

**The FIB image is the milling reference and is never resampled.** Only the
confocal image is warped into the FIB pixel grid.

Headless core + PyQt5/qtpy GUI, on the vendored elymaker theme. Isolated module,
developed entirely offline against synthetic data — nothing here touches the
instrument. The GUI is verified headless (offscreen) only. The elymaker handoff
is cross-checked against elymaker's own `load_job` / `coords` (below), but not
yet exercised on the instrument.

> **Name history:** pyCLEM (v0.1–0.6) → pyCORREM (v0.7) → **pyMap (v0.8+)**.
> The Python package is now `pymap` (was `pyclem`, then `pycorrem`): launch with
> `python -m pymap.gui` / `python -m pymap.cli`. Session files saved under
> either old name load unchanged. The elymaker handoff contract
> (`elymaker.handoff.v1`) is unchanged; only pyMap's own `targets.json` sidecar
> ID changed (now `pymap.targets.v1`).

## v0.9.2 — crash logging (Evan: "a lot of crashes associated with pyMap")

pyMap now keeps a log that says what it was doing when something went wrong.

- **Where:** `<folder holding pymap>/logs/pymap.log`. Bundled in pyFIB that is
  `<pyFIB>/logs/pymap.log`, beside `pyfib_gui_errors.log` and `pyfib_faults.log`.
  Change it with `build_widget(..., log_dir=...)`, `python -m pymap.gui --log-dir`, or
  the environment variable `PYMAP_LOG_DIR`. It rotates at 5 MB and keeps 5 old files.
  The toolbar's **Diagnostics** button shows the path and opens the folder.
- **What goes in it:**
  - a header for each run: versions of Python, Qt, numpy, scikit-image, Pillow,
    tifffile, imagecodecs and readlif, plus the host (pyFIB version) and memory;
  - `BEGIN <action>` *before* every button, menu action and click, and
    `END ... ok 0.12 s | memory` or `FAIL` with the full traceback, pyMap's state
    (images, landmark and target counts, transform, session) and the last few quiet
    events (drags, slider moves). Every line is written to disk at once, so after a
    hard crash the last `BEGIN` without an `END` is the action that was running;
  - image loads (file size, shape, dtype, which reader, any fallback) and every
    registration (transform, pairs, sizes, RMS);
  - Python warnings raised during pyMap actions (e.g. NaN or divide-by-zero).
    Standalone only: embedded, the host keeps its own warning handling;
  - Qt's own warnings and fatal messages (chained to any handler already set);
  - the window's lifecycle: created, reparented (pyFIB's own window and back),
    destroyed.
- **Hard crashes and kills:** each process leaves a small run marker. The next start
  reports a run that ended without closing, and what it was doing, in the log and in
  the status bar. A native crash's Python stack is in the host's `pyfib_faults.log`
  (standalone: `pymap_faults.log`).
- **Freezes:** an action still running after 30 s dumps every thread's stack to
  `pymap_hangs.log`. A "Not responding" window that gets closed looks like a crash.
  For actions that open a file dialog, only the loading or saving is timed, not the
  browsing. The dump uses faulthandler's single process-wide timer; a host that
  needs that timer for itself sets `PYMAP_HANG_DUMPS=0`.
- **No more errors escaping into pyFIB:** every entry point is now a guarded slot.
  This covers toolbar and control widgets, view clicks and drags, the
  controller → view signals and pyFIB's `set_session` call. An error is logged, shown
  once (at most one box per action every 10 s) and stopped there. Before, it reached
  pyFIB's error hook; standalone, it ended the program.
- **Off switch:** `PYMAP_DIAG=0` (no files, no hooks). `PYMAP_LOG_LEVEL=DEBUG` logs
  more detail.

**Bugs fixed.** An independent code review found these; each has a regression test
in `tests/test_v092_fixes.py`. A second independent review then checked the changes:
old against new gives identical warps and target coordinates on 48 cases. Its
findings are fixed too (tests at the end of `test_diag.py` and `test_v092_fixes.py`):

- **Freezes:**
  - **Thin-plate spline:** the warp is now evaluated in blocks, once for the image
    and its coverage mask together. TPS with 40 pairs on a 1024×768 FIB took 3.7 s
    and +1.3 GB per click; it now takes 0.7 s and +98 MB. At 2048×1536 with 20 pairs,
    5.9 s / 2.6 GB became 1.7 s / 0.3 GB.
  - **Clicks that don't complete a pair:** half a pair, or deleting an unpaired
    point, no longer re-warp.
  - **Flicker compare mode:** it re-rendered the whole overlay every 450 ms, even
    with pyMap hidden behind another pyFIB tab. Both frames are now rendered once, and
    the timer does nothing while the overlay isn't visible.
- **Errors:**
  - **Landmarks on a line:** three collinear landmarks under affine raised numpy's
    `LinAlgError` straight into pyFIB's error box. Degenerate sets now get a clear
    refusal ("points on one line, or repeated"): collinear for affine/TPS, repeated
    TPS points, or all points the same.
  - **Float TIFFs with NaN or inf:** these (e.g. from Fiji or deconvolution) broke
    Open Confocal. Bad pixels are set to 0 and counted in the log.
  - **Session with an unknown transform:** one such as `"TPS"` used to load and then
    break every later click. Case is now forgiven and unknown names are refused.
  - **Load Session is all-or-nothing:** if any part fails, the work on screen is
    left untouched.
  - **`recompute` failures:** an unexpected error no longer leaves the views
    half-updated; it is shown in the fit-quality box and logged.
- **Wrong results:**
  - **Load Session and the control panel:** the panel didn't follow the loaded
    session (transform, rotation, flips, LUT, compare mode). Ticking a flip
    afterwards silently reset the loaded rotation.
  - **Channel black/white changes:** a change made with the keyboard, the wheel or a
    groove click never reached the overlay. Now it is applied 0.35 s after the last
    change. While a slider is dragged only the confocal pane follows; the overlay is
    re-warped once, on release.
  - **Open FIB kept another image's pixel size:** it kept the pixel size of a pyFIB
    record opened earlier (target µm, QC µm, elymaker handoff). Metadata from a record
    is now dropped, and metadata from a file is kept only if its size matches. The
    status bar says so when metadata is dropped. Load FIB meta now updates the µm
    figures at once.
  - **Multi-image `.lif` files:** these always opened the first image, which may be a
    preview. pyMap now asks which image, preselecting the largest; the choice is
    saved in the session. A tile scan says that only tile 1 is shown.
  - **Degenerate leave-one-out:** the refusal now names the pair to move instead of
    "needs 4 pairs". *Behaviour change:* if leaving out one pair makes the rest
    degenerate (e.g. 4 pairs, 3 of them in one FIB row), Write targets is refused
    until a pair is moved or added. v0.9.1 wrote a meaningless error instead, or
    crashed with affine.
  - **Wheel zoom:** it is limited to 0.01–100 screen px per image px. Far enough
    out, the view stopped being invertible and clicks landed at (1, 1).

## v0.9.1 — robustness for pyFIB (review revision 6)

- **No more crash on an unreadable file.** Every toolbar action that reads or
  writes a file (Open FIB overview, Open FIB, Open Confocal, Load/Save Session,
  Load FIB meta, Export, Export handoff, Write targets) reports any error in a
  message box and pyMap keeps running. Standalone, an unexpected error anywhere
  shows a message (with the traceback under "Show Details") instead of PyQt5
  ending the program; embedded in pyFIB, pyFIB's own error handling applies.
- **LZW / deflate TIFFs without imagecodecs:** when tifffile can't decode the
  pixels, Pillow decodes the pages into the same shape, so channels and z are
  kept (the status bar says "decoded with Pillow"). If neither can, the message
  says to install imagecodecs. `requirements.txt` now lists
  `imagecodecs==2024.6.1` on Python 3.9 (pyFIB's pin) and `>=2024.6.1` on newer.
- **Plain multipage TIFFs keep every page.** A TIFF with no OME/ImageJ metadata
  used to load as its first page only. Now up to 6 gray pages load as channels
  (each with its own display controls); more load as a z stack (MIP).
- **Own window:** the embedded widget can be reparented (into its own window and
  back) while in use. The Controls dock is at least 270 px wide, can be floated,
  and the toolbar's "Controls" button shows/hides it.
- **`widget.set_session(session_dir, positions_file=None)`** for a host starting a
  new session. A FIB overview from the old session is unlinked, and "Write targets"
  now refuses an overview whose positions file is outside the current session.
- **Session files remember the pyFIB overview** (`pyfib_source`): loading one
  re-links the overview so targets can be written again; loading a session without
  that link clears any link left from before.

## pyFIB integration (v0.9.0)

Built to the plan agreed with pyFIB (project docs `claude/pyMap_plan_for_pyFIB.md`,
`claude/pyFIB_review_of_pyMap_plan.md`, `claude/pyMap_response_to_pyFIB_review.md`).
pyMap finds and measures; pyFIB moves the stage. Nothing here talks to SmartSEM.

- **`pymap.api`** imports only the standard library (numpy and Pillow load inside
  `locate_site` / `locate_targets`), so pyFIB's command-line tools can use it without
  Qt or scikit-image. `import pymap` is also light now: the old top-level names load
  on first use.
  - `pymap.targets.v2` targets files (`sources` list, tile-ready) with a
    `pymap.ready.v1` marker beside each (`<file>.ready.json`, SHA-256);
    `write_targets`, `read_targets`, `validate_targets_doc`, `check_source_against_record`.
  - pyFIB position records: `load_positions`, `record_image_path` (Windows paths
    normalised), `check_overview_record` (pyFIB's limits: scan rotation <= 0.05 deg,
    beam shift <= 0.01 um, v2 record), `fib_source_from_record`.
  - pyFIB SEM overviews: `load_sem_overviews` (`pyfib.sem_overviews.v1`).
  - `locate_targets(source, site_record, targets) -> [(id, x_px, y_px, score)]`:
    where the targets sit in a site image taken at the overview's tilt. Score =
    match margin (0..1); a periodic grid that matches a square over scores ~0.
- **Embedding:** `pymap.gui.embed.build_widget(parent, *, session_dir,
  positions_file=None, on_targets_written=None)` returns the whole UI as a child
  widget. It never creates a QApplication or applies a theme.
- **GUI:** "Open FIB overview (pyFIB)" opens a position from
  `<session>/clem/overview_positions.json` with the record's own pixel size (no
  `meta.json`). "Write targets for pyFIB" writes
  `<session>/clem/targets_<overview>.json` + ready marker (and the pyMap session
  beside it). It refuses without a pyFIB overview, with more than 30 targets, or
  before the error can be measured (one more landmark pair than the transform needs).
- **Error in micrometres:** RMS and leave-one-out RMS in um, plus the worst-axis
  leave-one-out error **on the grid** (pyFIB warns above 1 um, refuses above 3 um).
  The grid error is found from the fit's own scales, with no angle assumed: on the
  FIB at +30 deg a vertical image error is ~1.7x larger on the grid. "Squash" shows
  the fit's smallest/largest scale: ~0.59 for the FIB at +30 deg, ~1 for the SEM.
- **Fixes:**
  - The overlay was drawn up to half a pixel x (scale - 1) off (one pixel at 2x);
    target coordinates were always right.
  - Points picked before a rotate/flip moved one confocal pixel when remapped.
  - `Image.fromarray(..., mode=...)` removed: Pillow 13 (2026-10-15) drops that
    argument, which would have broken Export on unpinned installs.
- **Standalone with a session:** `python -m pymap.gui --session-dir <session>`.

## What it does

1. Load FIB bitmap (fixed; bmp/tif) + confocal (moving). Confocal may be a
   Leica Stellaris export: OME-TIFF / ImageJ hyperstack / `.lif` / plain
   tif/png/jpg/bmp, single- or multi-channel, a z-stack or a single plane.
   Stacks are MIP-projected and channels merged into one multicolor image.
2. Coarse pre-align the moving image (rot90 + H/V flip), BigWarp-style.
3. Pick corresponding landmarks on both images.
4. Fit a transform: `rigid` / `similarity` / `affine` / `tps` (thin-plate spline).
5. Warp the confocal into the FIB grid; blend into an overlay.
6. Report fit quality (RMS + leave-one-out RMS) so the overlay is trustworthy.

## Transform models

| kind        | dof                              | min pairs |
|-------------|----------------------------------|-----------|
| rigid       | rotation + translation           | 2         |
| similarity  | + uniform scale                  | 2         |
| affine      | + shear / anisotropic scale      | 3         |
| tps         | deformable (thin-plate spline)   | 3         |

TPS is implemented in **pure numpy** (`pymap/tps.py`), not skimage, because
`skimage.transform.ThinPlateSplineTransform` needs scikit-image >= 0.24 which
requires Python >= 3.10 and will not install on the instrument's 3.9.13 pin.

**Fit QC.** For linear models, `registration.rms` is the true fit residual. TPS
interpolates its control points exactly (residual ~ 0), so use
`leave_one_out_rms()` — the honest measure of how well the deformable fit
generalises between control points. Both are emitted in the CLI report.

## Coordinate frames

- Fixed  = FIB pixels: origin top-left, +x right, +y **down**.
- Moving = pre-aligned confocal pixels (same convention). Landmarks are picked
  *after* the coarse pre-align, so the moving coords live in pre-aligned space.
- Transform maps **pre-aligned-moving px → fixed px**.

## ⚠ Pending reconciliation (do not trust for placement yet)

`pymap/meta.py` ties FIB **pixel** coords to FOV-centered **meters** (what
elymaker/pyFIB consume). The convention implemented — FOV-centered origin,
+X right, +Y **up** (Y flipped vs image space), no mirror — is chosen to match
elymaker's confirmed convention, but per the project principle **signs and
conventions must be verified, never derived**. It is explicit and
regression-tested, but must be cross-checked against elymaker before it drives
a real lamella placement.

The metadata fields (`fov_width_m`, `fib_pixel_size_m`, `image_width/height`,
plus a carry-through `extra` dict) mirror the pyFIB **v2 position record**.
Reconcile field names against the actual v2 record at integration. **The
registration core does not depend on any of this** — it works entirely in FIB
pixel space.

To finish the elymaker hand-off I'll need, at integration time: the elymaker
positions-schema/hand-off format, and confirmation of the v2 position-record
field names.

## Hand-off artifact

`RegistrationResult.warped_rgba()` returns the confocal alone, in FIB space, as
RGBA at the **exact FIB image dimensions** (alpha = coverage). Because it lines
up 1:1 with the milling-reference FIB image, elymaker can load it directly to
position the lamella. `overlay()` returns the blended verification image.

## Install (instrument-compatible pins)

    python -m venv .venv && . .venv/bin/activate      # 3.9.13 on instrument PC
    pip install -r requirements.txt                    # core + GUI (+ imagecodecs)
    # headless / CLI only (no Qt):
    pip install -r requirements-core.txt

Pins target Python 3.9.13: `scikit-image==0.22.0` (last 3.9 line with the
estimators we use), `numpy==1.26.4` (matches skimage's ABI), Pillow capped `<12`
(Pillow 12 dropped 3.9). The core is deliberately Qt-free, so
`requirements-core.txt` omits PyQt5/qtpy.

**Do not `pip install pymap`.** That name on PyPI belongs to an unrelated IMAP
mail server. pyMap is not installed with pip: run it from its own folder (or with
that folder on `PYTHONPATH`), which always takes precedence over anything named
`pymap` in site-packages.

## GUI

    python -m pymap.gui

Three panes: **FIB (fixed)** | **Confocal (moving, pre-aligned)** | **Overlay**.
Workflow (BigWarp-style): coarse-align the moving image with the Rotate/Flip
buttons, then click a feature on the FIB pane and the same feature on the
confocal pane to drop a numbered pair; repeat. Drag markers to nudge, click to
select, Delete/Clear to edit. Pick the transform (rigid/similarity/affine/tps);
the overlay and the RMS / leave-one-out-RMS readout update live. Export writes
`overlay.png`, `confocal_in_fib.png` (RGBA hand-off), `session.json`,
`report.json`.

Named LUTs (green/magenta/…) collapse the confocal to luminance and recolor it,
so they work whether the confocal loads as grayscale or as an RGB file (e.g.
single-channel fluorescence stored R=G=B). Pick `rgb` to pass a genuinely
multi-color merged-channel confocal through untouched.

- Landmarks are refit on discrete actions (add / drag-release / delete /
  prealign / transform). Display changes (LUT / opacity / contrast) re-render
  from the cached fit without refitting.
- Rotating/flipping after points exist **remaps** the moving points so they stay
  pinned to their features (`pymap/prealign_map.py`, regression-tested).
- Wheel = zoom, middle-drag = pan, per pane.

### Confocal formats

Confocal decode is isolated in `pymap/confocal/` behind one importer that always
returns a normalized `ConfocalImage` (channels + LUT colors + optional pixel
size, or a pre-merged RGB), so the registration core never sees a file format.
Backends degrade gracefully:

- **PIL** (always): png / jpg / bmp, and multipage TIFF (pages treated as z).
- **tifffile** (optional): OME-TIFF and ImageJ hyperstacks — parses C/Z/T axes,
  channel names/colors, and pixel size.
- **readlif** (optional, pure-python): Leica `.lif`. Missing -> a clear "install
  readlif" message, not a crash. (No Bio-Formats / JVM.)

Z is MIP-projected at import (mean/slice available in the core; slice-picking UI
is a later feature). A single-channel confocal shows mono; a multi-channel one is
merged into a multicolor image and the overlay LUT is set to `rgb` so the colors
survive the warp into FIB space.

**Per-channel controls.** The control panel shows a row per confocal channel:
include on/off, LUT color, and independent **black/white point** sliders. Each
channel holds true intensity (dtype-normalized) plus its own display window, so
adjusting one channel never touches another — this is how you brighten a dim
channel. Channels open with a robust default window ([p1, p99.5]) so a dim
channel with a few hot pixels isn't crushed by a min-max stretch, and with the
file's own LUT colors when present (OME `Color`, or ImageJ LUTs). Slider drags
update the moving view live and re-warp the overlay on release (the confocal can
be large). Gamma is in the model; a slider for it is a small follow-up.

The `.lif` adapter is unit-tested against a synthetic reader interface but **not
yet validated against real Leica `.lif` bytes** — flagged as instrument/real-data
untested.



Before committing a mill, check the registration on the overlay pane with the
**Verify alignment** controls:

- **blend** — alpha overlay (opacity slider), the default.
- **swipe** — FIB on the left of the split, confocal on the right; slide the
  *Swipe %* to check whether edges continue across the seam.
- **checker** — alternating FIB / confocal tiles (*Checker tile* size); misalignment
  shows as edges that jump at tile boundaries.
- **flicker** — auto-toggles FIB / overlay so misregistration jumps out by eye.

**Residual arrows** draws a gold arrow from each fixed landmark to where its
moving point actually maps, exaggerated by the scale slider (1–50×), so you can
see *where* and *in which direction* the fit is off — not just the RMS number.
The handoff/export overlay is always the plain blend, independent of the compare
mode shown here.



Switch **Pick mode** to *Targets* and click fluorescent features on the confocal.
Each target is mapped moving -> FIB px through the registration, and (once a FIB
metadata json is loaded) to FOV-centred layout um -- shown live in the Targets
table and as a cursor readout in the status bar, and marked on the overlay.

*Export elymaker handoff* writes a folder with `overlay.png` (targets burned on
as crosses), `job.json` (`elymaker.handoff.v1`), and `targets.json` (FIB px +
layout um). Open it in elymaker with:

    elymaker-gui --job <folder>/job.json

elymaker opens preloaded with the overlay as the calibrated underlay, centred on
the FOV = layout origin, and the operator places the lamella on the fluorescence.
The pixel->um convention matches elymaker's `coords.py` exactly, and the emitted
`job.json` was verified to load through elymaker's own `load_job` with matching
pixel size and target um. A FIB metadata json is required (it carries the
authoritative `fib_pixel_size_m`); load it via *Load FIB meta*.

Headless equivalent:

    python -m pymap.cli --session s.json --meta meta.json \
        --handoff out_handoff --ely lamella.ely

### Theme

Matches the **elymaker / pyFIB** look. elymaker's `gui/style.py` (Fusion base,
dark palette, teal `#17c3b2` accent, canvas `#12151b`) is **vendored verbatim**
into pyMap as `gui/style.py` and applied by default, so the standalone tool
looks identical without needing anything on the path. If the deployed ecosystem
ever exposes a shared `fib_dark_theme` module, `gui/theme.py` prefers it. The
status bar shows which was applied (`theme: elymaker-style (vendored)`).

Keep `gui/style.py` in sync with elymaker's canonical copy; it's a vendored
duplicate, not a fork.

## CLI (headless, no Qt)

    python -m pymap.cli --fixed fib.bmp --moving confocal.tif \
        --landmarks pairs.json --kind affine --cmap green --out out/
    # or reproduce a saved session:
    python -m pymap.cli --session session.json --out out/

## Tests

    QT_QPA_PLATFORM=offscreen PYTHONPATH=. python -m pytest -q    # 176 tests
    PYTHONPATH=. python -m pytest -q tests/test_api.py          # pyFIB contract only (no Qt)

## Module map

    pymap/model.py         session / landmarks / targets / prealign (serializable)
    pymap/meta.py          FIB metadata + pixel<->FOV-um (matches elymaker coords)
    pymap/reader.py        8/16-bit, gray/RGB image loading
    pymap/tps.py           numpy thin-plate spline (py3.9-safe)
    pymap/transform.py     rigid/similarity/affine/tps fitting + residual QC
    pymap/warp.py          prealign + warp confocal into FIB grid + coverage alpha
    pymap/composite.py     pseudocolor LUTs + alpha-blend overlay
    pymap/prealign_map.py  Qt-free point remap through a prealign change
    pymap/handoff.py       elymaker.handoff.v1 job writer (+ targets sidecar)
    pymap/session.py       session json save/load
    pymap/pipeline.py      end-to-end orchestration -> RegistrationResult
    pymap/api.py            pyFIB contract: targets v2 + ready markers, records, SEM
                            overviews, locate_targets (stdlib at import)
    pymap/qc.py             registration error in um, on the image and on the grid
    pymap/cli.py           headless entry point (overlay + handoff)
    pymap/confocal/        multi-format decode -> normalized ConfocalImage
                            (PIL / tifffile / readlif; MIP; channel merge)
    pymap/gui/             PyQt5/qtpy app (theme, style, views, controls, controller, window)
    pymap/gui/embed.py     build_widget(): pyMap as a child widget for pyFIB

## Next (candidates)

- v0.10.0 (agreed with pyFIB): SEM navigation route (confocal -> SEM overview ->
  FIB overview, chained, with per-step error), reading pyFIB's SEM overview records
  as fixed images; carrying a target down to the milling tilt through the fiducial
  (see `claude/pyMap_response_to_pyFIB_review.md`); `pymap.overlay.v1` sidecar for
  the ELYMaker tab.
- Tune `locate_targets`' score threshold on the first real +30 deg site images.
- Confocal: z slice-picker; per-channel gamma slider; multi-series .lif.
- Swipe: draggable on-canvas divider (currently a slider).
- Subpixel landmark refinement (centroid / cross-correlation snap).
- Confirm on the instrument: the authoritative `fib_pixel_size_m` from a real
  pyFIB capture; a real Leica `.lif` opening; the handoff opening live in elymaker.
