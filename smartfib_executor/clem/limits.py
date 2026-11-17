"""CLEM safety numbers (Evan, 2026-10-09: "implement these for now, we may need to relax
them in practice"). One place, so relaxing one is a one-line, versioned change."""

from __future__ import annotations

from ..refine.geometry import TARGET_MAX_BEAM_SHIFT_M, TARGET_MAX_SCAN_ROTATION_DEG  # noqa: F401

# Overviews are taken at SEM normal: stage tilt +30 deg (gisdep accepts 29-31), R 12.
SEM_NORMAL_TILT_RANGE_DEG = (29.0, 31.0)
SEM_NORMAL_ROT_DEG = 12.0
SEM_NORMAL_ROT_TOL_DEG = 1.0

# Visiting a target (v0.53): the live tilt must match the overview's within this.
LIVE_TILT_MATCH_DEG = 0.5

# Registration error (pyMap's combined leave-one-out RMS, on the grid when given):
QC_WARN_UM = 1.0        # warn above
QC_REFUSE_UM = 3.0      # refuse above, unless the operator overrides (logged)

MAX_TARGETS = 30        # per targets file (= per grid)

# Overview defaults (pyMap plan F4; Evan).
FIB_OVERVIEW_MAG = 400.0
SEM_OVERVIEW_MAG = 200.0
OVERVIEW_WIDTH_PX = 1024
OVERVIEW_HEIGHT_PX = 768

# An overview more than this far (X/Y) from its grid's SEM-normal fixture is warned
# (wrong grid selected?). Warning only: the grid fixtures are rough positions.
GRID_FIXTURE_WARN_M = 2.0e-3

# A stage calibration (refine --calibrate-stage) is taken as verifying the overview's
# scale when it was measured within these of the overview's magnification / tilt, and
# its measured scale is within SCALE_OK_FRAC of the model.
SCALE_CAL_MAG_FRAC = 0.05
SCALE_CAL_TILT_DEG = 1.0
SCALE_OK_FRAC = 0.03

# Markup trenches at SEM normal (v0.53.0, Evan 2026-10-10: Markup_Standard milled at the
# correlation magnification, several per overview; the tick marks the lamella).
MARKUP_FIELD_MARGIN_UM = 5.0        # every shape at least this far inside the field
MARKUP_TARGET_CLEARANCE_UM = 2.0    # no shape within this of any marked target point
MARKUP_STAGE_TOL_M = 1.0e-6         # live X/Y/Z/M vs the overview record before milling
MARKUP_ANGLE_TOL_DEG = 0.05         # live T/R vs the overview record before milling
MARKUP_MAG_TOL_FRAC = 0.01          # FIB magnification read-back vs the overview's
