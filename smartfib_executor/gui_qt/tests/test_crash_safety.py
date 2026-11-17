"""v0.55.0: no silent crashes (Evan, 2026-11-05, with pyMap v0.9.2).

pyMap's review (revision 3) asked pyFIB to (1) follow a session change in pyMap instead
of rebuilding it, (2) never leave the targets table's _filling flag stuck, (3) close
detached tool windows when pyFIB closes. On top, pyFIB's own crash paths: closing with a
command or GISDEP step running (a destroyed running QThread aborts the process), thread
errors touching widgets off the GUI thread, a hook that fails without a console, Qt fatal
messages, unraisable errors, child commands that crash natively, the output reader, and
a run marker that reports a run which ended without closing. Offline (offscreen Qt).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .test_smoke import HAVE_QT, _app

ROOT = Path(__file__).resolve().parents[3]


def _skip(name):
    print(f"SKIP {name} (Qt not installed)")


def _win(session=None):
    from ..app import MainWindow
    win = MainWindow()
    if session is not None:
        win.arm_bar.pos_edit.setText(str(Path(session) / "positions.json"))
    win.resize(1400, 850)
    win.show()
    _app().processEvents()
    return win


class _Patch:
    """Set attributes for the duration of a with-block."""

    def __init__(self, *triples):
        self.triples = triples
        self.saved = []

    def __enter__(self):
        for obj, name, val in self.triples:
            self.saved.append((obj, name, obj.__dict__.get(name, _MISSING) if isinstance(obj, type)
                               else getattr(obj, name, _MISSING)))
            setattr(obj, name, val)
        return self

    def __exit__(self, *exc):
        for obj, name, val in reversed(self.saved):
            if val is _MISSING:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
            else:
                setattr(obj, name, val)
        return False


_MISSING = object()


# ----------------------------------------------------------------- pyMap requests 1-3
def test_pymap_logs_stay_out_of_pyfibs_logs_folder_in_tests():
    d = os.environ.get("PYMAP_LOG_DIR")
    assert d and Path(d).resolve() != (ROOT / "logs").resolve()


def test_a_pymap_without_set_session_is_rebuilt_only_after_asking():
    if not HAVE_QT:
        return _skip("test_a_pymap_without_set_session_is_rebuilt_only_after_asking")
    from qtpy.QtWidgets import QLabel
    from .. import correlation_page as CP
    app = _app()
    win = _win()
    pane = win.correlation_page.register

    class _Old(QLabel):                 # an old pyMap: no set_session, 2 landmark pairs
        class c:                        # noqa: N801
            slots = [1, 2]
    old = _Old("old pyMap")
    pane._widget = old
    pane._built_for = "/sessions/A"
    pane._lay.addWidget(old)
    pane.host.set_widget(old)
    asked = []
    win.confirm = lambda m: asked.append(m) or False
    pane.ensure_built("/sessions/B")
    assert len(asked) == 1 and "2 landmark pair" in asked[0]
    assert pane._widget is old and pane._built_for == "/sessions/A"    # kept
    pane.ensure_built("/sessions/B")                                    # asked once only
    assert len(asked) == 1
    assert "restart declined" in win.log_page.view.toPlainText()
    win.confirm = lambda m: asked.append(m) or True
    with _Patch((CP, "find_pymap", lambda: (None, False))):
        pane.ensure_built("/sessions/C")
    app.processEvents()
    assert len(asked) == 2 and pane._widget is None and pane._built_for is None
    assert "rebuilding for the new session" in win.log_page.view.toPlainText()
    win.close()


def test_the_targets_table_never_jams_after_an_error():
    if not HAVE_QT:
        return _skip("test_the_targets_table_never_jams_after_an_error")
    from ...clem import targets as TG
    from ...tests.test_clem import _session
    app = _app()
    d, clem, tp = _session()
    win = _win(d)
    page = win.correlation_page
    page.refresh_targets()
    assert page.plan is not None and page.table.rowCount() == 2 and not page._filling
    real = TG.plan_targets

    def broken_plan(*a, **k):
        plan = real(*a, **k)
        plan.rows[1].stage_y_m = None            # formatting this row raises TypeError
        return plan
    raised = False
    with _Patch((TG, "plan_targets", broken_plan)):
        try:
            page.refresh_targets()
        except TypeError:
            raised = True
    assert raised and page._filling is False     # was stuck True before v0.55.0
    page.refresh_targets()
    mk = page.table.item(0, page.T_MARK)
    from qtpy.QtCore import Qt
    mk.setCheckState(Qt.Unchecked)               # ticks still reach the selection
    app.processEvents()
    assert page._mark_sel.get(page.table.item(0, page.T_ID).text()) is False

    def unreadable(*a, **k):
        raise ValueError("Expecting value: line 1 column 1 <truncated>")
    with _Patch((TG, "plan_targets", unreadable)):
        page.refresh_targets()                   # shown in the tab, not raised
    assert page.plan is None and "could not be read" in page.file_label.text()
    assert "&lt;truncated&gt;" in page.file_label.text()
    win.close()


def test_a_failing_targets_poll_goes_to_the_log_once_per_distinct_error():
    if not HAVE_QT:
        return _skip("test_a_failing_targets_poll_goes_to_the_log_once_per_distinct_error")
    win = _win()
    page = win.correlation_page
    errs = ["file in use", "file in use", "file in use", "share lost"]

    def sig():
        raise OSError(errs.pop(0))
    page._signature = sig
    for _ in range(4):
        page._poll()                             # must not raise
    log = win.log_page.view.toPlainText()
    assert log.count("targets refresh failed: OSError: file in use") == 1
    assert log.count("targets refresh failed: OSError: share lost") == 1
    win.close()


def test_closing_pyfib_docks_a_detached_tool_window_back():
    if not HAVE_QT:
        return _skip("test_closing_pyfib_docks_a_detached_tool_window_back")
    from qtpy.QtWidgets import QLabel
    app = _app()
    win = _win()
    host = win.correlation_page.register.host
    w = QLabel("tool")
    host._slot.addWidget(w)
    host.set_widget(w)
    host.detach()
    app.processEvents()
    tool_window = host.window
    assert host.detached and tool_window.isVisible()
    assert win.close()
    app.processEvents()
    assert not host.detached
    try:
        assert not tool_window.isVisible()       # hidden (and scheduled for deletion)
    except RuntimeError:
        pass                                     # already deleted: also closed


# ----------------------------------------------------------------- closing with work running
class _FakeWorker:
    def __init__(self, stops=True):
        self.stopped = False
        self._stops = stops
        self.done = False

    def isRunning(self):
        return not self.done and not (self.stopped and self._stops)

    def request_stop(self):
        self.stopped = True

    def wait(self, ms=None):
        return self._stops


def _choose(label, side_effect=None):
    def fake_exec(self):
        self._chosen = next(b for b in self.buttons() if b.text().startswith(label))
        if side_effect is not None:
            side_effect()
        return 0
    return fake_exec


def _clicked(self):
    return self._chosen


def _yes(*a, **k):
    from qtpy.QtWidgets import QMessageBox
    return QMessageBox.Yes


def _no(*a, **k):
    from qtpy.QtWidgets import QMessageBox
    return QMessageBox.No


def test_closing_with_a_command_running_defaults_to_keeping_pyfib_open():
    if not HAVE_QT:
        return _skip("test_closing_with_a_command_running_defaults_to_keeping_pyfib_open")
    from qtpy.QtWidgets import QMessageBox
    win = _win()
    fw = _FakeWorker()
    win._worker = fw
    with _Patch((QMessageBox, "exec_", _choose("Keep")), (QMessageBox, "clickedButton", _clicked)):
        assert win.close() is False
    assert win.isVisible() and not fw.stopped and not win._closing
    # "Kill it now..." answered No at the second question: nothing happens either
    with _Patch((QMessageBox, "exec_", _choose("Kill")), (QMessageBox, "clickedButton", _clicked),
                (QMessageBox, "question", staticmethod(_no))):
        assert win.close() is False
    assert not fw.stopped and win.isVisible()
    fw.done = True
    win.close()


def test_close_when_it_finishes_blocks_new_commands_then_closes():
    if not HAVE_QT:
        return _skip("test_close_when_it_finishes_blocks_new_commands_then_closes")
    from qtpy.QtWidgets import QMessageBox
    app = _app()
    win = _win()
    fw = _FakeWorker()
    win._worker = fw
    win._command_text = "smartfib_executor.cli mill-positions rough.ely --arm"
    with _Patch((QMessageBox, "exec_", _choose("Close when")),
                (QMessageBox, "clickedButton", _clicked)):
        assert win.close() is False                    # waits for the command
    assert win.isVisible() and not fw.stopped and win._close_when_done
    assert "will close when this command finishes" in win.log_page.view.toPlainText()
    fw.done = True                                      # (as if it ended; nothing started)
    assert win.run_argv([sys.executable, "-c", "print(1)"]) is False   # nothing new starts
    assert "not started (pyFIB is closing)" in win.log_page.view.toPlainText()
    win._close_when_idle()
    app.processEvents()
    assert not win.isVisible()


def test_a_command_started_while_the_close_box_is_up_keeps_pyfib_open():
    """Review finding 1: the box runs an event loop; if a run ends and another starts
    in it, the new one must never be left running behind a closed pyFIB."""
    if not HAVE_QT:
        return _skip("test_a_command_started_while_the_close_box_is_up_keeps_pyfib_open")
    from qtpy.QtWidgets import QMessageBox
    win = _win()
    old, new = _FakeWorker(), _FakeWorker()
    win._worker = old

    def swap():
        old.done = True
        win._worker = new                              # e.g. the tilt-skip rerun
    with _Patch((QMessageBox, "exec_", _choose("Kill", swap)),
                (QMessageBox, "clickedButton", _clicked),
                (QMessageBox, "question", staticmethod(_yes))):
        assert win.close() is False
    assert win.isVisible() and not new.stopped and not old.stopped
    new.done = True
    win.close()


def test_kill_then_close_ends_a_real_running_child():
    if not HAVE_QT:
        return _skip("test_kill_then_close_ends_a_real_running_child")
    from qtpy.QtWidgets import QMessageBox
    app = _app()
    win = _win()
    code = "import time\nwhile True:\n    print('working', flush=True)\n    time.sleep(0.05)"
    assert win.run_argv([sys.executable, "-c", code], show_log=False)
    w = win._worker
    for _ in range(100):
        app.processEvents()
        if w._proc is not None:
            break
        time.sleep(0.02)
    proc = w._proc
    with _Patch((QMessageBox, "exec_", _choose("Kill")), (QMessageBox, "clickedButton", _clicked),
                (QMessageBox, "question", staticmethod(_yes))):
        assert win.close() is True
    assert proc.poll() is not None and not w.isRunning()
    assert "operator killed the running command" in win.log_page.view.toPlainText()
    for _ in range(20):
        app.processEvents()               # the exit arrives: logged as a kill, no crash box
    assert "killed by the operator" in win.log_page.view.toPlainText()


def test_closing_stays_open_when_the_command_will_not_end():
    if not HAVE_QT:
        return _skip("test_closing_stays_open_when_the_command_will_not_end")
    from qtpy.QtWidgets import QMessageBox
    win = _win()
    win._worker = _FakeWorker(stops=False)
    warned = []
    with _Patch((QMessageBox, "exec_", _choose("Kill")), (QMessageBox, "clickedButton", _clicked),
                (QMessageBox, "question", staticmethod(_yes)),
                (QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2]) or 0))):
        assert win.close() is False
    assert warned and "did not end" in warned[0] and win.isVisible() and not win._closing
    win._worker = None
    win.close()


def test_closing_while_gisdep_runs_a_step_is_refused():
    if not HAVE_QT:
        return _skip("test_closing_while_gisdep_runs_a_step_is_refused")
    from qtpy.QtWidgets import QMessageBox
    win = _win()

    class _G:
        _worker = _FakeWorker()
    saved = win.gisdep_page._gwin
    win.gisdep_page._gwin = _G()
    warned = []
    with _Patch((QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2]) or 0))):
        assert win.close() is False
    assert warned and "STOP in the GISDEP tab" in warned[0] and not win._closing
    win.gisdep_page._gwin = saved
    win.close()


# ----------------------------------------------------------------- child commands
def test_crash_exit_codes_are_named():
    from ..errors import describe_crash_exit
    assert describe_crash_exit(0) is None and describe_crash_exit(1) is None
    assert describe_crash_exit(3) is None and describe_crash_exit(4) is None   # pyFIB refusals
    assert describe_crash_exit(3221225477) == "0xC0000005 ACCESS VIOLATION"
    assert describe_crash_exit(-1073741819) == "0xC0000005 ACCESS VIOLATION"
    assert "STACK BUFFER OVERRUN" in describe_crash_exit(0xC0000409)
    assert describe_crash_exit(-11) == "killed by SIGSEGV"
    assert "BREAKPOINT" in describe_crash_exit(0x80000003)
    from ..errors import describe_interrupt_exit
    assert describe_crash_exit(0xC000013A) is None and describe_crash_exit(-2) is None
    assert "Ctrl+C" in describe_interrupt_exit(0xC000013A)


def test_a_command_that_crashes_natively_is_shown_as_a_crash():
    if not HAVE_QT:
        return _skip("test_a_command_that_crashes_natively_is_shown_as_a_crash")
    from qtpy.QtWidgets import QMessageBox
    app = _app()
    win = _win()
    boxes = []
    with _Patch((QMessageBox, "critical", staticmethod(lambda *a, **k: boxes.append(a[2]) or 0))):
        assert win.run_argv([sys.executable, "-c", "import ctypes; ctypes.string_at(0)"],
                            show_log=False)
        assert win._worker.wait(30000)
        for _ in range(50):
            app.processEvents()
            if boxes:
                break
            time.sleep(0.02)
        assert boxes and "CRASHED" in boxes[0], boxes
        win._on_rc(3)                                    # an ordinary refusal: no box
    assert len(boxes) == 1
    log = win.log_page.view.toPlainText()
    assert "COMMAND CRASHED" in log and "[exit 3]" in log
    win.close()


def test_the_output_reader_survives_utf8_and_undecodable_bytes():
    if not HAVE_QT:
        return _skip("test_the_output_reader_survives_utf8_and_undecodable_bytes")
    from ..worker import SubprocessWorker
    app = _app()
    code = ("import sys; print('stage 12.5 µm → done ✕'); sys.stdout.flush(); "
            "sys.stdout.buffer.write(b'raw \\x81\\x8d\\xff bytes\\n'); sys.stdout.flush(); "
            "print('after'); sys.exit(0)")
    w = SubprocessWorker([sys.executable, "-c", code])
    lines, rcs, fails = [], [], []
    w.line.connect(lines.append)
    w.finished_rc.connect(rcs.append)
    w.failed.connect(fails.append)
    w.start()
    assert w.wait(30000)
    for _ in range(20):
        app.processEvents()
    assert any("12.5 µm → done ✕" in s for s in lines), lines
    assert any(s.startswith("raw ") and "bytes" in s for s in lines) and "after" in lines
    assert rcs == [0] and not fails


# ----------------------------------------------------------------- the hooks
def _hooks_saved():
    import faulthandler  # noqa: F401
    return (sys.excepthook, threading.excepthook, sys.unraisablehook, sys.stderr)


def _hooks_restore(saved):
    import faulthandler
    from .. import errors
    sys.excepthook, threading.excepthook, sys.unraisablehook, sys.stderr = saved
    faulthandler.disable()
    f = errors._state["fault_file"]
    if f is not None:
        f.close()
    errors._state.update(fault_file=None, installed=False)


def test_an_error_in_another_thread_reaches_the_log_on_the_gui_thread():
    if not HAVE_QT:
        return _skip("test_an_error_in_another_thread_reaches_the_log_on_the_gui_thread")
    from .. import errors
    app = _app()
    seen = []

    class _Win:
        class log_page:                       # noqa: N801
            @staticmethod
            def append(text):
                seen.append((threading.current_thread() is threading.main_thread(), text))

        @staticmethod
        def status(msg):
            pass
    saved = _hooks_saved()
    saved_relay = errors._state["relay"]
    logs = Path(tempfile.mkdtemp())
    try:
        errors._state["relay"] = None
        errors.install(lambda: _Win, logs)

        def work():
            raise RuntimeError("pyMap worker: boom")
        t = threading.Thread(target=work, name="tworker")
        t.start()
        t.join()
        assert not seen                        # nothing touched widgets from that thread
        for _ in range(20):
            app.processEvents()
        assert seen and seen[0][0] is True and "pyMap worker: boom" in seen[0][1]
        assert "thread tworker" in (logs / errors.ERRORS_LOG).read_text()
    finally:
        _hooks_restore(saved)
        errors._state["relay"] = saved_relay


def test_the_hook_works_without_a_console_and_logs_unraisable_errors():
    if not HAVE_QT:
        return _skip("test_the_hook_works_without_a_console_and_logs_unraisable_errors")
    import gc
    from qtpy.QtWidgets import QMessageBox
    from .. import errors
    saved = _hooks_saved()
    logs = Path(tempfile.mkdtemp())
    try:
        with _Patch((QMessageBox, "warning", staticmethod(lambda *a, **k: 0))):
            errors.install(lambda: None, logs)
            sys.stderr = None                  # pythonw / a desktop shortcut
            try:
                raise KeyError("no console")
            except KeyError:
                sys.excepthook(*sys.exc_info())          # must not raise

            class _Bad:
                def __del__(self):
                    raise ValueError("raised in __del__")
            b = _Bad()
            del b
            gc.collect()
        text = (logs / errors.ERRORS_LOG).read_text()
        assert "KeyError: 'no console'" in text
        assert "unraisable" in text and "raised in __del__" in text
    finally:
        _hooks_restore(saved)


def test_a_repeating_error_shows_one_box_per_minute_and_counts_the_rest():
    """Review finding 3: an error from a fast timer (GISDEP refreshes every 150 ms) used
    to stack warning boxes faster than they could be closed."""
    if not HAVE_QT:
        return _skip("test_a_repeating_error_shows_one_box_per_minute_and_counts_the_rest")
    from qtpy.QtWidgets import QMessageBox
    from .. import errors
    win = _win()
    saved = _hooks_saved()
    logs = Path(tempfile.mkdtemp())
    boxes = []
    try:
        with _Patch((QMessageBox, "warning",
                     staticmethod(lambda *a, **k: boxes.append(a[2]) or 0))):
            errors.install(lambda: win, logs)

            def tick():
                raise AttributeError("'NoneType' object has no attribute 'needle'")
            for _ in range(6):
                try:
                    tick()
                except AttributeError:
                    sys.excepthook(*sys.exc_info())
            assert len(boxes) == 1
            key = next(k for k in errors._state["recent"] if k[0] == "AttributeError")
            errors._state["recent"][key]["t"] -= errors.REPEAT_WINDOW_S + 1   # a minute on
            try:
                tick()
            except AttributeError:
                sys.excepthook(*sys.exc_info())
            assert len(boxes) == 2
        text = (logs / errors.ERRORS_LOG).read_text()
        assert text.count("AttributeError: 'NoneType' object has no attribute 'needle'") == 2
        assert "raised 5 more time(s)" in text
    finally:
        _hooks_restore(saved)
    win.close()


_FATAL = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from smartfib_executor.gui_qt import errors
errors.install(lambda: None, sys.argv[2])
errors.start_run_marker(sys.argv[2])
errors.update_run_marker(page="correlation", command="smartfib_executor.cli clem-markup --arm",
                         command_state="running")
from qtpy.QtWidgets import QApplication
from qtpy.QtCore import qCritical, qFatal
app = QApplication([])
qCritical("a critical Qt message")
errors.close_run_marker()          # as after aboutToQuit: a fatal now must still be reported
qFatal("QThread: Destroyed while thread is still running")
print("NOT REACHED")
'''


def test_a_qt_fatal_abort_is_logged_and_reported_at_the_next_start():
    """A real Qt abort in a child pyFIB-like process: the fatal message, the faulthandler
    stack and the run marker survive it, and the next start reports the run."""
    if not HAVE_QT:
        return _skip("test_a_qt_fatal_abort_is_logged_and_reported_at_the_next_start")
    from .. import errors
    logs = Path(tempfile.mkdtemp())
    p = subprocess.run([sys.executable, "-c", _FATAL, str(ROOT), str(logs)],
                       capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert "NOT REACHED" not in p.stdout and p.returncode != 0
    text = (logs / errors.ERRORS_LOG).read_text()
    assert "Qt critical" in text and "a critical Qt message" in text
    assert "Qt FATAL" in text and "Destroyed while thread is still running" in text
    assert "Fatal Python error" in (logs / errors.FAULTS_LOG).read_text()
    markers = list(logs.glob(errors.RUN_MARKER_PREFIX + "*.json"))
    assert len(markers) == 1 and json.loads(markers[0].read_text())["state"] == "running"
    found = errors.check_previous_runs(logs)
    assert len(found) == 1, found
    assert "tab: correlation" in found[0] and "clem-markup --arm (running)" in found[0]
    assert "Qt fatal message: QThread: Destroyed" in found[0]
    assert not list(logs.glob(errors.RUN_MARKER_PREFIX + "*.json"))   # reported once


def test_run_markers_only_report_runs_that_died():
    from .. import errors
    logs = Path(tempfile.mkdtemp())
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True).stdout.strip()
    dead2 = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                           capture_output=True, text=True).stdout.strip()
    now = time.time()

    def marker(pid, state, updated, **extra):
        m = {"pid": int(pid), "state": state, "updated": updated, "started_text": "t0",
             "updated_text": "t1", "version": "0.54.4", "command": "mill-positions",
             "command_state": "exit 0"}
        m.update(extra)
        (logs / f"{errors.RUN_MARKER_PREFIX}{pid}.json").write_text(json.dumps(m))
    # died while its command was still running -- and that command is STILL alive
    marker(dead, "running", now - 120, command_state="running", command_pid=os.getpid())
    # died, its command also gone
    marker(dead2, "running", now - 60, command_state="running", command_pid=int(dead))
    marker(os.getppid(), "running", now - 30)          # alive and fresh: another pyFIB
    marker(999999, "closed", now - 8 * 24 * 3600)      # closed long ago: tidied
    saved = {k: errors._state[k] for k in ("marker", "marker_path", "previous_unclean",
                                           "errors_log")}
    try:
        errors._state["errors_log"] = logs / errors.ERRORS_LOG
        found = errors.start_run_marker(logs)
        assert len(found) == 2, found
        one = next(f for f in found if f"(pid {dead})" in f)
        two = next(f for f in found if f"(pid {dead2})" in f)
        assert "pyFIB 0.54.4" in one and "STILL RUNNING" in one
        assert "no longer running" in two
        names = sorted(p.name for p in logs.glob(errors.RUN_MARKER_PREFIX + "*.json"))
        assert names == sorted([f"{errors.RUN_MARKER_PREFIX}{os.getppid()}.json",
                                f"{errors.RUN_MARKER_PREFIX}{os.getpid()}.json"])
        errors.update_run_marker(page="mill")
        errors.close_run_marker()
        m = json.loads((logs / f"{errors.RUN_MARKER_PREFIX}{os.getpid()}.json").read_text())
        assert m["state"] == "closed" and m["page"] == "mill"
        assert (logs / errors.ERRORS_LOG).read_text().count(
            "PREVIOUS RUN ENDED WITHOUT CLOSING") == 2
    finally:
        errors._state.update(saved)


def _run_all() -> int:
    if HAVE_QT:
        _app()
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
