"""Read, check and plan pyMap's CLEM targets (files only; nothing talks to SmartSEM).

Input: <session>/clem/<name>.json, schema pymap.targets.v2, with its ready marker
<name>.json.ready.json (schema pymap.ready.v1, "sha256" of the targets file). Shape
(tile-ready; this version accepts exactly ONE fib_position source):

    {"schema": "pymap.targets.v2", "pymap_version": "...", "created": "...",
     "sources": [{"id": "ov1", "kind": "fib_position",
                  "positions_file": "overview_positions.json",   # relative to clem/
                  "position": "clem_ov_grid1_1", "image": "images/clem_ov_grid1_1.bmp",
                  "image_sha256": "sha256:...", "image_width_px": 1024,
                  "image_height_px": 768, "fib_pixel_size_m": 2.79e-07,
                  "captured_unix_s": ..., "stage_at": {"x_m": .., "y_m": .., "z_m": ..,
                                                       "t_deg": .., "r_deg": ..}}],
     "registration": {"chain": [...], "combined_loo_rms_um": 1.1,
                      "combined_loo_rms_grid_um": 1.6},
     "pixel_convention": "corner: ...",
     "targets": [{"id": "T1", "label": "1", "source": "ov1",
                  "fib_px": [612.4, 301.8], "layout_um": [..], "warnings": [], "note": ""}]}

Checks (refuse = never planned for a visit):
  file   schema; ready marker present and its hash matches; <= 30 targets; unique ids;
         exactly one fib_position source (tiling: later); the source record exists,
         its image hash / size / pixel size / capture time / stage match the echoes;
         registration error <= 3 um (warn > 1 um) unless the operator overrides.
  target fib_px inside the overview image; the image point converts to a stage move
         (refine.geometry.target_stage_delta: model tilt/rotation range, scan rotation
         and beam shift 0).

Output: TargetPlan (also written as <session>/clem/pyfib_clem_plan.json,
pyfib.clem_targets.v1): per target the stage X/Y (the overview's X/Y + the delta;
Z/T/R/M stay as recorded -- a visit moves X/Y only), its offset from the overview
centre, warnings and refusals; plus whether the overview magnification's stage scale
has been measured (a refine --calibrate-stage at that magnification and tilt).
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import (CLEM_DIRNAME, OVERVIEW_POSITIONS, PLAN_FILE, PLAN_SCHEMA, READY_SCHEMA,
               SEM_OVERVIEWS, TARGETS_SCHEMA)
from . import limits as L


def file_sha256(path) -> str:
    """'sha256:<hex>' of a file (the elymaker / pyMap convention)."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _norm_sha(v) -> str:
    s = str(v or "").strip().lower()
    return s if s.startswith("sha256:") else ("sha256:" + s if s else "")


def ready_marker_path(targets_path) -> Path:
    return Path(str(targets_path) + ".ready.json")


def _resolve(base, p) -> Optional[Path]:
    if not p:
        return None
    q = Path(str(p).replace("\\", "/"))
    return q if q.is_absolute() else Path(base) / q


@dataclass
class TargetRow:
    id: str
    label: str
    source: str
    fib_px: Tuple[float, float]
    status: str = "ok"                     # ok | warn | refused
    reasons: List[str] = field(default_factory=list)      # why refused
    warnings: List[str] = field(default_factory=list)     # pyMap's + pyFIB's
    note: str = ""
    stage_dx_m: Optional[float] = None
    stage_dy_m: Optional[float] = None
    stage_x_m: Optional[float] = None
    stage_y_m: Optional[float] = None
    offset_um: Optional[float] = None      # image-plane distance from the overview centre

    def refuse(self, why: str) -> None:
        self.status = "refused"
        self.reasons.append(why)

    def warn(self, why: str) -> None:
        if self.status == "ok":
            self.status = "warn"
        self.warnings.append(why)


@dataclass
class TargetPlan:
    targets_file: str
    targets_sha256: Optional[str] = None
    ok: bool = True                         # False: the whole file is refused
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    source: Dict[str, Any] = field(default_factory=dict)
    overview_positions_file: Optional[str] = None
    overview: Any = None                    # PositionRecord
    qc_um: Optional[float] = None
    qc_kind: Optional[str] = None           # "grid" | "image"
    qc_override: bool = False
    scale: Dict[str, Any] = field(default_factory=lambda: {"verified": False,
                                                           "text": "not checked"})
    rows: List[TargetRow] = field(default_factory=list)

    def refuse(self, why: str) -> None:
        self.ok = False
        self.errors.append(why)

    def counts(self) -> Dict[str, int]:
        c = {"ok": 0, "warn": 0, "refused": 0}
        for r in self.rows:
            c[r.status] = c.get(r.status, 0) + 1
        return c

    def lines(self) -> List[str]:
        out = [f"[clem] targets file: {self.targets_file}"]
        if self.overview is not None:
            st = self.overview.stage_at
            out.append(f"[clem] overview {self.overview.name}: {self.overview.fib_magnification_x:g}x, "
                       f"stage X {st.x_m * 1e3:.4f} Y {st.y_m * 1e3:.4f} Z {st.z_m * 1e3:.4f} mm, "
                       f"T {st.t_deg if st.t_deg is None else round(st.t_deg, 2)} R {st.r_deg:.2f}")
        if self.qc_um is not None:
            out.append(f"[clem] registration error {self.qc_um:.2f} um ({self.qc_kind} plane)"
                       + (" -- OVERRIDDEN by the operator" if self.qc_override else ""))
        out.append(f"[clem] overview scale: {self.scale.get('text')}")
        for e in self.errors:
            out.append(f"[clem] REFUSED: {e}")
        for w in self.warnings:
            out.append(f"[clem] WARNING: {w}")
        for r in self.rows:
            pos = ("" if r.stage_x_m is None else
                   f"X {r.stage_x_m * 1e3:.4f} Y {r.stage_y_m * 1e3:.4f} mm "
                   f"(d {r.stage_dx_m * 1e6:+.1f}, {r.stage_dy_m * 1e6:+.1f} um; "
                   f"{r.offset_um:.0f} um from centre)")
            why = "; ".join(r.reasons + r.warnings)
            out.append(f"[clem] {r.id} ({r.label}): {r.status.upper():7s} {pos}"
                       + (f" -- {why}" if why else ""))
        c = self.counts()
        out.append(f"CLEM-PLAN: ok={c['ok']} warn={c['warn']} refused={c['refused']} "
                   f"file={'ok' if self.ok else 'refused'}")
        return out

    def to_json(self) -> Dict[str, Any]:
        try:
            from .. import __version__ as ver
        except Exception:  # noqa: BLE001
            ver = None
        ov = self.overview
        return {
            "schema": PLAN_SCHEMA, "pyfib_version": ver,
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "targets_file": self.targets_file, "targets_sha256": self.targets_sha256,
            "file_ok": self.ok, "errors": self.errors, "warnings": self.warnings,
            "overview": None if ov is None else {
                "positions_file": self.overview_positions_file, "position": ov.name,
                "magnification_x": ov.fib_magnification_x,
                "stage_at": asdict(ov.stage_at)},
            "registration_error_um": self.qc_um, "registration_error_plane": self.qc_kind,
            "registration_override": self.qc_override,
            "overview_scale": self.scale,
            "move_axes": "XY",                      # Z/T/R/M are never changed
            "targets": [asdict(r) for r in self.rows],
        }


def _ready_ok(plan: TargetPlan, path: Path) -> None:
    mp = ready_marker_path(path)
    if not mp.exists():
        plan.refuse(f"no ready marker ({mp.name}): pyMap has not marked this file ready")
        return
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        plan.refuse(f"ready marker unreadable: {e}")
        return
    if str(m.get("schema", "")).split(".")[:2] != READY_SCHEMA.split(".")[:2]:
        plan.refuse(f"ready marker has schema {m.get('schema')!r}, expected {READY_SCHEMA}")
        return
    want = _norm_sha(m.get("sha256") or m.get("content_hash") or m.get("file_sha256"))
    if not want:
        plan.refuse("ready marker has no sha256")
    elif want != plan.targets_sha256:
        plan.refuse("the targets file has CHANGED since pyMap marked it ready")


def _check_source(plan: TargetPlan, src: Dict[str, Any], clem: Path):
    """Load and cross-check the source overview record. Returns the record or None."""
    from ..positions import PositionDatabase
    if src.get("kind") != "fib_position":
        plan.refuse(f"source kind {src.get('kind')!r} is not supported yet (fib_position only)")
        return None
    pf = _resolve(clem, src.get("positions_file") or OVERVIEW_POSITIONS)
    plan.overview_positions_file = str(pf)
    try:
        db = PositionDatabase.load(pf)
        rec = db.get(str(src.get("position")))
    except Exception as e:  # noqa: BLE001
        plan.refuse(f"source position {src.get('position')!r} not found in {pf}: {e}")
        return None
    plan.overview = rec
    img = _resolve(pf.parent, rec.image_bmp)
    if img is None or not img.exists():
        plan.refuse(f"overview image missing: {img}")
        return rec
    if src.get("image_sha256") and _norm_sha(src["image_sha256"]) != file_sha256(img):
        plan.refuse(f"overview image {img.name} differs from the one pyMap registered "
                    f"(hash mismatch)")
    if not (rec.image_width_px and rec.image_height_px and rec.fib_pixel_size_m):
        plan.refuse(f"overview record {rec.name} has no image size / pixel size (v1 record?)")
        return rec
    for key, val in (("image_width_px", rec.image_width_px),
                     ("image_height_px", rec.image_height_px)):
        if src.get(key) is not None and int(src[key]) != int(val):
            plan.refuse(f"{key} {src[key]} does not match the record ({val})")
    if src.get("fib_pixel_size_m") is not None and \
            abs(float(src["fib_pixel_size_m"]) / float(rec.fib_pixel_size_m) - 1.0) > 1e-3:
        plan.refuse(f"pixel size {src['fib_pixel_size_m']} does not match the record "
                    f"({rec.fib_pixel_size_m})")
    if src.get("captured_unix_s") is not None and \
            abs(float(src["captured_unix_s"]) - float(rec.captured_unix_s)) > 1.0:
        plan.refuse(f"{rec.name} was re-captured after pyMap registered it")
    echo = src.get("stage_at") or {}
    st = rec.stage_at
    for k, v, tol in (("x_m", st.x_m, 1e-6), ("y_m", st.y_m, 1e-6), ("z_m", st.z_m, 1e-6),
                      ("t_deg", st.t_deg, 0.01), ("r_deg", st.r_deg, 0.01)):
        if echo.get(k) is not None and v is not None and abs(float(echo[k]) - float(v)) > tol:
            plan.refuse(f"stage {k} echo {echo[k]} does not match the record ({v})")
    if st.t_deg is None:
        plan.refuse(f"{rec.name} has no recorded tilt")
    elif not (L.SEM_NORMAL_TILT_RANGE_DEG[0] <= st.t_deg <= L.SEM_NORMAL_TILT_RANGE_DEG[1]):
        plan.warnings.append(f"{rec.name} was not taken at SEM normal (T {st.t_deg:.2f} deg)")
    return rec


def _check_qc(plan: TargetPlan, data: Dict[str, Any], accept_qc: bool) -> None:
    reg = data.get("registration") or {}
    grid = reg.get("combined_loo_rms_grid_um")
    img = reg.get("combined_loo_rms_um")
    if grid is not None:
        plan.qc_um, plan.qc_kind = float(grid), "grid"
    elif img is not None:
        plan.qc_um, plan.qc_kind = float(img), "image"
    if plan.qc_um is None:
        if accept_qc:
            plan.qc_override = True
            plan.warnings.append("no registration error given -- accepted by the operator")
        else:
            plan.refuse("no registration error (combined leave-one-out RMS) given; "
                        "override only if you have checked the registration")
        return
    if plan.qc_um > L.QC_REFUSE_UM:
        if accept_qc:
            plan.qc_override = True
            plan.warnings.append(f"registration error {plan.qc_um:.2f} um is above "
                                 f"{L.QC_REFUSE_UM:g} um -- accepted by the operator")
        else:
            plan.refuse(f"registration error {plan.qc_um:.2f} um is above the "
                        f"{L.QC_REFUSE_UM:g} um limit")
    elif plan.qc_um > L.QC_WARN_UM:
        plan.warnings.append(f"registration error {plan.qc_um:.2f} um is above "
                             f"{L.QC_WARN_UM:g} um")


def find_stage_calibration(magnification: float, tilt_deg: Optional[float],
                           dirs: Iterable) -> Optional[Dict[str, Any]]:
    """Newest refine --calibrate-stage result (refine_stage_calibration*.json) measured
    at this magnification (+/-5 %) and tilt (+/-1 deg) in any of `dirs`, or None."""
    best = None
    for d in dirs:
        if not d or not Path(d).is_dir():
            continue
        for p in Path(d).glob("refine_stage_calibration*.json"):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                mag = float(c.get("magnification"))
            except Exception:  # noqa: BLE001
                continue
            if abs(mag / float(magnification) - 1.0) > L.SCALE_CAL_MAG_FRAC:
                continue
            t = c.get("tilt_deg")
            if tilt_deg is None or t is None or abs(float(t) - float(tilt_deg)) > L.SCALE_CAL_TILT_DEG:
                continue
            if best is None or p.stat().st_mtime > best[0]:
                best = (p.stat().st_mtime, p, c)
    return None if best is None else {"path": str(best[1]), **best[2]}


def _check_scale(plan: TargetPlan, cal_dirs) -> None:
    from ..refine.geometry import stage_scale
    ov = plan.overview
    if ov is None:
        return
    mag, tilt = float(ov.fib_magnification_x), ov.stage_at.t_deg
    cal = find_stage_calibration(mag, tilt, cal_dirs)
    if cal is None:
        plan.scale = {"verified": False,
                      "text": f"UNVERIFIED -- no stage calibration at {mag:g}x / "
                              f"T {tilt if tilt is None else round(tilt, 1)} deg "
                              f"(refine --calibrate-stage --magnification {mag:g})"}
        plan.warnings.append(f"the {mag:g}x stage scale is unmeasured; visits stay off "
                             f"until it is")
        return
    mx, my = stage_scale(float(tilt))
    try:
        gx = abs(float(cal["axes"]["x"]["response_raw"]))
        gy = abs(float(cal["axes"]["y"]["response_raw"]))
    except Exception:  # noqa: BLE001
        plan.scale = {"verified": False, "text": f"calibration {cal['path']} unreadable"}
        return
    ex, ey = gx / mx - 1.0, gy / my - 1.0
    ok = abs(ex) <= L.SCALE_OK_FRAC and abs(ey) <= L.SCALE_OK_FRAC
    plan.scale = {"verified": ok, "path": cal["path"],
                  "measured": [round(gx, 4), round(gy, 4)],
                  "model": [round(mx, 4), round(my, 4)],
                  "error": [round(ex, 4), round(ey, 4)],
                  "text": (f"{'verified' if ok else 'OFF'} at {mag:g}x: X {gx:.3f} (model "
                           f"{mx:.3f}, {ex * 100:+.1f}%), Y {gy:.3f} (model {my:.3f}, "
                           f"{ey * 100:+.1f}%)")}
    if not ok:
        plan.warnings.append(f"measured {mag:g}x stage scale differs from the model by more "
                             f"than {L.SCALE_OK_FRAC * 100:g}%: {plan.scale['text']}")


def plan_targets(targets_path, *, accept_qc: bool = False,
                 cal_dirs: Optional[Iterable] = None) -> TargetPlan:
    """Check a pyMap targets file and compute each target's stage X/Y. Files only."""
    from ..refine.geometry import TargetGeometryError, target_stage_delta
    path = Path(targets_path)
    plan = TargetPlan(targets_file=str(path))
    clem = path.parent
    try:
        plan.targets_sha256 = file_sha256(path)
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        plan.refuse(f"targets file unreadable: {e}")
        return plan
    if data.get("schema") != TARGETS_SCHEMA:
        plan.refuse(f"schema {data.get('schema')!r}, expected {TARGETS_SCHEMA}")
        return plan
    _ready_ok(plan, path)

    sources = data.get("sources")
    if sources is None and isinstance(data.get("source"), dict):     # draft single form
        sources = [dict(data["source"], id=data["source"].get("id", "src"))]
    sources = list(sources or [])
    if len(sources) != 1:
        plan.refuse(f"{len(sources)} sources: exactly one FIB overview is supported "
                    f"(tiling is not implemented yet)")
    src = sources[0] if sources else {}
    plan.source = dict(src)
    rec = _check_source(plan, src, clem) if src else None
    _check_qc(plan, data, accept_qc)
    _check_scale(plan, cal_dirs if cal_dirs is not None else [clem, clem.parent])

    tlist = list(data.get("targets") or [])
    if len(tlist) > L.MAX_TARGETS:
        plan.refuse(f"{len(tlist)} targets: at most {L.MAX_TARGETS} per file")
    ids = [str(t.get("id")) for t in tlist]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        plan.refuse(f"duplicate target ids: {', '.join(dup)}")

    for t in tlist:
        try:
            px = (float(t["fib_px"][0]), float(t["fib_px"][1]))
        except Exception:  # noqa: BLE001
            px = (float("nan"), float("nan"))
        row = TargetRow(id=str(t.get("id")), label=str(t.get("label", t.get("id"))),
                        source=str(t.get("source", src.get("id", ""))), fib_px=px,
                        note=str(t.get("note") or ""))
        for w in t.get("warnings") or []:
            row.warn(f"pyMap: {w}")
        plan.rows.append(row)
        if not plan.ok:
            row.refuse("file refused")
            continue
        if src.get("id") and row.source != str(src.get("id")):
            row.refuse(f"source {row.source!r} is not the overview {src.get('id')!r}")
            continue
        W, H = int(rec.image_width_px), int(rec.image_height_px)
        if not (math.isfinite(px[0]) and math.isfinite(px[1])
                and 0.0 <= px[0] < W and 0.0 <= px[1] < H):
            row.refuse(f"outside the overview image ({W} x {H} px)")
            continue
        st = rec.stage_at
        try:
            dx, dy = target_stage_delta(
                px[0], px[1], image_width_px=W, image_height_px=H,
                pixel_size_m=float(rec.fib_pixel_size_m), tilt_deg=float(st.t_deg),
                rot_deg=float(st.r_deg), scan_rotation_deg=rec.fib_scan_rotation_deg,
                beam_shift_m=(rec.fib_beam_shift_x_m, rec.fib_beam_shift_y_m))
        except TargetGeometryError as e:
            row.refuse(str(e))
            continue
        row.stage_dx_m, row.stage_dy_m = dx, dy
        row.stage_x_m, row.stage_y_m = st.x_m + dx, st.y_m + dy
        row.offset_um = math.hypot(px[0] - W / 2.0, px[1] - H / 2.0) \
            * float(rec.fib_pixel_size_m) * 1e6
    return plan


def write_plan(plan: TargetPlan, out_path=None) -> Path:
    """Atomic write of the plan (pyfib.clem_targets.v1) beside the targets file."""
    out = Path(out_path) if out_path else Path(plan.targets_file).parent / PLAN_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(plan.to_json(), indent=1), encoding="utf-8")
    os.replace(str(tmp), str(out))
    return out


def find_targets_file(clem: Path) -> Optional[Path]:
    """The newest pymap.targets.v2 file in <session>/clem (ready marker or not)."""
    clem = Path(clem)
    if not clem.is_dir():
        return None
    skip = {OVERVIEW_POSITIONS, SEM_OVERVIEWS, PLAN_FILE}
    best = None
    for p in clem.glob("*.json"):
        if p.name in skip or p.name.endswith(".ready.json"):
            continue
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("schema") != TARGETS_SCHEMA:
                continue
        except Exception:  # noqa: BLE001
            continue
        if best is None or p.stat().st_mtime > best.stat().st_mtime:
            best = p
    return best
