"""CLEM targeting with pyMap, part 1 (v0.52.0): offline checks, no instrument.

Evan 2026-10-09: pyFIB owns the stage side. Image point -> stage move uses refine's
measured model (target_stage_delta); pyMap's targets are checked against their FIB
overview and the agreed safety limits (inside the image, no scan rotation / beam shift,
registration error warn > 1 um / refuse > 3 um unless overridden, <= 30 targets, one
overview for now); overviews (FIB 400x, SEM grabbed by pyFIB with the stage) refuse
unless at SEM normal and not milling, and never move the stage.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from ..clem import targets as T
from ..clem import limits as L
from ..positions import PositionDatabase, PositionRecord, StageAt
from ..refine.geometry import TargetGeometryError, target_stage_delta

PS_2000 = 5.5810546875e-08          # 2000x, 1024 px (MAG_REF_WIDTH_M / 1024)
PS_400 = PS_2000 * 5


# --------------------------------------------------------------------------- F1
def test_pixel_to_stage_matches_the_calibration_and_refuses_outside_the_model():
    kw = dict(image_width_px=1024, image_height_px=768, pixel_size_m=PS_2000,
              tilt_deg=30.0, rot_deg=12.0)
    dx, dy = target_stage_delta(512 + 323.3, 384, **kw)     # 323.3 px right of centre
    assert abs(dx * 1e6 - (-19.78)) < 0.01 and abs(dy) < 1e-12
    dx, dy = target_stage_delta(512, 384 - 309.9, **kw)     # 309.9 px above centre
    assert abs(dy * 1e6 - (-20.00)) < 0.01 and abs(dx) < 1e-12
    assert target_stage_delta(512, 384, **kw) == (0.0, 0.0)  # centre: no move
    for bad in (dict(tilt_deg=35.0), dict(rot_deg=30.0), dict(scan_rotation_deg=0.2),
                dict(scan_rotation_deg=None), dict(beam_shift_m=(5e-8, 0.0)),
                dict(beam_shift_m=(0.0, None)), dict(pixel_size_m=0.0)):
        try:
            target_stage_delta(600, 300, **{**kw, **bad})
            raise AssertionError(f"not refused: {bad}")
        except TargetGeometryError:
            pass


# --------------------------------------------------------------------------- plan
def _overview(name="clem_ov_grid1_1", **over):
    base = dict(name=name, captured_unix_s=1791580000.0, fib_magnification_x=400.0,
                fib_objective_potential_v=16400.0,
                stage_at=StageAt(x_m=44.691e-3, y_m=51.183e-3, z_m=29.497e-3, r_deg=12.0,
                                 m_m=0.0, t_deg=30.0),
                image_bmp=f"images\\{name}.bmp",           # Windows separator, as on the PC
                image_width_px=1024, image_height_px=768, fib_pixel_size_m=PS_400,
                fib_scan_rotation_deg=0.0, fib_beam_shift_x_m=0.0, fib_beam_shift_y_m=0.0)
    base.update(over)
    return PositionRecord(**base)


def _session(rec=None, targets=None, *, qc=0.8, grid_qc=None, ready=True, sources=None,
             echo=True):
    d = Path(tempfile.mkdtemp())
    clem = d / "clem"
    (clem / "images").mkdir(parents=True)
    rec = rec or _overview()
    img = clem / "images" / f"{rec.name}.bmp"
    img.write_bytes(b"BM-overview-" + rec.name.encode())
    db = PositionDatabase()
    db.upsert(rec)
    db.save(clem / "overview_positions.json")
    src = {"id": "ov1", "kind": "fib_position", "positions_file": "overview_positions.json",
           "position": rec.name, "image": f"images/{rec.name}.bmp",
           "image_sha256": T.file_sha256(img), "image_width_px": 1024,
           "image_height_px": 768, "fib_pixel_size_m": PS_400}
    if echo:
        src.update(captured_unix_s=rec.captured_unix_s,
                   stage_at={"x_m": rec.stage_at.x_m, "y_m": rec.stage_at.y_m,
                             "z_m": rec.stage_at.z_m, "t_deg": rec.stage_at.t_deg,
                             "r_deg": rec.stage_at.r_deg})
    reg = {"chain": [], "combined_loo_rms_um": qc}
    if grid_qc is not None:
        reg["combined_loo_rms_grid_um"] = grid_qc
    data = {"schema": "pymap.targets.v2", "pymap_version": "0.9.0",
            "sources": sources if sources is not None else [src],
            "registration": reg,
            "targets": targets if targets is not None else [
                {"id": "T1", "label": "1", "source": "ov1", "fib_px": [512 + 323.3 / 5, 384]},
                {"id": "T2", "label": "2", "source": "ov1", "fib_px": [100.0, 700.0],
                 "warnings": ["fiducial footprint overlaps fluorescence"]}]}
    tp = clem / "grid1_targets.json"
    tp.write_text(json.dumps(data))
    if ready:
        T.ready_marker_path(tp).write_text(json.dumps(
            {"schema": "pymap.ready.v1", "sha256": T.file_sha256(tp), "pymap_version": "0.9.0"}))
    return d, clem, tp


def test_plan_computes_stage_xy_and_writes_the_plan():
    d, clem, tp = _session()
    plan = T.plan_targets(tp, cal_dirs=[])
    assert plan.ok, plan.errors
    r1, r2 = plan.rows
    assert r1.status == "ok" and abs(r1.stage_dx_m * 1e6 - (-19.78)) < 0.02   # same 18 um
    assert abs(r1.stage_x_m - (44.691e-3 + r1.stage_dx_m)) < 1e-12
    assert abs(r1.stage_y_m - 51.183e-3) < 1e-12 and abs(r1.offset_um - 18.04) < 0.05
    assert r2.status == "warn" and "pyMap: fiducial footprint" in r2.warnings[0]
    assert r2.stage_dx_m > 0 and r2.stage_dy_m > 0         # left of / below centre
    assert plan.scale["verified"] is False and "UNVERIFIED" in plan.scale["text"]
    out = T.write_plan(plan)
    j = json.loads(out.read_text())
    assert j["schema"] == "pyfib.clem_targets.v1" and j["move_axes"] == "XY"
    assert j["targets"][0]["id"] == "T1" and j["file_ok"] is True
    assert out.name == "pyfib_clem_plan.json" and T.find_targets_file(clem) == tp


def test_file_level_refusals():
    cases = []
    d, clem, tp = _session(ready=False)                                 # not marked ready
    cases.append((tp, "no ready marker"))
    d, clem, tp = _session()
    tp.write_text(tp.read_text().replace('"1"', '"one"'))               # edited after marking
    cases.append((tp, "CHANGED since pyMap marked it ready"))
    d, clem, tp = _session()
    (clem / "images" / "clem_ov_grid1_1.bmp").write_bytes(b"other")     # image replaced
    cases.append((tp, "hash mismatch"))
    rec = _overview()
    d, clem, tp = _session(rec)
    db = PositionDatabase.load(clem / "overview_positions.json")        # re-captured
    import dataclasses
    db.upsert(dataclasses.replace(rec, captured_unix_s=rec.captured_unix_s + 60))
    db.save(clem / "overview_positions.json")
    cases.append((tp, "re-captured"))
    many = [{"id": f"T{i}", "source": "ov1", "fib_px": [500, 400]} for i in range(31)]
    cases.append((_session(targets=many)[2], "at most 30"))
    dup = [{"id": "T1", "source": "ov1", "fib_px": [500, 400]}] * 2
    cases.append((_session(targets=dup)[2], "duplicate target ids"))
    d, clem, tp = _session()
    data = json.loads(tp.read_text())
    data["sources"] = data["sources"] * 2                                # tiling: later
    tp.write_text(json.dumps(data))
    T.ready_marker_path(tp).write_text(json.dumps({"schema": "pymap.ready.v1",
                                                   "sha256": T.file_sha256(tp)}))
    cases.append((tp, "tiling is not implemented"))
    for path, want in cases:
        plan = T.plan_targets(path, cal_dirs=[])
        assert not plan.ok and any(want in e for e in plan.errors), (want, plan.errors)
        assert all(r.status == "refused" for r in plan.rows)


def test_safety_limits_per_target_and_registration_error():
    tg = [{"id": "IN", "source": "ov1", "fib_px": [10.0, 10.0]},
          {"id": "EDGE", "source": "ov1", "fib_px": [1024.0, 300.0]},      # x == W: outside
          {"id": "NEG", "source": "ov1", "fib_px": [-0.5, 300.0]}]
    plan = T.plan_targets(_session(targets=tg)[2], cal_dirs=[])
    st = {r.id: r.status for r in plan.rows}
    assert st == {"IN": "ok", "EDGE": "refused", "NEG": "refused"}, st
    # beam shift / scan rotation on the overview -> every target refused
    for over in (dict(fib_beam_shift_x_m=2e-8), dict(fib_scan_rotation_deg=0.3),
                 dict(fib_beam_shift_y_m=None)):
        plan = T.plan_targets(_session(_overview(**over))[2], cal_dirs=[])
        assert plan.ok and all(r.status == "refused" for r in plan.rows), over
    # registration error: grid-plane value wins; warn > 1, refuse > 3 unless overridden
    plan = T.plan_targets(_session(qc=0.5, grid_qc=1.5)[2], cal_dirs=[])
    assert plan.ok and plan.qc_kind == "grid" and any("above 1" in w for w in plan.warnings)
    _, _, tp = _session(qc=3.4)
    plan = T.plan_targets(tp, cal_dirs=[])
    assert not plan.ok and "above the 3 um limit" in plan.errors[0]
    plan = T.plan_targets(tp, accept_qc=True, cal_dirs=[])
    assert plan.ok and plan.qc_override and plan.rows[0].status == "ok"
    _, _, tp = _session(qc=None)
    assert not T.plan_targets(tp, cal_dirs=[]).ok                       # none given
    assert T.plan_targets(tp, accept_qc=True, cal_dirs=[]).ok
    # not at SEM normal: warned (the model gate still decides)
    plan = T.plan_targets(_session(_overview(stage_at=StageAt(0.0446, 0.0512, 0.0295, 12.0,
                                                               0.0, 20.0)))[2], cal_dirs=[])
    assert plan.ok and any("not taken at SEM normal" in w for w in plan.warnings)


def test_scale_is_verified_only_by_a_matching_stage_calibration():
    d, clem, tp = _session()
    cal = {"magnification": 400.0, "tilt_deg": 30.1, "rot_deg": 12.0,
           "axes": {"x": {"response_raw": 0.905}, "y": {"response_raw": -0.860}}}
    (d / "refine_stage_calibration_400x.json").write_text(json.dumps(cal))
    (d / "refine_stage_calibration.json").write_text(json.dumps({**cal, "magnification": 2000.0}))
    plan = T.plan_targets(tp, cal_dirs=[clem, d])
    assert plan.scale["verified"] and "verified at 400x" in plan.scale["text"]
    cal["axes"]["x"]["response_raw"] = 0.85                                # 7 % off the model
    (d / "refine_stage_calibration_400x.json").write_text(json.dumps(cal))
    plan = T.plan_targets(tp, cal_dirs=[d])
    assert not plan.scale["verified"] and any("differs from the model" in w for w in plan.warnings)
    cal["tilt_deg"] = 2.0                                                  # wrong tilt: ignored
    (d / "refine_stage_calibration_400x.json").write_text(json.dumps(cal))
    assert "UNVERIFIED" in T.plan_targets(tp, cal_dirs=[d]).scale["text"]


# --------------------------------------------------------------------------- overviews
class Scope:
    """Fake microscope: SEM-normal stage, idle, records every set/execute/move."""

    def __init__(self, *, t=30.0, r=12.0, milling=False, beam=(3e-6, -1e-6), sticky_beam=False,
                 sem_width=1.428e-3, store_px=1024):
        self.calls, self.moves = [], []
        self.imaging, self.frozen = "FIB", True
        self.milling = milling
        self.sticky = sticky_beam
        self.v = {"AP_STAGE_AT_X": 44.70e-3, "AP_STAGE_AT_Y": 51.19e-3, "AP_STAGE_AT_Z": 29.497e-3,
                  "AP_STAGE_AT_T": t, "AP_STAGE_AT_R": r, "AP_STAGE_AT_M": 0.0,
                  "AP_FIB_BEAM_SHIFT_X": beam[0], "AP_FIB_BEAM_SHIFT_Y": beam[1],
                  "AP_FIB_SCAN_ROTATION": 0.0, "AP_FIB_OBJECTIVE_POTENTIAL": 16400.0,
                  "AP_MAG": 1000.0, "AP_WIDTH": sem_width, "AP_HEIGHT": sem_width * 0.75,
                  "AP_PIXEL_SIZE": sem_width / store_px, "AP_WD": 5.0e-3,
                  "AP_ACTUALKV": 2000.0, "AP_SCANROTATION": 0.0, "AP_TILT_ANGLE": 0.0,
                  "DP_PATTERNING_MODE": 0.0}

    def get_float(self, m):
        if m == "DP_FIB_MODE":
            return 2.0 if self.milling else 0.0
        if m == "DP_FIB_IMAGING":
            return 1.0 if self.imaging == "FIB" else 0.0
        return self.v.get(m, 0.0)

    def get_str(self, m):
        if m == "DP_FIB_IMAGING":
            return self.imaging
        if m == "DP_FROZEN":
            return "Frozen" if self.frozen else "Live"
        if m == "DP_DETECTOR_TYPE":
            return "SESI"
        if m == "DP_SCAN_ROT":
            return "Off"
        if m == "DP_FREEZE_ON":                     # v0.54.0: read back by frame_grab
            return str(self.v.get(m, "End Frame"))
        return ""

    def set_value(self, m, v):
        self.calls.append(("set", m, v))
        if m.startswith("AP_FIB_BEAM_SHIFT") and self.sticky:
            return
        self.v[m] = v

    def execute(self, c):
        self.calls.append(("cmd", c))
        if c == "CMD_FIB_MODE_FIB":
            self.imaging = "FIB"
        elif c == "CMD_FIB_MODE_SEM":
            self.imaging = "SEM"
        elif c == "CMD_UNFREEZE_ZONE":
            self.frozen = False
        elif c in ("CMD_FREEZE_ZONE", "CMD_FREEZE_ALL"):
            self.frozen = True

    def grab_bmp(self, path, width=1024, height=768, **kw):
        from PIL import Image
        self.calls.append(("grab", Path(path).name, self.imaging))
        Image.new("L", (int(width), int(height)), 90).save(path)

    def move_stage(self, *a):
        self.moves.append(a)


def test_fib_overview_zeroes_beam_shift_records_400x_and_never_moves():
    from ..clem.overview import capture_fib_overview
    d = Path(tempfile.mkdtemp())
    sem, logs = Scope(), []
    rec = capture_fib_overview(sem, d, "Grid1", guard_s=0.0, frame_wait_s=0.0, log=logs.append)
    assert rec.name == "clem_ov_grid1_1" and rec.image_bmp == "images/clem_ov_grid1_1.bmp"
    assert rec.fib_magnification_x == 400.0 and rec.image_width_px == 1024
    assert abs(rec.fib_pixel_size_m - PS_400) < 1e-15
    assert rec.fib_beam_shift_x_m == 0.0 and rec.fib_beam_shift_y_m == 0.0
    kinds = [c[1] if c[0] != "set" else c[1] for c in sem.calls]
    assert kinds.index("AP_FIB_BEAM_SHIFT_X") < kinds.index("clem_ov_grid1_1.bmp")   # zero first
    assert sum(1 for c in sem.calls if c[0] == "grab") == 1                         # no 800x
    assert sem.moves == [] and not any(c[1] == "AP_FIB_MAGNIFICATION" and c[2] != 400.0
                                       for c in sem.calls if c[0] == "set")
    db = PositionDatabase.load(d / "clem" / "overview_positions.json")
    assert list(db.positions) == ["clem_ov_grid1_1"] and (d / "clem" / rec.image_bmp).exists()
    rec2 = capture_fib_overview(Scope(), d, "Grid1", guard_s=0.0, frame_wait_s=0.0,
                                log=logs.append)
    assert rec2.name == "clem_ov_grid1_2"
    assert not (d / "positions.json").exists()                  # never the milling file


def test_overviews_refuse_off_sem_normal_while_milling_or_with_a_stuck_beam_shift():
    from ..clem.overview import OverviewRefused, capture_fib_overview, capture_sem_overview
    d = Path(tempfile.mkdtemp())
    for sem, fn, want in ((Scope(t=2.0), capture_fib_overview, "SEM normal"),
                          (Scope(r=40.0), capture_sem_overview, "SEM normal"),
                          (Scope(milling=True), capture_fib_overview, "milling"),
                          (Scope(milling=True), capture_sem_overview, "milling"),
                          (Scope(sticky_beam=True), capture_fib_overview, "beam shift")):
        try:
            fn(sem, d, "Grid1", guard_s=0.0, frame_wait_s=0.0, log=lambda m: None)
            raise AssertionError(f"not refused: {want}")
        except OverviewRefused as e:
            assert want in str(e), (want, str(e))
        assert sem.moves == [] and not any(c[0] == "grab" for c in sem.calls)
        if want != "beam shift":
            assert not any(c[0] == "set" for c in sem.calls)       # refused before any set
    assert not (d / "clem" / "overview_positions.json").exists()


def test_sem_overview_grabs_one_sem_frame_records_the_stage_and_stays_in_its_lane():
    from ..clem.overview import OverviewRefused, capture_sem_overview, _SemOnly
    d = Path(tempfile.mkdtemp())
    sem, logs = Scope(), []
    e = capture_sem_overview(sem, d, "Grid1", mag=200.0, guard_s=0.0, frame_wait_s=0.0,
                             settle_s=0.0, log=logs.append)
    assert e["name"] == "clem_sem_grid1_1" and e["image"] == "images/clem_sem_grid1_1.bmp"
    assert e["stage_at"]["t_deg"] == 30.0 and abs(e["stage_at"]["x_m"] - 44.70e-3) < 1e-12
    assert abs(e["pixel_size_m"] - 1.428e-3 / 1024) < 1e-15 and e["detector"] == "SESI"
    assert e["image_sha256"].startswith("sha256:") and e["warnings"] == []
    grabs = [c for c in sem.calls if c[0] == "grab"]
    assert grabs == [("grab", "clem_sem_grid1_1.bmp", "SEM")]                  # one SEM frame
    assert {c[1] for c in sem.calls if c[0] == "set"} <= {"AP_MAG", "DP_FREEZE_ON"}
    assert {c[1] for c in sem.calls if c[0] == "cmd"} <= {
        "CMD_FIB_MODE_SEM", "CMD_UNFREEZE_ZONE", "CMD_FREEZE_ZONE", "CMD_FREEZE_ALL"}
    assert sem.moves == [] and sem.frozen                                      # left frozen
    data = json.loads((d / "clem" / "sem_overviews.json").read_text())
    assert data["schema"] == "pyfib.sem_overviews.v1" and len(data["overviews"]) == 1
    o = data["overviews"][0]                                   # v0.52.1: pyMap's field names
    assert o["pixel_size_nominal"] is True and o["scan_rotation_on"] is False
    assert o["tilt_correction_on"] is None and o["image_width_px"] == 1024
    # a store larger than the grab is flagged (pixel size uncertain)
    e2 = capture_sem_overview(Scope(store_px=2048), d, "Grid1", guard_s=0.0, frame_wait_s=0.0,
                              settle_s=0.0, log=lambda m: None)
    assert any("store size differs" in w for w in e2["warnings"])
    g = _SemOnly(Scope())
    for bad in (lambda: g.set_value("AP_STAGE_GOTO_X", 0.0), lambda: g.execute("CMD_FIB_MODE_MILL"),
                lambda: g.move_stage(0, 0, 0, 0, 0, 0), lambda: g.set_value("AP_WD", 0.005)):
        try:
            bad()
            raise AssertionError("guard let it through")
        except OverviewRefused:
            pass


# --------------------------------------------------------------------------- CLI
def _cli(argv, sem=None):
    import smartfib_executor.cli as C
    connected = []
    sem = sem or Scope()

    class _Ctx:
        def __enter__(self):
            connected.append(1)
            return sem
        def __exit__(self, *a): pass
    sem.connect_local = sem.connect_remoting = lambda *a: None
    import smartfib_executor.smartsem_com as SC
    old = SC.SmartSEM
    SC.SmartSEM = _Ctx
    try:
        with redirect_stdout(io.StringIO()) as out:
            rc = C.main(argv)
    finally:
        SC.SmartSEM = old
    return rc, out.getvalue(), bool(connected)


def test_cli_targets_and_overview_dry_runs():
    d, clem, tp = _session()
    rc, out, conn = _cli(["clem-targets", "--positions-file", str(d / "positions.json")])
    assert rc == 0 and not conn and "CLEM-PLAN: ok=1 warn=1 refused=0 file=ok" in out
    assert (clem / "pyfib_clem_plan.json").exists()
    _, _, tp3 = _session(qc=3.4)
    rc, out, conn = _cli(["clem-targets", str(tp3), "--positions-file", str(tp3.parent.parent / "p.json"),
                          "--no-write"])
    assert rc == 2 and "REFUSED: registration error" in out
    assert not (tp3.parent / "pyfib_clem_plan.json").exists()
    rc, out, conn = _cli(["clem-targets", str(tp3), "--accept-qc", "--no-write",
                          "--positions-file", str(tp3.parent.parent / "p.json")])
    assert rc == 0 and "OVERRIDDEN" in out
    for kind in ("fib", "sem"):
        rc, out, conn = _cli(["clem-overview", "--kind", kind, "--grid", "Grid2",
                              "--positions-file", str(d / "positions.json")])
        assert rc == 0 and not conn and "[DRY RUN]" in out and "never moved" in out
    rc, out, conn = _cli(["clem-overview", "--kind", "fib", "--grid", "Grid1", "--arm",
                          "--positions-file", str(d / "positions.json")], Scope(t=5.0))
    assert rc == 4 and conn and "REFUSED" in out and "SEM normal" in out


def test_v0520_sem_file_with_entries_is_still_read_and_rewritten():
    from ..clem.overview import capture_sem_overview, load_sem_overviews
    d = Path(tempfile.mkdtemp())
    (d / "clem").mkdir()
    (d / "clem" / "sem_overviews.json").write_text(json.dumps(
        {"schema": "pyfib.sem_overviews.v1", "entries": [{"name": "clem_sem_grid1_1"}]}))
    assert [e["name"] for e in load_sem_overviews(d)["overviews"]] == ["clem_sem_grid1_1"]
    e = capture_sem_overview(Scope(), d, "Grid1", guard_s=0.0, frame_wait_s=0.0,
                             settle_s=0.0, log=lambda m: None)
    data = json.loads((d / "clem" / "sem_overviews.json").read_text())
    assert e["name"] == "clem_sem_grid1_2" and "entries" not in data
    assert [x["name"] for x in data["overviews"]] == ["clem_sem_grid1_1", "clem_sem_grid1_2"]


def test_contract_with_the_bundled_pymap():
    """pyFIB overview -> pyMap's own API writes the targets file -> pyFIB plans it; and
    pyMap's reader accepts pyFIB's SEM overview file. Skips if pyMap is not bundled."""
    try:
        import pymap.api as A
    except Exception:  # noqa: BLE001
        print("SKIP test_contract_with_the_bundled_pymap (pymap not importable)")
        return
    from ..clem.overview import capture_fib_overview, capture_sem_overview
    d = Path(tempfile.mkdtemp())
    rec = capture_fib_overview(Scope(), d, "Grid1", guard_s=0.0, frame_wait_s=0.0,
                               log=lambda m: None)
    capture_sem_overview(Scope(), d, "Grid1", guard_s=0.0, frame_wait_s=0.0, settle_s=0.0,
                         log=lambda m: None)
    clem = d / "clem"
    pf = clem / "overview_positions.json"
    r = A.load_positions(pf)[rec.name]
    assert A.check_overview_record(r) == ([], [])              # pyMap accepts the record
    src = A.fib_source_from_record(r, positions_file=pf, targets_dir=clem)
    px = [512 + 323.3 / 5, 384 - 309.9 / 5]
    tg = [{"id": "T1", "label": "1", "source": "ov1", "fib_px": px,
           "layout_um": list(A.layout_um(px, 1024, 768, src["fib_pixel_size_m"])),
           "warnings": [], "note": ""}]
    doc = A.new_targets_doc(sources=[src], targets=tg, confocal={"file": "c.tif"},
                            registration={"chain": [], "combined_loo_rms_um": 0.2,
                                          "combined_loo_rms_grid_um": 0.31})
    tp = A.write_targets(clem / A.targets_filename(rec.name), doc)
    assert T.find_targets_file(clem) == tp
    plan = T.plan_targets(tp, cal_dirs=[])
    assert plan.ok and plan.qc_kind == "grid", plan.errors
    r1 = plan.rows[0]
    assert r1.status == "ok"
    assert abs(r1.stage_dx_m * 1e6 - (-19.78)) < 0.02 and abs(r1.stage_dy_m * 1e6 - (-20.0)) < 0.02
    sem = A.load_sem_overviews(clem / "sem_overviews.json")
    assert sem[0]["name"] == "clem_sem_grid1_1" and sem[0]["pixel_size_nominal"] is True
    # a re-capture under the same name is caught by BOTH programs
    import dataclasses
    db = PositionDatabase.load(pf)
    db.upsert(dataclasses.replace(db.get(rec.name), captured_unix_s=rec.captured_unix_s + 30))
    db.save(pf)
    assert not T.plan_targets(tp, cal_dirs=[]).ok
    assert A.check_source_against_record(src, A.load_positions(pf)[rec.name], targets_dir=clem)


def _run_all() -> int:
    import smartfib_executor.site_ops as SO
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc()
            failed += 1; print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
