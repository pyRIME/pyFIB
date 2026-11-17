"""pymap.diag (v0.9.2): crash logging, guarded slots, run markers, hang dumps."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyqt5")

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]

from pymap import diag


def _run(code, env_extra, timeout=60):
    env = dict(os.environ)
    env.update(env_extra)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _log_text():
    return Path(diag.log_path()).read_text(encoding="utf-8")


def test_import_is_stdlib_only():
    r = _run("""
        import sys, pymap.diag
        heavy = [m for m in ("numpy", "PIL", "skimage", "qtpy", "PyQt5", "tifffile")
                 if m in sys.modules]
        print(heavy)
    """, {})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]"


def test_slot_passes_signal_args_like_pyqt():
    got = []
    diag.slot("zero", lambda: got.append("zero"))(True)              # clicked(bool)
    diag.slot("one", lambda v, idx=7: got.append((v, idx)))(5)       # valueChanged(int)
    diag.slot("var", lambda *a: got.append(a))(1, 2)
    assert got == ["zero", (5, 7), (1, 2)]


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PyQt5")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, monkeypatch):
    from qtpy.QtWidgets import QMessageBox
    from pymap.gui.main_window import MainWindow
    boxes = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: boxes.append(a[1:3]))
    escaped = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: escaped.append(a))
    win = MainWindow()
    win._boxes, win._escaped = boxes, escaped
    return win


def _process_events(app, n=3):
    for _ in range(n):
        app.processEvents()


def test_failing_slot_is_logged_and_shown_not_escaped(app, window):
    from qtpy.QtWidgets import QPushButton
    b = QPushButton()

    def boom():
        raise ZeroDivisionError("injected for the test")
    b.clicked.connect(diag.slot("test button", boom, owner=window))
    b.click()
    _process_events(app)
    assert window._escaped == []                       # never reached the host's hook
    assert window._boxes and "test button" in window._boxes[0][0]
    text = _log_text()
    assert "FAIL  test button" in text and "ZeroDivisionError: injected for the test" in text
    assert "state: fixed=None" in text and "Traceback" in text
    # a second failure within 10 s is logged but shows no second box
    b.click()
    _process_events(app)
    assert len(window._boxes) == 1


def test_controller_signal_slot_failure_is_caught(app, window):
    """PyQt sends a slot's exception to sys.excepthook, not to emit(): the
    controller -> view connections must be guarded as well."""
    window.c.fixedImageChanged.emit(np.zeros((4, 4, 2), np.float32))   # bad shape
    _process_events(app)
    assert window._escaped == []
    assert "FAIL  show FIB image" in _log_text()


def test_view_event_handler_failure_is_caught(app, window, monkeypatch):
    from qtpy.QtCore import QPoint
    from qtpy.QtTest import QTest
    from qtpy.QtCore import Qt
    v = window.fixed_view
    v.set_image(np.zeros((20, 30), np.float32))
    window.resize(900, 600)
    window.show()
    monkeypatch.setattr(type(v), "_hit", lambda self, pos: (_ for _ in ()).throw(
        KeyError("injected")))
    QTest.mousePress(v.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(10, 10))
    _process_events(app)
    assert window._escaped == []
    assert "FAIL  event mousePressEvent on LandmarkView: KeyError" in _log_text()


def test_actions_write_begin_end_and_marker(app, window):
    window.fit_views()                              # plain call: no log line
    a = [x for x in window.findChildren(type(window.dock.toggleViewAction()))
         if x.text() == "Fit views"][0]
    a.trigger()
    text = _log_text()
    assert "BEGIN Fit views" in text and "END   Fit views ok" in text
    m = json.loads((Path(diag.log_dir()) / ("pymap_run_%d.json" % os.getpid())).read_text())
    assert m["state"] == "running" and m["action"] == "Fit views" and m["action_done"]


def test_qt_warnings_are_logged(app, window):
    from qtpy.QtCore import qWarning
    qWarning("pymap-test qt message")
    assert "[qt warning] pymap-test qt message" in _log_text()


def test_set_session_never_raises_into_host(app, window, monkeypatch):
    monkeypatch.setattr(window, "_set_session", lambda *a: 1 / 0)
    window.set_session("/x")                       # pyFIB calls this directly
    _process_events(app)
    assert window._escaped == [] and window._boxes
    assert "FAIL  set_session" in _log_text()


def test_unclean_previous_run_is_reported(tmp_path):
    # run 1 starts an action and dies without atexit (as in a hard crash)
    r = _run("""
        import os, pymap.diag as d
        d.setup(qt=False)
        with d.action("Open confocal", detail="big.tif"):
            os._exit(3)
    """, {"PYMAP_LOG_DIR": str(tmp_path)})
    assert r.returncode == 3, r.stderr
    # run 2 finds it
    r = _run("""
        import pymap.diag as d
        d.setup(qt=False)
        print(d.previous_unclean())
    """, {"PYMAP_LOG_DIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert "action 'Open confocal' was RUNNING" in r.stdout
    log = (tmp_path / "pymap.log").read_text()
    assert "PREVIOUS RUN ENDED WITHOUT CLOSING" in log
    assert "BEGIN Open confocal | big.tif" in log
    # run 2 closed normally: a third run reports nothing
    r = _run("import pymap.diag as d; d.setup(qt=False); print(d.previous_unclean())",
             {"PYMAP_LOG_DIR": str(tmp_path)})
    assert r.stdout.strip() == "[]"


def test_hang_dumps_all_stacks(tmp_path):
    r = _run("""
        import time, pymap.diag as d
        d.HANG_S = 0.3
        d.setup(qt=False)
        def slow_warp():
            time.sleep(1.0)
        with d.action("Register"):
            slow_warp()
    """, {"PYMAP_LOG_DIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    dump = (tmp_path / "pymap_hangs.log").read_text()
    assert "Timeout" in dump and "slow_warp" in dump
    assert "all thread stacks were dumped" in (tmp_path / "pymap.log").read_text()


def test_diag_off_writes_nothing(tmp_path):
    r = _run("""
        import sys, pymap.diag as d
        hook = sys.excepthook
        print(d.setup(), sys.excepthook is hook)
    """, {"PYMAP_LOG_DIR": str(tmp_path), "PYMAP_DIAG": "0"})
    assert r.stdout.split() == ["None", "True"]
    assert list(tmp_path.iterdir()) == []


def test_uncaught_pymap_exception_is_logged_then_passed_on(tmp_path):
    r = _run("""
        import sys, pymap.diag as d
        seen = []
        sys.excepthook = lambda *a: seen.append(a[0].__name__)
        d.setup(qt=False)
        from pymap import meta
        try:
            meta.FibImageMeta.from_dict({})          # KeyError inside pymap
        except Exception:
            sys.excepthook(*sys.exc_info())
        print(seen)
    """, {"PYMAP_LOG_DIR": str(tmp_path)})
    assert r.stdout.strip() == "['KeyError']", r.stderr
    assert "UNCAUGHT exception" in (tmp_path / "pymap.log").read_text()


# ---- second review (v0.9.2 verification) ------------------------------------ #
def test_dialog_actions_and_opt_out_do_not_arm_hang_dumps(tmp_path):
    r = _run("""
        import os, time, faulthandler, pymap.diag as d
        d.HANG_S = 0.2
        d.setup(qt=False)
        with d.action("Open FIB (browsing)", hang=False):
            time.sleep(0.5)                 # a file dialog left open
        with d.hang_watch("open FIB work"):
            time.sleep(0.5)                 # the load itself
    """, {"PYMAP_LOG_DIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    dump = (tmp_path / "pymap_hangs.log").read_text()
    assert dump.count("Timeout") == 1       # only the work part
    r = _run("""
        import time, pymap.diag as d
        d.HANG_S = 0.2
        d.setup(qt=False)
        with d.action("Register"):
            time.sleep(0.5)
    """, {"PYMAP_LOG_DIR": str(tmp_path / "b"), "PYMAP_HANG_DUMPS": "0"})
    assert r.returncode == 0, r.stderr
    assert "Timeout" not in (tmp_path / "b" / "pymap_hangs.log").read_text()


def test_embedded_leaves_warning_handling_to_the_host(tmp_path):
    r = _run("""
        import warnings, pymap.diag as d
        seen = []
        warnings.showwarning = lambda m, *a, **k: seen.append(str(m))
        d.setup(qt=False, embedded=True)
        with d.action("Register"):
            warnings.warn("host should see this")
        print(seen)
    """, {"PYMAP_LOG_DIR": str(tmp_path)})
    assert r.stdout.strip() == "['host should see this']", r.stderr


def test_diag_off_sends_nothing_to_the_host_root_logger(tmp_path):
    r = _run("""
        import logging, pymap.diag as d
        got = []
        class H(logging.Handler):
            def emit(self, rec): got.append(rec.getMessage())
        logging.getLogger().addHandler(H()); logging.getLogger().setLevel(logging.DEBUG)
        d.setup(qt=False)
        d.slot("click", lambda: 1)()
        logging.getLogger("pymap.confocal").warning("x")
        print(got)
    """, {"PYMAP_LOG_DIR": str(tmp_path), "PYMAP_DIAG": "0"})
    assert r.stdout.strip() == "[]", r.stderr + r.stdout


def test_rotation_keeps_backups_when_the_log_is_held(tmp_path, monkeypatch):
    import logging
    base = tmp_path / "pymap.log"
    for i in range(1, 4):
        (tmp_path / ("pymap.log.%d" % i)).write_text("backup %d\n" % i)
    h = diag._SafeRotatingFileHandler(str(base), maxBytes=200, backupCount=3,
                                      encoding="utf-8")
    real = os.replace

    def held(src, dst):
        if os.path.abspath(src) == os.path.abspath(str(base)):
            raise PermissionError("held by another process")
        return real(src, dst)
    monkeypatch.setattr(diag.os, "replace", held)
    lg = logging.getLogger("pymap.rotation_test")
    lg.addHandler(h)
    lg.propagate = False
    for i in range(30):
        lg.warning("line %03d %s", i, "x" * 40)
    assert [(tmp_path / ("pymap.log.%d" % i)).read_text() for i in range(1, 4)] == \
        ["backup 1\n", "backup 2\n", "backup 3\n"]       # nothing deleted
    assert "line 029" in base.read_text()              # still logging
    monkeypatch.setattr(diag.os, "replace", real)
    h._retry_at = 0.0
    lg.warning("after release")                     # one rollover, then this line
    assert "line 029" in (tmp_path / "pymap.log.1").read_text()
    assert (tmp_path / "pymap.log.2").read_text() == "backup 1\n"    # shifted once
    assert (tmp_path / "pymap.log.3").read_text() == "backup 2\n"    # oldest dropped
    assert "after release" in base.read_text()
    lg.removeHandler(h)
    h.close()


def test_reused_pid_with_stale_heartbeat_is_reported(tmp_path):
    marker = {"pid": os.getpid(), "state": "running", "heartbeat": True,
              "started_text": "2026-11-01 09:00:00", "updated": 1.0,
              "action": "Open confocal", "action_done": False, "action_t": 1.0}
    (tmp_path / ("pymap_run_%d.json" % os.getpid())).write_text(json.dumps(marker))
    r = _run("import pymap.diag as d; d.setup(qt=False); print(d.previous_unclean())",
             {"PYMAP_LOG_DIR": str(tmp_path)})
    assert "Open confocal' was RUNNING" in r.stdout, r.stderr
