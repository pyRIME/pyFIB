"""CLEM targeting with pyMap (v0.52.0, part 1: nothing moves to a target).

Evan, 2026-10-09: correlate the confocal with the grid in pyMap, turn each picked target
(a lamella centre) into a stage position, visit it at SEM normal (+30 deg, R 12), mill a
fiducial, then walk to the milling tilt (0-12 deg, by hand for now).

Split (agreed with the pyMap plan, claude/pyFIB_review_of_pyMap_plan.md):
  * pyMap: images only -- registration, QC, target picking. Writes
    <session>/clem/<name>.json (pymap.targets.v2) + <name>.json.ready.json.
  * pyFIB: everything on the stage side -- overviews, the pixel -> stage maths
    (refine.geometry.target_stage_delta), validation, and (v0.53) the visits.

Files in <session>/clem/ (never the milling positions.json):
  overview_positions.json   FIB overviews at SEM normal (PositionRecords, 400x, 1024x768)
  sem_overviews.json        SEM overviews grabbed by pyFIB, with stage readings
                            (pyfib.sem_overviews.v1)
  images/                   both overviews' BMPs
  pyfib_clem_plan.json      the checked plan (pyfib.clem_targets.v1)
  markup/<ov>_markup.ely    v0.53.0: the markup layout for an overview (+ .pyfib.json
                            record; milled/ keeps each milled copy)
  pyfib_clem_markups.json   v0.53.0: every markup milling (pyfib.clem_markups.v1)

Modules: limits (the safety numbers), targets (read + check + plan, files only),
overview (FIB / SEM overview capture, ARM-gated through the CLI), markup (v0.53.0: mark
targets with the markup trenches at the overview position), cli.
"""

from __future__ import annotations

CLEM_DIRNAME = "clem"
IMAGES_DIRNAME = "images"
OVERVIEW_POSITIONS = "overview_positions.json"
SEM_OVERVIEWS = "sem_overviews.json"
PLAN_FILE = "pyfib_clem_plan.json"

TARGETS_SCHEMA = "pymap.targets.v2"
READY_SCHEMA = "pymap.ready.v1"
PLAN_SCHEMA = "pyfib.clem_targets.v1"
SEM_SCHEMA = "pyfib.sem_overviews.v1"

# pyFIB setup fixtures at SEM normal (gisdep's GRID1 / GRID2), by grid.
GRID_FIXTURES = {"Grid1": "Grid1 GIS Normal", "Grid2": "Grid2 GIS Normal"}


def clem_dir(session_dir):
    from pathlib import Path
    return Path(session_dir) / CLEM_DIRNAME
