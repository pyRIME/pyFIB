"""Embedded tools (v0.53.2): ELYMaker / pyMap cannot resize pyFIB, panels scroll, both
can move into their own window and back, and a failing button no longer closes pyFIB.

Evan 2026-10-12: selecting a shape in ELYMaker pushed the maximised window past the
screen; the Correlation tab "seems to be crashing quite a bit"; the panels are too
small -- can they be new windows? Offline (offscreen Qt).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .test_smoke import HAVE_QT, _app

ROOT = Path(__file__).resolve().parents[3]
MARKUP = ROOT / "elymaker_tests" / "fixtures" / "Markup_Standard.ely"


def _skip(name):
    print(f"SKIP {name} (Qt not installed)")


def _win(session=None):
    from ..app import MainWindow
    win = MainWindow()
    if session is not None:
        win.arm_bar.pos_edit.setText(str(Path(session) / "positions.json"))
    win.resize(1600, 900)
    win.show()
    _app().processEvents()
    return win


def test_selecting_a_shape_in_elymaker_no_longer_grows_pyfib():
    if not HAVE_QT:
        return _skip("test_selecting_a_shape_in_elymaker_no_longer_grows_pyfib")
    from qtpy.QtWidgets import QDockWidget, QScrollArea
    from elymaker.reader import read_ely
    app = _app()
    win = _win()
    win.show_page("elymaker")
    app.processEvents()
    ew = win.elymaker_page._ewin
    docks = {d.objectName(): d for d in ew.findChildren(QDockWidget)}
    for name in ("dock_tools", "dock_properties", "dock_defaults"):
        assert isinstance(docks[name].widget(), QScrollArea), name     # panels scroll
    size0, min0 = win.size(), win.minimumSizeHint().height()
    ew.scene.load_layout(read_ely(str(MARKUP)))
    rect = [i for i in ew.scene.items() if type(i).__name__ == "RectItem"][0]
    rect.setSelected(True)                       # Properties fills with ~13 rows
    app.processEvents()
    assert win.size() == size0, (win.size(), size0)
    assert win.minimumSizeHint().height() == min0 < 900, min0
    win.close()


def test_elymaker_opens_in_its_own_window_and_comes_back_with_its_layout():
    if not HAVE_QT:
        return _skip("test_elymaker_opens_in_its_own_window_and_comes_back_with_its_layout")
    from PIL import Image
    from elymaker.reader import read_ely
    app = _app()
    win = _win()
    tab = win.elymaker_page
    win.show_page("elymaker")
    ew = tab._ewin
    ew.scene.load_layout(read_ely(str(MARKUP)))
    n_items = len(ew.scene.items())
    tab.detach()
    app.processEvents()
    assert tab.detached and ew.window() is tab.host.window and ew.window() is not win
    assert tab.host.placeholder.isVisible() and not tab.detach_btn.isEnabled()
    # A handoff (Positions "Author pattern", Correlation "Edit in ELYMaker") goes to it.
    d = Path(tempfile.mkdtemp())
    Image.new("L", (1024, 768), 80).save(d / "img.png")
    tab.open_handoff(image_path=str(d / "img.png"), pixel_size_um=0.05581,
                     out_path=str(d / "out.ely"), load_path=str(MARKUP),
                     fov_width_um=57.15, site="p1")
    win.show_page("elymaker")                   # raises its window, no error
    assert tab.detached and len(ew.scene.items()) >= 3
    tab.host.window.close()                     # closing the window docks it back
    app.processEvents()
    assert not tab.detached and ew.window() is win and tab.detach_btn.isEnabled()
    assert not tab.host.placeholder.isVisible()
    assert len(ew.scene.items()) >= n_items - 1   # same widget, nothing rebuilt
    win.close()


def test_pymap_opens_in_its_own_window_and_survives_a_session_change():
    if not HAVE_QT:
        return _skip("test_pymap_opens_in_its_own_window_and_survives_a_session_change")
    from ...tests.test_clem import _session
    from .. import correlation_page as CP
    root, has_embed = CP.find_pymap()
    if root is None or not has_embed:
        return _skip("test_pymap_opens_in_its_own_window (pyMap not importable)")
    app = _app()
    d, clem, tp = _session()
    win = _win(d)
    win.show_page("correlation")
    pane = win.correlation_page.register
    w = pane._widget
    assert w is not None and pane.detach_btn.isEnabled()
    min_before = win.minimumSizeHint().width()
    pane.detach()
    app.processEvents()
    assert pane.detached and w.window() is pane.host.window
    assert not pane.wide_btn.isEnabled()
    d2 = Path(tempfile.mkdtemp())                       # new session while detached
    pane.ensure_built(d2)
    app.processEvents()
    # v0.55.0: pyMap follows the session in place (set_session): same widget, images and
    # landmarks kept, still in its own window; no rebuild.
    assert pane._widget is w and pane.detached and pane._built_for == str(d2)
    assert w.c.session_dir == str(d2)
    assert "[pyMap] session changed" in win.log_page.view.toPlainText()
    assert pane._widget.window() is pane.host.window
    pane.host.reattach()
    app.processEvents()
    assert not pane.detached and pane._widget.window() is win and pane.wide_btn.isEnabled()
    assert win.minimumSizeHint().width() <= min_before
    win.close()


_SCRIPT = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from qtpy.QtWidgets import QApplication, QPushButton, QMessageBox
from qtpy.QtCore import QTimer
app = QApplication([])
if sys.argv[2] == "hook":
    from smartfib_executor.gui_qt.errors import install
    QMessageBox.warning = staticmethod(lambda *a, **k: 0)
    install(lambda: None, sys.argv[3])
b = QPushButton()
def slot():
    raise ValueError("<COMPRESSION.LZW: 5> requires the 'imagecodecs' package")
b.clicked.connect(slot)
QTimer.singleShot(0, b.click)
QTimer.singleShot(300, app.quit)
app.exec_()
print("SURVIVED")
'''


def test_a_failing_button_no_longer_closes_pyfib():
    """Without the hook PyQt5 aborts the process (the Correlation tab crashes); with
    pyFIB's hook the error is logged and the program carries on."""
    if not HAVE_QT:
        return _skip("test_a_failing_button_no_longer_closes_pyfib")
    logs = Path(tempfile.mkdtemp())
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    runs = {}
    for mode in ("bare", "hook"):
        p = subprocess.run([sys.executable, "-c", _SCRIPT, str(ROOT), mode, str(logs)],
                           capture_output=True, text=True, timeout=60, env=env)
        runs[mode] = p
    assert "SURVIVED" not in runs["bare"].stdout and runs["bare"].returncode != 0
    assert "SURVIVED" in runs["hook"].stdout and runs["hook"].returncode == 0, runs["hook"]
    text = (logs / "pyfib_gui_errors.log").read_text()
    assert "requires the 'imagecodecs' package" in text and "Traceback" in text
    assert (logs / "pyfib_faults.log").exists()


def test_the_error_hook_reports_into_the_log_tab():
    if not HAVE_QT:
        return _skip("test_the_error_hook_reports_into_the_log_tab")
    import faulthandler
    import threading
    from qtpy.QtWidgets import QMessageBox
    from .. import errors
    app = _app()
    win = _win()
    saved = (sys.excepthook, threading.excepthook, QMessageBox.warning)
    saved_unraisable = sys.unraisablehook
    boxes = []
    QMessageBox.warning = staticmethod(lambda *a, **k: boxes.append(a[2]) or 0)
    logs = Path(tempfile.mkdtemp())
    try:
        errors.install(lambda: win, logs)
        try:
            raise KeyError("pyMap: channel 3")
        except KeyError:
            sys.excepthook(*sys.exc_info())
        app.processEvents()
        assert "KeyError" in win.log_page.view.toPlainText() and "pyFIB kept running" in win.log_page.view.toPlainText()
        assert boxes and "keeps running" in boxes[0]
    finally:
        sys.excepthook, threading.excepthook, QMessageBox.warning = saved
        sys.unraisablehook = saved_unraisable
        faulthandler.disable()
        f = errors._state["fault_file"]
        if f is not None:
            f.close()
        errors._state.update(fault_file=None, installed=False)
    win.close()


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
