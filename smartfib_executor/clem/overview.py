"""FIB and SEM overviews at SEM normal for CLEM targeting (v0.52.0).

Both refuse unless the stage is at SEM normal (T 29-31 deg, R 12 +/- 1) and SmartSEM has
read "not milling" for a 5 s window. Neither moves the stage.

FIB overview (pyMap plan F4): zero the FIB beam shift (refuse if it will not read 0),
then the normal capture (capture_position) at 400x, 1024 x 768, with NO 800x frame, into
<session>/clem/overview_positions.json. One FIB frame of dose over the ~286 x 214 um field.

SEM overview (Evan, 2026-10-09: pyFIB grabs it and records the stage): switch to SEM
imaging (CMD_FIB_MODE_SEM), set AP_MAG, grab ONE fresh frame (frame_grab: end-of-frame
freeze), and record it in <session>/clem/sem_overviews.json (pyfib.sem_overviews.v1)
with the stage and the SEM settings. Detector, scan speed, brightness and contrast are
left as the operator set them. Only AP_MAG and DP_FREEZE_ON are set and only SEM-mode /
freeze commands are sent -- enforced by _SemOnly. The pixel size is AP_WIDTH / grabbed
width and is NOMINAL until measured; AP_PIXEL_SIZE is recorded for the log only (it
follows the display store -- the v0.9.1 store-vs-grab lesson), and a mismatch between
the two warns that the store size differs from the grab.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import (GRID_FIXTURES, IMAGES_DIRNAME, OVERVIEW_POSITIONS, SEM_OVERVIEWS,
               SEM_SCHEMA, clem_dir)
from . import limits as L


class OverviewRefused(RuntimeError):
    pass


SEM_ALLOWED_SETS = frozenset({"AP_MAG", "DP_FREEZE_ON"})
SEM_ALLOWED_CMDS = frozenset({"CMD_FIB_MODE_SEM", "CMD_UNFREEZE_ZONE", "CMD_FREEZE_ZONE",
                              "CMD_FREEZE_ALL"})


class _SemOnly:
    """Wraps the SmartSEM connection for the SEM overview: reads and Grab() pass through;
    only AP_MAG / DP_FREEZE_ON may be set and only SEM-mode / freeze commands sent; any
    stage call raises. A structural guarantee, like the SEM-autofocus guard."""

    def __init__(self, sem):
        object.__setattr__(self, "_sem", sem)

    def set_value(self, mnemonic, value):
        if mnemonic not in SEM_ALLOWED_SETS:
            raise OverviewRefused(f"SEM overview may not set {mnemonic!r}")
        return self._sem.set_value(mnemonic, value)

    def execute(self, command):
        if command not in SEM_ALLOWED_CMDS:
            raise OverviewRefused(f"SEM overview may not execute {command!r}")
        return self._sem.execute(command)

    def move_stage(self, *a, **k):
        raise OverviewRefused("SEM overview never moves the stage")

    def __getattr__(self, name):
        if name.startswith(("move", "goto", "wait_stage")):
            raise OverviewRefused(f"SEM overview may not call {name}")
        return getattr(self._sem, name)


def _f(sem, mnemonic) -> Optional[float]:
    try:
        v = float(sem.get_float(mnemonic))
        return v if math.isfinite(v) else None
    except Exception:  # noqa: BLE001
        return None


def _s(sem, mnemonic) -> Optional[str]:
    try:
        return str(sem.get_str(mnemonic)).strip()
    except Exception:  # noqa: BLE001
        return None


def stage_readings(sem) -> Dict[str, Optional[float]]:
    out = {k: _f(sem, f"AP_STAGE_AT_{a}") for k, a in
           (("x_m", "X"), ("y_m", "Y"), ("z_m", "Z"), ("m_m", "M"), ("t_deg", "T"),
            ("r_deg", "R"))}
    return out


def check_sem_normal(sem) -> Dict[str, Optional[float]]:
    """Refuse unless the stage reads T 29-31 deg and R 12 +/- 1 deg."""
    st = stage_readings(sem)
    t, r = st["t_deg"], st["r_deg"]
    lo, hi = L.SEM_NORMAL_TILT_RANGE_DEG
    if t is None or r is None:
        raise OverviewRefused("stage tilt / rotation unreadable; overview refused")
    d = (r - L.SEM_NORMAL_ROT_DEG + 180.0) % 360.0 - 180.0
    if not (lo <= t <= hi) or abs(d) > L.SEM_NORMAL_ROT_TOL_DEG:
        raise OverviewRefused(
            f"the stage is at T {t:.2f} deg, R {r:.2f} deg -- overviews are taken at SEM "
            f"normal (T {lo:g}-{hi:g}, R {L.SEM_NORMAL_ROT_DEG:g} +/- "
            f"{L.SEM_NORMAL_ROT_TOL_DEG:g}). Go to the grid first.")
    return st


def grid_fixture(grid: str) -> Optional[Dict[str, float]]:
    """X/Y (m) of the grid's SEM-normal setup fixture (assets/setup_positions.json), or
    None."""
    name = GRID_FIXTURES.get(grid, grid)
    try:
        from ..setup_move.loader import load_setup_db
        st = load_setup_db().get(name).stage_at
        return {"x_m": float(st.x_m), "y_m": float(st.y_m)}
    except Exception:  # noqa: BLE001
        return None


def _grid_warning(grid: str, st: Dict[str, Optional[float]]) -> Optional[str]:
    fx = grid_fixture(grid)
    if not fx or st.get("x_m") is None or st.get("y_m") is None:
        return None
    d = math.hypot(st["x_m"] - fx["x_m"], st["y_m"] - fx["y_m"])
    if d > L.GRID_FIXTURE_WARN_M:
        return (f"the stage is {d * 1e3:.2f} mm from the {GRID_FIXTURES.get(grid, grid)} "
                f"fixture -- is {grid} the right grid?")
    return None


def _slug(grid: str) -> str:
    return "".join(c for c in str(grid).lower() if c.isalnum()) or "grid"


def next_name(existing, prefix: str) -> str:
    n = 1
    existing = set(existing)
    while f"{prefix}_{n}" in existing:
        n += 1
    return f"{prefix}_{n}"


def _atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(path))


def load_sem_overviews(session_dir) -> Dict[str, Any]:
    """{"schema", "overviews": [...]} (v0.52.1: list key "overviews", as pyMap reads it;
    a v0.52.0 file's "entries" is read too and rewritten as "overviews")."""
    p = clem_dir(session_dir) / SEM_OVERVIEWS
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("schema") == SEM_SCHEMA:
            return {"schema": SEM_SCHEMA,
                    "overviews": list(d.get("overviews", d.get("entries", [])))}
    return {"schema": SEM_SCHEMA, "overviews": []}


# ------------------------------------------------------------------------- FIB
def capture_fib_overview(sem, session_dir, grid: str, *, mag: float = L.FIB_OVERVIEW_MAG,
                         name: Optional[str] = None, guard_s: Optional[float] = None,
                         frame_wait_s: float = 3.0, log: Callable[[str], None] = print,
                         _capture=None):
    """Capture the FIB overview into <session>/clem/overview_positions.json. Returns the
    PositionRecord (image path relative to the clem folder)."""
    import dataclasses
    from ..batch import _zero_fib_beam_shift
    from ..positions import PositionDatabase, capture_position
    from ..site_ops import GUARD_S, SiteRefused, _milling_guard
    clem = clem_dir(session_dir)
    try:
        _milling_guard(sem, "FIB overview", GUARD_S if guard_s is None else guard_s, log=log)
    except SiteRefused as e:
        raise OverviewRefused(str(e))
    st = check_sem_normal(sem)
    pf = clem / OVERVIEW_POSITIONS
    db = PositionDatabase.load(pf) if pf.exists() else PositionDatabase()
    name = name or next_name(db.positions.keys(), f"clem_ov_{_slug(grid)}")
    log(f"[clem] FIB overview {name}: zeroing the FIB beam shift first.")
    if not _zero_fib_beam_shift(sem, logger=log):
        raise OverviewRefused("the FIB beam shift does not read 0 after zeroing; overview "
                              "refused (a beam-shifted overview would offset every target)")
    img = clem / IMAGES_DIRNAME / f"{name}.bmp"
    img.parent.mkdir(parents=True, exist_ok=True)
    log(f"[clem] FIB overview {name}: {mag:g}x, {L.OVERVIEW_WIDTH_PX} x "
        f"{L.OVERVIEW_HEIGHT_PX}, no 800x frame -> {img}")
    from ..frame_grab import FreezeModeError
    try:
        rec = (_capture or capture_position)(
            sem, name, fib_magnification_x=float(mag), image_bmp=img,
            grab_width=L.OVERVIEW_WIDTH_PX, grab_height=L.OVERVIEW_HEIGHT_PX,
            include_tilt=True, coarse_magnification_x=None, frame_wait_s=frame_wait_s)
    except FreezeModeError as e:            # v0.54.0: nothing was scanned
        raise OverviewRefused(str(e))
    rel = Path(str(rec.image_bmp)).resolve()
    try:
        rel = rel.relative_to(clem.resolve())
    except ValueError:
        pass
    note = f"CLEM FIB overview, {grid} at SEM normal"
    rec = dataclasses.replace(rec, image_bmp=Path(rel).as_posix(),
                              notes=(note + ("\n" + rec.notes if rec.notes else "")))
    db.upsert(rec)
    db.save(pf)
    w = _grid_warning(grid, st)
    if w:
        log(f"*** WARNING: {w} ***")
    for label, v in (("scan rotation", rec.fib_scan_rotation_deg),
                     ("beam shift X", rec.fib_beam_shift_x_m),
                     ("beam shift Y", rec.fib_beam_shift_y_m)):
        if v is None:
            log(f"*** WARNING: {label} was not recorded; targets on this overview will be "
                f"refused ***")
    log(f"[clem] recorded {name} in {pf}")
    return rec


# ------------------------------------------------------------------------- SEM
def capture_sem_overview(sem, session_dir, grid: str, *, mag: float = L.SEM_OVERVIEW_MAG,
                         name: Optional[str] = None, guard_s: Optional[float] = None,
                         width: int = L.OVERVIEW_WIDTH_PX, height: int = L.OVERVIEW_HEIGHT_PX,
                         frame_wait_s: float = 3.0, frozen_timeout_s: float = 60.0,
                         settle_s: float = 0.5, mode_timeout_s: float = 15.0,
                         clock: Callable[[], float] = time.time,
                         sleep: Callable[[float], None] = time.sleep,
                         log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Grab one SEM frame and record it with the stage. Returns the entry."""
    from ..frame_grab import grab_fresh_frame
    from .targets import file_sha256
    from ..site_ops import GUARD_S, SiteRefused, _milling_guard
    g = _SemOnly(sem)
    clem = clem_dir(session_dir)
    try:
        _milling_guard(g, "SEM overview", GUARD_S if guard_s is None else guard_s, log=log)
    except SiteRefused as e:
        raise OverviewRefused(str(e))
    st = check_sem_normal(g)
    data = load_sem_overviews(session_dir)
    name = name or next_name([e.get("name") for e in data["overviews"]], f"clem_sem_{_slug(grid)}")

    mode = _s(g, "DP_FIB_IMAGING")
    if (mode or "").upper() != "SEM":
        log(f"[clem] imaging mode {mode!r} -> CMD_FIB_MODE_SEM")
        g.execute("CMD_FIB_MODE_SEM")
        t0 = clock()
        while (_s(g, "DP_FIB_IMAGING") or "").upper() != "SEM":
            if clock() - t0 > mode_timeout_s:
                raise OverviewRefused(f"SEM imaging did not come on within "
                                      f"{mode_timeout_s:g} s")
            sleep(0.25)
    g.set_value("AP_MAG", float(mag))
    if settle_s > 0:
        sleep(settle_s)
    got = _f(g, "AP_MAG")
    if got is not None and abs(got / float(mag) - 1.0) > 0.01:
        raise OverviewRefused(f"SEM magnification reads {got:g}, asked {mag:g}")

    img = clem / IMAGES_DIRNAME / f"{name}.bmp"
    img.parent.mkdir(parents=True, exist_ok=True)
    log(f"[clem] SEM overview {name}: {mag:g}x, {width} x {height} -> {img}")
    from ..frame_grab import FreezeModeError
    try:
        warn = grab_fresh_frame(g, str(img), width=int(width), height=int(height),
                                frame_wait_s=frame_wait_s, frozen_timeout_s=frozen_timeout_s,
                                log=log)
    except FreezeModeError as e:            # v0.54.0: nothing was scanned
        raise OverviewRefused(str(e))
    try:
        from ..positions import _recover_grab_file
        w2 = _recover_grab_file(g, img)
        if w2:
            warn = (warn + " " if warn else "") + w2
    except Exception:  # noqa: BLE001
        pass
    if not img.exists():
        raise OverviewRefused(f"Grab() produced no file at {img}")
    from ..frame_grab import is_stale_warning
    if is_stale_warning(warn):
        # v0.53.4: the file is the previous frame store, not this overview.
        # v0.54.0: so is a frame that ended too soon or repeats the previous grab.
        sus = img.with_name(img.stem + "_suspect.bmp")
        try:
            img.replace(sus)
        except Exception:  # noqa: BLE001
            pass
        raise OverviewRefused(f"the SEM overview is not a usable new frame ({warn}) Kept as "
                              f"{sus.name}; nothing recorded. Try again.")
    try:
        from PIL import Image
        with Image.open(img) as im:
            wpx, hpx = im.size
    except Exception:  # noqa: BLE001
        wpx, hpx = int(width), int(height)

    fw, fh = _f(g, "AP_WIDTH"), _f(g, "AP_HEIGHT")
    ap_px = _f(g, "AP_PIXEL_SIZE")
    px = (fw / wpx) if (fw and wpx) else None
    warnings = [warn] if warn else []
    if fw and ap_px:
        store_w = fw / ap_px
        if abs(store_w / wpx - 1.0) > 0.02:
            warnings.append(f"AP_WIDTH / AP_PIXEL_SIZE = {store_w:.0f} px but {wpx} px were "
                            f"grabbed: the store size differs from the grab; pixel size "
                            f"uncertain")
    gw = _grid_warning(grid, st)
    if gw:
        warnings.append(gw)
    rot_state = _s(g, "DP_SCAN_ROT")
    entry = {
        "name": name, "grid": grid, "kind": "sem_overview",
        "image": (Path(IMAGES_DIRNAME) / img.name).as_posix(), "image_sha256": file_sha256(img),
        "image_width_px": int(wpx), "image_height_px": int(hpx),
        "sem_magnification_x": got if got is not None else float(mag),
        "field_width_m": fw, "field_height_m": fh,
        "pixel_size_m": px, "pixel_size_nominal": True,
        "pixel_size_source": "AP_WIDTH / grabbed width (nominal, unmeasured)",
        "ap_pixel_size_m": ap_px,
        "working_distance_m": _f(g, "AP_WD"), "eht_v": _f(g, "AP_ACTUALKV"),
        "detector": _s(g, "DP_DETECTOR_TYPE"),
        "scan_rotation_deg": _f(g, "AP_SCANROTATION"), "scan_rotation_state": rot_state,
        "scan_rotation_on": (None if not rot_state else
                             rot_state.strip().lower() in ("on", "yes", "1", "true")),
        "tilt_correction_angle_deg": _f(g, "AP_TILT_ANGLE"),
        # No tilt-correction on/off state in the Remote API manual this was built from;
        # not guessed (an unknown mnemonic is never probed).
        "tilt_correction_on": None,
        "stage_at": st,
        "captured_unix_s": clock(),
        "captured": datetime.fromtimestamp(clock()).astimezone().isoformat(timespec="seconds"),
        "warnings": warnings,
    }
    data["overviews"] = [e for e in data["overviews"] if e.get("name") != name] + [entry]
    _atomic_json(clem / SEM_OVERVIEWS, data)
    for w in warnings:
        log(f"*** WARNING: {w} ***")
    if entry["scan_rotation_deg"] and abs(entry["scan_rotation_deg"]) > 0.05:
        log(f"*** WARNING: SEM scan rotation is {entry['scan_rotation_deg']:.2f} deg ***")
    log(f"[clem] recorded {name} in {clem / SEM_OVERVIEWS} (scan left frozen)")
    return entry
