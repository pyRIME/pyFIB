"""Subprocess worker for the Qt GUI.

Same model pyFIB has always used: the GUI shells out to the existing CLI
(``python -u -m ...``) and streams the child's stdout line-by-line. Here that runs
on a ``QThread`` and forwards each line as a Qt signal, so the engine, the CLI, and
all the safety logic are untouched -- this is a re-skin, not a rewrite.

The worker is import-safe without Qt (it degrades to a no-op base) so the rest of the
package can be imported and unit-tested where Qt is absent; the real class is used
only when Qt is available.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

try:  # Qt is optional at import time
    from qtpy.QtCore import QThread, Signal
    _HAVE_QT = True
except Exception:  # pragma: no cover - exercised only where Qt is absent
    QThread = object  # type: ignore
    Signal = None  # type: ignore
    _HAVE_QT = False


def build_argv(subcmd_args: List[str]) -> List[str]:
    """Build the argv for a pyFIB CLI subcommand, unbuffered for live streaming.

    ``-u`` matters: without it the child block-buffers stdout when it is a pipe, so
    the GUI would only see output at process exit (the buffering bug we fixed once
    already). Kept here so every Qt-launched command streams live.
    """
    return [sys.executable, "-u", "-m", "smartfib_executor.cli", *subcmd_args]


def build_module_argv(module: str, args: List[str]) -> List[str]:
    """argv for a standalone pyFIB module (e.g. the coincidence tool), unbuffered."""
    return [sys.executable, "-u", "-m", module, *args]


if _HAVE_QT:

    class SubprocessWorker(QThread):
        """Run a CLI command off the UI thread, streaming stdout as signals."""

        line = Signal(str)               # one line of child stdout (newline stripped)
        finished_rc = Signal(int)        # process return code when it exits
        started_pid = Signal(int)        # v0.55.0: the child's pid (for the run marker)
        failed = Signal(str)             # a launch/read error (not a child error)

        def __init__(self, argv: List[str], cwd: Optional[Path] = None,
                     input_text: Optional[str] = None, parent=None):
            super().__init__(parent)
            self._argv = list(argv)
            self._cwd = cwd
            self._input_text = input_text
            self._proc: Optional[subprocess.Popen] = None
            self._stop = False

        def run(self) -> None:  # QThread entry point
            # v0.55.0: the child writes UTF-8 (PYTHONIOENCODING) and the GUI decodes it
            # with errors="replace". Before, both used the Windows code page: a byte that
            # code page cannot decode raised in this loop, the GUI reported the command
            # as finished (exit 1) while the child kept running unread -- and once the
            # pipe filled, the child blocked mid-batch. A child printing a character
            # outside the code page (a path, a unit) crashed on its own print.
            try:
                self.line.emit("$ " + " ".join(self._argv))
                # backslashreplace: even a lone surrogate cannot crash the child's print
                env = dict(os.environ, PYTHONIOENCODING="utf-8:backslashreplace")
                self._proc = subprocess.Popen(
                    self._argv,
                    cwd=str(self._cwd) if self._cwd else None,
                    stdin=(subprocess.PIPE if self._input_text is not None else None),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )
            except Exception as exc:  # noqa: BLE001   could not start the command
                self.failed.emit(f"could not start the command: {exc}")
                self.finished_rc.emit(1)
                return
            try:
                self.started_pid.emit(int(self._proc.pid))
            except Exception:  # noqa: BLE001
                pass
            if self._stop:                   # a stop requested before the child existed
                try:
                    self._proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
            try:
                if self._input_text is not None and self._proc.stdin is not None:
                    try:
                        self._proc.stdin.write(self._input_text)
                        self._proc.stdin.flush()
                        self._proc.stdin.close()
                    except Exception:
                        pass
                assert self._proc.stdout is not None
                # Read to EOF even after a stop request: a child that is still alive
                # must never block on a full pipe while wait() waits for it.
                for raw in self._proc.stdout:
                    self.line.emit(raw.rstrip("\n"))
            except Exception as exc:  # noqa: BLE001
                # Reading failed: say so, but keep the child's pipe drained and report
                # its REAL exit code -- never "finished" while it is still running.
                self.failed.emit(f"reading the command's output failed: {exc}; "
                                 f"waiting for the command to end")
                try:
                    out = self._proc.stdout
                    while out is not None and out.read(65536):
                        pass
                except Exception:  # noqa: BLE001
                    pass
            try:
                rc = self._proc.wait()
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(f"could not read the command's exit code: {exc}")
                rc = 1
            self.finished_rc.emit(int(rc))

        def request_stop(self) -> None:
            """Ask the child to stop (best-effort terminate)."""
            self._stop = True
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass

else:  # pragma: no cover - placeholder when Qt is unavailable

    class SubprocessWorker:  # type: ignore
        def __init__(self, *a, **k):
            raise RuntimeError(
                "SubprocessWorker requires qtpy/PyQt5, which is not installed here.")
