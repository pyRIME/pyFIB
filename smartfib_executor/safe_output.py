"""Output that can never fail a command (v0.55.1, bug #16 path 4).

A pyFIB command started from the GUI prints into a pipe that the pyFIB window reads. If
the window dies (a crash in an embedded tool, Task Manager), the pipe closes and the next
``print`` raises BrokenPipeError / OSError. Inside a milling batch that error used to
leave the site in the middle of a mill and run its clean-up (the beam-shift reset) while
SmartFIB was still milling.

``install()`` wraps sys.stdout / sys.stderr so a write to a closed pipe is dropped instead
of raised, and remembers that it happened. ``output_lost()`` lets a batch stop at the next
safe point (before the next stage move). The run log's console copy
(``runs/run_<time>.log``) wraps these streams, so it keeps recording after the loss.

Nothing here touches the microscope.
"""

from __future__ import annotations

import sys
import threading
from typing import Optional

_lock = threading.Lock()
_state = {"lost": False, "why": None}


def _mark_lost(name: str, exc: BaseException) -> None:
    with _lock:
        if not _state["lost"]:
            _state["lost"] = True
            _state["why"] = f"{name}: {type(exc).__name__}: {exc}"


def output_lost() -> bool:
    """True once a write to stdout / stderr has failed (the reader is gone)."""
    return bool(_state["lost"])


def lost_reason() -> Optional[str]:
    return _state["why"]


def _reset_for_tests() -> None:
    with _lock:
        _state["lost"] = False
        _state["why"] = None


class SafeStream:
    """A text stream whose write() and flush() never raise.

    The first failure (OSError, or ValueError for a closed file) marks the output as
    lost; later writes are dropped without touching the dead stream again."""

    _pyfib_safe = True

    def __init__(self, stream, name: str = "stdout"):
        self._stream = stream
        self._name = name
        self._dead = False

    def write(self, s):
        if self._dead:
            return len(s) if isinstance(s, str) else 0
        try:
            return self._stream.write(s)
        except UnicodeError:
            # A character the stream's encoding cannot write (output redirected to a
            # file in a Windows code page). NOT a lost reader: write it escaped, or drop
            # this one piece, and carry on.
            try:
                enc = getattr(self._stream, "encoding", None) or "ascii"
                return self._stream.write(
                    s.encode(enc, "backslashreplace").decode(enc, "replace"))
            except UnicodeError:
                return len(s) if isinstance(s, str) else 0
            except (OSError, ValueError) as e:
                self._dead = True
                _mark_lost(self._name, e)
                return len(s) if isinstance(s, str) else 0
        except (OSError, ValueError) as e:
            self._dead = True
            _mark_lost(self._name, e)
            return len(s) if isinstance(s, str) else 0

    def flush(self):
        if self._dead:
            return
        try:
            self._stream.flush()
        except (OSError, ValueError) as e:
            self._dead = True
            _mark_lost(self._name, e)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install() -> None:
    """Wrap sys.stdout and sys.stderr (idempotent; a missing stream is left alone)."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or getattr(stream, "_pyfib_safe", False):
            continue
        setattr(sys, name, SafeStream(stream, name))


def say(msg: str) -> None:
    """print() that never raises, wrapped streams or not (for clean-up paths)."""
    try:
        print(msg)
    except Exception:  # noqa: BLE001
        pass
