"""v0.54.0: Prepolish / Polish refine at scan speed 3 with a speed-8 retry (Evan,
2026-10-16), the speed set once per refine, and the refine summary fields.

refine_position / refine_two_stage are stubbed; the real ScopeRefineAdapter runs over a
dummy SEM, so the scan-speed commands and beam-shift restores are the real ones.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from ..milling_workflow import RefineConfig, refine_scan_rates
from .test_workflow import _BeamSem, _rec, _rr


class _RateSem(_BeamSem):
    def __init__(self):
        super().__init__()
        self.rate, self.cmds = 6, []

    def get_float(self, m):
        if m == "DP_SCANRATE":
            return float(self.rate)
        return super().get_float(m)

    def execute(self, c):
        self.cmds.append(c)
        if c.startswith("CMD_SCANRATE"):
            self.rate = int(c[len("CMD_SCANRATE"):])


def _cfg(**kw):
    kw.setdefault("imaging_scan_rate", 3)
    kw.setdefault("fallback_scan_rate", 8)
    kw.setdefault("settle_s", 0.0)
    kw.setdefault("coarse_fallback", False)
    return RefineConfig(**kw)


def _run(cfg, results, *, two=None, coarse=False, raise_exc=None):
    import numpy as np
    from PIL import Image
    import smartfib_executor.refine as R
    import smartfib_executor.refine.refine as RR
    from ..milling_workflow import run_refine_at_position
    d = Path(tempfile.mkdtemp())
    Image.fromarray(np.full((48, 64), 100, dtype=np.uint8)).save(d / "p1.bmp")
    Image.fromarray(np.full((48, 64), 90, dtype=np.uint8)).save(d / "p1_coarse.bmp")
    rec = _rec("p1")
    extra = {"image_bmp": "p1.bmp"}
    if coarse:
        extra.update(image_bmp_coarse="p1_coarse.bmp", coarse_magnification_x=800.0)
    rec = rec.__class__(**{**rec.__dict__, **extra})
    sem, logs, passes, coarse_calls = _RateSem(), [], [], []
    results = list(results)

    def fake_fine(scope, ref, mag, **kw):
        passes.append({"rate": scope.imaging_scan_rate,
                       "beam": (sem.beam["AP_FIB_BEAM_SHIFT_X"],
                                sem.beam["AP_FIB_BEAM_SHIFT_Y"])})
        scope._use_scan_rate(scope.imaging_scan_rate)      # what a grab does
        sem.beam["AP_FIB_BEAM_SHIFT_X"] = 9e-6              # a pass leaves a shift behind
        if raise_exc is not None:
            raise raise_exc
        return results.pop(0)

    def fake_two(scope, *a, **kw):
        coarse_calls.append(kw)
        return two

    o1, o2 = R.refine_position, RR.refine_two_stage
    R.refine_position, RR.refine_two_stage = fake_fine, fake_two
    try:
        out = run_refine_at_position(sem, rec, cfg, ref_base_dir=d, logger=logs.append)
    finally:
        R.refine_position, RR.refine_two_stage = o1, o2
    return out, passes, coarse_calls, logs, sem


def test_stage_defaults():
    assert refine_scan_rates("prepolish", 8) == (3, 8)
    assert refine_scan_rates("polish", 8) == (3, 8)
    assert refine_scan_rates("rough", 8) == (8, None)           # until tested the same way
    assert refine_scan_rates(None, 8) == (8, None)
    assert refine_scan_rates("polish", 8, 5) == (5, 8)
    assert refine_scan_rates("polish", 8, 8) == (8, None)       # fallback = speed: dropped
    assert refine_scan_rates("polish", 8, None, -1) == (3, None)
    assert refine_scan_rates("polish", 8, -1) == (None, None)   # as-is: no fallback
    assert refine_scan_rates("rough", 8, None, 6) == (8, 6)


def test_low_snr_at_speed_3_retries_at_speed_8_from_the_original_beam_shift():
    out, passes, _, logs, sem = _run(_cfg(), [
        _rr(False, low_conf=True, msg="Low confidence: correlation snr 3.1 < 4.0."),
        _rr(True, msg="Refined to 0.04 um with beam shift.")])
    assert [p["rate"] for p in passes] == [3, 8]
    assert passes[1]["beam"] == passes[0]["beam"] == (2e-6, -1e-6)   # put back first
    assert out.success and out.speed_fallback_used and out.scan_rate == 3
    assert out.speed_fallback_scan_rate == 8 and isinstance(out.duration_s, float)
    assert [c for c in sem.cmds if c.startswith("CMD_SCANRATE")] == \
        ["CMD_SCANRATE3", "CMD_SCANRATE8", "CMD_SCANRATE6"]     # speed 6 restored at the end
    assert any("retrying the 2000x pass at speed 8" in l for l in logs)
    assert any("refine took" in l and "then 8" in l for l in logs)
    from ..report.runlog import refine_summary
    s = refine_summary(out)
    assert s["scan_rate"] == 3 and s["speed_fallback_used"] is True
    assert s["speed_fallback_scan_rate"] == 8 and s["duration_s"] is not None


def test_speed_is_set_once_and_restored_when_no_retry_is_needed():
    out, passes, _, _, sem = _run(_cfg(), [_rr(True, msg="Refined.")])
    assert out.success and not out.speed_fallback_used and len(passes) == 1
    assert [c for c in sem.cmds if c.startswith("CMD_SCANRATE")] == \
        ["CMD_SCANRATE3", "CMD_SCANRATE6"]


def test_no_speed_retry_after_refusals_wrong_sign_or_stage_outcomes():
    from ..refine.refine import RefineResult
    wrong_sign = _rr(False, msg="Correction INCREASED the drift (wrong-sign signature).")
    wrong_sign.method = "beam_shift"
    stage_fail = RefineResult(success=False, method="stage", initial_drift_x_m=0,
                              initial_drift_y_m=0, initial_drift_mag_m=0,
                              residual_drift_m=1e-5, message="Stage move reduced drift only")
    for first in (_rr(False, low_conf=True, msg="Refused: scan rotation is 3 deg"),
                  wrong_sign,
                  _rr(False, disabled=True, msg="Drift 20 um exceeds the limit"),
                  _rr(False, refused=True, msg="Stage correction refused: tilt"),
                  _rr(False, stage=(5e-6, 0.0), msg="Residual 2 um"),
                  stage_fail):
        out, passes, _, _, _ = _run(_cfg(), [first])
        assert len(passes) == 1 and not out.speed_fallback_used, first.message


def test_no_speed_retry_without_a_fallback_speed():
    for cfg in (_cfg(fallback_scan_rate=None), _cfg(imaging_scan_rate=8, fallback_scan_rate=8)):
        _, passes, _, _, _ = _run(cfg, [_rr(False, low_conf=True, msg="Low confidence: x")])
        assert len(passes) == 1


def test_speed_retry_comes_before_the_800x_fallback():
    from ..refine.refine import TwoStageResult
    two = TwoStageResult(_rr(True), _rr(True, msg="ok"), True, "ok", False)
    out, passes, coarse_calls, logs, _ = _run(
        _cfg(coarse_fallback=True),
        [_rr(False, low_conf=True, msg="Unreliable: aliased"),
         _rr(False, low_conf=True, msg="Low confidence: snr 2")], two=two, coarse=True)
    assert [p["rate"] for p in passes] == [3, 8] and len(coarse_calls) == 1
    assert out.success and out.speed_fallback_used and out.coarse_fallback_tried


def test_beam_and_speed_after_an_exception():
    from ..frame_grab import StaleFrameError
    import numpy as np
    from PIL import Image
    import smartfib_executor.refine as R
    from ..milling_workflow import run_refine_at_position
    d = Path(tempfile.mkdtemp())
    Image.fromarray(np.full((48, 64), 100, dtype=np.uint8)).save(d / "p1.bmp")
    sem, logs = _RateSem(), []

    def boom(scope, ref, mag, **kw):
        scope._use_scan_rate(scope.imaging_scan_rate)
        sem.beam["AP_FIB_BEAM_SHIFT_X"] = 9e-6
        raise StaleFrameError("refine frame not usable")

    o = R.refine_position
    R.refine_position = boom
    try:
        run_refine_at_position(sem, _rec("p1"), _cfg(), ref_base_dir=d, logger=logs.append)
        raise AssertionError("not raised")
    except StaleFrameError:
        pass
    finally:
        R.refine_position = o
    assert sem.beam["AP_FIB_BEAM_SHIFT_X"] == 2e-6          # put back
    assert sem.rate == 6                                     # speed restored
    assert any("refine stopped; beam shift put back" in l for l in logs)


def test_an_error_after_a_refine_stage_move_warns_and_never_moves_the_stage():
    # review finding 3: a manual Align with stage correction can be left off by up to
    # 100 um. Second review: an automatic move-back in an error path is an unchecked stage
    # move (a mill may have started, the stage may have faulted) -> warn loudly, no motion.
    from ..frame_grab import StaleFrameError
    import numpy as np
    from PIL import Image
    import smartfib_executor.refine as R
    from ..milling_workflow import run_refine_at_position

    class _StageSem(_RateSem):
        def __init__(self):
            super().__init__()
            self.xy, self.moves = [1.0e-3, 2.0e-3], []
        def get_stage_position(self):
            class P: pass
            p = P()
            p.x_m, p.y_m, p.z_m, p.tilt_deg, p.rot_deg, p.master_z_m = (
                self.xy[0], self.xy[1], 3e-3, 12.0, 12.0, 0.0)
            return p
        def move_stage(self, x, y, z, t, r, m):
            self.moves.append((x, y, z, t, r, m)); self.xy = [x, y]
        def wait_stage_arrival(self, **kw):
            pass

    d = Path(tempfile.mkdtemp())
    Image.fromarray(np.full((48, 64), 100, dtype=np.uint8)).save(d / "p1.bmp")
    for exc in (StaleFrameError("refine frame not usable"), KeyboardInterrupt()):
        sem, logs = _StageSem(), []

        def boom(scope, ref, mag, **kw):
            scope.move_stage_xy(1.0e-3 + 20e-6, 2.0e-3)   # refine's one stage move
            raise exc

        o = R.refine_position
        R.refine_position = boom
        try:
            run_refine_at_position(sem, _rec("p1"), _cfg(enable_stage=True),
                                   ref_base_dir=d, logger=logs.append)
            raise AssertionError("not raised")
        except (StaleFrameError, KeyboardInterrupt):
            pass
        finally:
            R.refine_position = o
        assert len(sem.moves) == 1                             # only refine's own move
        assert any("NOT moved back" in l and "+20.0" in l and "Go to" in l for l in logs)
    # stage correction off: refine never moves the stage, nothing to undo
    sem = _StageSem()
    try:
        R.refine_position = lambda scope, ref, mag, **kw: (_ for _ in ()).throw(
            StaleFrameError("x"))
        run_refine_at_position(sem, _rec("p1"), _cfg(), ref_base_dir=d, logger=lambda m: None)
    except StaleFrameError:
        pass
    finally:
        R.refine_position = o
    assert sem.moves == []


def test_autofocus_puts_the_objective_back_after_an_error():
    from ..autofocus.search import autofocus
    from ..autofocus.sources import SimulatedImageSource
    from ..frame_grab import StaleFrameError

    class Flaky(SimulatedImageSource):
        n = 0
        def grab(self):
            Flaky.n += 1
            if Flaky.n == 3:
                raise StaleFrameError("autofocus frame not usable")
            return super().grab()

    src = Flaky(true_focus=16425.0, start_voltage=16400.0)
    try:
        autofocus(src, half_range=150.0, coarse_points=7)
        raise AssertionError("not raised")
    except StaleFrameError:
        pass
    assert src._voltage == 16400.0


def test_cli_stage_defaults_and_overrides():
    import argparse
    from ..cli import _refine_rates_kw, build_parser
    p = build_parser()
    a = p.parse_args(["site", "align", "p1", "--stage", "prepolish"])
    assert _refine_rates_kw(a) == {"imaging_scan_rate": 3, "fallback_scan_rate": 8}
    a = p.parse_args(["site", "align", "p1", "--stage", "rough"])
    assert _refine_rates_kw(a) == {"imaging_scan_rate": 8, "fallback_scan_rate": None}
    a = p.parse_args(["site", "align", "p1", "--stage", "polish", "--refine-scan-rate", "5",
                      "--refine-fallback-scan-rate", "-1"])
    assert _refine_rates_kw(a) == {"imaging_scan_rate": 5, "fallback_scan_rate": None}
    a = p.parse_args(["mill-positions", "x.ely", "p1", "--stage", "polish"])
    assert _refine_rates_kw(a) == {"imaging_scan_rate": 3, "fallback_scan_rate": 8}
    assert a.imaging_scan_rate == 8                          # autofocus speed unchanged
    a = p.parse_args(["mill-positions", "x.ely", "p1", "--imaging-scan-rate", "7"])
    assert _refine_rates_kw(a) == {"imaging_scan_rate": 7, "fallback_scan_rate": None}
    assert isinstance(a, argparse.Namespace)


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
