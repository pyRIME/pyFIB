from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterable, List, Literal, Optional, Tuple

from .positions import PositionDatabase, PositionRecord, recall_position
from .runner import FIB_MODE_NAME_TO_CODE, MILLING_IMAGING_CODES, PatternJob, PatternRunner
from .smartsem_com import SmartSEM, SmartSEMError
from .safe_output import output_lost as _output_lost, lost_reason as _lost_reason, say as _say
from .tilt_guard import BATCH_TILT_LIMIT_DEG, TiltLimitError, plan_batch_tilts, read_live_tilt
from .milling_workflow import (
    AutofocusConfig, RefineConfig,
    run_autofocus_at_position, run_refine_at_position,
)


def natural_position_sort(names: Iterable[str]) -> List[str]:
    """Sort position names like position_1, position_2, ... in numeric order.

    Falls back to plain lexical order if a trailing integer can't be parsed.
    """

    def key(n: str):
        m = re.match(r"^(.*?)(\d+)$", n)
        if m:
            prefix, num = m.group(1), m.group(2)
            return (prefix, int(num))
        return (n, -1)

    return sorted(list(names), key=key)


def _safe_get_float_int(sem: SmartSEM, mnemonic: str) -> Optional[int]:
    try:
        return int(sem.get_float(mnemonic))
    except Exception:
        return None


def _safe_get_float(sem: SmartSEM, mnemonic: str) -> Optional[float]:
    try:
        return float(sem.get_float(mnemonic))
    except Exception:
        return None


# Scan rotation is expected to match capture (refine does not change it). Beyond this
# tolerance the pattern could land rotated, so we WARN (Phase D: warn, never refuse).
MILL_SCAN_ROT_TOL_DEG = 0.5


def _resolve_position_ely(rec, default_ely: Path, ref_base_dir=None,
                          force_pattern: bool = False):
    """Pick the .ely to mill for a position: the AUTHORED pattern (rec.ely_pattern) if
    it exists, otherwise the standard default. Returns (path, source, message) where
    source is 'authored' | 'standard' | 'authored_missing' | 'forced'.

    force_pattern=True mills the GIVEN pattern regardless of any authored one -- used
    for fiducials, which must never be replaced by the position's lamella pattern."""
    if force_pattern:
        return Path(default_ely), "forced", "using the given pattern (forced; authored override bypassed)"
    authored = getattr(rec, "ely_pattern", None)
    if authored:
        ap = Path(authored)
        if not ap.is_absolute() and ref_base_dir:
            ap = Path(ref_base_dir) / ap
        if ap.exists():
            return ap, "authored", f"using authored pattern {authored}"
        return (Path(default_ely), "authored_missing",
                f"*** WARNING: authored pattern {authored} is missing -- falling back "
                f"to the standard pattern. ***")
    return Path(default_ely), "standard", "using the standard pattern"


def _report_ely_pattern(idx: int, total: int, rec, ref_base_dir=None) -> None:
    """If this position has a linked elymaker .ely, WARN when the file no longer
    matches its ready-marker (edited since it was marked ready). Warn only."""
    ely = getattr(rec, "ely_pattern", None)
    if not ely:
        return
    from pathlib import Path as _P
    p = _P(ely)
    if not p.is_absolute() and ref_base_dir:
        p = _P(ref_base_dir) / ely
    try:
        from .ely_link import verify_ely_against_marker
        ok = verify_ely_against_marker(str(p))
    except Exception:
        ok = None
    if ok is False:
        print(f"[{idx}/{total}] *** WARNING: the .ely for {rec.name} ({ely}) has "
              f"CHANGED since it was marked ready -- you may be milling a different "
              f"pattern than was authored. ***")
    elif ok is True:
        print(f"[{idx}/{total}] pattern {ely}: matches its ready-marker.")


def _report_mill_conditions(idx: int, total: int, rec, sem: SmartSEM) -> dict:
    """List the current FIB scan rotation + beam shift vs the recorded capture values.

    Returns the values it read (for the run log; v0.40.0). No new reads.

    Informational only -- milling proceeds regardless (warn, never refuse). Scan
    rotation should match capture; a CHANGED beam shift is EXPECTED when refine ran
    (refine intentionally shifts the beam to land the pattern on the feature), so the
    beam-shift lines are reported without alarming.
    """
    cur_rot = _safe_get_float(sem, "AP_FIB_SCAN_ROTATION")
    cur_bx = _safe_get_float(sem, "AP_FIB_BEAM_SHIFT_X")
    cur_by = _safe_get_float(sem, "AP_FIB_BEAM_SHIFT_Y")

    def _fmt(v, unit, scale=1.0):
        return "n/a" if v is None else f"{v * scale:+.4f} {unit}"

    def _delta(cur, rec_v, unit, scale=1.0):
        if rec_v is None:
            return "(no recorded value)"
        if cur is None:
            return "(current unavailable)"
        return f"delta {(cur - rec_v) * scale:+.4f} {unit}"

    print(f"[{idx}/{total}] Mill-time conditions for {rec.name} "
          f"(warn only; milling proceeds):")
    print(f"    FIB scan rotation : now {_fmt(cur_rot, 'deg')}  |  "
          f"capture {_fmt(getattr(rec, 'fib_scan_rotation_deg', None), 'deg')}  |  "
          f"{_delta(cur_rot, getattr(rec, 'fib_scan_rotation_deg', None), 'deg')}")
    print(f"    FIB beam shift X  : now {_fmt(cur_bx, 'um', 1e6)}  |  "
          f"capture {_fmt(getattr(rec, 'fib_beam_shift_x_m', None), 'um', 1e6)}  |  "
          f"{_delta(cur_bx, getattr(rec, 'fib_beam_shift_x_m', None), 'um', 1e6)}")
    print(f"    FIB beam shift Y  : now {_fmt(cur_by, 'um', 1e6)}  |  "
          f"capture {_fmt(getattr(rec, 'fib_beam_shift_y_m', None), 'um', 1e6)}  |  "
          f"{_delta(cur_by, getattr(rec, 'fib_beam_shift_y_m', None), 'um', 1e6)}")

    rec_rot = getattr(rec, "fib_scan_rotation_deg", None)
    if cur_rot is not None and rec_rot is not None \
            and abs(cur_rot - rec_rot) > MILL_SCAN_ROT_TOL_DEG:
        print(f"    *** WARNING: scan rotation differs from capture by "
              f"{abs(cur_rot - rec_rot):.3f} deg (> {MILL_SCAN_ROT_TOL_DEG} deg). "
              f"The pattern may land rotated relative to the feature. ***")
    elif cur_rot is not None and rec_rot is None \
            and abs(cur_rot) > MILL_SCAN_ROT_TOL_DEG:
        print(f"    *** WARNING: scan rotation is {cur_rot:.3f} deg (no recorded "
              f"capture value to compare). ***")
    return {"scan_rotation_deg": cur_rot,
            "beam_shift_um": [None if cur_bx is None else round(cur_bx * 1e6, 4),
                              None if cur_by is None else round(cur_by * 1e6, 4)]}


def _safe_get_str(sem: SmartSEM, mnemonic: str) -> Optional[str]:
    try:
        s = str(sem.get_str(mnemonic)).strip()
        return s if s else None
    except Exception:
        return None


def _zero_fib_beam_shift(sem: SmartSEM, *, logger=print, verify: bool = True,
                         tol_m: float = 1e-9) -> bool:
    """Set FIB beam shift X and Y to 0. Best-effort; never raises.

    Returns True if both axes are confirmed ~0. A leftover beam shift would
    otherwise become the baseline for the NEXT site's refine (a cumulative offset)
    and skew the reduced-raster/park geometry, so this is called at batch start and
    after every position.
    """
    log = logger or (lambda m: None)
    ok = True
    for mn in ("AP_FIB_BEAM_SHIFT_X", "AP_FIB_BEAM_SHIFT_Y"):
        try:
            sem.set_value(mn, 0.0)
        except Exception as e:  # noqa: BLE001
            ok = False
            log(f"[beam-shift] WARNING: could not zero {mn}: {e}")
    if verify:
        for mn in ("AP_FIB_BEAM_SHIFT_X", "AP_FIB_BEAM_SHIFT_Y"):
            try:
                v = float(sem.get_float(mn))
                if abs(v) > tol_m:
                    ok = False
                    log(f"[beam-shift] WARNING: {mn} reads {v:.3e} m after reset "
                        f"(expected 0). Check the column before the next site.")
            except Exception:
                pass
    return ok


def _norm_state(text: str) -> str:
    return text.upper().replace(" ", "").replace("_", "").replace("-", "")


MILLING_MODE_CODES = (2, 5)          # DP_FIB_MODE: Milling, To Milling
_MODE_CODES = {_norm_state(k): v for k, v in FIB_MODE_NAME_TO_CODE.items()}
# v0.53.5 (bug #15): DP_PATTERNING_MODE codes MEASURED on this Crossbeam 550 (Evan,
# 2026-10-13, debug-fib-imaging; number and text agreed in every state):
#   0 'Off'        after a pattern finishes
#   2 'FIB'        after a SmartFIB snapshot -- the beam is imaging, NOT milling
#   3 'FIB + SEM'  while milling
# pyFIB's own runner WRITES DP_PATTERNING_MODE = 'FIB' right after CMD_FIB_START_MILLING
# (copied from SerialFIB), so during a pyFIB-started mill 'FIB' may well mean milling.
# Hence two rule sets (review 2026-10-14):
#   strict  (default; batch interlock, after images): only Off (0) is not milling;
#   snapshot_ok (the operator's manual buttons / CLEM actions): FIB (2) is not milling
#            either -- the operator starts their own mills in SmartFIB (3 'FIB + SEM').
PATTERNING_CODES = {"OFF": 0, "FIB": 2, "FIB+SEM": 3}
PATTERNING_NOT_MILLING_STRICT = (0,)
PATTERNING_NOT_MILLING_SNAPSHOT_OK = (0, 2)


def _desc(text: Optional[str], num: Optional[int]) -> str:
    if text is not None and num is not None:
        return f"{num} '{text}'"
    if text is not None:
        return f"'{text}'"
    return str(num) if num is not None else "unreadable"


def milling_state(sem: SmartSEM, *, snapshot_ok: bool = False) -> Tuple[bool, str]:
    """Conservative check of whether SmartSEM is milling. Returns (active, reason).

    Each flag is read as text AND as a number; if EITHER says milling, it is milling
    (the instrument's numbers and text have agreed so far, but the runner's notes say a
    number can disagree, so neither is trusted alone). Milling when:
      - DP_FIB_MODE is Milling (2) or To Milling (5);
      - DP_FIB_IMAGING's text contains 'MILL', or its number is 2 or 4;
      - DP_PATTERNING_MODE is anything but Off (0). With ``snapshot_ok`` (the manual
        buttons only), FIB (2) is not milling either: it is what a SmartFIB snapshot
        leaves (bug #15). A text label not in the measured map says nothing; the number
        decides;
      - DP_FIB_MODE's NUMBER can't be read (as in v0.53.4), or DP_FIB_IMAGING /
        DP_PATTERNING_MODE can't be read at all (neither text nor number).
    ``reason`` names the flag that decided it, with what it read."""
    # -- FIB mode
    mode_t, mode_n = _safe_get_str(sem, 'DP_FIB_MODE'), _safe_get_float_int(sem, 'DP_FIB_MODE')
    mode_tc = _MODE_CODES.get(_norm_state(mode_t)) if mode_t is not None else None
    if mode_tc in MILLING_MODE_CODES or mode_n in MILLING_MODE_CODES:
        return True, f"DP_FIB_MODE = {_desc(mode_t, mode_n)}"
    # -- imaging
    img_t, img_n = _safe_get_str(sem, 'DP_FIB_IMAGING'), _safe_get_float_int(sem, 'DP_FIB_IMAGING')
    if (img_t is not None and 'MILL' in img_t.upper()) or img_n in MILLING_IMAGING_CODES:
        return True, f"DP_FIB_IMAGING = {_desc(img_t, img_n)}"
    # -- patterning mode (a label the text map doesn't know leaves it to the number)
    pat_t, pat_n = _safe_get_str(sem, 'DP_PATTERNING_MODE'), _safe_get_float_int(sem, 'DP_PATTERNING_MODE')
    ok_codes = PATTERNING_NOT_MILLING_SNAPSHOT_OK if snapshot_ok else PATTERNING_NOT_MILLING_STRICT
    pat_tc = None
    if pat_t is not None:
        pat_tc = (int(pat_t.strip()) if re.fullmatch(r"[0-9]+", pat_t.strip())
                  else PATTERNING_CODES.get(_norm_state(pat_t)))
    for code in (pat_tc, pat_n):
        if code is not None and code not in ok_codes:
            note = ("" if code in PATTERNING_CODES.values() else
                    " (a value never seen before: treated as milling)")
            return True, f"DP_PATTERNING_MODE = {_desc(pat_t, pat_n)}{note}"
    # -- unreadable = cannot tell = milling (never move the stage on a guess)
    if mode_n is None:          # the number is required, as before v0.53.5 (review)
        return True, f"DP_FIB_MODE unreadable as a number (text {mode_t!r})"
    if img_t is None and img_n is None:
        return True, "DP_FIB_IMAGING unreadable"
    if pat_tc is None and pat_n is None:
        return True, ("DP_PATTERNING_MODE unreadable" if pat_t is None else
                      f"DP_PATTERNING_MODE unreadable as a number and text {pat_t!r} is not a "
                      f"known value")
    return False, (f"not milling (DP_FIB_MODE = {_desc(mode_t, mode_n)}, DP_FIB_IMAGING = "
                   f"{_desc(img_t, img_n)}, DP_PATTERNING_MODE = {_desc(pat_t, pat_n)})")


def _is_milling_active(sem: SmartSEM) -> bool:
    """True while SmartSEM indicates milling (or the state cannot be read). See
    milling_state for the rules; used to never move the stage mid-pattern."""
    return milling_state(sem)[0]


def _wait_not_milling_stable(
    sem: SmartSEM,
    *,
    stable_s: float = 5.0,
    timeout_s: float = 600.0,
    poll_s: float = 0.5,
) -> None:
    """Wait until SmartSEM indicates milling is inactive for stable_s seconds."""

    t0 = time.time()
    stable_t0: Optional[float] = None

    while True:
        active = _is_milling_active(sem)

        if not active:
            if stable_t0 is None:
                stable_t0 = time.time()
            elif (time.time() - stable_t0) >= float(stable_s):
                return
        else:
            stable_t0 = None

        if timeout_s > 0 and (time.time() - t0) > float(timeout_s):
            raise SmartSEMError(
                f"Timeout waiting for milling to become inactive before stage move (needed stable {stable_s:.0f}s)."
            )

        time.sleep(float(poll_s))


class BatchStopped(RuntimeError):
    """v0.55.1: the batch stopped itself at a safe point (before a stage move), e.g.
    because the pyFIB window that started it is gone. Not an instrument error."""


# Waiting modes in which the runner returns only after SmartSEM confirmed the mill ended.
CONFIRMED_WAIT_STRATEGIES = ("mode_change", "blanked", "auto")
SNAPSHOT_HINT = (" If nothing is milling: a SmartFIB snapshot leaves DP_PATTERNING_MODE at "
                 "'FIB'; switching the SEM on and off clears it.")


def _reset_beam_shift_when_idle(sem: SmartSEM, *, logger=print, where: str = "",
                                quiet_s: float = 0.0, poll_s: float = 0.5,
                                _sleep=time.sleep, _clock=time.time) -> Optional[bool]:
    """Zero the FIB beam shift ONLY while SmartSEM does not indicate milling (v0.55.1,
    bug #16). Never raises.

    A beam-shift write in the middle of a pattern moves where the rest of it lands, so
    nothing is written when milling is indicated or the state cannot be read (the strict
    batch rule of milling_state). ``quiet_s`` > 0: the state must read "not milling" on
    every poll for that long first (used when the site ended WITHOUT a confirmed end of
    milling: a single reading can fall in the gap between two parts of a pattern); one
    milling reading ends the wait at once.

    Returns True (zeroed and confirmed), False (written, not confirmed) or None (NOT
    written: milling indicated; the caller resets before the next stage move).
    (A second Ctrl+C during the quiet window still ends the process; nothing is written.)"""
    log = logger or (lambda m: None)

    def _l(msg):
        try:
            log(msg)
        except Exception:  # noqa: BLE001
            pass

    try:
        t0 = _clock()
        if quiet_s and float(quiet_s) > 0:
            _l(f"[beam-shift] checking for {float(quiet_s):.0f} s that milling has stopped "
               f"before the FIB beam shift is reset {where}...")
        while True:
            active, why = milling_state(sem)
            if active:
                _l(f"[beam-shift] *** NOT reset {where}: SmartSEM indicates milling ({why}). "
                   f"The FIB beam shift is left as it is, so a running pattern is not moved. "
                   f"pyFIB resets it before its next stage move; Go to resets it too. ***")
                return None
            if not quiet_s or (_clock() - t0) >= float(quiet_s):
                break
            _sleep(float(poll_s))
    except Exception as e:  # noqa: BLE001   cannot tell = do not write
        _l(f"[beam-shift] *** NOT reset {where}: the milling state could not be checked "
           f"({e}). ***")
        return None
    return _zero_fib_beam_shift(sem, logger=_l)


def mill_positions_sequential(
    sem: SmartSEM,
    *,
    db: PositionDatabase,
    position_names: Iterable[str],
    ely_local_path: Path,
    force_pattern: bool = False,
    record_fiducial: Optional[str] = None,   # "left" | "right": set fiducial_side on success
    db_path: Optional[Path] = None,          # where to save the db after recording
    move_method: Literal["ap_goto", "movestage", "none"] = "movestage",
    wait_stage_idle: bool = True,
    stage_timeout_s: float = 120.0,
    skip_objective: bool = False,
    set_mag: bool = False,
    settle_s: float = 0.25,
    load_method: Literal["smartfib_drop", "none"] = "smartfib_drop",
    smartfib_drop_dir: Path = Path(r"C:\\ProgramData\\Carl Zeiss\\SmartFIB\\API\\Drop"),
    smartfib_drop_name: str = "ApiLayout.ely",
    smartfib_load_delay_s: float = 1.0,
    smartfib_prepare_delay_s: float = 1.0,
    set_patterning_mode_fib: bool = True,
    ensure_fib_mode_mill: bool = False,
    wait_strategy: Literal["none", "mode_change", "blanked", "auto", "sleep"] = "mode_change",
    wait_timeout_s: float = 5400.0,
    done_confirm_s: float = 30.0,
    sleep_s: float = 0.0,
    continue_on_error: bool = False,
    # --- per-position autofocus / refine integration ---
    autofocus_names: Optional[Iterable[str]] = None,
    refine_names: Optional[Iterable[str]] = None,
    autofocus_config: Optional[AutofocusConfig] = None,
    refine_config: Optional[RefineConfig] = None,
    ref_base_dir: Optional[Path] = None,
    dry_run_focus_refine: bool = False,
    reset_beam_shift_between_positions: bool = True,
    run_log=None,                            # report.runlog.RunLog (v0.40.0); never affects milling
    after_image: bool = False,               # v0.41.0: one FIB frame per site after milling
    after_image_dir: Optional[Path] = None,  # default <ref_base_dir>/images
    # v0.46.0 batch tilt limit (tilt_guard): positions whose recorded tilt is more than
    # tilt_limit_deg from the live tilt (or from the previous site's) are never recalled.
    # They must be listed in tilt_skip_ok (the user OKed skipping them), else the batch
    # refuses before ANY motion. tilt_limit_deg=None disables the check (tests only).
    tilt_limit_deg: Optional[float] = BATCH_TILT_LIMIT_DEG,
    tilt_skip_ok: Optional[Iterable[str]] = None,
    # v0.51.0 milling stages (mill_stages.py). stage=None: as before. "rough": as before,
    # but the after image is named <name>_1-rough_<time>.bmp and recorded in
    # positions.json. "prepolish" / "polish": the GIVEN .ely at every position (authored
    # rough patterns are never milled), refine aligns to the previous stage's latest after
    # image, and a position without that image is never recalled -- it must be listed in
    # reference_skip_ok (the user OKed skipping it), else the batch refuses before ANY
    # motion.
    stage: Optional[str] = None,
    reference_skip_ok: Optional[Iterable[str]] = None,
    # Injection hooks (tests replace these to check ordering without hardware).
    _autofocus_fn=run_autofocus_at_position,
    _refine_fn=run_refine_at_position,
) -> None:
    """Recall each named position and mill the same .ely pattern at each site.

    This function assumes:
    - The stage movement and objective potential recall are sufficient to relocate to each site.
    - The milling layout is defined by the .ely file (loaded via SmartFIB drop or already loaded).

    Parameters match the CLI for 'mill-positions'.

    run_log (v0.40.0): optional RunLog that records each site for the report. Every
    call into it goes through _log(), which swallows any error, so the log can never
    change what the microscope does.
    """

    def _log(method: str, *a, **k) -> None:
        if run_log is None:
            return
        try:
            getattr(run_log, method)(*a, **k)
        except Exception as e:  # noqa: BLE001
            print(f"[run log] WARNING: {method} failed ({e}); milling continues.")

    runner = PatternRunner(sem)

    af_set = {str(n) for n in (autofocus_names or [])}
    rf_set = {str(n) for n in (refine_names or [])}
    af_cfg = autofocus_config or AutofocusConfig()
    rf_cfg = refine_config or RefineConfig()

    names = list(position_names)
    total = len(names)
    if total == 0:
        print("No positions to mill.")
        return

    # Milling stage (v0.51.0): references decided BEFORE any motion (and before the tilt
    # plan, so the tilt chain is over the positions that will really be milled).
    from .mill_stages import (FORCED_PATTERN_STAGES, PREVIOUS_STAGE, STAGE_LABEL,
                              StageReferenceError, check_stage, plan_stage,
                              record_after_image, resolve_stage_pattern)
    stage = check_stage(stage)
    stage_refs = {}
    if stage in FORCED_PATTERN_STAGES:
        if load_method != "smartfib_drop":
            raise ValueError(
                f"{STAGE_LABEL[stage]} needs load_method 'smartfib_drop' (with "
                f"{load_method!r} SmartFIB would mill whatever is already loaded); nothing moved")
        # A Prepolish / Polish site whose refine failed is never milled: off by the rough
        # refine correction (several um) the pattern could cut the lamella.
        import dataclasses as _dcs
        rf_cfg = _dcs.replace(rf_cfg, abort_on_failure=True)
    if stage is not None:
        print(f"[stage] {STAGE_LABEL[stage]}"
              + (f": every position mills {Path(ely_local_path).name}; refine aligns to the "
                 f"latest {STAGE_LABEL[PREVIOUS_STAGE[stage]]} after image"
                 if stage in FORCED_PATTERN_STAGES else ""))
    if stage is not None and PREVIOUS_STAGE[stage] is not None:
        splan = plan_stage(db, names, stage, ref_base_dir)
        ok_ref = {str(n) for n in (reference_skip_ok or [])}
        not_ok = [n for n in splan.missing if n not in ok_ref]
        if not_ok:
            for line in splan.lines():
                print(line)
            raise StageReferenceError(
                f"{', '.join(not_ok)}: no {STAGE_LABEL[PREVIOUS_STAGE[stage]]} after image to "
                f"align {STAGE_LABEL[stage]} to, and skipping was not OKed; nothing moved")
        if splan.missing:
            for n, why in splan.missing.items():
                print(f"[stage] NOT milling {n}: {why}")
            names = [n for n in names if n not in splan.missing]
        _log("stage_check", stage, dict(splan.missing))
        stage_refs = dict(splan.references)
        total = len(names)
        if total == 0:
            print(f"[stage] No position has a {STAGE_LABEL[PREVIOUS_STAGE[stage]]} after "
                  f"image; nothing to mill.")
            return
    elif stage is not None:
        _log("stage_check", stage, {})

    # Batch tilt limit (v0.46.0): decided BEFORE any motion, including the beam-shift zero.
    tilt_plan = None
    if move_method != "none" and tilt_limit_deg is not None:
        live_t = read_live_tilt(sem)                       # raises -> nothing has moved
        tilt_plan = plan_batch_tilts(names, {n: db.get(n).stage_at.t_deg for n in names},
                                     live_t, float(tilt_limit_deg))
        ok = {str(n) for n in (tilt_skip_ok or [])}
        excluded = tilt_plan.excluded
        not_ok = [n for n in excluded if n not in ok]
        if not_ok:
            for line in tilt_plan.table_lines():
                print(line)
            raise TiltLimitError(
                f"recorded tilt of {', '.join(not_ok)} is outside the {tilt_limit_deg:g} deg "
                f"batch limit and skipping it was not OKed; nothing moved")
        skip = {n: excluded.get(n, "OKed to skip") for n in names if n in excluded or n in ok}
        if skip:
            names = [n for n in names if n not in skip]
            again = plan_batch_tilts(names, {n: db.get(n).stage_at.t_deg for n in names},
                                     live_t, float(tilt_limit_deg))
            if again.excluded:                              # removing sites changed the chain
                for line in again.table_lines():
                    print(line)
                raise TiltLimitError(
                    f"without the skipped positions, {', '.join(again.excluded)} would change "
                    f"tilt by more than {tilt_limit_deg:g} deg; nothing moved")
            tilt_plan = again
            for n, why in skip.items():
                print(f"[tilt] NOT milling {n}: {why}")
        _log("tilt_check", live_t, float(tilt_limit_deg), skip)
        total = len(names)
        if total == 0:
            print("[tilt] No position is within the tilt limit; nothing to mill.")
            return

    # Start from a known-zero FIB beam shift so the FIRST site's refine is not
    # baselined on a leftover shift (which may already be high before we begin).
    # v0.55.1 (#16): never while SmartSEM indicates milling. A mill that is still running
    # when the batch starts is waited for first (the same interlock as before a stage
    # move), so its pattern is not moved by the reset.
    beam_reset_pending = False       # a reset that was skipped because milling was indicated
    if reset_beam_shift_between_positions and not dry_run_focus_refine:
        _active, _why = milling_state(sem)
        if _active:
            print(f"[safety] SmartSEM indicates milling activity ({_why}) at batch start; "
                  f"waiting for it to become inactive before the FIB beam shift is reset..."
                  + (SNAPSHOT_HINT if "DP_PATTERNING_MODE" in _why else ""))
            _wait_not_milling_stable(
                sem, stable_s=10.0,
                timeout_s=float(wait_timeout_s) if float(wait_timeout_s) > 0 else 0.0,
                poll_s=0.5)
        print("[beam-shift] zeroing FIB beam shift X/Y before the first site.")
        if _reset_beam_shift_when_idle(sem, logger=print, where="before the first site") is None:
            beam_reset_pending = True

    for idx, name in enumerate(names, start=1):
        # v0.55.1 (#16): the pyFIB window that started this batch is gone (its output pipe
        # closed). The site in progress was finished normally; stop here, before the next
        # stage move, instead of running on with nobody able to see it.
        if _output_lost():
            raise BatchStopped(
                f"the pyFIB window that started this batch is gone ({_lost_reason()}); "
                f"stopped before {name}: {idx - 1} of {total} position(s) were handled, "
                f"nothing was moved for the rest")
        rec: PositionRecord = db.get(name)
        runner_entered = False       # CMD_FIB_START_MILLING may have been sent at this site
        mill_confirmed_done = False  # ... and SmartSEM confirmed that the mill ended
        print("\n" + "=" * 72)
        print(f"[{idx}/{total}] Milling at {rec.name}")
        print(
            f"Stage target: X={rec.stage_at.x_m:.6e} m, Y={rec.stage_at.y_m:.6e} m, Z={rec.stage_at.z_m:.6e} m, "
            f"R={rec.stage_at.r_deg:.6f} deg, M={rec.stage_at.m_m:.6e} m"
        )
        print(f"Objective potential: {rec.fib_objective_potential_v:.6f} V")
        print("=" * 72)
        _log("site_start", rec.name, idx, total, rec)

        try:
            # Safety interlock: never move the stage if SmartSEM indicates milling is still active.
            #
            # This protects against false-positive "milling finished" detections that could
            # otherwise trigger a stage move mid-pattern (high risk for sample motion / damage).
            # If SmartSEM still indicates milling, wait here rather than moving the stage.
            # This prevents mid-pattern stage motion if the milling-complete detection was wrong.
            _active, _why = milling_state(sem)
            if _active:
                print(
                    f"[safety] SmartSEM still indicates milling activity ({_why}); waiting "
                    f"for it to become inactive before stage move..."
                )
                _wait_not_milling_stable(
                    sem,
                    stable_s=10.0,
                    timeout_s=float(wait_timeout_s) if float(wait_timeout_s) > 0 else 0.0,
                    poll_s=0.5,
                )

            # v0.55.1 (#16): a beam-shift reset that was skipped while milling was
            # indicated is done now, before anything moves. If milling is indicated
            # again, this site is not started.
            if (beam_reset_pending and reset_beam_shift_between_positions
                    and not dry_run_focus_refine):
                print(f"[beam-shift] resetting the FIB beam shift left from the previous "
                      f"site before {rec.name}.")
                if _reset_beam_shift_when_idle(sem, logger=print,
                                               where=f"before {rec.name}",
                                               quiet_s=float(done_confirm_s)) is None:
                    raise SmartSEMError(
                        f"the FIB beam shift could not be reset before {rec.name}: SmartSEM "
                        f"indicates milling again; not moving")
                beam_reset_pending = False
                _log("run_note", "beam_shift_not_reset", None)

            # 1) Move stage first (if requested)
            if tilt_plan is not None and rec.stage_at.t_deg is not None:
                # Re-check against the LIVE tilt right before this recall (v0.46.0).
                t_now = read_live_tilt(sem)
                t_rec = float(rec.stage_at.t_deg)
                lim = float(tilt_limit_deg)
                if (abs(t_rec - t_now) > lim + 1e-6
                        or abs(t_rec - tilt_plan.live_deg) > lim + 1e-6):
                    raise TiltLimitError(
                        f"tilt guard: {rec.name} is recorded at {t_rec:.2f} deg, the stage is "
                        f"at {t_now:.2f} deg (batch started at {tilt_plan.live_deg:.2f}); "
                        f"limit {lim:g} deg -- not moving")
            if move_method != "none":
                recall_position(
                    sem,
                    rec,
                    move_method=move_method,
                    set_objective=False,  # apply objective after stage motion
                    set_magnification=bool(set_mag),
                    wait_stage_idle=bool(wait_stage_idle),
                    stage_timeout_s=float(stage_timeout_s),
                )
                _log("site_set", recall={"method": str(move_method), "done": True})

            # 2) Apply objective potential at the site
            if not skip_objective:
                sem.set_value("AP_FIB_OBJECTIVE_POTENTIAL", float(rec.fib_objective_potential_v))

            if settle_s and float(settle_s) > 0:
                time.sleep(float(settle_s))

            # 2a) Autofocus (optional, per position). Uses the saved objective
            #     voltage as the expected focus and sweeps above/below; leaves the
            #     column at best focus. Runs BEFORE refine so refine images in focus.
            if name in af_set:
                print(f"[{idx}/{total}] Autofocus at {rec.name}")
                af_res = _autofocus_fn(sem, rec, af_cfg, dry_run=bool(dry_run_focus_refine),
                                       logger=print)
                _log("site_autofocus", af_res)
                print(f"[{idx}/{total}] Autofocus done at {rec.name}")

            # 2b) Refine (optional, per position). Cross-correlates the live image
            #     against the reference BMP and nulls the residual offset with beam
            #     shift; the correction stays applied so milling lands on target.
            if name in rf_set:
                print(f"[{idx}/{total}] Refine at {rec.name}")
                _ref_kw = ({"reference_path": stage_refs[name]} if name in stage_refs
                           else {})
                rf_res = _refine_fn(sem, rec, rf_cfg, ref_base_dir=ref_base_dir,
                                    dry_run=bool(dry_run_focus_refine), logger=print,
                                    **_ref_kw)
                _log("site_refine", rf_res)
                print(f"[{idx}/{total}] Refine done at {rec.name}")

            # 3) Run milling. DEFAULT: the standard pattern (ely_local_path). OVERRIDE:
            #    if this position has an authored pattern (rec.ely_pattern) and the file
            #    exists, mill THAT instead.
            if stage in FORCED_PATTERN_STAGES:
                # Prepolish / Polish: the stage pattern, never the authored rough one.
                per_pos_ely, _src, _msg = resolve_stage_pattern(
                    rec, stage, Path(ely_local_path), ref_base_dir)
            else:
                per_pos_ely, _src, _msg = _resolve_position_ely(
                    rec, Path(ely_local_path), ref_base_dir, force_pattern=force_pattern)
            print(f"[{idx}/{total}] {rec.name}: {_msg}")
            _log("site_pattern", per_pos_ely, _src, _msg)

            job = PatternJob(
                ely_local_path=per_pos_ely,
                load_method=load_method,
                smartfib_drop_dir=Path(smartfib_drop_dir),
                smartfib_drop_name=str(smartfib_drop_name),
                smartfib_load_delay_s=float(smartfib_load_delay_s),
                smartfib_prepare_delay_s=float(smartfib_prepare_delay_s),
                set_patterning_mode_fib=bool(set_patterning_mode_fib),
                ensure_fib_mode_mill=bool(ensure_fib_mode_mill),
                arm=True,
                wait_strategy=wait_strategy,
                wait_timeout_s=float(wait_timeout_s),
                done_confirm_s=float(done_confirm_s),
                sleep_s=float(sleep_s),
            )

            # Phase D: report FIB scan rotation + beam shift vs the recorded capture
            # conditions (warn only -- milling proceeds regardless).
            _cond = _report_mill_conditions(idx, total, rec, sem)
            _log("site_set", mill_conditions=_cond)
            if _src == "authored":
                _report_ely_pattern(idx, total, rec, ref_base_dir)

            # v0.53.1: the objective in effect now (recorded value or autofocus result)
            # is restored for the after image -- SmartFIB may change it while milling.
            pre_mill_obj = _safe_get_float(sem, "AP_FIB_OBJECTIVE_POTENTIAL")
            if pre_mill_obj is None and not skip_objective:
                pre_mill_obj = float(rec.fib_objective_potential_v)
            # v0.55.1 (review): the window went away BEFORE this site's mill was started
            # (during recall, autofocus or refine). Do not start a mill nobody can see;
            # the clean-up below resets the beam shift (nothing is milling).
            if _output_lost():
                raise BatchStopped(
                    f"the pyFIB window that started this batch is gone ({_lost_reason()}); "
                    f"stopped at {rec.name} before its mill was started: "
                    f"{idx - 1} of {total} position(s) were milled")
            runner_entered = True
            runner.run(job)
            mill_confirmed_done = str(wait_strategy) in CONFIRMED_WAIT_STRATEGIES
            print(f"[{idx}/{total}] Completed milling at {rec.name}")

            # 3b) After-mill image (optional, v0.41.0). Taken BEFORE the beam-shift reset
            #     so the frame shares the view the pattern was milled in. Skipped (with
            #     a warning) unless milling is confirmed inactive; never fails the site.
            if after_image and not dry_run_focus_refine:
                try:
                    from .after_image import grab_after_mill_image
                    img_dir = (Path(after_image_dir) if after_image_dir else
                               (Path(ref_base_dir) / "images" if ref_base_dir else Path("images")))
                    a_path, a_warn = grab_after_mill_image(
                        sem, rec, img_dir, milling_active=lambda: _is_milling_active(sem),
                        log=print, stage=stage, objective_v=pre_mill_obj)
                except Exception as e:  # noqa: BLE001
                    a_path, a_warn = None, f"after-mill image for {rec.name} FAILED ({e})"
                if a_path is not None:
                    print(f"[{idx}/{total}] after-mill image: {a_path}")
                    if stage is not None and db_path is not None:
                        # v0.51.0: the latest after image of this stage -> positions.json
                        # (the next stage refines to it). Never fails the site.
                        try:
                            rel = record_after_image(db_path, rec.name, stage, a_path, db=db)
                            print(f"[{idx}/{total}] recorded {STAGE_LABEL[stage]} after image "
                                  f"for {rec.name}: {rel}")
                        except Exception as e:  # noqa: BLE001
                            a_warn = ((a_warn + " ") if a_warn else "") + (
                                f"could not record it in positions.json ({e})")
                if a_warn:
                    print(f"[{idx}/{total}] *** WARNING: {a_warn} ***")
                _log("site_after_image", a_path, a_warn)

            if record_fiducial:
                # Remember that a fiducial now exists on this side of the image at this
                # position (drift correction will look for it).
                import dataclasses as _dc
                try:
                    db.positions[rec.name] = _dc.replace(db.positions.get(rec.name, rec),
                                                         fiducial_side=str(record_fiducial))
                    if db_path is not None:
                        db.save(Path(db_path))
                    print(f"[{idx}/{total}] recorded fiducial_side = {record_fiducial} "
                          f"for {rec.name}")
                    _log("site_set", fiducial_recorded=str(record_fiducial))
                except Exception as e:  # noqa: BLE001
                    print(f"[{idx}/{total}] WARNING: could not record fiducial_side: {e}")
            _log("site_end", "milled")

        except KeyboardInterrupt:
            # PatternRunner already attempts abort milling on Ctrl+C; just re-raise
            _log("site_end", "stopped", "interrupted (Ctrl+C)")
            raise

        except BatchStopped as e:
            # never swallowed by --continue-on-error: the batch ends here
            _say(f"Stopped at {rec.name}: {e}")
            _log("site_end", "stopped", str(e))
            raise

        except Exception as e:
            # _say: these lines must never raise themselves (v0.55.1) -- an error while
            # reporting an error skipped the run-log entry and went straight to clean-up.
            _say(f"Error at {rec.name}: {e}")
            if runner_entered and not mill_confirmed_done:
                _say(f"[{idx}/{total}] *** The pattern at {rec.name} may still be running in "
                     f"SmartFIB: pyFIB has NOT stopped it and does not touch the FIB beam "
                     f"shift while SmartSEM indicates milling. Check SmartFIB. ***")
            _log("site_end", "error", f"{type(e).__name__}: {e}")
            if not continue_on_error:
                raise
            _say("Continuing to next position due to --continue-on-error...")

        finally:
            # Return FIB beam shift to zero after milling completes (or after an
            # error) so any refine correction applied for THIS site does not carry
            # into the next one. Runs on success, handled/again-raised errors, and
            # Ctrl+C, leaving the column ready for the next site.
            # v0.55.1 (#16): ONLY while SmartSEM does not indicate milling. When the site
            # ended without a confirmed end of milling (an error or Ctrl+C after the mill
            # was started, or a wait mode that does not confirm), "not milling" must hold
            # for the confirm-done window first. Otherwise the reset is left for the next
            # site's start (after its interlock) or for Go to.
            if reset_beam_shift_between_positions and not dry_run_focus_refine:
                _quiet = (float(done_confirm_s)
                          if (runner_entered and not mill_confirmed_done) else 0.0)
                _res = _reset_beam_shift_when_idle(
                    sem, logger=_say, where=f"after {rec.name}", quiet_s=_quiet)
                if _res is None:
                    beam_reset_pending = True
                    # noted at once (review): the batch may end by an error or a stop
                    # before the end-of-batch block below is reached
                    _log("run_note", "beam_shift_not_reset",
                         f"the FIB beam shift was NOT reset after {rec.name} (SmartSEM "
                         f"indicated milling); Go to resets it")
                else:
                    beam_reset_pending = False
                    _log("run_note", "beam_shift_not_reset", None)
                    if _res:
                        _say(f"[{idx}/{total}] FIB beam shift X/Y reset to 0 "
                             f"after {rec.name}.")

    # v0.55.1 (review): the batch must not end quietly with a refine correction still on
    # the column. One more guarded try (milling may just have ended); otherwise say so
    # loudly and note it in the run log. Still never written while milling is indicated.
    if beam_reset_pending and reset_beam_shift_between_positions and not dry_run_focus_refine:
        _res = _reset_beam_shift_when_idle(sem, logger=_say, where="at the end of the batch",
                                           quiet_s=float(done_confirm_s))
        if _res is None:
            msg = ("the FIB beam shift was NOT reset at the end of the batch (SmartSEM "
                   "indicates milling). It still holds the last site's correction: press "
                   "Go to before the next capture or mill (it resets it).")
            _say(f"*** WARNING: {msg} ***")
            _log("run_note", "beam_shift_not_reset", msg)
        else:
            _log("run_note", "beam_shift_not_reset", None)
            if _res:
                _say("[beam-shift] FIB beam shift X/Y reset to 0 at the end of the batch.")
