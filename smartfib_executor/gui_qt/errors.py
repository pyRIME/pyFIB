"""Keep pyFIB running when a button handler raises (v0.53.2), and leave a trace of
anything that still ends the process (v0.55.0).

PyQt5 ABORTS the whole process when a Python exception escapes a Qt slot and no
sys.excepthook has been installed (measured: exit status 134). Every embedded tool
(pyMap, ELYMaker, GISDEP) runs in pyFIB's process, so one unhandled error in any of
them closed pyFIB. Evan, 2026-10-12: "the correlation tab seems to be crashing quite a
bit" -- e.g. pyMap's Open Confocal on an LZW-compressed RGB TIFF raised tifffile's
"requires the 'imagecodecs' package" ValueError, which pyMap's handler did not catch.

install() sets:
  * sys.excepthook (and threading.excepthook): the traceback goes to the Log tab, to
    <pyFIB>/logs/pyfib_gui_errors.log, and to a warning box; pyFIB keeps running. The
    action that failed stops where it failed (nothing is retried).
  * faulthandler -> <pyFIB>/logs/pyfib_faults.log, so a hard crash inside Qt / C code
    still leaves a Python stack to look at.
Instrument actions run in their own processes (the CLI); their errors are unaffected.

v0.55.0 (Evan, 2026-11-05, with pyMap v0.9.2: "ensure there will not be any silent
crashes"):
  * An error raised in another thread is passed to the GUI thread (queued signal)
    before it touches the Log tab or the status bar: changing a Qt widget from a
    worker thread can itself crash natively.
  * Works without a console (pythonw / a shortcut: sys.stderr is None): the hook no
    longer raises inside itself.
  * sys.unraisablehook: errors Python cannot raise anywhere (in __del__, in callbacks
    during shutdown) are written to the errors log instead of a console nobody sees.
  * A Qt message handler writes Qt's critical and FATAL messages (a fatal one aborts the
    process right after, e.g. "QThread: Destroyed while thread is still running") and,
    briefly, its warnings to the errors log; it calls the handler that was there before.
    pyMap (v0.9.2) installs its own on top and calls this one.
  * Run marker logs/pyfib_run_<pid>.json: written at start, updated every 60 s and on
    each page change and command, marked "closed" at a normal exit. A marker still
    "running" whose process is gone means pyFIB ended without closing (hard crash,
    kill, power loss): the next start says so in a box, in the Log tab and in the
    errors log, with what was running at the time.
  * pyFIB never uses faulthandler.dump_traceback_later: it is one timer per process and
    pyMap uses it for its hang dumps (logs/pymap_hangs.log).
"""

from __future__ import annotations

import faulthandler
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, List, Optional

ERRORS_LOG = "pyfib_gui_errors.log"
FAULTS_LOG = "pyfib_faults.log"
RUN_MARKER_PREFIX = "pyfib_run_"
HEARTBEAT_MS = 60_000
STALE_HEARTBEAT_S = 10 * 60          # a live pyFIB updates its marker every 60 s
MARKER_KEEP_S = 7 * 24 * 3600        # closed markers older than this are removed
QT_WARNING_REPEATS = 3               # each distinct Qt warning is logged this often
REPEAT_WINDOW_S = 60.0               # the same error (type + innermost line) is shown and
                                     # logged once per window; repeats are counted

_state = {"showing": False, "fault_file": None, "installed": False, "relay": None,
          "errors_log": None, "log_dir": None, "qt_handler": None, "qt_counts": {},
          "marker": None, "marker_path": None, "heartbeat": None,
          "previous_unclean": [], "lock": threading.RLock(), "recent": {}}


def _version() -> str:
    try:
        from .. import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001
        return "?"


def format_error(t, v, tb) -> str:
    return "".join(traceback.format_exception(t, v, tb))


def _stderr(text: str) -> None:
    """Write to the console if there is one (pythonw has sys.stderr = None)."""
    try:
        if sys.stderr is not None:
            sys.stderr.write(text)
    except Exception:  # noqa: BLE001
        pass


def _append_log(header: str, body: str = "") -> None:
    p = _state.get("errors_log")
    if p is None:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _state["lock"]:
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(f"\n=== {stamp} pyFIB {_version()} {header} ===\n"
                         + (body if body.endswith("\n") or not body else body + "\n"))
    except Exception:  # noqa: BLE001
        pass


def _on_gui_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def _make_relay(get_window):
    """A QObject living in the GUI thread: a signal emitted from any thread reaches the
    window through Qt's queued connection, never by touching widgets from that thread."""
    try:
        from qtpy.QtCore import QObject, Signal

        class _Relay(QObject):
            message = Signal(str, str)

        relay = _Relay()

        def deliver(log_text: str, status_text: str) -> None:
            try:
                win = get_window()
                if win is None:
                    return
                win.log_page.append(log_text)
                win.status(status_text)
            except Exception:  # noqa: BLE001
                pass

        relay.message.connect(deliver)
        relay._deliver = deliver          # keep the slot alive with the relay
        return relay
    except Exception:  # noqa: BLE001
        return None


def _error_key(t, tb) -> tuple:
    """Same error = same exception type raised at the same innermost file:line."""
    last = None
    try:
        for frame, lineno in traceback.walk_tb(tb):
            last = (frame.f_code.co_filename, lineno)
    except Exception:  # noqa: BLE001
        pass
    return (getattr(t, "__name__", str(t)),) + (last or ("?", 0))


def _first_in_window(key) -> Optional[int]:
    """None when this error was already reported within REPEAT_WINDOW_S (counted);
    else the number of repeats suppressed since it was last reported (0 = new)."""
    now = time.time()
    with _state["lock"]:
        rec = _state["recent"].get(key)
        if rec is not None and now - rec["t"] < REPEAT_WINDOW_S:
            rec["n"] += 1
            return None
        n = rec["n"] if rec is not None else 0
        _state["recent"][key] = {"t": now, "n": 0}
        if len(_state["recent"]) > 500:              # bounded
            for k in sorted(_state["recent"], key=lambda k: _state["recent"][k]["t"])[:250]:
                _state["recent"].pop(k, None)
        return n


# --------------------------------------------------------------------------- hooks
def install(get_window: Callable[[], Optional[object]], log_dir) -> Path:
    """Install the hooks. Returns the errors log path."""
    log_dir = Path(log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    errors_log = log_dir / ERRORS_LOG
    _state["errors_log"] = errors_log
    _state["log_dir"] = log_dir
    try:
        if _state["fault_file"] is None:
            f = open(log_dir / FAULTS_LOG, "a", encoding="utf-8")
            f.write(f"\n=== pyFIB {_version()} pid {os.getpid()} started "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            f.flush()
            faulthandler.enable(f, all_threads=True)
            _state["fault_file"] = f
    except Exception:  # noqa: BLE001
        pass
    if _state["relay"] is None and _on_gui_thread():
        _state["relay"] = _make_relay(get_window)

    def report(t, v, tb, where: str = "") -> None:
        # v0.55.0 (review): a repeating error (e.g. from a 150 ms timer) is shown and
        # logged once per minute, with a count -- it used to stack warning boxes faster
        # than they could be closed, keeping the operator from GISDEP's STOP.
        repeats = _first_in_window(_error_key(t, tb))
        if repeats is None:
            return
        text = format_error(t, v, tb)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if repeats:
            text += (f"(the same error was raised {repeats} more time(s) in the last "
                     f"{REPEAT_WINDOW_S:.0f} s; those were counted, not shown)\n")
        _append_log(where.strip() or "error", text)
        _stderr(text)
        log_text = (f"*** GUI ERROR {stamp} {where}(pyFIB kept running; the action "
                    f"that failed was stopped) ***\n{text.rstrip()}")
        status_text = f"Error: {t.__name__}: {v} -- see the Log tab"
        if not _on_gui_thread():
            relay = _state.get("relay")
            try:
                if relay is not None:
                    relay.message.emit(log_text, status_text)   # queued to the GUI thread
            except Exception:  # noqa: BLE001
                pass
            return                       # thread errors: logged only, no box
        win = None
        try:
            win = get_window()
        except Exception:  # noqa: BLE001
            win = None
        if win is None:
            return
        try:
            win.log_page.append(log_text)
            win.status(status_text)
        except Exception:  # noqa: BLE001
            pass
        if where or _state["showing"]:
            return                       # nested errors (while a box is up): logged only
        _state["showing"] = True
        try:
            from qtpy.QtWidgets import QMessageBox
            QMessageBox.warning(
                win, "pyFIB: an action failed",
                f"{t.__name__}: {v}\n\nThat action was stopped; pyFIB keeps running.\n"
                f"Details: the Log tab and {errors_log}")
        except Exception:  # noqa: BLE001
            pass
        finally:
            _state["showing"] = False

    def hook(t, v, tb):
        try:
            if issubclass(t, KeyboardInterrupt):
                sys.__excepthook__(t, v, tb)
                return
            report(t, v, tb)
        except Exception:  # noqa: BLE001   the hook itself must never raise
            pass

    def thread_hook(args):
        try:
            if args.exc_type is SystemExit:
                return
            report(args.exc_type, args.exc_value, args.exc_traceback,
                   where=f"thread {getattr(args.thread, 'name', '?')} ")
        except Exception:  # noqa: BLE001
            pass

    def unraisable_hook(u):
        try:
            text = format_error(u.exc_type, u.exc_value, u.exc_traceback)
            what = u.err_msg or "Exception ignored"
            try:
                obj = repr(u.object)[:200]
            except Exception:  # noqa: BLE001
                obj = "?"
            _append_log(f"unraisable ({what} in {obj})", text)
        except Exception:  # noqa: BLE001
            pass
        try:
            sys.__unraisablehook__(u)
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook
    threading.excepthook = thread_hook
    sys.unraisablehook = unraisable_hook
    install_qt_message_handler()
    _state["installed"] = True
    return errors_log


def install_qt_message_handler() -> bool:
    """Log Qt's critical / fatal messages (and the first few of each warning) to the
    errors log, then pass every message on to the handler that was there before (or to
    the console, Qt's default). Never raises."""
    if _state["qt_handler"] is not None:
        return True
    try:
        from qtpy.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:  # noqa: BLE001
        return False
    prev_box = [None]

    def handler(mode, context, message):
        try:
            fatal = mode == QtMsgType.QtFatalMsg
            if fatal or mode == QtMsgType.QtCriticalMsg:
                where = ""
                try:
                    if context is not None and context.file:
                        where = f" ({context.file}:{context.line})"
                except Exception:  # noqa: BLE001
                    pass
                _append_log("Qt FATAL (Qt aborts the process now)" if fatal else
                            "Qt critical", f"{message}{where}")
                if fatal:   # still "running": even after aboutToQuit, report it next start
                    update_run_marker(qt_fatal=str(message)[:300], state="running")
            elif mode == QtMsgType.QtWarningMsg:
                n = _state["qt_counts"].get(message, 0) + 1
                _state["qt_counts"][message] = n
                if n <= QT_WARNING_REPEATS:
                    _append_log("Qt warning" + (f" (seen {n}x)" if n > 1 else ""), str(message))
        except Exception:  # noqa: BLE001
            pass
        prev = prev_box[0]
        if prev is not None:
            try:
                prev(mode, context, message)
                return
            except Exception:  # noqa: BLE001
                pass
        _stderr(str(message) + "\n")

    try:
        prev_box[0] = qInstallMessageHandler(handler)
        _state["qt_handler"] = handler          # keep a reference for the process lifetime
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- run marker
def _pid_alive(pid: int) -> Optional[bool]:
    """True / False, or None when it can't be told (then nothing is reported)."""
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.GetExitCodeProcess.restype = wintypes.BOOL
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False if ctypes.get_last_error() == 87 else None   # 87: no such pid
            try:
                code = wintypes.DWORD()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return None
                return code.value == 259                  # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        return None


def _write_marker() -> None:
    p = _state.get("marker_path")
    m = _state.get("marker")
    if p is None or m is None:
        return
    try:
        with _state["lock"]:
            m["updated"] = time.time()
            m["updated_text"] = time.strftime("%Y-%m-%d %H:%M:%S")
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(m, indent=1), encoding="utf-8")
            for attempt in range(3):            # Windows: antivirus may hold the file
                try:
                    os.replace(str(tmp), str(p))
                    break
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05)
    except Exception:  # noqa: BLE001
        pass


def update_run_marker(**fields) -> None:
    """Record what pyFIB is doing (page, command, ...) in this run's marker."""
    m = _state.get("marker")
    if m is None:
        return
    try:
        with _state["lock"]:
            m.update(fields)
            _write_marker()
    except Exception:  # noqa: BLE001
        return


def describe_unclean(m: dict) -> str:
    parts = [f"pyFIB {m.get('version', '?')} (pid {m.get('pid', '?')}) started "
             f"{m.get('started_text', '?')}",
             f"last sign of life {m.get('updated_text', '?')}"]
    if m.get("page"):
        parts.append(f"tab: {m['page']}")
    cmd = m.get("command")
    if cmd:
        st = m.get("command_state", "?")
        parts.append(f"last command: {cmd} ({st})")
        cpid = m.get("command_pid")
        if st == "running" and cpid:
            alive = _pid_alive(cpid)
            if alive is True:
                parts.append(f"THAT COMMAND IS STILL RUNNING (pid {cpid}), without pyFIB "
                             f"reading it: let it finish or end it in Task Manager, and "
                             f"check SmartSEM")
            elif alive is False:
                parts.append(f"that command (pid {cpid}) is no longer running")
    if m.get("qt_fatal"):
        parts.append(f"Qt fatal message: {m['qt_fatal']}")
    return "; ".join(parts)


def check_previous_runs(log_dir) -> List[str]:
    """Summaries of earlier pyFIB runs that ended without closing. Their markers are
    removed once reported; old closed markers are tidied. Never raises."""
    out: List[str] = []
    try:
        d = Path(log_dir)
        now = time.time()
        for p in sorted(d.glob(RUN_MARKER_PREFIX + "*.json")):
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
                pid = int(m.get("pid", -1))
                if pid == os.getpid():
                    continue
                age = now - float(m.get("updated", now))
                if m.get("state") != "running":
                    if age > MARKER_KEEP_S:
                        p.unlink()
                    continue
                alive = _pid_alive(pid)
                if alive is True and age > STALE_HEARTBEAT_S:
                    alive = False          # that pid now belongs to another program
                if alive is not False:
                    continue               # still running (another pyFIB) or unknown
                out.append(describe_unclean(m))
                p.unlink()
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return out


def start_run_marker(log_dir) -> List[str]:
    """Report earlier unclean runs, then write this run's marker. Returns the earlier
    runs' summaries (also kept in _state["previous_unclean"])."""
    log_dir = Path(log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    prev = check_previous_runs(log_dir)
    _state["previous_unclean"] = list(prev)
    for s in prev:
        _append_log("PREVIOUS RUN ENDED WITHOUT CLOSING (hard crash, kill or power loss)", s)
    _state["marker_path"] = log_dir / f"{RUN_MARKER_PREFIX}{os.getpid()}.json"
    _state["marker"] = {"pid": os.getpid(), "version": _version(), "state": "running",
                        "started": time.time(),
                        "started_text": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "python": sys.version.split()[0], "executable": sys.executable}
    _write_marker()
    return prev


def start_heartbeat(app) -> None:
    """Update the marker every 60 s from the GUI thread ('last sign of life')."""
    if _state["heartbeat"] is not None or _state.get("marker") is None:
        return
    try:
        from qtpy.QtCore import QTimer
        t = QTimer(app)
        t.setInterval(HEARTBEAT_MS)
        t.timeout.connect(_write_marker)
        t.start()
        _state["heartbeat"] = t
    except Exception:  # noqa: BLE001
        pass


def close_run_marker() -> None:
    """A normal exit: the marker says 'closed' (kept a week, then tidied)."""
    if _state.get("marker") is None:
        return
    update_run_marker(state="closed", closed_text=time.strftime("%Y-%m-%d %H:%M:%S"))


# --------------------------------------------------------------------------- exit codes
_WINDOWS_CRASH = {
    0xC0000005: "ACCESS VIOLATION",
    0xC0000409: "STACK BUFFER OVERRUN / fail-fast",
    0xC0000374: "HEAP CORRUPTION",
    0xC00000FD: "STACK OVERFLOW",
    0xC000001D: "ILLEGAL INSTRUCTION",
    0xC0000094: "INTEGER DIVIDE BY ZERO",
    0xC0000135: "DLL NOT FOUND",
    0xC0000142: "DLL INITIALISATION FAILED",
    0xE06D7363: "unhandled C++ exception",
    0xE0434352: "unhandled .NET exception",
    0x80000003: "BREAKPOINT in native code",
    0x80000002: "DATATYPE MISALIGNMENT",
    0x40010004: "terminated (debugger / DBG_TERMINATE_PROCESS)",
}
_WINDOWS_INTERRUPT = {0xC000013A: "interrupted: Ctrl+C or the console was closed"}


def describe_crash_exit(rc: int) -> Optional[str]:
    """A description when a child's exit code means it CRASHED (not an ordinary
    pyFIB exit such as 1 = error or 3 = refused, and not a Ctrl+C), else None.
    A kill from Task Manager exits 1 and cannot be told from an ordinary error."""
    try:
        rc = int(rc)
    except Exception:  # noqa: BLE001
        return None
    if -128 < rc < 0:                                # POSIX: killed by a signal
        import signal
        if -rc in (getattr(signal, "SIGINT", 2), getattr(signal, "SIGTERM", 15)):
            return None                  # an interrupt / a stop request, not a crash
        try:
            name = signal.Signals(-rc).name
        except Exception:  # noqa: BLE001
            name = f"signal {-rc}"
        return f"killed by {name}"
    u = rc & 0xFFFFFFFF
    if u in _WINDOWS_INTERRUPT:
        return None
    if u in _WINDOWS_CRASH:
        return f"0x{u:08X} {_WINDOWS_CRASH[u]}"
    if u >= 0x80000000:
        return f"0x{u:08X} (Windows error / exception status)"
    return None


def describe_interrupt_exit(rc: int) -> Optional[str]:
    """'interrupted ...' for a Ctrl+C-type exit, else None."""
    try:
        rc = int(rc)
    except Exception:  # noqa: BLE001
        return None
    if rc == -2:
        return "interrupted (SIGINT)"
    if rc == -15:
        return "stopped (SIGTERM)"
    u = rc & 0xFFFFFFFF
    return _WINDOWS_INTERRUPT.get(u)
