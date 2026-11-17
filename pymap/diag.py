"""pyMap diagnostics: a crash log that says what pyMap was doing (v0.9.2).

Evan saw pyFIB close or show errors "associated with pyMap" with no record of what
was happening. This module writes ``pymap.log`` so the next crash can be traced:

* **Where:** ``<folder holding pymap>/logs/pymap.log``. Bundled in pyFIB that is
  ``<pyFIB>/logs/``, beside pyFIB's ``pyfib_gui_errors.log`` and ``pyfib_faults.log``.
  Override with ``build_widget(log_dir=...)``, ``--log-dir`` or env ``PYMAP_LOG_DIR``.
  Rotates at 5 MB, 5 old files kept.
* **What:**
  - one header per process (versions of Python, Qt and every image library, the host);
  - a BEGIN line *before* every user action, and an END (duration, memory) or FAIL
    (full traceback + pyMap's state) after it. Each line is flushed at once, so after
    a hard crash the last BEGIN without an END names the action that was running;
  - image loads, registrations and warps with their sizes and timings;
  - Python warnings raised inside pyMap actions (e.g. NaN / divide-by-zero);
  - Qt's own warnings / fatal messages (chained to any handler already installed);
  - an action still running after ``HANG_S`` seconds dumps every thread's stack to
    ``pymap_hangs.log`` (a "Not responding" window that gets killed looks like a crash);
  - a per-process run marker: the next start reports a previous run that ended
    without closing (a hard crash or a kill), with its last action.
* **Never harmful:** nothing here raises into the host; every hook chains to the one
  already installed; ``PYMAP_DIAG=0`` turns all of it off (no files, no hooks).
  faulthandler is left to the host when it already has it on (pyFIB does).

Importing this module needs only the standard library (no Qt, no numpy).
"""

from __future__ import annotations

import atexit
import contextlib
import faulthandler
import inspect
import json
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
import traceback
import warnings
import weakref
from collections import deque
from pathlib import Path
from typing import Callable, Optional

LOGGER_NAME = "pymap"
LOG_FILE = "pymap.log"
HANGS_FILE = "pymap_hangs.log"
FAULTS_FILE = "pymap_faults.log"          # standalone only; pyFIB keeps its own
RUN_MARKER_PREFIX = "pymap_run_"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5
HANG_S = 30.0                             # stack dump if one action runs this long
HEARTBEAT_MS = 30_000
SLOW_S = 1.0                              # END lines of quick actions are DEBUG
QT_REPEAT_LIMIT = 20                      # identical Qt messages logged at most this often
MARKER_MAX_AGE_S = 14 * 86400

log = logging.getLogger(LOGGER_NAME)
log.addHandler(logging.NullHandler())     # a library: no "no handler" noise when unused

_state = {
    "setup": False, "disabled": False, "log_dir": None, "log_path": None,
    "embedded": None, "hang_file": None, "fault_file": None, "qt_handler": False,
    "excepthook": False, "marker": None, "previous_unclean": [], "depth": 0,
    "heartbeat": None, "qt_counts": {}, "warn_seen": set(),
}
_lock = threading.RLock()
_recent: deque = deque(maxlen=60)         # recent quiet events, dumped with a failure


def enabled() -> bool:
    return os.environ.get("PYMAP_DIAG", "1").strip().lower() not in ("0", "off", "no", "false")


# --------------------------------------------------------------------------- #
# log file
# --------------------------------------------------------------------------- #
class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotation that never stops logging: on Windows another process (e.g. pyFIB and
    a standalone pyMap) may hold the file, so a failed rename keeps appending and is
    retried a minute later instead of on every line."""

    _retry_at = 0.0

    def shouldRollover(self, record):
        if time.monotonic() < self._retry_at:
            return False
        try:
            return super().shouldRollover(record)
        except Exception:  # noqa: BLE001
            return False

    def doRollover(self):
        """Move the full log aside FIRST; shift the backups only if that worked.
        (The stdlib shifts .1 -> .2 ... and deletes the oldest before renaming the
        base file, so with the file held by another process on Windows every retry
        would delete one more backup while the log kept growing.)"""
        if self.stream:
            try:
                self.stream.close()
            except Exception:  # noqa: BLE001
                pass
            self.stream = None
        base = self.baseFilename
        aside = "%s.rotating.%d" % (base, os.getpid())
        try:
            os.replace(base, aside)
        except Exception:  # noqa: BLE001
            self._retry_at = time.monotonic() + 60.0
        else:
            try:
                for i in range(self.backupCount - 1, 0, -1):
                    src, dst = "%s.%d" % (base, i), "%s.%d" % (base, i + 1)
                    if os.path.exists(src):
                        os.replace(src, dst)
                os.replace(aside, base + ".1")
            except Exception:  # noqa: BLE001
                self._retry_at = time.monotonic() + 60.0
        try:
            self.stream = self._open()
        except Exception:  # noqa: BLE001
            self.stream = None

    def handleError(self, record):          # never print "--- Logging error ---" storms
        pass


class _Formatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        t = time.localtime(record.created)
        return time.strftime("%Y-%m-%d %H:%M:%S", t) + ".%03d" % record.msecs


def default_log_dir() -> Path:
    """<folder holding the pymap package>/logs (pyFIB's logs folder when bundled)."""
    return Path(__file__).resolve().parent.parent / "logs"


def _fallback_dirs():
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        yield Path(os.environ["LOCALAPPDATA"]) / "pyMap" / "logs"
    yield Path.home() / ".cache" / "pymap" / "logs"
    import tempfile
    yield Path(tempfile.gettempdir()) / "pymap_logs"


def _writable_dir(candidates) -> Optional[Path]:
    for d in candidates:
        if d is None:
            continue
        try:
            d = Path(d)
            d.mkdir(parents=True, exist_ok=True)
            probe = d / (".pymap_write_test_%d" % os.getpid())
            probe.write_text("x")
            probe.unlink()
            return d
        except Exception:  # noqa: BLE001
            continue
    return None


def log_path() -> Optional[str]:
    return _state["log_path"]


def log_dir() -> Optional[str]:
    return _state["log_dir"]


def previous_unclean() -> list:
    """Summaries of earlier runs that ended without closing (found at setup)."""
    return list(_state["previous_unclean"])


def setup(log_dir=None, *, embedded: Optional[bool] = None, qt: bool = True) -> Optional[str]:
    """Start logging (idempotent: later calls only fill in what was missing).
    Returns the log file path, or None when diagnostics are off or no folder is
    writable. Never raises."""
    try:
        with _lock:
            if not enabled():
                _state["disabled"] = True
                log.propagate = False      # nothing reaches the host's root logger
                return None
            if embedded is not None and _state["embedded"] is None:
                _state["embedded"] = bool(embedded)
            if not _state["setup"]:
                _setup_file(log_dir)
                if _state["log_path"]:
                    _write_header()
                    _setup_faults_and_hangs()
                    _check_previous_runs()
                    _write_marker(state="running")
                    atexit.register(_on_exit)
                _state["setup"] = True
            if qt:
                _install_qt_message_handler()
                _start_heartbeat()
            _chain_excepthook()
            return _state["log_path"]
    except Exception:  # noqa: BLE001
        return _state.get("log_path")


def _setup_file(log_dir):
    env = os.environ.get("PYMAP_LOG_DIR")
    d = _writable_dir([log_dir, env, default_log_dir(), *_fallback_dirs()])
    if d is None:
        return
    path = d / LOG_FILE
    h = _SafeRotatingFileHandler(str(path), maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                 encoding="utf-8", delay=False)
    h.setFormatter(_Formatter("%(asctime)s pid=%(process)d %(levelname)-7s %(name)s: %(message)s"))
    level = os.environ.get("PYMAP_LOG_LEVEL", "INFO").upper()
    h.setLevel(getattr(logging, level, logging.INFO))
    log.addHandler(h)
    log.setLevel(logging.DEBUG)
    log.propagate = False                  # pyMap's lines stay in pymap.log
    _state["log_dir"] = str(d)
    _state["log_path"] = str(path)


def _version_of(dist: str, module: str = None) -> str:
    try:
        from importlib import metadata
        return metadata.version(dist)
    except Exception:  # noqa: BLE001
        pass
    m = sys.modules.get(module or dist)
    return str(getattr(m, "__version__", "not installed" if m is None else "?"))


def environment() -> dict:
    """Versions and platform facts (no imports of heavy packages)."""
    from . import __version__
    env = {
        "pymap": __version__,
        "python": sys.version.split()[0] + " (%s)" % platform.architecture()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "embedded": _state["embedded"],
    }
    for dist, mod in (("numpy", None), ("scikit-image", "skimage"), ("scipy", None),
                      ("Pillow", "PIL"), ("tifffile", None), ("imagecodecs", None),
                      ("readlif", None), ("QtPy", "qtpy"), ("PyQt5", None)):
        env[dist] = _version_of(dist, mod)
    try:
        qtcore = sys.modules.get("PyQt5.QtCore")
        if qtcore is not None:
            env["Qt"] = qtcore.QT_VERSION_STR
    except Exception:  # noqa: BLE001
        pass
    host = sys.modules.get("smartfib_executor")
    if host is not None:
        env["host"] = "pyFIB %s" % getattr(host, "__version__", "?")
    env["faulthandler"] = ("on (host)" if faulthandler.is_enabled() and not _state["fault_file"]
                           else "on (pymap_faults.log)" if _state["fault_file"] else "off")
    mem = memory()
    if mem:
        env["memory"] = mem
    return env


def _write_header():
    log.info("=" * 70)
    try:
        env = environment()
        log.info("pyMap %s started: %s", env.get("pymap"),
                 ", ".join("%s=%s" % (k, v) for k, v in env.items() if k != "pymap"))
    except Exception as e:  # noqa: BLE001
        log.info("pyMap started (environment unavailable: %s)", e)


# --------------------------------------------------------------------------- #
# memory (process working set / private bytes, system available)
# --------------------------------------------------------------------------- #
_mem_broken = [os.environ.get("PYMAP_NO_MEMINFO") == "1"]


def memory() -> str:
    """'ws 812 MB (peak 1.4 GB), private 900 MB, free 5.1 GB' or '' if unknown."""
    if _mem_broken[0]:
        return ""
    try:
        if os.name == "nt":
            return _memory_windows()
        return _memory_linux()
    except Exception:  # noqa: BLE001
        _mem_broken[0] = True
        return ""


def _mb(n) -> str:
    n = float(n)
    return "%.1f GB" % (n / 2 ** 30) if n >= 2 ** 30 else "%.0f MB" % (n / 2 ** 20)


_win = {}


def _memory_windows() -> str:
    import ctypes
    from ctypes import wintypes
    if not _win:
        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        class MSX(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetCurrentProcess.argtypes = []
        k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC),
                                                wintypes.DWORD]
        k32.GlobalMemoryStatusEx.restype = wintypes.BOOL
        k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MSX)]
        _win.update(k32=k32, PMC=PMC, MSX=MSX)
    k32, PMC, MSX = _win["k32"], _win["PMC"], _win["MSX"]
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    out = []
    if k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        out.append("ws %s (peak %s), private %s" % (_mb(pmc.WorkingSetSize),
                                                     _mb(pmc.PeakWorkingSetSize),
                                                     _mb(pmc.PagefileUsage)))
    msx = MSX()
    msx.dwLength = ctypes.sizeof(MSX)
    if k32.GlobalMemoryStatusEx(ctypes.byref(msx)):
        out.append("free %s of %s" % (_mb(msx.ullAvailPhys), _mb(msx.ullTotalPhys)))
    return ", ".join(out)


def _memory_linux() -> str:
    vals = {}
    with open("/proc/self/status") as f:
        for line in f:
            k, _, v = line.partition(":")
            if k in ("VmRSS", "VmHWM"):
                vals[k] = int(v.split()[0]) * 1024
    out = []
    if "VmRSS" in vals:
        out.append("rss %s (peak %s)" % (_mb(vals["VmRSS"]), _mb(vals.get("VmHWM", 0))))
    try:
        with open("/proc/meminfo") as f:
            mi = dict(l.split(":", 1) for l in f if ":" in l)
        out.append("free %s" % _mb(int(mi["MemAvailable"].split()[0]) * 1024))
    except Exception:  # noqa: BLE001
        pass
    return ", ".join(out)


# --------------------------------------------------------------------------- #
# hard crashes and hangs
# --------------------------------------------------------------------------- #
def _setup_faults_and_hangs():
    d = Path(_state["log_dir"])
    if not faulthandler.is_enabled() and _state["embedded"] is not True:
        try:
            f = open(d / FAULTS_FILE, "a", encoding="utf-8")
            f.write("\n=== pyMap pid %d started %s ===\n" % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")))
            f.flush()
            faulthandler.enable(f, all_threads=True)
            _state["fault_file"] = f
        except Exception:  # noqa: BLE001
            pass
    try:
        _state["hang_file"] = open(d / HANGS_FILE, "a", encoding="utf-8")
    except Exception:  # noqa: BLE001
        _state["hang_file"] = None


def hang_dumps_enabled() -> bool:
    """faulthandler.dump_traceback_later is ONE timer per process: pyMap arms it
    around its own actions, which would replace a host's watchdog. pyFIB has none
    (v0.54.3); a host that adds one sets PYMAP_HANG_DUMPS=0."""
    return os.environ.get("PYMAP_HANG_DUMPS", "1") != "0"


def _arm_hang(name: str):
    f = _state["hang_file"]
    if f is None or not hang_dumps_enabled():
        return
    try:
        faulthandler.dump_traceback_later(HANG_S, repeat=False, file=f, exit=False)
    except Exception:  # noqa: BLE001
        pass


def _disarm_hang():
    if _state["hang_file"] is None or not hang_dumps_enabled():
        return
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# run markers: find runs that ended without closing
# --------------------------------------------------------------------------- #
def _marker_path(pid=None) -> Optional[Path]:
    if not _state["log_dir"]:
        return None
    return Path(_state["log_dir"]) / ("%s%d.json" % (RUN_MARKER_PREFIX, pid or os.getpid()))


def _write_marker(**fields):
    p = _marker_path()
    if p is None:
        return
    try:
        with _lock:
            m = _state["marker"] or {"pid": os.getpid(), "started": time.time(),
                                     "started_text": time.strftime("%Y-%m-%d %H:%M:%S")}
            m.update(fields)
            m["updated"] = time.time()
            _state["marker"] = m
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(m))
            for attempt in range(3):        # Windows: a reader or antivirus may hold it
                try:
                    os.replace(str(tmp), str(p))
                    break
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05)
    except Exception:  # noqa: BLE001
        pass


def _pid_alive(pid: int) -> Optional[bool]:
    """True / False, or None when it can't be told (then nothing is reported)."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.GetExitCodeProcess.restype = wintypes.BOOL
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
            if not h:
                return False if ctypes.get_last_error() == 87 else None   # 87: no such pid
            try:
                code = wintypes.DWORD()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return None
                return code.value == 259                    # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        os.kill(int(pid), 0)                                 # POSIX only: signal 0 tests
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        return None


def _check_previous_runs():
    d = Path(_state["log_dir"])
    now = time.time()
    for p in sorted(d.glob(RUN_MARKER_PREFIX + "*.json")):
        try:
            m = json.loads(p.read_text())
            pid = int(m.get("pid", -1))
            if pid == os.getpid():
                continue
            age = now - float(m.get("updated", now))
            if m.get("state") != "running":
                if age > MARKER_MAX_AGE_S:
                    p.unlink()
                continue
            alive = _pid_alive(pid)
            if alive is True and m.get("heartbeat") and age > 10 * 60:
                # a live pyMap GUI updates its marker every 30 s: this pid now belongs
                # to another program (reused after a reboot or a crash)
                alive = False
            if alive is not False:
                if age > MARKER_MAX_AGE_S:
                    p.unlink()
                continue
            summary = _describe_unclean(m)
            _state["previous_unclean"].append(summary)
            log.warning("PREVIOUS RUN ENDED WITHOUT CLOSING (hard crash, kill or power "
                        "loss): %s", summary)
            p.unlink()
        except Exception:  # noqa: BLE001
            continue


def _describe_unclean(m: dict) -> str:
    def t(v):
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(v)))
        except Exception:  # noqa: BLE001
            return "?"
    parts = ["pid %s started %s" % (m.get("pid"), m.get("started_text", "?"))]
    if m.get("host"):
        parts.append("host %s" % m["host"])
    parts.append("last sign of life %s" % t(m.get("updated")))
    act = m.get("action")
    if act:
        if m.get("action_done"):
            parts.append("last action '%s' had finished (%s)" % (act, m.get("action_result", "ok")))
        else:
            parts.append("action '%s' was RUNNING (started %s)" % (act, t(m.get("action_t"))))
    return "; ".join(parts)


def _on_exit():
    _write_marker(state="closed")
    try:
        log.info("pyMap process exiting normally")
    except Exception:  # noqa: BLE001
        pass


def _start_heartbeat():
    """Update the run marker every 30 s from the GUI thread ('last sign of life')."""
    if _state["heartbeat"] is not None or not _state["log_path"]:
        return
    try:
        from qtpy.QtCore import QTimer, QCoreApplication
        app = QCoreApplication.instance()
        if app is None:
            return
        t = QTimer(app)
        t.setInterval(HEARTBEAT_MS)
        t.timeout.connect(lambda: _write_marker())
        t.start()
        _state["heartbeat"] = t
        host = sys.modules.get("smartfib_executor")
        _write_marker(heartbeat=True, host=("pyFIB %s" % getattr(host, "__version__", "?")) if host else
                      ("standalone" if _state["embedded"] is False else "?"))
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Qt messages and uncaught exceptions (both chained to what was there)
# --------------------------------------------------------------------------- #
def _install_qt_message_handler():
    if _state["qt_handler"] or os.environ.get("PYMAP_QT_MESSAGES", "1") == "0":
        return
    try:
        from qtpy.QtCore import qInstallMessageHandler, QtMsgType
    except Exception:  # noqa: BLE001
        return
    names = {QtMsgType.QtDebugMsg: "debug", QtMsgType.QtWarningMsg: "warning",
             QtMsgType.QtCriticalMsg: "critical", QtMsgType.QtFatalMsg: "FATAL"}
    try:
        names[QtMsgType.QtInfoMsg] = "info"
    except Exception:  # noqa: BLE001
        pass
    prev_box = [None]

    def handler(mode, context, message):
        try:
            kind = names.get(mode, str(mode))
            n = _state["qt_counts"].get(message, 0) + 1
            _state["qt_counts"][message] = n
            if mode == QtMsgType.QtFatalMsg:
                log.critical("[qt %s] %s  (Qt aborts the process after this)", kind, message)
                _write_marker(action="Qt fatal: %s" % message[:200], action_done=False,
                              action_t=time.time())
            elif n <= QT_REPEAT_LIMIT or n % 1000 == 0:
                where = ""
                try:
                    if context is not None and context.file:
                        where = " (%s:%s)" % (context.file, context.line)
                except Exception:  # noqa: BLE001
                    pass
                lvl = logging.DEBUG if mode == QtMsgType.QtDebugMsg else logging.WARNING
                log.log(lvl, "[qt %s] %s%s%s", kind, message, where,
                        "  (repeated %d times)" % n if n > 1 else "")
        except Exception:  # noqa: BLE001
            pass
        prev = prev_box[0]
        if prev is not None:
            try:
                prev(mode, context, message)
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            sys.stderr.write(message + "\n")       # Qt's default output
        except Exception:  # noqa: BLE001
            pass

    try:
        prev_box[0] = qInstallMessageHandler(handler)
        _state["qt_handler"] = True
    except Exception:  # noqa: BLE001
        pass


def _is_pymap_traceback(tb) -> bool:
    marker = os.sep + "pymap" + os.sep
    for frame, _ in traceback.walk_tb(tb):
        if marker in frame.f_code.co_filename or "/pymap/" in frame.f_code.co_filename:
            return True
    return False


def _chain_excepthook():
    """Log uncaught exceptions that went through pyMap code, then hand them to the
    hook that was installed (pyFIB's error box, or pyMap's standalone one)."""
    current = sys.excepthook
    if getattr(current, "_pymap_logging", False):
        return

    def hook(t, v, tb, _prev=current):
        try:
            if tb is not None and _is_pymap_traceback(tb):
                log.error("UNCAUGHT exception (passed on to %s):\n%s",
                          getattr(_prev, "__module__", "?"),
                          "".join(traceback.format_exception(t, v, tb)).rstrip())
        except Exception:  # noqa: BLE001
            pass
        _prev(t, v, tb)

    hook._pymap_logging = True
    sys.excepthook = hook
    _state["excepthook"] = True


# --------------------------------------------------------------------------- #
# actions: BEGIN / END / FAIL around every user action
# --------------------------------------------------------------------------- #
def _short(v, n=120) -> str:
    try:
        s = repr(v)
    except Exception:  # noqa: BLE001
        s = "<unrepresentable %s>" % type(v).__name__
    return s if len(s) <= n else s[: n - 3] + "..."


def note(msg: str, *args):
    """A quiet event kept in memory and written out only with a failure."""
    try:
        _recent.append("%s %s" % (time.strftime("%H:%M:%S"), (msg % args) if args else msg))
    except Exception:  # noqa: BLE001
        pass


def _log_warnings(name, caught):
    for w in caught:
        try:
            key = (w.category.__name__, str(w.message), w.filename, w.lineno)
            if key in _state["warn_seen"]:
                continue
            _state["warn_seen"].add(key)
            log.warning("%s: %s: %s (%s:%s)", name, w.category.__name__, w.message,
                        w.filename, w.lineno)
        except Exception:  # noqa: BLE001
            pass


class ActionFailed(Exception):
    """Raised by run_action(reraise=False) callers that want to know it failed."""


def _state_text(state_fn) -> str:
    if state_fn is None:
        return ""
    try:
        s = state_fn()
        if isinstance(s, dict):
            return "; ".join("%s=%s" % (k, v) for k, v in s.items())
        return str(s)
    except Exception as e:  # noqa: BLE001
        return "(state unavailable: %s)" % e


@contextlib.contextmanager
def action(name: str, *, detail: str = "", state: Callable[[], object] = None,
           quiet: bool = False, boundary: bool = False, hang: bool = True):
    """BEGIN/END/FAIL lines around a block. quiet=True (drags, slider moves) keeps
    only an in-memory note unless it fails. Exceptions propagate; the FAIL line is
    written once, by the outermost action or by a boundary (a slot that swallows
    the exception, since PyQt never passes a slot's exception back to emit())."""
    outer = _state["depth"] == 0
    _state["depth"] += 1
    t0 = time.perf_counter()
    if quiet:
        note("%s %s", name, detail)
    else:
        log.info("BEGIN %s%s%s", name, " | " if detail else "", detail)
        if outer:
            _write_marker(action=name, action_t=time.time(), action_done=False)
            if hang:
                _state["hang_armed"] = True
                _arm_hang(name)
    caught = []
    # Python's warning filters are process-wide and not thread-safe, so warnings are
    # only recorded around outermost, non-quiet actions of a standalone pyMap (a host
    # such as pyFIB keeps its own warning handling)
    record = outer and not quiet and _state["embedded"] is not True
    try:
        if record:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("default")
                yield
        else:
            yield
        _log_warnings(name, caught)
        dt = time.perf_counter() - t0
        if not quiet:
            lvl = logging.INFO if (dt >= SLOW_S or outer) else logging.DEBUG
            log.log(lvl, "END   %s ok %.2f s%s%s", name, dt,
                    (" | " + memory()) if outer else "",
                    "  (longer than %.0f s: all thread stacks were dumped to %s)"
                    % (HANG_S, HANGS_FILE) if (outer and hang and dt > HANG_S
                                               and hang_dumps_enabled()) else "")
            if outer:
                _write_marker(action_done=True, action_result="ok %.2f s" % dt)
        elif dt >= SLOW_S:
            log.info("slow  %s %.2f s", name, dt)
    except BaseException as e:
        dt = time.perf_counter() - t0
        _log_warnings(name, caught or [])
        if (outer or boundary) and not getattr(e, "_pymap_logged", False):
            try:
                recent = "\n    ".join(list(_recent)[-15:])
                log.error("FAIL  %s after %.2f s: %s: %s%s\n  state: %s\n  recent:\n    %s\n%s",
                          name, dt, type(e).__name__, e,
                          (" | " + detail) if detail else "", _state_text(state),
                          recent or "(none)", traceback.format_exc().rstrip())
                e._pymap_logged = True
            except Exception:  # noqa: BLE001
                pass
            if outer:
                _write_marker(action_done=True, action_result="FAILED %s" % type(e).__name__)
        raise
    finally:
        _state["depth"] -= 1
        if outer and not quiet and hang:
            _disarm_hang()
            _state["hang_armed"] = False


@contextlib.contextmanager
def hang_watch(name: str):
    """Arm the hang dump around just the work of an action whose slot opens a file
    dialog (the slot is guarded with hang=False: browsing for 30 s is not a hang)."""
    armed = _state["depth"] <= 1 and not _state.get("hang_armed")
    if armed:
        _state["hang_armed"] = True
        _arm_hang(name)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        if armed:
            _disarm_hang()
            _state["hang_armed"] = False
            dt = time.perf_counter() - t0
            if dt > HANG_S:
                log.info("slow  %s %.1f s (all thread stacks were dumped to %s)",
                         name, dt, HANGS_FILE)


# --------------------------------------------------------------------------- #
# guarded Qt slots
# --------------------------------------------------------------------------- #
def _n_positional(fn) -> Optional[int]:
    """How many signal arguments to pass, as PyQt would: all positional parameters
    the callable accepts (None = all of them, for *args or uninspectable).
    Note: C++ (sip) methods such as ``widget.hide`` can't be inspected, so they get
    every argument; wrap them in a lambda (``lambda *_: w.hide()``)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    n = 0
    for p in sig.parameters.values():
        if p.kind == p.VAR_POSITIONAL:
            return None
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            n += 1
    return n


def slot(name: str, fn: Callable, *, owner=None, quiet: bool = False,
         state: Callable[[], object] = None, hang: bool = True):
    """A Qt slot that never lets an exception reach Qt or the host.

    The failure is logged with a traceback and pyMap's state, and shown through
    ``owner.report_error(name, exc)`` (a weakly held widget) after the event
    handler has returned. quiet=True for high-rate signals (drag, slider)."""
    n_args = _n_positional(fn)
    ref = weakref.ref(owner) if owner is not None else None

    def wrapper(*args):
        a = args if n_args is None else args[:n_args]
        o = ref() if ref is not None else None
        st = state or (getattr(o, "diag_state", None) if o is not None else None)
        try:
            detail = ", ".join(_short(x, 80) for x in a) if (a and not quiet) else ""
            with action(name, detail=detail, state=st, quiet=quiet, boundary=True,
                        hang=hang):
                return fn(*a)
        except Exception as e:  # noqa: BLE001
            report_to_owner(o, name, e)
            return None

    wrapper.__name__ = "pymap_slot_" + name.replace(" ", "_")
    wrapper.__wrapped__ = fn
    return wrapper


def report_to_owner(owner, name, exc):
    if owner is None:
        return
    try:
        fn = getattr(owner, "report_error", None)
        if fn is not None:
            fn(name, exc)
    except Exception:  # noqa: BLE001
        pass


def guard_event(name: str):
    """Decorator for overridden Qt event handlers (mouse, wheel, paint): an
    exception is logged and swallowed instead of reaching Qt."""
    def deco(method):
        def wrapper(self, *args):
            try:
                return method(self, *args)
            except Exception as e:  # noqa: BLE001
                try:
                    log.error("FAIL  event %s on %s: %s: %s\n  recent:\n    %s\n%s", name,
                              type(self).__name__, type(e).__name__, e,
                              "\n    ".join(list(_recent)[-15:]) or "(none)",
                              traceback.format_exc().rstrip())
                except Exception:  # noqa: BLE001
                    pass
                report_to_owner(getattr(self, "error_owner", None), name, e)
                return None
        wrapper.__name__ = method.__name__
        wrapper.__doc__ = method.__doc__
        return wrapper
    return deco
