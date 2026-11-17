"""REGRESSION: capture-position saved a STALE image (the previous position's frame).

Grab() saves the frame store as-is. The old capture did CMD_UNFREEZE_ZONE and then
Grab() immediately, before any new frame had been scanned -- so whenever the scan was
frozen going in (every capture ends frozen; refine/autofocus leave it frozen), the BMP
was the LAST frozen frame. Capture #2 saved capture #1's image, and refine then used
that stale reference.

The mock models exactly that: the frame store only refreshes when a frame completes
(freeze at end of frame) after an unfreeze; Grab() writes the store's contents.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from ..positions import capture_position


class _FrameStoreSem:
    def __init__(self, scene: int):
        self.scene = scene          # what the beam would image at the current stage pos
        self.store = 0              # the frame store (what Grab() saves)
        self.live = False
        self.never_frozen = False   # simulate DP_FROZEN never reporting Frozen
        self.freeze_on = None
        self.mag = None             # v0.53.4: AP_FIB_MAGNIFICATION is read back
        self.unfreeze_ignored = 0   # v0.53.4: unfreezes SmartSEM drops (scan stays frozen)

    def set_value(self, m, v):
        if m == "DP_FREEZE_ON":
            self.freeze_on = v
        if m == "AP_FIB_MAGNIFICATION":
            self.mag = float(v)

    def execute(self, c):
        if c == "CMD_UNFREEZE_ZONE":
            if self.unfreeze_ignored > 0:
                self.unfreeze_ignored -= 1
                return
            self.live = True
        elif c in ("CMD_FREEZE_ZONE", "CMD_FREEZE_ALL"):
            if self.live:
                # end-of-frame freeze completes a FRESH frame; a lower mag images a
                # different field (the scene value stands in for the picture)
                self.store = self.scene + (100 if self.mag == 800.0 else 0)
            self.live = False

    def get_str(self, m):
        if m == "DP_FROZEN":
            return "Live" if (self.live or self.never_frozen) else "Frozen"
        return ""

    def get_float(self, m):
        if m == "AP_FIB_MAGNIFICATION":
            return self.mag if self.mag is not None else 348.7
        return {"AP_STAGE_AT_X": 1e-3, "AP_STAGE_AT_Y": 2e-3, "AP_STAGE_AT_Z": 3e-3,
                "AP_FIB_OBJECTIVE_POTENTIAL": 16400.0, "AP_HEIGHT": 4.286e-5}.get(m, 0.0)

    def grab_bmp(self, path, **k):
        from PIL import Image
        Image.fromarray(np.full((48, 64), self.store, dtype=np.uint8)).save(path)


def _value(path) -> int:
    from PIL import Image
    return int(np.asarray(Image.open(path))[0, 0])


def _cap(sem, name, d, **kw):
    kw.setdefault("frame_wait_s", 0.0)
    return capture_position(sem, name, fib_magnification_x=2000.0,
                            image_bmp=d / f"{name}.bmp", ensure_fib_image=False, **kw)


def test_second_capture_saves_the_new_site_not_the_previous_frame():
    d = Path(tempfile.mkdtemp())
    sem = _FrameStoreSem(scene=50)                 # site A
    _cap(sem, "A", d)
    assert _value(d / "A.bmp") == 50
    assert not sem.live                            # capture leaves the scan frozen
    sem.scene = 200                                # stage moved to site B; store still A
    _cap(sem, "B", d)
    assert _value(d / "B.bmp") == 200, "stale grab: saved the previous position's frame"
    assert sem.freeze_on in ("End Frame", 0.0)     # freezes at END of frame


def test_as_is_flag_still_grabs_the_frame_on_screen():
    # --no-unfreeze-before-grab keeps its explicit meaning: grab what is displayed.
    d = Path(tempfile.mkdtemp())
    sem = _FrameStoreSem(scene=50); _cap(sem, "A", d)
    sem.scene = 200
    _cap(sem, "B", d, unfreeze_before_grab=False)
    assert _value(d / "B.bmp") == 50


def test_freeze_after_grab_false_leaves_scan_live():
    d = Path(tempfile.mkdtemp())
    sem = _FrameStoreSem(scene=50)
    _cap(sem, "A", d, freeze_after_grab=False)
    assert _value(d / "A.bmp") == 50 and sem.live


def test_unconfirmed_frame_is_refused():
    # v0.54.0 review: with the immediate end-of-frame freeze, a frame whose end was never
    # seen may still be the PREVIOUS frame store -> never a reference (was: a note).
    from ..smartsem_com import SmartSEMError
    d = Path(tempfile.mkdtemp())
    sem = _FrameStoreSem(scene=50); sem.never_frozen = True
    try:
        _cap(sem, "A", d, frozen_timeout_s=0.3)
    except SmartSEMError as e:
        assert "DP_FROZEN did not report Frozen" in str(e) and "capture refused" in str(e)
    else:
        raise AssertionError("an unconfirmed frame was recorded")
    assert (d / "A_suspect.bmp").exists() and not (d / "A.bmp").exists()


def test_coarse_reference_is_grabbed_after_fine_and_mag_restored():
    d = Path(tempfile.mkdtemp())
    class _Sem(_FrameStoreSem):
        def __init__(self, scene):
            super().__init__(scene); self.mags = []; self.cmds = []; self.rate = 8
        def set_value(self, m, v):
            super().set_value(m, v)
            if m == "AP_FIB_MAGNIFICATION": self.mags.append(float(v))
        def execute(self, c):
            super().execute(c); self.cmds.append(c)
            if c.startswith("CMD_SCANRATE"): self.rate = int(c[len("CMD_SCANRATE"):])
        def get_float(self, m):
            if m == "DP_SCANRATE": return float(self.rate)
            return super().get_float(m)
    sem = _Sem(scene=50)
    rec = _cap(sem, "A", d, coarse_magnification_x=800.0, coarse_scan_rate=5)
    assert (d / "A.bmp").exists() and (d / "A_coarse.bmp").exists()
    assert rec.image_bmp_coarse and rec.image_bmp_coarse.endswith("A_coarse.bmp")
    assert rec.coarse_magnification_x == 800.0 and rec.fib_magnification_x == 2000.0
    assert sem.mags == [2000.0, 800.0, 2000.0]            # fine, coarse, restored
    assert "CMD_SCANRATE5" in sem.cmds and sem.rate == 8   # coarse at 5, then restored
    assert not sem.live                                     # left frozen
    assert not rec.notes


def test_coarse_failure_keeps_fine_capture_and_warns():
    d = Path(tempfile.mkdtemp())
    class _Sem(_FrameStoreSem):
        def __init__(self, scene):
            super().__init__(scene); self.mag = 2000.0; self.mags = []
        def set_value(self, m, v):
            super().set_value(m, v)
            if m == "AP_FIB_MAGNIFICATION": self.mags.append(float(v))
        def grab_bmp(self, path, **k):
            if self.mag == 800.0: raise RuntimeError("coarse grab failed")
            super().grab_bmp(path, **k)
    sem = _Sem(scene=50)
    rec = _cap(sem, "A", d, coarse_magnification_x=800.0)
    assert (d / "A.bmp").exists() and _value(d / "A.bmp") == 50   # fine intact
    assert rec.image_bmp_coarse is None and rec.coarse_magnification_x is None
    assert rec.notes and "coarse reference" in rec.notes and "FAILED" in rec.notes
    assert sem.mags[-1] == 2000.0                               # mag restored


def test_position_1_case_a_scan_that_never_restarts_gives_no_coarse_copy():
    """Evan 2026-10-12: position_1's 800x file was a byte copy of its 2000x image -- the
    scan never restarted for the coarse frame. Now: unfreeze resent, then retried once;
    still stale -> no coarse reference (kept as _suspect), the fine capture stands."""
    d = Path(tempfile.mkdtemp())
    sem = _FrameStoreSem(scene=50)
    real_set = sem.set_value

    def set_value(m, v):                      # SmartSEM drops every unfreeze at 800x
        real_set(m, v)
        if m == "AP_FIB_MAGNIFICATION" and float(v) == 800.0:
            sem.unfreeze_ignored = 2
    sem.set_value = set_value
    rec = _cap(sem, "position_1", d, coarse_magnification_x=800.0)
    assert _value(d / "position_1.bmp") == 50
    assert rec.image_bmp_coarse is None and rec.coarse_magnification_x is None
    assert "not a new frame twice" in rec.notes and "FAILED" in rec.notes
    assert (d / "position_1_coarse_suspect.bmp").exists()
    assert not (d / "position_1_coarse.bmp").exists()
    assert sem.mag == 2000.0                                   # restored


def test_a_dropped_unfreeze_is_resent_and_the_frame_is_fresh():
    d = Path(tempfile.mkdtemp())
    sem = _FrameStoreSem(scene=50)
    real_set = sem.set_value

    def set_value(m, v):                      # the first unfreeze at 800x is dropped
        real_set(m, v)
        if m == "AP_FIB_MAGNIFICATION" and float(v) == 800.0:
            sem.unfreeze_ignored = 1
    sem.set_value = set_value
    rec = _cap(sem, "A", d, coarse_magnification_x=800.0)
    assert rec.coarse_magnification_x == 800.0 and _value(d / "A_coarse.bmp") == 150
    assert (d / "A.bmp").read_bytes() != (d / "A_coarse.bmp").read_bytes()


def test_identical_coarse_frame_is_retried_once():
    """The frame store did not change although the scan reported Live (a dropped
    magnification change, say): byte-identical to the fine image -> one retry."""
    d = Path(tempfile.mkdtemp())
    sem = _FrameStoreSem(scene=50)
    calls = {"n": 0}
    real_exec = sem.execute

    def execute(c):
        real_exec(c)
        if c in ("CMD_FREEZE_ZONE", "CMD_FREEZE_ALL") and sem.mag == 800.0:
            calls["n"] += 1
            if calls["n"] == 1:
                sem.store = 50                # first coarse frame: same picture as fine
    sem.execute = execute
    rec = _cap(sem, "A", d, coarse_magnification_x=800.0)
    assert calls["n"] == 2 and rec.coarse_magnification_x == 800.0
    assert _value(d / "A_coarse.bmp") == 150


def test_magnification_that_does_not_take_refuses_or_skips():
    from ..smartsem_com import SmartSEMError
    d = Path(tempfile.mkdtemp())
    sem = _FrameStoreSem(scene=50)
    sem.set_value = lambda m, v: None         # sets ignored: reads 348.7x
    try:
        _cap(sem, "A", d)
    except SmartSEMError as e:
        assert "reads 348.7x after setting 2000x" in str(e)
    else:
        raise AssertionError("capture at the wrong magnification was accepted")
    assert not (d / "A.bmp").exists()
    sem = _FrameStoreSem(scene=50)            # coarse set ignored: fine kept, no coarse
    real_set = sem.set_value
    sem.set_value = lambda m, v: None if (m == "AP_FIB_MAGNIFICATION" and float(v) == 800.0) \
        else real_set(m, v)
    rec = _cap(sem, "B", d, coarse_magnification_x=800.0)
    assert rec.image_bmp and rec.image_bmp_coarse is None
    assert "reads 2000.0x after setting 800x" in rec.notes and not (d / "B_coarse.bmp").exists()


def test_a_magnification_change_during_the_fine_frame_refuses_the_capture():
    from ..smartsem_com import SmartSEMError
    d = Path(tempfile.mkdtemp())
    sem = _FrameStoreSem(scene=50)
    real_grab = sem.grab_bmp

    def grab(path, **k):
        real_grab(path, **k)
        sem.mag = 52.0                         # SmartSEM changed it under the frame
    sem.grab_bmp = grab
    try:
        _cap(sem, "A", d)
    except SmartSEMError as e:
        assert "read 52.0x after the frame" in str(e)
    else:
        raise AssertionError("not refused")
    assert (d / "A_suspect.bmp").exists() and not (d / "A.bmp").exists()


def test_no_coarse_by_default():
    d = Path(tempfile.mkdtemp()); sem = _FrameStoreSem(scene=50)
    rec = _cap(sem, "A", d)
    assert rec.image_bmp_coarse is None and not (d / "A_coarse.bmp").exists()


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
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
