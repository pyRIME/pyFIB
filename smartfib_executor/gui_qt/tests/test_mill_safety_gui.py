"""v0.55.1 (code review 2026-11-11), GUI side. Offline (offscreen Qt).

#21  "Mill timeout (min)" on the three milling tabs -> --timeout in seconds (was never
     passed: the CLI's 90 min applied).
#18  GISDEP reads the microscope only while its tab is shown and no pyFIB command runs;
     its panel is greyed out while one runs; a pyFIB command is refused while GISDEP runs
     a step; the tab is not opened (no connection made) while a command runs.
#19  the Positions tab saves positions.json only when a pattern link really changed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .test_smoke import HAVE_QT, _app
from .test_crash_safety import _Patch, _skip
from .test_crash_safety import _win as _cs_win


def _win(session=None):
    _app()                       # the QApplication must exist before any widget
    return _cs_win(session)


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, f):
        self.slots.append(f)

    def emit(self, *a):
        for f in list(self.slots):
            f(*a)


class _FakeWorker:
    """Stands in for SubprocessWorker: never starts a process."""
    made = []

    def __init__(self, argv, **kw):
        self.argv = list(argv)
        self.line, self.failed, self.started_pid, self.finished_rc = _Sig(), _Sig(), _Sig(), _Sig()
        self.running = False
        _FakeWorker.made.append(self)

    def start(self):
        self.running = True

    def isRunning(self):  # noqa: N802
        return self.running

    def finish(self, rc=0):
        self.running = False
        self.finished_rc.emit(rc)


def _session():
    from ...positions import PositionDatabase
    from ...tests.test_workflow import _rec
    d = Path(tempfile.mkdtemp())
    db = PositionDatabase()
    db.positions["position_1"] = _rec("position_1")
    db.save(d / "positions.json")
    return d


def test_mill_timeout_box_is_passed_as_seconds_on_all_three_tabs():
    if not HAVE_QT:
        return _skip("mill timeout")
    from .. import pages
    d = _session()
    win = _win(d)
    try:
        for page in win.mill_pages():
            page.reload()
            page.ely_edit.setText(str(d / "x.ely"))
            assert page.mill_timeout_spin.value() == pages.MILL_TIMEOUT_DEFAULT_MIN == 240
            argv = page.build_argv()
            assert argv is not None, page.title
            assert argv[argv.index("--timeout") + 1] == str(240 * 60)
            page.mill_timeout_spin.setValue(1)                      # floor
            assert page.mill_timeout_spin.value() == pages.MILL_TIMEOUT_MIN_MIN
            page.mill_timeout_spin.setValue(300)
            argv = page.build_argv()
            assert argv[argv.index("--timeout") + 1] == "18000"
    finally:
        win.close()


def test_gisdep_polls_only_when_shown_and_no_command_runs():
    if not HAVE_QT:
        return _skip("gisdep polling")
    from .. import app as app_mod
    win = _win(_session())
    _FakeWorker.made.clear()
    try:
        with _Patch((app_mod, "SubprocessWorker", _FakeWorker)):
            win.show_page("gisdep"); _app().processEvents()
            gp = win.gisdep_page
            assert gp._gwin is not None and gp.polling()            # shown: live state on
            win.show_page("capture")
            assert not gp.polling()                                 # hidden: nothing is read
            win.show_page("gisdep")
            assert gp.polling() and gp._gwin.isEnabled()
            # a pyFIB command starts: no reads, panel greyed out -- even on its own tab
            assert win.run_argv(["py", "-u", "-m", "x", "site", "goto"], show_log=False)
            assert not gp.polling() and not gp._gwin.isEnabled()
            win.show_page("gisdep")
            assert not gp.polling() and not gp._gwin.isEnabled()
            _FakeWorker.made[-1].finish(0)                           # the command ends
            assert gp.polling() and gp._gwin.isEnabled()
            # ... and when it ends on another tab, GISDEP stays quiet until it is shown
            win.show_page("capture")
            assert win.run_argv(["py", "-u", "-m", "x", "site", "goto"], show_log=False)
            _FakeWorker.made[-1].finish(0)
            assert not gp.polling() and gp._gwin.isEnabled()
    finally:
        win.close()


def test_pyfib_command_is_refused_while_gisdep_runs_a_step():
    if not HAVE_QT:
        return _skip("gisdep busy")
    from .. import app as app_mod
    win = _win(_session())
    _FakeWorker.made.clear()
    try:
        with _Patch((app_mod, "SubprocessWorker", _FakeWorker),
                    (win, "_gisdep_busy", lambda: True)):
            assert win.run_argv(["py", "-u", "-m", "x", "site", "goto"], show_log=False) is False
            assert _FakeWorker.made == [] and not win._cmd_active
            assert "GISDEP is running a step" in win.log_page.view.toPlainText()
    finally:
        win._gisdep_busy = lambda: False
        win.close()


def test_gisdep_tab_is_not_opened_while_a_command_runs():
    if not HAVE_QT:
        return _skip("gisdep deferred")
    from .. import app as app_mod
    win = _win(_session())
    _FakeWorker.made.clear()
    try:
        with _Patch((app_mod, "SubprocessWorker", _FakeWorker)):
            assert win.run_argv(["py", "-u", "-m", "x", "mill-positions"], show_log=False)
            win.show_page("gisdep"); _app().processEvents()
            gp = win.gisdep_page
            assert gp._gwin is None                                  # no connection attempt
            assert "opens when" in gp._status.text()
            # the command's process has only just exited: GISDEP does not connect back to
            # back with it (review), it opens a moment later
            _FakeWorker.made[-1].finish(0)
            _app().processEvents()
            assert gp._gwin is None and "in a moment" in gp._status.text()
            win.GISDEP_OPEN_DELAY_S = 0.0
            win._open_gisdep_when_safe()
            assert gp._gwin is not None and gp.polling()             # built now
    finally:
        win.close()


def test_late_exit_code_after_close_does_not_wake_gisdep():
    if not HAVE_QT:
        return _skip("late exit code")
    from .. import app as app_mod
    win = _win(_session())
    _FakeWorker.made.clear()
    with _Patch((app_mod, "SubprocessWorker", _FakeWorker)):
        win.show_page("gisdep"); _app().processEvents()
        gp = win.gisdep_page
        assert gp.polling()
        win.show_page("capture")
        assert win.run_argv(["py", "-u", "-m", "x", "site", "goto"], show_log=False)
        w = _FakeWorker.made[-1]
        w.running = False                    # as after "Kill it now": the thread has ended
        win.show_page("gisdep")
        win.close()                          # ... pyFIB closes, GISDEP is shut down
        assert not gp.polling()
        w.finished_rc.emit(1)                # the exit code arrives late
        assert not gp.polling()              # GISDEP stays shut


def test_gisdep_is_built_once_however_often_the_tab_is_looked_at():
    if not HAVE_QT:
        return _skip("single build")
    from .. import app as app_mod
    win = _win(_session())
    _FakeWorker.made.clear()
    built = []
    try:
        with _Patch((app_mod, "SubprocessWorker", _FakeWorker)):
            assert win.run_argv(["py", "-u", "-m", "x", "site", "goto"], show_log=False)
            _FakeWorker.made[-1].finish(0)
            gp = win.gisdep_page
            real = gp.ensure_built

            def counting():
                built.append(1)
                win._open_gisdep_when_safe()          # re-entered while building: ignored
                return real()
            gp.ensure_built = counting
            for _ in range(5):                        # looked at five times during the wait
                win.show_page("capture"); win.show_page("gisdep")
            assert built == [] and win._gisdep_open_timer.isActive()
            win.GISDEP_OPEN_DELAY_S = 0.0
            win._open_gisdep_when_safe()
            win._open_gisdep_when_safe()
            assert built == [1] and gp._gwin is not None
    finally:
        win.close()


def test_failed_launch_does_not_leave_gisdep_greyed_out():
    if not HAVE_QT:
        return _skip("failed launch")
    from .. import app as app_mod

    class Boom(_FakeWorker):
        def start(self):
            raise RuntimeError("could not start a thread")
    win = _win(_session())
    try:
        win.show_page("gisdep"); _app().processEvents()
        gp = win.gisdep_page
        with _Patch((app_mod, "SubprocessWorker", Boom)):
            try:
                win.run_argv(["py", "-u", "-m", "x", "site", "goto"], show_log=False)
            except RuntimeError:
                pass
            else:
                raise AssertionError("the launch error was swallowed")
        assert not win._cmd_active and gp._gwin.isEnabled() and gp.polling()
    finally:
        win.close()


def test_files_only_tools_do_not_pause_or_wait_for_gisdep():
    if not HAVE_QT:
        return _skip("files-only tools")
    from .. import app as app_mod
    win = _win(_session())
    _FakeWorker.made.clear()
    try:
        with _Patch((app_mod, "SubprocessWorker", _FakeWorker)):
            win.show_page("gisdep"); _app().processEvents()
            gp = win.gisdep_page
            # the standalone pyMap window and the report are started with instrument=False
            win.correlation_page.register._open_separately()
            assert _FakeWorker.made[-1].argv[-1] == "pymap.gui" or "pymap.gui" in _FakeWorker.made[-1].argv
            assert not win._cmd_active and gp.polling() and gp._gwin.isEnabled()
            _FakeWorker.made[-1].finish(0)
            with _Patch((win, "_gisdep_busy", lambda: True)):     # GISDEP runs a step
                assert win.run_argv(["py", "-u", "-m", "x.report"], show_log=False,
                                    instrument=False) is True
            _FakeWorker.made[-1].finish(0)
    finally:
        win._gisdep_busy = lambda: False
        win.close()


def test_gisdep_connection_is_released_when_pyfib_closes():
    if not HAVE_QT:
        return _skip("gisdep shutdown")
    win = _win(_session())
    win.show_page("gisdep"); _app().processEvents()
    gp = win.gisdep_page
    closed = []

    class Api:
        def close(self):
            closed.append(1)
    gp._gwin.api = Api()
    win.close()
    assert closed == [1] and not gp.polling()


def test_positions_tab_saves_only_when_a_link_changed():
    if not HAVE_QT:
        return _skip("link save")
    import dataclasses
    from ... import ely_link
    from ...positions import PositionDatabase
    d = _session()
    win = _win(d)
    saves = []
    real_save = PositionDatabase.save

    def counting_save(self, path):
        saves.append(str(path))
        return real_save(self, path)

    def link_same(db, pf):                                           # already linked: no change
        return {"linked": ["position_1"], "stale": [], "unmatched": []}

    def link_new(db, pf):
        db.positions["position_1"] = dataclasses.replace(
            db.positions["position_1"], ely_pattern="patterns/position_1.ely")
        return {"linked": ["position_1"], "stale": [], "unmatched": []}
    try:
        with _Patch((PositionDatabase, "save", counting_save),
                    (ely_link, "link_patterns_to_positions", link_same)):
            win.show_page("positions"); win.show_page("capture"); win.show_page("positions")
            assert saves == [], saves                                # looking never writes
        with _Patch((PositionDatabase, "save", counting_save),
                    (ely_link, "link_patterns_to_positions", link_new)):
            win.positions_page._on_refresh_patterns()
            assert len(saves) == 1                                   # a new link is saved
            win.show_page("capture"); win.show_page("positions")
            assert len(saves) == 1                                   # ... once
        assert PositionDatabase.load(d / "positions.json").positions["position_1"].ely_pattern \
            == "patterns/position_1.ely"
    finally:
        win.close()


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
