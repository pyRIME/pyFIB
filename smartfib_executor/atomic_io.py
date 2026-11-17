"""Atomic file writes that survive Windows file locks (v0.55.1, bug #19).

``write_text_atomic`` writes a temp file beside the target and renames it over the target,
so a reader sees the old file or the new one, never half of one. Two things the plain
"<name>.tmp + os.replace" did not cover:

* Two pyFIB processes (the window and a running batch) saving the same file used the SAME
  temp name; overlapping saves could promote a half-written file. The temp name is now
  unique per process and call.
* On Windows os.replace raises PermissionError while another program has the target open
  (the other pyFIB process reading it, a virus scanner, a sync client). That is retried
  for about a second before it is reported.

Nothing here touches the microscope.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

REPLACE_TRIES = 6
REPLACE_DELAY_S = 0.2


def unique_tmp(path) -> Path:
    p = Path(path)
    return p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")


def replace_with_retry(tmp, dst, *, tries: int = REPLACE_TRIES, delay_s: float = REPLACE_DELAY_S,
                       _replace=os.replace, _sleep=time.sleep) -> int:
    """os.replace(tmp, dst), retried on PermissionError. Returns the number of retries
    used; raises the last PermissionError if every try failed."""
    tries = max(1, int(tries))
    for i in range(tries):
        try:
            _replace(str(tmp), str(dst))
            return i
        except PermissionError:
            if i == tries - 1:
                raise
            _sleep(float(delay_s))
    return tries - 1          # not reached


BACKUP_SUFFIX = ".prev"          # not ".bak": refine.offset_position keeps its own .bak
STALE_TMP_S = 3600.0


def sweep_stale_tmp(path, *, older_than_s: float = STALE_TMP_S, _now=time.time) -> int:
    """Remove this file's own temp files (<name>.<pid>.<hex>.tmp) left by a process that
    was killed mid-save, once they are over an hour old. Best effort; returns the count."""
    p = Path(path)
    n = 0
    try:
        for t in p.parent.glob(p.name + ".*.tmp"):
            try:
                if _now() - t.stat().st_mtime > float(older_than_s):
                    t.unlink()
                    n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n


def write_text_atomic(path, text: str, *, encoding: str = "utf-8", backup: bool = False,
                      _replace=os.replace, _sleep=time.sleep) -> None:
    """Write `text` to `path` atomically. backup=True first copies the existing file to
    ``<name>.prev`` (best effort), so the previous version survives a bad write."""
    p = Path(path)
    tmp = unique_tmp(p)
    try:
        tmp.write_text(text, encoding=encoding)
        if backup and p.exists():
            try:
                shutil.copy2(str(p), str(p.with_name(p.name + BACKUP_SUFFIX)))
            except OSError:
                pass
            sweep_stale_tmp(p)
        replace_with_retry(tmp, p, _replace=_replace, _sleep=_sleep)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
