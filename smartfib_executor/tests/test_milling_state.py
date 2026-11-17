"""Milling check (v0.53.5, bug #15) and the 3 s manual-button wait.

Evan, 2026-10-13: Record after image refused "SmartSEM indicates milling" right after a
SmartFIB snapshot, with nothing milling. debug-fib-imaging on the instrument (number and
text agreed every time):
  milling      DP_FIB_MODE=2'Milling'  DP_FIB_IMAGING=4'Mill + SEM'  DP_PATTERNING_MODE=3'FIB + SEM'
  finished     DP_FIB_MODE=0'Imaging'  DP_FIB_IMAGING=0'SEM'         DP_PATTERNING_MODE=0'Off'
  snapshot     DP_FIB_MODE=0'Imaging'  DP_FIB_IMAGING=1'FIB'         DP_PATTERNING_MODE=2'FIB'
The old check refused on any patterning mode other than 0, so the snapshot state blocked
every manual button.

Review 2026-10-14: pyFIB's own runner writes DP_PATTERNING_MODE = 'FIB' right after it
starts a mill, so 'FIB' is relaxed ONLY for the operator's manual buttons
(snapshot_ok=True); the batch interlock and after images keep "anything but Off = milling".
"""

from __future__ import annotations

from .. import batch as B
from .. import site_ops as SO

MILLING = {"DP_FIB_MODE": ("Milling", 2.0), "DP_FIB_IMAGING": ("Mill + SEM", 4.0),
           "DP_PATTERNING_MODE": ("FIB + SEM", 3.0)}
FINISHED = {"DP_FIB_MODE": ("Imaging", 0.0), "DP_FIB_IMAGING": ("SEM", 0.0),
            "DP_PATTERNING_MODE": ("Off", 0.0)}
SNAPSHOT = {"DP_FIB_MODE": ("Imaging", 0.0), "DP_FIB_IMAGING": ("FIB", 1.0),
            "DP_PATTERNING_MODE": ("FIB", 2.0)}


class _Scope:
    """Reads (text, number) per flag; a missing entry raises like a failed COM read.
    A None in either slot makes only that read fail."""
    def __init__(self, state, **override):
        self.state = dict(state)
        self.state.update(override)
        self.calls = []

    def get_str(self, m):
        v = self.state.get(m)
        if v is None or v[0] is None:
            raise RuntimeError(f"cannot read {m}")
        return v[0]

    def get_float(self, m):
        v = self.state.get(m)
        if v is None or v[1] is None:
            raise RuntimeError(f"cannot read {m}")
        return v[1]

    def set_value(self, m, v):
        self.calls.append(("set", m, v))

    def execute(self, c, *a):
        self.calls.append(("execute", c))


def test_the_three_measured_states():
    for ok in (False, True):
        active, why = B.milling_state(_Scope(MILLING), snapshot_ok=ok)
        assert active and why == "DP_FIB_MODE = 2 'Milling'"
        active, why = B.milling_state(_Scope(FINISHED), snapshot_ok=ok)
        assert not active and why.startswith("not milling")
    # bug #15: after a SmartFIB snapshot the manual buttons proceed ...
    active, why = B.milling_state(_Scope(SNAPSHOT), snapshot_ok=True)
    assert not active and "DP_PATTERNING_MODE = 2 'FIB'" in why, why
    # ... but the batch interlock stays strict (pyFIB itself writes 'FIB' during its mills)
    active, why = B.milling_state(_Scope(SNAPSHOT))
    assert active and why == "DP_PATTERNING_MODE = 2 'FIB'"
    assert B._is_milling_active(_Scope(SNAPSHOT)) is True
    assert B._is_milling_active(_Scope(FINISHED)) is False
    assert B._is_milling_active(_Scope(MILLING)) is True


def test_each_milling_flag_on_its_own_still_refuses():
    a, why = B.milling_state(_Scope(FINISHED, DP_FIB_MODE=("To Milling", 5.0)))
    assert a and "To Milling" in why
    a, why = B.milling_state(_Scope(FINISHED, DP_FIB_IMAGING=("Milling", 2.0)))
    assert a and why.startswith("DP_FIB_IMAGING")
    a, why = B.milling_state(_Scope(FINISHED, DP_PATTERNING_MODE=("FIB + SEM", 3.0)))
    assert a and why == "DP_PATTERNING_MODE = 3 'FIB + SEM'"


def test_text_or_number_saying_milling_is_enough():
    # a number that disagrees with the text is never trusted to mean "safe"
    a, why = B.milling_state(_Scope(FINISHED, DP_FIB_MODE=("Imaging", 2.0)))
    assert a and "DP_FIB_MODE" in why
    a, _ = B.milling_state(_Scope(FINISHED, DP_FIB_IMAGING=("SEM", 4.0)))
    assert a
    a, _ = B.milling_state(_Scope(FINISHED, DP_PATTERNING_MODE=("Off", 3.0)))
    assert a
    a, _ = B.milling_state(_Scope(FINISHED, DP_PATTERNING_MODE=("FIB + SEM", 0.0)))
    assert a


def test_values_never_seen_and_unreadable_flags_count_as_milling():
    a, why = B.milling_state(_Scope(FINISHED, DP_PATTERNING_MODE=("SEM", 1.0)))
    assert a and "never seen before" in why
    a, why = B.milling_state(_Scope(FINISHED, DP_PATTERNING_MODE=(None, 1.0)))
    assert a and "never seen before" in why
    # a label the measured map doesn't know: the number decides (0 = Off) ...
    a, why = B.milling_state(_Scope(FINISHED, DP_PATTERNING_MODE=("Imaging", 0.0)))
    assert not a, why
    # ... and with no number either, it can't be read: milling
    a, why = B.milling_state(_Scope(FINISHED, DP_PATTERNING_MODE=("Imaging", None)))
    assert a and "not a known value" in why
    # odd labels never raise and never read as a code (review)
    for odd in ("-2", "\u00b2", "+2"):
        a, why = B.milling_state(_Scope(FINISHED, DP_PATTERNING_MODE=(odd, 0.0)))
        assert not a, (odd, why)
    # DP_FIB_MODE needs its NUMBER, as before v0.53.5: text alone is not enough (review)
    a, why = B.milling_state(_Scope(FINISHED, DP_FIB_MODE=("Imaging", None)), snapshot_ok=True)
    assert a and why.startswith("DP_FIB_MODE unreadable as a number"), why
    for flag in ("DP_FIB_MODE", "DP_FIB_IMAGING", "DP_PATTERNING_MODE"):
        st = dict(FINISHED)
        del st[flag]
        a, why = B.milling_state(_Scope(st))
        assert a and why.startswith(flag) and "unreadable" in why, (flag, why)
    # text alone, or the number alone, is enough to read a flag
    a, _ = B.milling_state(_Scope(SNAPSHOT, DP_PATTERNING_MODE=("FIB", None)), snapshot_ok=True)
    assert not a
    a, _ = B.milling_state(_Scope(SNAPSHOT, DP_PATTERNING_MODE=(None, 2.0)), snapshot_ok=True)
    assert not a
    a, _ = B.milling_state(_Scope(FINISHED, DP_FIB_MODE=(None, 0.0)))
    assert not a


def test_refusal_names_the_flag_and_nothing_is_sent():
    sem = _Scope(FINISHED, DP_PATTERNING_MODE=("FIB + SEM", 3.0))
    try:
        SO._milling_guard(sem, "Record after image for position_17", 3.0, log=lambda m: None,
                          clock=lambda: 0.0, sleep=lambda dt: None)
        raise AssertionError("not refused")
    except SO.SiteRefused as e:
        assert str(e) == ("Record after image for position_17 refused: SmartSEM indicates "
                          "milling (DP_PATTERNING_MODE = 3 'FIB + SEM'). Nothing was changed.")
    assert sem.calls == []


def test_after_a_snapshot_the_guard_passes_and_is_read_only():
    sem, t, logs = _Scope(SNAPSHOT), [0.0], []
    SO._milling_guard(sem, "Go to position_18", SO.MANUAL_GUARD_S, log=logs.append,
                      clock=lambda: t[0], sleep=lambda dt: t.__setitem__(0, t[0] + dt))
    assert t[0] >= 3.0 and sem.calls == []
    assert logs == ["[site] checking that SmartSEM is not milling (3 s) ..."]


def test_every_manual_button_waits_3_s():
    """Evan, 2026-10-13: Go to, Align, Push pattern and Record after image all watch for
    3 s (were 10 s / 5 s). CLEM overview and Mark targets keep 5 s."""
    assert SO.MANUAL_GUARD_S == 3.0 and SO.GUARD_S == 5.0
    from .test_mill_stages import _session
    from ..milling_workflow import RefineConfig
    from ..positions import PositionDatabase
    d, pf = _session()
    db = PositionDatabase.load(pf)
    rec = db.get("position_1")
    seen = {}

    class _Stop(Exception):
        pass

    def fake_guard(sem, what, window_s, **k):
        seen[what.split(" for ")[0].split(" position")[0]] = window_s
        raise _Stop()

    real = SO._milling_guard
    SO._milling_guard = fake_guard
    try:
        for fn in (lambda: SO.site_goto(None, rec),
                   lambda: SO.site_align(None, rec, "rough", d, RefineConfig(enable_stage=True)),
                   lambda: SO.site_align(None, rec, "rough", d, RefineConfig(enable_stage=False)),
                   lambda: SO.site_push(None, rec, "rough", d / "rough.ely", d, drop_dir=d,
                                        drop_name="A.ely"),
                   lambda: SO.site_after_image(None, db, rec, "rough", pf)):
            try:
                fn()
            except _Stop:
                pass
    finally:
        SO._milling_guard = real
    assert seen == {"Go to": 3.0, "Align": 3.0, "Push pattern": 3.0,
                    "Record after image": 3.0}, seen


def test_record_after_image_after_a_snapshot_is_not_skipped():
    """The exact case Evan hit: Record after image right after a SmartFIB snapshot. Both
    its guard and its own pre-frame check must use the manual-button rule."""
    from .test_mill_stages import _session
    from ..positions import PositionDatabase
    d, pf = _session()
    db = PositionDatabase.load(pf)
    rec = db.get("position_1")
    seen = []

    def fake_grab(sem, rec, images_dir, *, milling_active, **k):
        seen.append(milling_active())
        return None, "test: no frame"

    SO.site_after_image(_Scope(SNAPSHOT), db, rec, "prepolish", pf, log=lambda m: None,
                        run_log=False, _grab_fn=fake_grab, guard_s=0.0)
    assert seen == [False]
    seen.clear()
    try:                                                     # really milling: refused, no frame
        SO.site_after_image(_Scope(SNAPSHOT, DP_PATTERNING_MODE=("FIB + SEM", 3.0)), db, rec,
                            "prepolish", pf, log=lambda m: None, run_log=False,
                            _grab_fn=fake_grab, guard_s=0.0)
        raise AssertionError("not refused while milling")
    except SO.SiteRefused as e:
        assert "FIB + SEM" in str(e) and seen == []


def test_runner_reports_the_measured_patterning_codes():
    from ..runner import PatternRunner
    r = PatternRunner.__new__(PatternRunner)
    for text, code in (("Off", 0), ("FIB", 2), ("FIB + SEM", 3)):
        r.sem = _Scope(FINISHED, DP_PATTERNING_MODE=(text, 99.0))
        assert r._get_patterning_mode_code() == code, text
    r.sem = _Scope(FINISHED, DP_PATTERNING_MODE=("SEM", None))
    assert r._get_patterning_mode_code() != 0          # unknown label: never "done"


def test_milling_check_cli_is_read_only():
    import argparse
    import io
    from contextlib import redirect_stdout
    from .. import cli
    scopes = []

    class _Fake(_Scope):
        def __init__(self):
            super().__init__(SNAPSHOT)
            scopes.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def connect_remoting(self):
            pass

    real = cli.SmartSEM
    cli.SmartSEM = _Fake
    try:
        with redirect_stdout(io.StringIO()) as out:
            rc = cli.cmd_milling_check(argparse.Namespace(connect="remoting", machine=""))
    finally:
        cli.SmartSEM = real
    text = out.getvalue()
    assert rc == 0 and "NOT MILLING: the manual buttons would proceed" in text, text
    assert "MILLING: a batch would wait before its next stage move" in text, text
    assert scopes and scopes[0].calls == []                  # nothing sent


def _run_all() -> int:
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
