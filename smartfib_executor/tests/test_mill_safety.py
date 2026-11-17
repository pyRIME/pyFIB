"""v0.55.1 (code review 2026-11-11): crash sources that could reach the microscope.

#16  the batch never writes the FIB beam shift while SmartSEM indicates milling -- after a
     mill timeout, when the pyFIB window is gone (output pipe closed), on a wait mode that
     does not confirm the end, at batch start; a skipped reset is done before the next
     stage move; a lost window stops the batch before the next site.
#19  positions.json: unique temp name, retry on a Windows lock, .bak, no half-written file.
#20  non-finite numbers: never sent to MoveStage, never "arrived", never loaded or saved.
#18  the Log notes when the pyFIB window holds a second SmartSEM connection.

Offline: fakes only, nothing connects.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path

from .test_after_image import ScopeSem
from .test_workflow import _rec
from ..positions import PositionDatabase, PositionRecord, StageAt
from ..smartsem_com import SmartSEM, SmartSEMError

BEAM = ("AP_FIB_BEAM_SHIFT_X", "AP_FIB_BEAM_SHIFT_Y")


def _beam_writes(events):
    return [e for e in events if e[0] == "set" and e[1] in BEAM]


def _run_batch(runner_cls, *, names=("p1", "p2"), sem=None, events=None, wait="auto",
               continue_on_error=False, done_confirm_s=0.0, patch_wait=True):
    """mill_positions_sequential with a fake scope / runner. Returns (events, sem, error)."""
    import smartfib_executor.batch as batch
    events = events if events is not None else []
    sem = sem or ScopeSem(events, fib="SEM")
    db = PositionDatabase()
    for n in names:
        db.positions[n] = _rec(n)
    real = (batch.PatternRunner, batch.recall_position, batch._wait_not_milling_stable)
    batch.PatternRunner = lambda s: runner_cls(sem, events)
    batch.recall_position = lambda s, rec, **kw: events.append(("recall", rec.name))
    if patch_wait:
        def waited(s, **kw):                       # the mill ends while the batch waits
            events.append(("wait_not_milling",))
            sem.fib = "SEM"
        batch._wait_not_milling_stable = waited
    err = None
    old_out = sys.stdout
    try:
        batch.mill_positions_sequential(
            sem, db=db, position_names=list(names), ely_local_path=Path("x.ely"),
            move_method="movestage", wait_stage_idle=False, settle_s=0.0, load_method="none",
            wait_strategy=wait, done_confirm_s=done_confirm_s, tilt_limit_deg=None,
            continue_on_error=continue_on_error)
    except BaseException as e:  # noqa: BLE001
        err = e
    finally:
        sys.stdout = old_out
        batch.PatternRunner, batch.recall_position, batch._wait_not_milling_stable = real
    return events, sem, err


class _Base:
    def __init__(self, sem, events):
        self.sem, self.events = sem, events


class TimeoutRunner(_Base):
    """runner.py lines 512 / 653 / 722: the wait gives up while SmartFIB still mills."""
    def run(self, job):
        self.sem.fib = "Mill"
        self.sem.beam["AP_FIB_BEAM_SHIFT_X"] = 0.9e-6      # the refine correction in use
        self.events.append(("mill_start",))
        raise SmartSEMError("Timeout waiting for milling to finish (no stable completion "
                            "signature detected).")


# ------------------------------------------------------------------ #16: mill timeout
def test_mill_timeout_writes_no_beam_shift_while_milling():
    ev, sem, err = _run_batch(TimeoutRunner, continue_on_error=False)
    assert isinstance(err, SmartSEMError)
    i = ev.index(("mill_start",))
    assert _beam_writes(ev[i:]) == [], ev[i:]
    assert sem.beam["AP_FIB_BEAM_SHIFT_X"] == 0.9e-6        # the correction is still on
    assert ("recall", "p2") not in ev


def test_skipped_reset_is_done_before_the_next_stage_move():
    ev, sem, err = _run_batch(TimeoutRunner, continue_on_error=True)
    i = ev.index(("mill_start",))
    after = ev[i + 1:]
    # nothing is written until the interlock has seen milling end ...
    w = after.index(("wait_not_milling",))
    assert _beam_writes(after[:w]) == [], after[:w]
    # ... then the beam shift is zeroed, and only then does the stage move to p2
    z = next(k for k, e in enumerate(after) if e[0] == "beamzero")
    r = after.index(("recall", "p2"))
    assert w < z < r, after


def test_milling_again_before_the_pending_reset_does_not_move():
    class StaysMilling(TimeoutRunner):
        pass
    import smartfib_executor.batch as batch
    events = []
    sem = ScopeSem(events, fib="SEM")
    real = batch._wait_not_milling_stable
    batch._wait_not_milling_stable = lambda s, **kw: events.append(("wait_not_milling",))
    try:                                           # the wait "passes" but milling still reads
        ev, sem, err = _run_batch(StaysMilling, sem=sem, events=events,
                                  continue_on_error=True, patch_wait=False)
    finally:
        batch._wait_not_milling_stable = real
    i = ev.index(("mill_start",))
    assert _beam_writes(ev[i:]) == []
    assert ("recall", "p2") not in ev              # p2 is never moved to


# ------------------------------------------------------------------ #16: the window is gone
class _Broken:
    """A stdout whose reader is gone."""
    encoding = "utf-8"

    def write(self, s):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


def test_broken_output_pipe_mid_mill_writes_no_beam_shift():
    """Without the safe stream (pyFIB used as a library): the print raises in the wait."""
    class Runner(_Base):
        def run(self, job):
            self.sem.fib = "Mill"
            self.events.append(("mill_start",))
            sys.stdout = _Broken()                 # the pyFIB window has just died
            print("  [wait] elapsed 12 s ...")
    ev, sem, err = _run_batch(Runner)
    assert isinstance(err, BrokenPipeError)
    i = ev.index(("mill_start",))
    assert _beam_writes(ev[i:]) == [], ev[i:]


def test_lost_window_finishes_the_site_and_stops_before_the_next():
    from .. import safe_output
    from ..batch import BatchStopped
    safe_output._reset_for_tests()

    class Runner(_Base):
        def run(self, job):
            self.sem.fib = "Mill"
            self.events.append(("mill_start",))
            sys.stdout = safe_output.SafeStream(_Broken(), "stdout")   # as cli.main installs
            print("  [wait] elapsed 12 s ...")     # dropped, not raised
            self.sem.fib = "SEM"                   # the mill ends normally
            self.events.append(("mill_done",))
    try:
        ev, sem, err = _run_batch(Runner)
        assert safe_output.output_lost()
        assert isinstance(err, BatchStopped) and "stopped before p2" in str(err), err
        assert ("mill_done",) in ev and ("recall", "p2") not in ev
        d = ev.index(("mill_done",))
        assert any(e[0] == "beamzero" for e in ev[d:])     # p1 was finished normally
    finally:
        safe_output._reset_for_tests()


def test_safe_stream_never_raises_and_remembers():
    from .. import safe_output
    safe_output._reset_for_tests()
    try:
        s = safe_output.SafeStream(_Broken(), "stdout")
        assert s.write("abc") == 3 and safe_output.output_lost()
        s.flush(); s.write("more")                 # the dead stream is not touched again
        ok = io.StringIO()
        g = safe_output.SafeStream(ok, "stdout")
        g.write("x"); g.flush()
        assert ok.getvalue() == "x"
        closed = io.StringIO(); closed.close()
        safe_output.SafeStream(closed, "stderr").write("y")    # ValueError: closed file
        old = sys.stdout, sys.stderr
        try:
            safe_output.install(); a = sys.stdout
            safe_output.install()
            assert sys.stdout is a and getattr(a, "_pyfib_safe", False)   # idempotent
        finally:
            sys.stdout, sys.stderr = old
        safe_output.say("never raises")
    finally:
        safe_output._reset_for_tests()


# ------------------------------------------------------------------ #16: other paths
def test_wait_mode_without_confirmation_does_not_reset_into_a_running_mill():
    class StartOnly(_Base):                        # --wait none: returns right after start
        def run(self, job):
            self.sem.fib = "Mill"
            self.events.append(("mill_start",))
    ev, sem, err = _run_batch(StartOnly, names=("p1",), wait="none")
    assert err is None
    i = ev.index(("mill_start",))
    assert _beam_writes(ev[i:]) == [], ev[i:]


def test_batch_start_waits_for_a_running_mill_before_the_first_reset():
    events = []
    sem = ScopeSem(events, fib="Mill")             # something is milling at Start

    class Ok(_Base):
        def run(self, job):
            self.events.append(("mill_start",))
    ev, sem, err = _run_batch(Ok, names=("p1",), sem=sem, events=events)
    assert err is None
    w = ev.index(("wait_not_milling",))
    assert _beam_writes(ev[:w]) == [], ev[:w]
    assert any(e[0] == "beamzero" for e in ev[w:])


def test_normal_site_still_resets_the_beam_shift():
    class Ok(_Base):
        def run(self, job):
            self.events.append(("mill_start",))
    ev, sem, err = _run_batch(Ok, names=("p1",))
    assert err is None and sem.beam["AP_FIB_BEAM_SHIFT_X"] == 0.0
    assert sem.beam["AP_FIB_BEAM_SHIFT_Y"] == 0.0


def test_quiet_window_one_milling_reading_cancels_the_reset():
    from ..batch import _reset_beam_shift_when_idle
    events = []
    sem = ScopeSem(events, fib="SEM")
    t = [0.0]
    polls = []

    def sleep(s):
        t[0] += s
        polls.append(t[0])
        if len(polls) == 3:
            sem.fib = "Mill"                       # the next part of the pattern starts
    said = []
    got = _reset_beam_shift_when_idle(sem, logger=said.append, where="after p1", quiet_s=20.0,
                                      _sleep=sleep, _clock=lambda: t[0])
    assert got is None and _beam_writes(events) == []
    assert any("NOT reset" in m for m in said)
    # quiet for the whole window -> written
    events.clear(); sem.fib = "SEM"; t[0] = 0.0
    got = _reset_beam_shift_when_idle(sem, logger=None, quiet_s=5.0,
                                      _sleep=lambda s: t.__setitem__(0, t[0] + s),
                                      _clock=lambda: t[0])
    assert got is True and len(_beam_writes(events)) == 2
    assert t[0] >= 5.0


def test_reset_helper_never_raises_and_does_not_write_when_state_unreadable():
    from ..batch import _reset_beam_shift_when_idle

    class Dead:
        writes = 0

        def get_str(self, m):
            raise RuntimeError("COM gone")

        def get_float(self, m):
            raise RuntimeError("COM gone")

        def set_value(self, m, v):
            Dead.writes += 1

    def bad_logger(m):
        raise BrokenPipeError(32, "Broken pipe")
    assert _reset_beam_shift_when_idle(Dead(), logger=bad_logger, where="x") is None
    assert Dead.writes == 0


# ------------------------------------------------------------------ #20: finite numbers
class _Api:
    def __init__(self):
        self.moves = []

    def MoveStage(self, *a):
        self.moves.append(a)
        return 0


def _bare_sem(pos=None, stage_is=0):
    sem = SmartSEM.__new__(SmartSEM)               # no COM
    sem.api = _Api()
    sem._get_stage_is_code = lambda: stage_is
    sem.get_stage_position = lambda: pos
    return sem


def test_move_stage_refuses_non_finite_before_the_api_call():
    for bad in (float("nan"), float("inf"), float("-inf"), None, "abc"):
        sem = _bare_sem()
        try:
            sem.move_stage(0.04, bad, 0.03, 2.0, 12.0, 0.013)
        except SmartSEMError as e:
            assert "Nothing was sent" in str(e)
        else:
            raise AssertionError(f"{bad!r} was sent to MoveStage")
        assert sem.api.moves == []
    sem = _bare_sem()
    sem.move_stage(0.04, 0.05, 0.03, 2.0, 12.0, 0.013)
    assert sem.api.moves == [(0.04, 0.05, 0.03, 2.0, 12.0, 0.013)]


def test_arrival_check_does_not_accept_nan():
    from ..smartsem_com import StagePosition
    at = StagePosition(x_m=0.04, y_m=0.05, z_m=0.03, tilt_deg=2.0, rot_deg=12.0, master_z_m=0.013)
    sem = _bare_sem(at)
    try:
        sem.wait_stage_arrival(x_m=float("nan"), y_m=0.05, z_m=0.03, timeout_s=0.0)
    except SmartSEMError as e:
        assert "not a finite number" in str(e)
    else:
        raise AssertionError("a NaN target counted as arrived")
    import dataclasses
    nan_read = dataclasses.replace(at, x_m=float("nan"))
    sem = _bare_sem(nan_read)
    try:
        sem.wait_stage_arrival(x_m=0.04, y_m=0.05, z_m=0.03, timeout_s=0.0, poll_s=0.0)
    except SmartSEMError as e:
        assert "Timeout" in str(e) and "nan" in str(e).lower()
    else:
        raise AssertionError("a NaN read-back counted as arrived")
    _bare_sem(at).wait_stage_arrival(x_m=0.04, y_m=0.05, z_m=0.03, timeout_s=0.0)   # arrives


def _write_positions(d, mutate=None, text=None):
    db = PositionDatabase()
    db.positions["position_1"] = _rec("position_1")
    pf = Path(d) / "positions.json"
    db.save(pf)
    if mutate is not None:
        data = json.loads(pf.read_text(encoding="utf-8"))
        mutate(data)
        pf.write_text(json.dumps(data), encoding="utf-8")
    if text is not None:
        pf.write_text(text, encoding="utf-8")
    return pf


def test_load_refuses_non_finite_and_wrong_shape():
    d = tempfile.mkdtemp()
    good = _write_positions(d)
    assert "position_1" in PositionDatabase.load(good).positions
    cases = {
        "NaN": lambda t: t.replace('"x_m": 0.001', '"x_m": NaN'),
        "Infinity": lambda t: t.replace('"x_m": 0.001', '"x_m": Infinity'),
        "1e999": lambda t: t.replace('"x_m": 0.001', '"x_m": 1e999'),
        "objective": lambda t: t.replace('"fib_objective_potential_v": 16400.0',
                                         '"fib_objective_potential_v": -1e999'),
    }
    base = good.read_text(encoding="utf-8")
    for label, f in cases.items():
        txt = f(base)
        assert txt != base, label
        pf = _write_positions(tempfile.mkdtemp(), text=txt)
        try:
            PositionDatabase.load(pf)
        except ValueError as e:
            assert "finite" in str(e), (label, e)
        else:
            raise AssertionError(f"{label} was loaded")
    for txt in ("[]", '"x"', "3"):
        try:
            PositionDatabase.load(_write_positions(tempfile.mkdtemp(), text=txt))
        except ValueError as e:
            assert "not a pyFIB positions file" in str(e)
        else:
            raise AssertionError(f"{txt} was loaded")


def test_save_refuses_non_finite_stage_and_nulls_optional_readings():
    d = Path(tempfile.mkdtemp())
    pf = _write_positions(d)
    before = pf.read_text(encoding="utf-8")
    db = PositionDatabase.load(pf)
    import dataclasses
    r = db.positions["position_1"]
    db.positions["position_1"] = dataclasses.replace(
        r, stage_at=dataclasses.replace(r.stage_at, y_m=float("nan")))
    try:
        db.save(pf)
    except ValueError as e:
        assert "position_1" in str(e) and "finite" in str(e)
    else:
        raise AssertionError("a NaN stage Y was saved")
    assert pf.read_text(encoding="utf-8") == before        # the file is untouched
    assert not list(d.glob("*.tmp"))
    db.positions["position_1"] = dataclasses.replace(r, fov_width_m=float("nan"),
                                                     fib_working_distance_m=float("inf"))
    db.save(pf)
    back = PositionDatabase.load(pf).positions["position_1"]
    assert back.fov_width_m is None and back.fib_working_distance_m is None
    assert "NaN" not in pf.read_text(encoding="utf-8")


def test_capture_refuses_a_non_finite_stage_reading():
    from .test_capture_fresh_frame import _FrameStoreSem, _cap

    class NanStage(_FrameStoreSem):
        def get_float(self, m):
            if m == "AP_STAGE_AT_Y":
                return float("nan")
            return super().get_float(m)
    d = Path(tempfile.mkdtemp())
    try:
        _cap(NanStage(40), "position_9", d)
    except SmartSEMError as e:
        assert "capture refused" in str(e) and "Nothing was recorded" in str(e)
    else:
        raise AssertionError("a NaN stage reading was captured")


# ------------------------------------------------------------------ #19: positions.json writes
def test_atomic_write_unique_tmp_retry_and_backup():
    from .. import atomic_io
    d = Path(tempfile.mkdtemp())
    p = d / "positions.json"
    assert atomic_io.unique_tmp(p) != atomic_io.unique_tmp(p)
    assert str(os.getpid()) in atomic_io.unique_tmp(p).name
    atomic_io.write_text_atomic(p, "one")
    calls, slept = [], []

    def locked_twice(a, b):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(13, "Access is denied")
        os.replace(a, b)
    atomic_io.write_text_atomic(p, "two", backup=True, _replace=locked_twice, _sleep=slept.append)
    assert p.read_text() == "two" and len(calls) == 3 and len(slept) == 2
    assert (d / "positions.json.prev").read_text() == "one"
    assert not list(d.glob("*.tmp"))

    def always_locked(a, b):
        raise PermissionError(13, "Access is denied")
    try:
        atomic_io.write_text_atomic(p, "three", _replace=always_locked, _sleep=lambda s: None)
    except PermissionError:
        pass
    else:
        raise AssertionError("a permanently locked file did not raise")
    assert p.read_text() == "two" and not list(d.glob("*.tmp"))    # old file intact, no litter


def test_positions_save_keeps_a_backup_and_two_writers_do_not_share_a_temp_file():
    d = Path(tempfile.mkdtemp())
    pf = _write_positions(d)
    first = pf.read_text(encoding="utf-8")
    db = PositionDatabase.load(pf)
    db.positions["position_2"] = _rec("position_2")
    db.save(pf)
    assert (d / "positions.json.prev").read_text(encoding="utf-8") == first
    assert set(PositionDatabase.load(pf).positions) == {"position_1", "position_2"}
    assert not (d / "positions.json.tmp").exists() and not list(d.glob("*.tmp"))


def test_run_log_save_survives_a_locked_file():
    from ..report.runlog import RunLog
    from .. import atomic_io
    d = Path(tempfile.mkdtemp())
    warned = []
    rl = RunLog(d, printer=warned.append)
    n = [0]
    real = os.replace

    def flaky(a, b):
        n[0] += 1
        if n[0] == 1:
            raise PermissionError(13, "Access is denied")
        real(a, b)
    old = atomic_io.os.replace, atomic_io.time.sleep
    try:
        atomic_io.REPLACE_DELAY_S = 0.0
        import functools
        orig = atomic_io.write_text_atomic
        atomic_io.write_text_atomic = functools.partial(orig, _replace=flaky, _sleep=lambda s: None)
        rl.site_start("p1", 1, 1)
    finally:
        atomic_io.write_text_atomic = orig
        atomic_io.REPLACE_DELAY_S = 0.2
    assert json.loads(rl.path.read_text())["sites"][0]["name"] == "p1"
    assert not [w for w in warned if "could not write" in str(w)]


# ------------------------------------------------------------------ #18: second connection note
def test_parent_holding_a_connection_is_noted_not_refused():
    import smartfib_executor.smartsem_com as S
    ppid = os.getppid()
    row = (f'"python.exe","{ppid}","Console","1","80,000 K","Running","PC\\\\sem","0:00:05",'
           f'"Remoting Forwarding Window"\n'
           f'"python.exe","99999","Console","1","80,000 K","Running","PC\\\\sem","0:00:05",'
           f'"Remoting Forwarding Window"\n')

    class R:
        def __init__(self, out):
            self.stdout = out

    def run(cmd, **kw):
        return R(row if "python.exe" in cmd[-1] else "")
    parent = []
    others = S.other_api_clients(_run=run, _is_windows=True, _parent_out=parent)
    assert [o["pid"] for o in others] == [99999]           # the parent is never "other"
    assert [o["pid"] for o in parent] == [ppid]
    assert S.other_api_clients(_run=run, _is_windows=True) == others      # old call unchanged

    said = []
    real = S.other_api_clients

    def fake(**kw):
        if kw.get("_parent_out") is not None:
            kw["_parent_out"].append({"pid": 4242, "image": "python.exe", "status": "Running",
                                      "title": "Remoting Forwarding Window"})
        return []
    S.other_api_clients = fake
    try:
        S.wait_for_other_api_clients(0.0, log=said.append)                # returns, no refusal
    finally:
        S.other_api_clients = real
    assert len(said) == 1 and "4242" in said[0] and "second" in said[0]


# ------------------------------------------------------------------ review round (2026-11-11)
def test_window_gone_before_the_mill_starts_does_not_start_it():
    """Lost during recall / autofocus / refine: before v0.55.1 the print before
    CMD_FIB_START_MILLING raised, so no mill started. That must still hold."""
    from .. import safe_output
    from ..batch import BatchStopped
    import smartfib_executor.batch as batch
    safe_output._reset_for_tests()
    started = []

    class Runner(_Base):
        def run(self, job):
            started.append(1)
            self.events.append(("mill_start",))
    events = []
    sem = ScopeSem(events, fib="SEM")
    real_recall = batch.recall_position
    try:
        for coe in (False, True):
            safe_output._reset_for_tests(); started.clear(); events.clear()

            def lose(*a, **k):
                safe_output._mark_lost("stdout", BrokenPipeError(32, "Broken pipe"))
            # the recall itself is patched by _run_batch; lose the window at the tilt/settle
            # point instead: the objective write happens right after the recall
            orig_set = sem.set_value

            def set_value(m, v, _o=orig_set):
                if m == "AP_FIB_OBJECTIVE_POTENTIAL":
                    lose()
                return _o(m, v)
            sem.set_value = set_value
            ev, _, err = _run_batch(Runner, sem=sem, events=events, continue_on_error=coe)
            sem.set_value = orig_set
            assert isinstance(err, BatchStopped) and "before its mill was started" in str(err), err
            assert started == [], "a mill was started with the window gone"
            assert ("recall", "p2") not in ev                # --continue-on-error does not go on
    finally:
        batch.recall_position = real_recall
        safe_output._reset_for_tests()


def test_batch_ending_with_the_reset_pending_warns_and_notes_it():
    import smartfib_executor.batch as batch
    from ..report.runlog import RunLog

    class StartOnly(_Base):                                  # --wait none on the only site
        def run(self, job):
            self.sem.fib = "Mill"
            self.sem.beam["AP_FIB_BEAM_SHIFT_X"] = 0.9e-6
            self.events.append(("mill_start",))
    events = []
    sem = ScopeSem(events, fib="SEM")
    db = PositionDatabase(); db.positions["p1"] = _rec("p1")
    d = Path(tempfile.mkdtemp())
    rl = RunLog(d, printer=lambda m: None)
    real = (batch.PatternRunner, batch.recall_position)
    batch.PatternRunner = lambda s: StartOnly(sem, events)
    batch.recall_position = lambda s, rec, **kw: None
    out = io.StringIO(); old = sys.stdout; sys.stdout = out
    try:
        batch.mill_positions_sequential(
            sem, db=db, position_names=["p1"], ely_local_path=Path("x.ely"),
            move_method="movestage", wait_stage_idle=False, settle_s=0.0, load_method="none",
            wait_strategy="none", done_confirm_s=0.0, tilt_limit_deg=None, run_log=rl)
    finally:
        sys.stdout = old
        batch.PatternRunner, batch.recall_position = real
    assert sem.beam["AP_FIB_BEAM_SHIFT_X"] == 0.9e-6         # never written while milling
    assert "NOT reset at the end of the batch" in out.getvalue()
    assert "beam_shift_not_reset" in json.loads(rl.path.read_text()).get("notes", {})

    # ... and when the mill has ended by then, the end-of-batch try resets it
    class Ends(_Base):
        n = 0

        def run(self, job):
            self.sem.fib = "Mill"
            self.events.append(("mill_start",))
    events2 = []
    sem2 = ScopeSem(events2, fib="SEM")
    real_reset = batch._reset_beam_shift_when_idle
    calls = []

    def reset(s, **kw):
        calls.append(kw.get("where"))
        if "end of the batch" in str(kw.get("where")):
            s.fib = "SEM"                                    # the mill has just ended
        return real_reset(s, **kw)
    batch._reset_beam_shift_when_idle = reset
    try:
        ev, sem2, err = _run_batch(Ends, names=("p1",), sem=sem2, events=events2, wait="none")
    finally:
        batch._reset_beam_shift_when_idle = real_reset
    assert err is None and sem2.beam["AP_FIB_BEAM_SHIFT_X"] == 0.0
    assert any("end of the batch" in str(c) for c in calls)


def test_ctrl_c_after_the_mill_started_checks_before_resetting():
    class Interrupted(_Base):
        def run(self, job):
            self.sem.fib = "Mill"
            self.events.append(("mill_start",))
            raise KeyboardInterrupt()                        # the abort has not taken effect yet
    ev, sem, err = _run_batch(Interrupted, names=("p1",))
    assert isinstance(err, KeyboardInterrupt)
    i = ev.index(("mill_start",))
    assert _beam_writes(ev[i:]) == []

    class Aborted(_Base):
        def run(self, job):
            self.events.append(("mill_start",))              # abort done: SmartSEM reads idle
            raise KeyboardInterrupt()
    ev, sem, err = _run_batch(Aborted, names=("p1",))
    assert isinstance(err, KeyboardInterrupt) and sem.beam["AP_FIB_BEAM_SHIFT_X"] == 0.0


def test_pending_reset_across_three_sites_with_continue_on_error():
    n = [0]

    class FirstTimesOut(_Base):
        def run(self, job):
            n[0] += 1
            self.events.append(("mill_start", n[0]))
            if n[0] == 1:
                self.sem.fib = "Mill"
                raise SmartSEMError("Timeout waiting for milling to finish")
    ev, sem, err = _run_batch(FirstTimesOut, names=("p1", "p2", "p3"), continue_on_error=True)
    assert err is None
    order = [e for e in ev if e[0] in ("mill_start", "recall", "beamzero", "wait_not_milling")]
    i1 = order.index(("mill_start", 1))
    w = order.index(("wait_not_milling",))
    z = next(k for k in range(w, len(order)) if order[k][0] == "beamzero")
    assert i1 < w < z < order.index(("recall", "p2")) < order.index(("mill_start", 2))
    assert ("mill_start", 3) in order and sem.beam["AP_FIB_BEAM_SHIFT_X"] == 0.0


def test_no_reset_option_and_dry_run_add_no_writes_and_no_waits():
    import smartfib_executor.batch as batch

    class Ok(_Base):
        def run(self, job):
            self.events.append(("mill_start",))
    for kw in ({"reset_beam_shift_between_positions": False}, {"dry_run_focus_refine": True}):
        events = []
        sem = ScopeSem(events, fib="SEM")
        db = PositionDatabase(); db.positions["p1"] = _rec("p1")
        real = (batch.PatternRunner, batch.recall_position, batch._reset_beam_shift_when_idle)
        batch.PatternRunner = lambda s: Ok(sem, events)
        batch.recall_position = lambda s, rec, **k: None

        def never(*a, **k):
            raise AssertionError("the reset helper ran")
        batch._reset_beam_shift_when_idle = never
        old = sys.stdout; sys.stdout = io.StringIO()
        try:
            batch.mill_positions_sequential(
                sem, db=db, position_names=["p1"], ely_local_path=Path("x.ely"),
                move_method="movestage", wait_stage_idle=False, settle_s=0.0,
                load_method="none", wait_strategy="auto", done_confirm_s=0.0,
                tilt_limit_deg=None, **kw)
        finally:
            sys.stdout = old
            batch.PatternRunner, batch.recall_position, batch._reset_beam_shift_when_idle = real
        assert _beam_writes(events) == [], kw


def test_unencodable_character_is_not_a_lost_window():
    from .. import safe_output
    safe_output._reset_for_tests()
    try:
        raw = io.BytesIO()
        cp = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
        s = safe_output.SafeStream(cp, "stdout")
        s.write("ok \u2603 \u00b5m\n")                    # the snowman is not in cp1252
        s.write("still here\n")
        assert not safe_output.output_lost()
        text = raw.getvalue().decode("cp1252")
        assert "still here" in text and "\\u2603" in text and "\u00b5m" in text
    finally:
        safe_output._reset_for_tests()


def test_optional_nan_in_an_older_file_still_loads_as_not_recorded():
    d = tempfile.mkdtemp()
    base = _write_positions(d).read_text(encoding="utf-8")
    data = json.loads(base)
    r = data["positions"]["position_1"]
    r["fov_width_m"] = float("nan"); r["image_width_px"] = float("nan")
    r["fib_working_distance_m"] = float("inf"); r["captured_unix_s"] = float("nan")
    pf = _write_positions(tempfile.mkdtemp(), text=json.dumps(data))
    assert "NaN" in pf.read_text()
    rec = PositionDatabase.load(pf).positions["position_1"]
    assert rec.fov_width_m is None and rec.image_width_px is None
    assert rec.fib_working_distance_m is None and rec.captured_unix_s == 0.0
    db = PositionDatabase.load(pf); db.save(pf)              # and it round-trips
    assert "position_1" in PositionDatabase.load(pf).positions


def test_save_never_drops_a_recorded_tilt_and_keeps_offset_positions_backup():
    import dataclasses
    d = Path(tempfile.mkdtemp())
    pf = _write_positions(d)
    db = PositionDatabase.load(pf)
    r = db.positions["position_1"]
    db.positions["position_1"] = dataclasses.replace(
        r, stage_at=dataclasses.replace(r.stage_at, t_deg=float("nan")))
    try:
        db.save(pf)
    except ValueError as e:
        assert "t_deg" in str(e)
    else:
        raise AssertionError("a NaN tilt was saved as 'no tilt'")
    # the previous version goes to .prev; a .bak made by refine.offset_position is untouched
    (d / "positions.json.bak").write_text("offset_position backup")
    db = PositionDatabase.load(pf); db.positions["position_2"] = _rec("position_2"); db.save(pf)
    assert (d / "positions.json.bak").read_text() == "offset_position backup"
    assert "position_2" not in (d / "positions.json.prev").read_text()


def test_stale_temp_files_are_swept_and_fresh_ones_kept():
    from .. import atomic_io
    d = Path(tempfile.mkdtemp())
    p = d / "positions.json"
    p.write_text("{}")
    old = d / "positions.json.123.deadbeef.tmp"; old.write_text("x")
    new = d / "positions.json.456.cafecafe.tmp"; new.write_text("y")
    other = d / "notes.tmp"; other.write_text("z")
    os.utime(old, (1.0, 1.0))
    assert atomic_io.sweep_stale_tmp(p) == 1
    assert not old.exists() and new.exists() and other.exists()


def test_recall_refuses_non_finite_before_anything_is_set():
    import dataclasses
    from ..positions import recall_position
    for method in ("ap_goto", "movestage"):
        ev = []
        sem = ScopeSem(ev, fib="SEM")
        r = _rec("p1")
        bad = dataclasses.replace(r, stage_at=dataclasses.replace(r.stage_at, y_m=float("inf")))
        try:
            recall_position(sem, bad, move_method=method, set_magnification=True)
        except SmartSEMError as e:
            assert "Nothing was set or moved" in str(e)
        else:
            raise AssertionError("recall accepted an infinite Y")
        assert ev == [], ev


def test_run_log_notes_an_unreset_beam_shift_even_when_the_batch_ends_by_error():
    import smartfib_executor.batch as batch
    from ..report.runlog import RunLog
    events = []
    sem = ScopeSem(events, fib="SEM")
    db = PositionDatabase(); db.positions["p1"] = _rec("p1")
    rl = RunLog(Path(tempfile.mkdtemp()), printer=lambda m: None)
    real = (batch.PatternRunner, batch.recall_position)
    batch.PatternRunner = lambda s: TimeoutRunner(sem, events)
    batch.recall_position = lambda s, rec, **kw: None
    old = sys.stdout; sys.stdout = io.StringIO()
    try:
        batch.mill_positions_sequential(
            sem, db=db, position_names=["p1"], ely_local_path=Path("x.ely"),
            move_method="movestage", wait_stage_idle=False, settle_s=0.0, load_method="none",
            wait_strategy="auto", done_confirm_s=0.0, tilt_limit_deg=None, run_log=rl)
    except SmartSEMError:
        pass
    else:
        raise AssertionError("the timeout was swallowed")
    finally:
        sys.stdout = old
        batch.PatternRunner, batch.recall_position = real
    assert "beam_shift_not_reset" in json.loads(rl.path.read_text()).get("notes", {})
    rl.run_note("beam_shift_not_reset", None)                # a later reset clears it
    assert "beam_shift_not_reset" not in json.loads(rl.path.read_text()).get("notes", {})


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
