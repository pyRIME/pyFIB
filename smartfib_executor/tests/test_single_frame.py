"""v0.54.0: the single-frame protocol (frame_grab.grab_fresh_frame) and the switch to FIB
imaging (frame_grab.ensure_fib_imaging, bug #14), offline.

The fake models the instrument as measured on 2026-10-16: AP_FRAME_TIME in ms; with
DP_FREEZE_ON = End Frame a freeze request stops the scan at the end of the frame in
progress; the frame store refreshes only when a frame completes; Grab() saves the store.
Time is virtual (frame_grab.time is patched), so a 10 s frame costs nothing here.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import smartfib_executor.frame_grab as FG


class VClock:
    def __init__(self, t0: float = 1000.0):
        self.t = t0

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, float(s))


class FrameSem:
    def __init__(self, clock, *, frame_ms=358.54, speed=3):
        self.clock = clock
        self.frame_ms = frame_ms            # AP_FRAME_TIME (None = unreadable)
        self.actual_s = None                # real frame length if it differs from the read
        self.speed = speed
        self.frozen = True
        self.freeze_on = "End Frame"
        self.freeze_on_sticks = True
        self.live_since = None
        self.pending = None
        self.store = 7                      # frame-store content (what Grab() saves)
        self.frames = 0
        self.cmds = []
        self.grabs = 0
        # fault switches
        self.immediate_freezes = 0          # the next N freezes stop the scan at once
        self.never_freezes = False
        self.store_never_refreshes = False
        self.unfreeze_ignored = 0
        self.frozen_unreadable = False
        self.imaging = "FIB"
        self.mag = 2000.0

    def _frame_s(self):
        return self.actual_s if self.actual_s is not None else (self.frame_ms or 1000.0) / 1e3

    def _settle(self):
        if self.pending is not None and self.clock.time() >= self.pending:
            self.pending = None
            self._complete()

    def _complete(self):
        self.frozen = True
        self.frames += 1
        if not self.store_never_refreshes:
            self.store = 100 + self.frames

    def get_str(self, m):
        self._settle()
        if m == "DP_FROZEN":
            if self.frozen_unreadable:
                raise RuntimeError("unreadable")
            return "Frozen" if self.frozen else "Live"
        if m == "DP_FREEZE_ON":
            return self.freeze_on
        if m == "DP_FIB_IMAGING":
            return self.imaging
        raise KeyError(m)

    def get_float(self, m):
        self._settle()
        if m == "AP_FRAME_TIME":
            if self.frame_ms is None:
                raise KeyError(m)
            return float(self.frame_ms)
        if m == "DP_SCANRATE":
            return float(self.speed)
        if m == "AP_FIB_MAGNIFICATION":
            return self.mag
        if m == "DP_FROZEN" and self.frozen_unreadable:
            raise RuntimeError("unreadable")
        raise KeyError(m)

    def set_value(self, m, v):
        if m == "DP_FREEZE_ON" and self.freeze_on_sticks:
            self.freeze_on = "End Frame" if str(v) in ("End Frame", "0", "0.0") else str(v)

    def execute(self, c):
        self._settle()
        self.cmds.append(c)
        if c == "CMD_UNFREEZE_ZONE":
            if self.unfreeze_ignored > 0:
                self.unfreeze_ignored -= 1
                return
            self.frozen, self.live_since, self.pending = False, self.clock.time(), None
        elif c in ("CMD_FREEZE_ZONE", "CMD_FREEZE_ALL"):
            if self.frozen or self.never_freezes:
                return
            if self.immediate_freezes > 0:
                self.immediate_freezes -= 1
                self._complete()
            elif self.pending is None:
                self.pending = self.live_since + self._frame_s()

    def grab_bmp(self, path, **k):
        self._settle()
        self.grabs += 1
        Path(path).write_bytes(b"BM" + bytes([self.store % 256]) * 16)


def _run(fn):
    clock = VClock()
    old = FG.time
    FG.time = clock
    try:
        return fn(clock)
    finally:
        FG.time = old


def _grab(sem, **kw):
    logs = []
    p = Path(tempfile.mkdtemp()) / "f.bmp"
    w = FG.grab_fresh_frame(sem, str(p), log=logs.append, **kw)
    return w, logs, p


def test_one_pass_at_speed_3_no_fixed_wait():
    def t(clock):
        sem = FrameSem(clock)
        t0 = clock.time()
        w, logs, p = _grab(sem)
        took = clock.time() - t0
        assert w is None and p.exists()
        assert sem.cmds.count("CMD_UNFREEZE_ZONE") == 1
        assert sem.cmds.count("CMD_FREEZE_ZONE") == 1 and sem.frames == 1   # ONE pass
        assert 0.35 <= took < 0.8, took                     # not the old 3 s live scan
        assert sem.frozen                                   # left frozen
        line = next(l for l in logs if l.startswith("[frame] speed"))
        assert "speed 3" in line and "AP_FRAME_TIME 0.36 s" in line and "Live -> Frozen 0.3" in line
    _run(t)


def test_speed_8_waits_for_the_whole_frame():
    def t(clock):
        sem = FrameSem(clock, frame_ms=10100.0, speed=8)
        t0 = clock.time()
        w, _, _ = _grab(sem)
        assert w is None and sem.frames == 1
        assert 10.1 <= clock.time() - t0 < 10.6
    _run(t)


def test_freeze_mode_must_read_end_frame_or_nothing_is_scanned():
    def t(clock):
        for shown in ("Command", "End Line"):
            sem = FrameSem(clock); sem.freeze_on, sem.freeze_on_sticks = shown, False
            try:
                _grab(sem)
            except FG.FreezeModeError as e:
                assert shown in str(e) and "no frame was taken" in str(e)
            else:
                raise AssertionError("not refused")
            assert "CMD_UNFREEZE_ZONE" not in sem.cmds and sem.grabs == 0
        # unreadable both ways is refused too
        class NoRead(FrameSem):
            def get_str(self, m):
                if m == "DP_FREEZE_ON":
                    raise RuntimeError("x")
                return super().get_str(m)
        sem = NoRead(clock)
        try:
            _grab(sem)
            raise AssertionError("not refused")
        except FG.FreezeModeError:
            assert sem.grabs == 0
        # the number 0 means End Frame
        class Num(NoRead):
            def get_float(self, m):
                return 0.0 if m == "DP_FREEZE_ON" else super().get_float(m)
        assert _grab(Num(clock))[0] is None
    _run(t)


def test_frame_that_ends_too_soon_is_retried_once():
    def t(clock):
        sem = FrameSem(clock); sem.immediate_freezes = 1
        w, logs, _ = _grab(sem)
        assert w is None and sem.cmds.count("CMD_UNFREEZE_ZONE") == 2 and sem.grabs == 1
        assert any("did not run. Taking one more frame" in l for l in logs)
    _run(t)


def test_frame_that_ends_too_soon_twice_is_stale():
    def t(clock):
        sem = FrameSem(clock); sem.immediate_freezes = 2
        w, _, p = _grab(sem)
        assert FG.FRAME_TOO_SHORT in w and FG.is_stale_warning(w)
        assert p.exists() and sem.grabs == 1               # kept for inspection
    _run(t)


def test_too_short_check_skipped_for_reduced_raster_and_unknown_frame_time():
    def t(clock):
        sem = FrameSem(clock); sem.immediate_freezes = 2
        assert _grab(sem, check_too_short=False)[0] is None
        sem = FrameSem(clock, frame_ms=None); sem.immediate_freezes = 2
        w, logs, _ = _grab(sem, frozen_timeout_s=5.0)
        assert w is None and any("AP_FRAME_TIME unreadable" in l for l in logs)
    _run(t)


def test_identical_frame_is_retried_then_stale():
    def t(clock):
        sem = FrameSem(clock)
        assert _grab(sem)[0] is None
        sem.store_never_refreshes = True
        w, logs, _ = _grab(sem)
        assert FG.FRAME_IDENTICAL in w and FG.is_stale_warning(w)
        assert sem.grabs == 3 and any("byte-identical" in l for l in logs)
        # a NEW frame after a repeat is fine; another connection has its own history
        sem.store_never_refreshes = False
        assert _grab(sem)[0] is None
        assert _grab(FrameSem(clock))[0] is None
    _run(t)


def test_wait_for_frozen_is_two_frame_times():
    def t(clock):
        sem = FrameSem(clock, frame_ms=1000.0); sem.never_freezes = True
        t0 = clock.time()
        w, _, _ = _grab(sem)
        assert 2.0 <= clock.time() - t0 < 2.5
        # v0.54.0 review: an unseen frame end may leave the PREVIOUS frame in the store
        assert FG.FRAME_NOT_FROZEN in w and FG.is_stale_warning(w)
        assert sem.cmds.count("CMD_FREEZE_ZONE") == 2      # asked once more to stop the dose
    _run(t)


def test_frame_time_read_before_a_speed_change_settles_does_not_cut_the_wait():
    def t(clock):
        sem = FrameSem(clock, frame_ms=358.54)
        sem.actual_s = 1.3                                  # the scan is really at speed 5
        orig = sem.get_float
        reads = {"n": 0}
        def gf(m):
            if m == "AP_FRAME_TIME":
                reads["n"] += 1
                return 358.54 if reads["n"] == 1 else 1300.0
            return orig(m)
        sem.get_float = gf
        w, logs, _ = _grab(sem)
        assert w is None and sem.frames == 1, w
        assert any("waiting" in l and "more" in l for l in logs)
    _run(t)


def test_a_brief_dp_frozen_read_failure_does_not_end_the_wait():
    # review finding 1A: one failed read used to return at once -> grabbed mid-frame
    def t(clock):
        sem = FrameSem(clock, frame_ms=10100.0, speed=8)
        orig = sem.get_str
        state = {"n": 0}
        def gs(m):
            if m == "DP_FROZEN" and sem.pending is not None and state["n"] < 3:
                state["n"] += 1
                raise RuntimeError("COM hiccup")
            return orig(m)
        sem.get_str = gs
        orig_f = sem.get_float                              # the number read fails too
        def gf(m):
            if m == "DP_FROZEN":
                raise RuntimeError("COM hiccup")
            return orig_f(m)
        sem.get_float = gf
        t0 = clock.time()
        w, _, _ = _grab(sem)
        assert w is None and sem.frames == 1 and clock.time() - t0 >= 10.1
    _run(t)


def test_a_brief_read_failure_while_waiting_for_live_is_not_unreadable():
    # second review: one failed read in the Live poll sent the frame down the unreadable
    # path (fixed wait, clean result) -> the Live wait polls through it
    def t(clock):
        sem = FrameSem(clock, frame_ms=10100.0, speed=8)
        orig = sem.get_str
        state = {"n": 0}
        def gs(m):
            if m == "DP_FROZEN" and not sem.frozen and state["n"] < 1:
                state["n"] += 1
                raise RuntimeError("COM hiccup")
            return orig(m)
        sem.get_str = gs
        w, logs, _ = _grab(sem)
        assert w is None and sem.frames == 1
        assert not any("DP_FROZEN unreadable" in l for l in logs)
    _run(t)


def test_unreadable_after_the_unfreeze_is_not_used_if_it_was_readable_before():
    def t(clock):
        sem = FrameSem(clock, frame_ms=10100.0, speed=8)
        sem.actual_s = 10.1
        n = {"unfrozen": False}
        orig_exec = sem.execute
        def ex(c):
            orig_exec(c)
            if c == "CMD_UNFREEZE_ZONE":
                sem.frozen_unreadable = True        # unreadable from now on
        sem.execute = ex
        sem.frame_ms = 358.54                        # AP_FRAME_TIME still at speed 3
        w, _, _ = _grab(sem, frame_wait_s=3.0)
        assert FG.FRAME_NOT_FROZEN in w and FG.is_stale_warning(w)
    _run(t)


def test_fast_speeds_are_not_timed():
    # speeds 1-2: the polls cannot tell a real frame from a cut one -> no false refusal
    def t(clock):
        sem = FrameSem(clock, frame_ms=90.0, speed=1); sem.immediate_freezes = 2
        w, _, _ = _grab(sem)
        assert w is None and sem.cmds.count("CMD_UNFREEZE_ZONE") == 1
    _run(t)


def test_dp_frozen_unreadable_waits_one_frame_time():
    def t(clock):
        sem = FrameSem(clock, frame_ms=1300.0); sem.frozen_unreadable = True
        t0 = clock.time()
        w, logs, _ = _grab(sem, frame_wait_s=0.25)
        assert w is None and sem.grabs == 1
        # the Live wait runs its 2 s (polling through failures), then one frame + margin
        assert abs((clock.time() - t0) - (FG.LIVE_TIMEOUT_S + 1.2 * 1.3 + 0.3)) < 0.1
        assert any("DP_FROZEN unreadable" in l for l in logs)
    _run(t)


def test_scan_that_never_restarts_is_stale():
    def t(clock):
        sem = FrameSem(clock); sem.unfreeze_ignored = 2
        w, _, _ = _grab(sem)
        assert FG.SCAN_NOT_RESTARTED in w and FG.is_stale_warning(w)
        assert sem.cmds.count("CMD_UNFREEZE_ZONE") == 2
    _run(t)


# ------------------------------------------------------------- ensure_fib_imaging
class SwitchSem:
    """SEM imaging after SmartFIB; CMD_FIB_MODE_FIB brings FIB on after 0.3 s and the FIB
    magnification resets to 175.9x at +reset_at s (None = never)."""
    def __init__(self, clock, *, imaging="SEM", reset_at=0.8, keeps_changing=False,
                 fib_ok=True):
        self.clock, self.imaging, self.reset_at = clock, imaging, reset_at
        self.keeps_changing, self.fib_ok = keeps_changing, fib_ok
        self.t_cmd = None
        self.mag0 = 2000.0
        self.cmds = []

    def _fib(self):
        return (self.imaging == "FIB" or (self.t_cmd is not None and self.fib_ok
                                          and self.clock.time() - self.t_cmd >= 0.3))

    def get_str(self, m):
        if m == "DP_FIB_IMAGING":
            return "FIB" if self._fib() else self.imaging
        raise KeyError(m)

    def get_float(self, m):
        if m == "DP_FIB_IMAGING":
            return 1.0 if self._fib() else 0.0
        if m == "AP_FIB_MAGNIFICATION":
            if self.t_cmd is None:
                return self.mag0
            dt = self.clock.time() - self.t_cmd
            if self.keeps_changing:
                return 100.0 + round(dt * 10)
            return 175.9 if (self.reset_at is not None and dt >= self.reset_at) else self.mag0
        raise KeyError(m)

    def execute(self, c):
        self.cmds.append(c)
        if c == "CMD_FIB_MODE_FIB":
            self.t_cmd = self.clock.time()


def test_already_in_fib_imaging_sends_nothing_and_waits_nothing():
    clock = VClock(); sem = SwitchSem(clock, imaging="FIB")
    assert FG.ensure_fib_imaging(sem, clock=clock.time, sleep=clock.sleep) is False
    assert sem.cmds == [] and clock.t == 1000.0


def test_switch_waits_for_the_late_magnification_reset():
    clock = VClock(); sem = SwitchSem(clock, reset_at=0.8)
    logs = []
    assert FG.ensure_fib_imaging(sem, log=logs.append, clock=clock.time,
                                 sleep=clock.sleep) is True
    assert sem.cmds == ["CMD_FIB_MODE_FIB"]
    took = clock.t - 1000.0
    assert took >= 0.8 + 1.0 - 1e-6, took                 # reset seen, then 1 s steady
    assert sem.get_float("AP_FIB_MAGNIFICATION") == 175.9  # the caller sets 2000x AFTER this
    assert "2000x -> 175.9x" in logs[-1] and "steady for 1 s" in logs[-1], logs
    # no reset at all: min 0.5 s + 1 s steady
    clock = VClock(); sem = SwitchSem(clock, reset_at=None)
    FG.ensure_fib_imaging(sem, log=logs.append, clock=clock.time, sleep=clock.sleep)
    assert 1.5 - 1e-6 <= clock.t - 1000.0 < 2.2 and "unchanged at 2000x" in logs[-1]


def test_switch_gives_up_waiting_after_max_settle_and_warns():
    clock = VClock(); sem = SwitchSem(clock, keeps_changing=True)
    logs = []
    FG.ensure_fib_imaging(sem, log=logs.append, clock=clock.time, sleep=clock.sleep)
    assert "WARNING: still changing after 5 s" in logs[-1]
    assert clock.t - 1000.0 < 6.0


def test_switch_timeout_raises():
    clock = VClock(); sem = SwitchSem(clock, fib_ok=False)
    try:
        FG.ensure_fib_imaging(sem, timeout_s=5.0, clock=clock.time, sleep=clock.sleep)
    except FG.FibSwitchTimeout as e:
        assert "did not come on within 5 s" in str(e)
    else:
        raise AssertionError("no timeout")


def test_after_image_retakes_once_when_the_column_changes_during_the_frame():
    from ..after_image import grab_after_mill_image
    from .test_after_image import ScopeSem, _Rec

    class OnceJump(ScopeSem):
        n = 0
        def grab_bmp(self, path, **kw):
            OnceJump.n += 1
            self.mag_jumps_in_frame = OnceJump.n == 1      # only the first frame
            super().grab_bmp(path, **kw)
            Path(path).write_bytes(b"BM" + bytes([OnceJump.n]))

    sem = OnceJump()
    d = Path(tempfile.mkdtemp()) / "images"
    logs = []
    path, warn = grab_after_mill_image(sem, _Rec(), d, milling_active=lambda: False,
                                       frame_wait_s=0.0, frozen_timeout_s=1.0,
                                       sleep=lambda s: None, log=logs.append)
    assert path is not None and path.exists() and warn is None, warn
    assert [e for e in sem.events if e[0] == "grab"].__len__() == 2
    assert any("one more frame" in l for l in logs)
    import json
    meta = json.loads(Path(str(path) + ".json").read_text())
    assert meta["first_frame_changed"]["fib_magnification_after_grab"] == 52.0


def test_after_image_does_not_retake_when_milling_or_imaging_changed():
    from ..after_image import grab_after_mill_image
    from .test_after_image import ScopeSem, _Rec

    class ToMill(ScopeSem):                          # a mill resumes during the frame
        def grab_bmp(self, path, **kw):
            super().grab_bmp(path, **kw)
            self.fib = "Mill + SEM"

    class MagJump(ScopeSem):                         # FIB still on, but SmartSEM says milling
        def grab_bmp(self, path, **kw):
            super().grab_bmp(path, **kw)
            self.v["AP_FIB_MAGNIFICATION"] = 52.0

    for cls, milling_after_first in ((ToMill, False), (MagJump, True)):
        sem = cls()
        calls = []
        def active():
            calls.append(1)
            return milling_after_first and len(calls) > 1
        d = Path(tempfile.mkdtemp()) / "images"
        path, warn = grab_after_mill_image(sem, _Rec(), d, milling_active=active,
                                           frame_wait_s=0.0, frozen_timeout_s=1.0,
                                           sleep=lambda s: None, log=lambda m: None)
        assert path is None and "NOT used" in warn, (cls.__name__, warn)
        assert len([e for e in sem.events if e[0] == "grab"]) == 1, cls.__name__
        n_fib = len([e for e in sem.events if e == ("cmd", "CMD_FIB_MODE_FIB")])
        assert n_fib <= 1, cls.__name__                   # nothing re-sent
        if cls is MagJump:
            assert len(calls) == 2                        # milling checked again


def test_capture_switches_only_when_not_in_fib_imaging():
    from ..positions import capture_position
    from .test_capture_fresh_frame import _FrameStoreSem

    class Sw(_FrameStoreSem):
        def __init__(self, scene, imaging):
            super().__init__(scene); self.imaging = imaging; self.cmds = []
        def execute(self, c):
            self.cmds.append(c)
            if c == "CMD_FIB_MODE_FIB":
                self.imaging = "FIB"
            super().execute(c)
        def get_str(self, m):
            if m == "DP_FIB_IMAGING":
                return self.imaging
            return super().get_str(m)

    d = Path(tempfile.mkdtemp())
    for imaging, sent in (("FIB", 0), ("SEM", 1)):
        sem = Sw(50, imaging)
        capture_position(sem, f"A{imaging}", fib_magnification_x=2000.0,
                         image_bmp=d / f"A{imaging}.bmp", frame_wait_s=0.0)
        assert sem.cmds.count("CMD_FIB_MODE_FIB") == sent, (imaging, sem.cmds)



def test_every_frame_caller_uses_the_protocol_and_the_shared_switch():
    """No caller keeps its own fixed live-scan wait or its own switch to FIB imaging."""
    root = Path(FG.__file__).parent
    for f in ("positions.py", "after_image.py", "clem/overview.py", "refine/sources.py",
              "autofocus/sources.py"):
        src = (root / f).read_text(encoding="utf-8")
        assert "grab_fresh_frame(" in src, f
        assert "sleep(self.frame_wait_s)" not in src and "sleep(frame_wait_s)" not in src, f
    for f in ("positions.py", "after_image.py", "refine/sources.py", "autofocus/sources.py",
              "clem/markup.py", "cli.py"):
        src = (root / f).read_text(encoding="utf-8")
        assert "ensure_fib_imaging(" in src, f
    # CMD_FIB_MODE_FIB is sent only by the shared switch (and the debug tool's target map)
    for f in ("positions.py", "after_image.py", "refine/sources.py", "autofocus/sources.py",
              "clem/markup.py"):
        assert 'execute("CMD_FIB_MODE_FIB")' not in (root / f).read_text(encoding="utf-8"), f

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
