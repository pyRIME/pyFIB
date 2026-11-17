"""pyFIB Qt GUI -- Step 1 scaffold.

Proves the shell for the re-skin: the shared theme, a left nav that switches a
QStackedWidget of pages, and a Log page fed live by a subprocess worker (the same
model the tkinter GUI uses -- it shells out to the CLI). Later phases fill the
Capture / Positions / Mill / Tools pages with real controls; the engine and CLI are
untouched.

Runs only where qtpy/PyQt5 is installed. It can be smoke-tested headless with
QT_QPA_PLATFORM=offscreen (see tests/), which is why a Qt GUI is more verifiable than
the tkinter one.
"""

from __future__ import annotations

import sys
from typing import Dict, List, Optional

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from . import errors as _errors
from .theme import apply_theme, scale_up
from .branding import app_icon, brand_label, set_windows_app_id
from .session import new_session_positions_path
from .worker import SubprocessWorker, build_argv, build_module_argv
from .pages import ArmBar, CapturePage, PositionsPage, MillPage, SetupPage, GisdepTab, ElymakerTab
from .tools_page import ToolsPage
from .correlation_page import CorrelationPage

APP_TITLE = "pyFIB"
NAV = [
    ("setup", "Setup"),
    ("gisdep", "GISDEP"),
    ("correlation", "Correlation"),  # v0.52.0: CLEM targeting with pyMap (at SEM normal)
    ("elymaker", "ELYMaker"),
    ("capture", "Capture"),
    ("positions", "Positions"),
    ("mill", "Rough Mill"),      # v0.50.0: was "Mill" (key kept: same page, same tests)
    ("prepolish", "Prepolish"),  # v0.51.0: own pattern; aligns to the rough after image
    ("polish", "Polish"),        # v0.51.0: own pattern; aligns to the prepolish image
    ("tools", "Tools"),          # v0.44.0: report, SEM autofocus, FIB autofocus
    ("log", "Log"),
]


class _Placeholder(QWidget):
    """A stand-in page used until a real page is built (Phase 2+)."""

    def __init__(self, title: str, note: str = ""):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        h = QLabel(title)
        h.setObjectName("pageTitle")
        f = h.font(); f.setPointSize(20); f.setBold(True); h.setFont(f)
        lay.addWidget(h)
        if note:
            sub = QLabel(note)
            sub.setWordWrap(True)
            sub.setStyleSheet("color: palette(mid);")
            lay.addWidget(sub)
        lay.addStretch(1)


class LogPage(QWidget):
    """Live output pane fed by the subprocess worker."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        title = QLabel("Log")
        f = title.font(); f.setPointSize(20); f.setBold(True); title.setFont(f)
        lay.addWidget(title)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(5000)
        lay.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.view.clear)
        row.addStretch(1)
        row.addWidget(self.btn_clear)
        lay.addLayout(row)

    def append(self, text: str) -> None:
        self.view.appendPlainText(text)


class MainWindow(QMainWindow):
    def __init__(self, positions_file: Optional[str] = None):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(app_icon())        # v0.54.1: the pyFIB logo
        self.resize(1360, 900)
        self._worker: Optional[SubprocessWorker] = None
        self._cmd_active = False             # v0.55.1: a pyFIB command is running
        self._preload_positions = positions_file

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Shared top bar: the logo + positions-file path (visible on every page).
        # v0.54.2 (Evan, 2026-10-18): the logo sits top left; the ARM toggle moved to the
        # bottom of the nav bar (still on every page; same button, same wiring).
        try:
            from .. import __version__ as _ver
        except Exception:  # noqa: BLE001
            _ver = "?"
        self.brand = brand_label(APP_TITLE, height=50, tooltip=f"pyFIB v{_ver}")
        self.brand.setFixedWidth(188)       # lines up with the 210 px nav column
        self.arm_bar = ArmBar(on_change=self._on_arm_change,
                              on_new_session=self._on_new_session,
                              brand=self.brand, arm_in_bar=False)
        outer.addWidget(self.arm_bar)

        content = QWidget()
        outer.addWidget(content, 1)
        root = QHBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- left nav ---
        nav = QWidget()
        nav.setObjectName("navBar")
        nav.setFixedWidth(210)
        nav_lay = QVBoxLayout(nav)
        nav_lay.setContentsMargins(10, 14, 10, 14)
        nav_lay.setSpacing(6)

        self._nav_buttons: Dict[str, QPushButton] = {}
        self._pages: Dict[str, QWidget] = {}
        self.stack = QStackedWidget()

        for key, label in NAV:
            btn = QPushButton(label)
            btn.setObjectName("toolButton")   # shared theme styles this
            btn.setCheckable(True)
            btn.setMinimumHeight(44)
            btn.clicked.connect(lambda _=False, k=key: self.show_page(k))
            nav_lay.addWidget(btn)
            self._nav_buttons[key] = btn

        nav_lay.addStretch(1)
        # v0.54.2: the ARM safety toggle, bottom left (created and wired by ArmBar)
        nav_lay.addSpacing(8)
        nav_lay.addWidget(self.arm_bar.arm_btn)

        # --- pages ---
        self.log_page = LogPage()
        self.capture_page = CapturePage(self)
        self.positions_page = PositionsPage(self)
        self.mill_page = MillPage(self, title="Rough Mill", stage="rough")
        self.prepolish_page = MillPage(self, title="Prepolish", stage="prepolish")
        self.polish_page = MillPage(self, title="Polish", stage="polish")
        self.setup_page = SetupPage(self)
        self.gisdep_page = GisdepTab(self)
        self.elymaker_page = ElymakerTab(self)
        self.tools_page = ToolsPage(self)
        self.correlation_page = CorrelationPage(self)
        self._pages["setup"] = self.setup_page
        self._pages["gisdep"] = self.gisdep_page
        self._pages["correlation"] = self.correlation_page
        self._pages["elymaker"] = self.elymaker_page
        self._pages["capture"] = self.capture_page
        self._pages["positions"] = self.positions_page
        self._pages["mill"] = self.mill_page
        self._pages["prepolish"] = self.prepolish_page
        self._pages["polish"] = self.polish_page
        self._pages["tools"] = self.tools_page
        self._pages["log"] = self.log_page
        for key, _ in NAV:
            self.stack.addWidget(self._pages[key])

        root.addWidget(nav)
        root.addWidget(self.stack, 1)

        # If the launcher passed an explicit positions file, load it; otherwise start
        # in a FRESH session (a unique timestamped positions dir, created on the first
        # capture). Load Previous... in the top bar opens an old one.
        if self._preload_positions:
            self.arm_bar.pos_edit.setText(str(self._preload_positions))
        else:
            self.arm_bar.pos_edit.setText(str(new_session_positions_path()))
        self.statusBar().showMessage("Ready")
        self.show_page("setup")

        # A tiny self-check button on the Tools placeholder is overkill for step 1;
        # instead, a menu action proves the subprocess plumbing end to end.
        act = self.menuBar().addMenu("&Run")
        a = act.addAction("Self-check: run 'list-positions --help'")
        a.triggered.connect(self._self_check)
        # v0.55.0: every crash log lives in one folder -- send all of it with a report.
        b = act.addAction("Open logs folder (send it with a crash report)")
        b.triggered.connect(self.open_logs_folder)

    # --- navigation ---
    def show_page(self, key: str) -> None:
        if key not in self._pages:
            return
        _errors.update_run_marker(page=key)       # v0.55.0: named if pyFIB dies here
        self.stack.setCurrentWidget(self._pages[key])
        for k, btn in self._nav_buttons.items():
            btn.setChecked(k == key)
        if key == "positions" and self.positions_file():
            self.positions_page.reload()
        if key in ("mill", "prepolish", "polish") and self.positions_file():
            self._pages[key].reload()
        if key == "tools":
            self.tools_page.on_show()
        if key == "correlation":
            self.correlation_page.on_show()
        if key == "gisdep":
            self._open_gisdep_when_safe()
        if key == "elymaker":
            self.elymaker_page.ensure_built()
            self.elymaker_page.raise_window()      # v0.53.2: if it is in its own window
        if key == "setup" and self.positions_file():
            self.setup_page.reload_positions()
        self._sync_gisdep()

    # A command's process has only just exited when its exit code arrives; GISDEP's own
    # connection is opened a moment later, never back to back with it (v0.55.1 review:
    # two connections about a second apart ended in a ZEISS access violation on
    # 2026-10-12, cause unconfirmed -- here it would take the window down).
    GISDEP_OPEN_DELAY_S = 3.0

    def _open_gisdep_when_safe(self) -> None:
        """Build the GISDEP tab (which connects to SmartSEM) only if its tab is on screen,
        pyFIB is not closing, no pyFIB command runs, and the last one ended at least
        GISDEP_OPEN_DELAY_S ago; otherwise say so on the tab and try again shortly."""
        gp = getattr(self, "gisdep_page", None)
        if gp is None or gp._gwin is not None or getattr(self, "_gisdep_opening", False):
            return                           # built, or being built right now (review)
        if (getattr(self, "_closing", False) or getattr(self, "_close_when_done", False)
                or getattr(self, "_gisdep_shut", False)):
            return
        if self.stack.currentWidget() is not gp:
            return
        if self._cmd_active:
            gp.ensure_built()                # shows "opens when it has finished"
            return
        import time as _time
        wait = self.GISDEP_OPEN_DELAY_S - (_time.monotonic()
                                           - getattr(self, "_cmd_ended_at", -1e9))
        if wait > 0:
            gp.show_waiting("The last pyFIB command has just ended.\nGISDEP opens in a moment.")
            # ONE reusable single-shot timer (review): repeated looks at the tab during
            # the wait re-arm it instead of queueing more calls
            t = getattr(self, "_gisdep_open_timer", None)
            if t is None:
                from qtpy.QtCore import QTimer
                t = self._gisdep_open_timer = QTimer(self)
                t.setSingleShot(True)
                t.timeout.connect(self._open_gisdep_when_safe)
            t.start(int(wait * 1000) + 50)
            return
        self._gisdep_opening = True
        try:
            gp.ensure_built()
        finally:
            self._gisdep_opening = False
        self._sync_gisdep()

    def _sync_gisdep(self) -> None:
        """v0.55.1 (#18): GISDEP reads the microscope only while its tab is on screen and
        no pyFIB command runs, and its panel is greyed out while one does."""
        gp = getattr(self, "gisdep_page", None)
        if gp is None or getattr(self, "_gisdep_shut", False):
            return
        try:
            cmd = bool(getattr(self, "_cmd_active", False))
            shown = self.stack.currentWidget() is gp
            gp.set_blocked(cmd)
            gp.set_polling(shown and not cmd)
        except Exception as e:  # noqa: BLE001
            _errors._append_log("could not pause / resume GISDEP", repr(e))

    def mill_pages(self):
        """The three milling stages, in order (v0.50.0)."""
        return [self.mill_page, self.prepolish_page, self.polish_page]

    # --- shared state the pages read ---
    def is_armed(self) -> bool:
        return self.arm_bar.is_armed()

    def positions_file(self) -> str:
        return self.arm_bar.positions_file()

    def status(self, msg: str) -> None:
        self.statusBar().showMessage(msg)

    def confirm(self, message: str) -> bool:
        from qtpy.QtWidgets import QMessageBox
        return QMessageBox.question(
            self, "Confirm", message,
            QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes

    def _on_arm_change(self, armed: bool) -> None:
        self.status("ARMED — instrument commands will execute for real."
                    if armed else "Dry-run — instrument commands print the plan only.")
        # Keep the embedded gisdep tab's arm state in lock-step with pyFIB's toggle.
        if getattr(self, "gisdep_page", None) is not None:
            self.gisdep_page.sync_arm()

    def _on_new_session(self) -> None:
        # Fresh session: clear stale table/dropdown data (the new file has no records
        # until the first capture writes to it).
        # v0.55.0: each page on its own, and an error is logged -- it used to be
        # swallowed silently, leaving the pages after the failing one on the old session.
        for name, fn in (("Positions", self.positions_page.reload),
                         *((p.title, p.reload) for p in self.mill_pages()),
                         ("Setup", self.setup_page.reload_positions),
                         ("Correlation", self.correlation_page.reload)):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.log_page.append(f"[new session] {name} tab could not reload: "
                                     f"{type(e).__name__}: {e}")
        self.status("New session started.")

    # --- subprocess plumbing (the model kept from tkinter) ---
    def run_argv(self, argv: List[str], *, input_text: Optional[str] = None,
                 on_line=None, on_done=None, show_log: bool = True,
                 instrument: bool = True) -> bool:
        """Launch any argv on the worker, streaming stdout to the Log page.

        ``on_line`` also receives each line (pages use it for live status parsing);
        ``on_done`` gets the return code. This is the shared core for run_cli /
        run_module -- the subprocess model, unchanged. show_log=False (v0.51.0, the
        milling tabs' manual buttons) keeps the current page on screen; the output still
        goes to the Log page.
        """
        if self._worker is not None and self._worker.isRunning():
            self.statusBar().showMessage("A command is already running")
            return False
        if getattr(self, "_closing", False):
            # v0.55.0 (review): while the close question is up or pyFIB waits to close,
            # nothing new starts (a run started from a dialog during the close question
            # used to be the one left running when pyFIB closed).
            self.statusBar().showMessage("pyFIB is closing: no new command started")
            self.log_page.append(f"[close] not started (pyFIB is closing): "
                                 f"{' '.join(str(a) for a in argv[3:])[:200]}")
            return False
        # instrument=False (v0.55.1 review): a files-only tool (the standalone pyMap
        # window, the report) never touches the microscope, so it neither waits for
        # GISDEP nor pauses it.
        if instrument and self._gisdep_busy():
            # v0.55.1 (#18): GISDEP is moving the stage / a needle from inside this
            # window; a pyFIB command must not drive the microscope at the same time.
            self.statusBar().showMessage(
                "GISDEP is running a step: wait for it (or press STOP in the GISDEP tab)")
            self.log_page.append(f"[gisdep] not started (GISDEP is running a step): "
                                 f"{' '.join(str(a) for a in argv[3:])[:200]}")
            return False
        try:
            if instrument:
                self._cmd_active = True
                self._sync_gisdep()          # GISDEP stops reading the microscope now
            if show_log:
                self.show_page("log")
            self._worker = SubprocessWorker(argv, input_text=input_text)
            self._worker.line.connect(self.log_page.append)
            if on_line is not None:
                self._worker.line.connect(on_line)
            self._worker.failed.connect(lambda m: self.log_page.append(f"[UI ERROR] {m}"))
            self._worker.started_pid.connect(
                lambda pid: _errors.update_run_marker(command_pid=pid))
            self._worker.finished_rc.connect(self._on_rc)
            if on_done is not None:
                self._worker.finished_rc.connect(on_done)
            self.statusBar().showMessage("Running…")
            self._command_text = " ".join(str(a) for a in argv[3:])[:300]   # after python -u -m
            _errors.update_run_marker(command=self._command_text, command_state="running",
                                      command_pid=None)
            self._worker.start()
        except Exception:
            # v0.55.1 (review): a failed launch must not leave GISDEP greyed out for good
            self._cmd_active = False
            self._sync_gisdep()
            raise
        return True

    def run_cli(self, subcmd_args: List[str], *, input_text: Optional[str] = None,
                on_line=None, on_done=None, show_log: bool = True,
                instrument: bool = True) -> bool:
        """Launch a pyFIB CLI subcommand (python -u -m smartfib_executor.cli ...)."""
        return self.run_argv(build_argv(subcmd_args), input_text=input_text,
                             on_line=on_line, on_done=on_done, show_log=show_log,
                             instrument=instrument)

    def run_module(self, module: str, args: List[str], *,
                   input_text: Optional[str] = None, on_line=None, on_done=None,
                   show_log: bool = True, instrument: bool = True) -> bool:
        """Launch a standalone pyFIB module (e.g. the coincidence tool)."""
        return self.run_argv(build_module_argv(module, args), input_text=input_text,
                             on_line=on_line, on_done=on_done, show_log=show_log,
                             instrument=instrument)

    def _on_rc(self, rc: int) -> None:
        _errors.update_run_marker(command_state=f"exit {rc}")
        # v0.55.1 (#18): the command has ended; GISDEP may read / be used again.
        was_instrument = bool(self._cmd_active)
        self._cmd_active = False
        if was_instrument:
            import time as _time
            self._cmd_ended_at = _time.monotonic()
        try:
            self._sync_gisdep()
            self._open_gisdep_when_safe()
        except Exception as e:  # noqa: BLE001
            _errors._append_log("could not resume GISDEP", repr(e))
        if getattr(self, "_killed_by_operator", False):
            self._killed_by_operator = False
            self.log_page.append(f"[exit {rc}] (killed by the operator to close pyFIB)")
            self.statusBar().showMessage("The command was killed")
            return
        crash = _errors.describe_crash_exit(rc)
        if crash is None:
            note = _errors.describe_interrupt_exit(rc)
            self.log_page.append(f"[exit {rc}]" + (f" ({note})" if note else ""))
            self.statusBar().showMessage(
                "Done" if rc == 0 else f"Exited with code {rc}" + (f" ({note})" if note else ""))
            return
        # v0.55.0: a command that CRASHED (e.g. an access violation inside the ZEISS
        # API) is named as such, never just "Exited with code 3221225477".
        logs = self.logs_dir()
        cmd = getattr(self, "_command_text", "") or "the command"
        msg = (f"The command CRASHED ({crash}):\n  {cmd}\n\nIt may have stopped in the "
               f"middle of an action. Check the microscope in SmartSEM (stage, beam, "
               f"milling, beam shift) before you continue. A Python stack, if any, is in "
               f"{logs / 'pyfib_faults.log'}.")
        self.log_page.append(f"[exit {rc}] *** COMMAND CRASHED: {crash} ***\n{msg}")
        self.statusBar().showMessage(f"The command CRASHED ({crash}) -- see the Log tab")
        _errors._append_log(f"command crashed ({crash})", cmd)
        if getattr(self, "_close_when_done", False):
            return                       # the operator is closing; the Log has it
        try:
            from qtpy.QtWidgets import QMessageBox
            QMessageBox.critical(self, "pyFIB: the command crashed", msg)
        except Exception:  # noqa: BLE001
            pass

    # --- logs and closing (v0.55.0) ---
    @staticmethod
    def logs_dir():
        from ..cli import _project_root
        return _project_root() / "logs"

    def open_logs_folder(self) -> None:
        d = self.logs_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            from qtpy.QtCore import QUrl
            from qtpy.QtGui import QDesktopServices
            ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))
        except Exception:  # noqa: BLE001
            ok = False
        self.status(f"Logs folder: {d}" + ("" if ok else " (could not open it here)"))

    def _gisdep_busy(self) -> bool:
        try:
            gw = getattr(self.gisdep_page, "_gwin", None)
            w = getattr(gw, "_worker", None) if gw is not None else None
            return bool(w is not None and w.isRunning())
        except Exception:  # noqa: BLE001
            return False

    def _command_running(self) -> bool:
        w = getattr(self, "_worker", None)
        try:
            return bool(w is not None and w.isRunning())
        except Exception:  # noqa: BLE001
            return True                  # unknown: treat as running (never drop a thread)

    def _busy(self) -> bool:
        return self._command_running() or self._gisdep_busy()

    def _close_when_idle(self) -> None:
        """Close as soon as no command / GISDEP step runs and no dialog is open (polled)."""
        from qtpy.QtWidgets import QApplication
        if not getattr(self, "_close_when_done", False):
            return
        if self._busy() or QApplication.activeModalWidget() is not None:
            return
        self._close_timer.stop()
        self.log_page.append("[close] the command has finished: closing pyFIB")
        self.close()

    def _ask_close_with_command(self, cmd: str) -> str:
        """'keep' | 'wait' | 'kill'. Keep is the default and Esc."""
        from qtpy.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("pyFIB: a command is still running")
        box.setText(f"A command is still running:\n  {cmd}")
        box.setInformativeText(
            "Close when it finishes: pyFIB starts nothing new and closes by itself once "
            "the command has ended normally.\n\nKill it now: only for a command that hangs "
            "(you will be asked again).")
        keep = box.addButton("Keep pyFIB open", QMessageBox.RejectRole)
        wait = box.addButton("Close when it finishes", QMessageBox.AcceptRole)
        kill = box.addButton("Kill it now…", QMessageBox.DestructiveRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.exec_() if hasattr(box, "exec_") else box.exec()
        clicked = box.clickedButton()
        if clicked is wait:
            return "wait"
        if clicked is kill:
            sure = QMessageBox.question(
                self, "pyFIB: kill the running command?",
                f"Kill this command now, like Task Manager would?\n  {cmd}\n\n"
                "pyFIB will NOT abort milling: stop a running pattern in SmartFIB yourself. "
                "Whatever the command was in the middle of (a stage move, a beam shift, a "
                "probe or mode change, a valve step) is left as it is, and nothing after it "
                "happens (no beam-shift reset, no after image). Check the microscope in "
                "SmartSEM afterwards and press Go to before the next capture or mill. A "
                "standalone pyMap window loses what it has not saved.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            return "kill" if sure == QMessageBox.Yes else "keep"
        return "keep"

    def closeEvent(self, ev):  # noqa: N802 (Qt)
        """v0.55.0: closing pyFIB with a command or a GISDEP step still running used to
        drop a running QThread (Qt may abort the process, or pyFIB exits leaving the
        child running unread). And a detached pyMap / ELYMaker window kept the process
        alive with no main window. Review: the state is re-read after every dialog (the
        dialogs run an event loop in which a command can end and another start), new
        commands are refused while closing, and the safe way out is "close when it
        finishes"; a kill is offered only behind a second, explicit question."""
        from qtpy.QtWidgets import QMessageBox
        try:
            if getattr(self, "_close_when_done", False) and self._busy():
                self.status("pyFIB will close when the command finishes.")
                ev.ignore()
                return
            self._closing = True
            try:
                if self._gisdep_busy():
                    QMessageBox.warning(
                        self, "pyFIB: GISDEP is still running",
                        "GISDEP is still running a step. Press STOP in the GISDEP tab and "
                        "wait until it has finished, then close pyFIB.")
                    self._closing = False
                    ev.ignore()
                    return
                w = self._worker
                if self._command_running():
                    cmd = getattr(self, "_command_text", "") or "a command"
                    choice = self._ask_close_with_command(cmd)
                    if self._worker is not w:          # changed while the box was up
                        choice = "keep"
                        self.status("Another command started; nothing was closed.")
                    if choice == "keep":
                        self._closing = False
                        ev.ignore()
                        return
                    if choice == "wait":
                        self._close_when_done = True
                        if getattr(self, "_close_timer", None) is None:
                            from qtpy.QtCore import QTimer
                            self._close_timer = QTimer(self)
                            self._close_timer.setInterval(500)
                            self._close_timer.timeout.connect(self._close_when_idle)
                        self._close_timer.start()
                        self.log_page.append(f"[close] pyFIB will close when this command "
                                             f"finishes: {cmd}")
                        self.status("pyFIB will close when the command finishes.")
                        ev.ignore()
                        return
                    # kill (asked twice)
                    self.log_page.append(f"[close] operator killed the running command and "
                                         f"closed pyFIB: {cmd}")
                    _errors._append_log("operator killed the running command to close pyFIB "
                                        "(milling NOT aborted by pyFIB)", cmd)
                    self._killed_by_operator = True
                    w.request_stop()
                    if not w.wait(10000):
                        QMessageBox.warning(self, "pyFIB", "The command did not end within "
                                            "10 s; pyFIB stays open. Try closing again.")
                        self._closing = False
                        ev.ignore()
                        return
                # re-check after every dialog: nothing may be running when the window goes
                if self._busy():
                    self._closing = False
                    ev.ignore()
                    self.status("Something is still running; pyFIB stays open.")
                    return
            except Exception:
                self._closing = False
                raise
            for host in (getattr(self.correlation_page.register, "host", None),
                         getattr(self.elymaker_page, "host", None)):
                try:
                    if host is not None and host.detached:
                        host.reattach()          # pyMap review, request 3
                except Exception as e:  # noqa: BLE001
                    _errors._append_log("could not close a detached tool window", repr(e))
            try:
                self._gisdep_shut = True         # a late exit code must not wake it again
                self.gisdep_page.shutdown()      # v0.55.1 (#18): release its connection
            except Exception as e:  # noqa: BLE001
                _errors._append_log("could not shut GISDEP down", repr(e))
        except Exception as e:  # noqa: BLE001
            # A failure here must not trap the operator in pyFIB, but only close when
            # nothing is running (a running QThread must never be dropped).
            _errors._append_log("closeEvent failed", repr(e))
            if self._busy():
                ev.ignore()
                return
        super().closeEvent(ev)

    def _self_check(self) -> None:
        # Harmless: prints the CLI help. Proves argv build + streaming + exit handling.
        self.run_cli(["list-positions", "--help"])


def _report_previous_unclean(win, summaries) -> None:
    """v0.55.0: the last pyFIB run ended without closing -- say so (once per such run)."""
    if not summaries:
        return
    logs = win.logs_dir()
    text = "\n\n".join(summaries)
    win.log_page.append("*** pyFIB did not close normally last time (hard crash, kill or "
                        f"power loss) ***\n{text}\nLogs: {logs}")
    try:
        from qtpy.QtWidgets import QMessageBox
        QMessageBox.warning(
            win, "pyFIB: the last run ended without closing",
            "pyFIB did not close normally last time (a hard crash, a kill from Task "
            f"Manager, or power loss):\n\n{text}\n\nCheck the microscope in SmartSEM before "
            f"you continue. Please send the whole logs folder with a report (Run menu > "
            f"Open logs folder):\n{logs}")
    except Exception:  # noqa: BLE001
        pass


def main(argv=None, positions_file=None) -> int:
    set_windows_app_id()                      # v0.54.1: taskbar shows the pyFIB icon
    # v0.53.2: an error in any button handler (pyMap, ELYMaker, GISDEP, pyFIB) is shown
    # and logged instead of PyQt5 aborting the whole process. v0.55.0: installed before
    # the QApplication exists (faulthandler and the Qt message handler cover start-up),
    # plus the run marker that reports a run which ended without closing.
    holder = {}
    previous = []
    try:
        from ..cli import _project_root
        logs = _project_root() / "logs"
        _errors.install(lambda: holder.get("win"), logs)
        previous = _errors.start_run_marker(logs)
    except Exception as e:  # noqa: BLE001
        _errors._stderr(f"WARNING: error hook not installed ({e})\n")
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    try:
        app.aboutToQuit.connect(_errors.close_run_marker)
        _errors.start_heartbeat(app)
    except Exception:  # noqa: BLE001
        pass
    app.setWindowIcon(app_icon())
    apply_theme(app)
    scale_up(app)
    win = MainWindow(positions_file=positions_file)
    holder["win"] = win
    if _errors._state.get("relay") is None:      # created on the GUI thread, now
        _errors._state["relay"] = _errors._make_relay(lambda: holder.get("win"))
    win.show()
    if previous:
        from qtpy.QtCore import QTimer
        QTimer.singleShot(300, lambda: _report_previous_unclean(win, previous))
    rc = app.exec_() if hasattr(app, "exec_") else app.exec()
    _errors.close_run_marker()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
