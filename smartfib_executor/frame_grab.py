"""Single-frame FIB/SEM grab and the switch to FIB imaging -- shared by every caller.

v0.54.0 (Evan, 2026-10-16/17): ONE protocol wherever pyFIB needs a single frame
(capture, after image, refine, FIB autofocus, CLEM overviews), instead of a fixed
3 s live scan before the freeze. At scan speed 3 (frame 0.36 s) the 3 s wait scanned
about eight frames.

WHY a protocol at all: SmartSEM's Grab() saves whatever is in the frame store at that
instant. Right after CMD_UNFREEZE_ZONE the store still holds the LAST FROZEN frame, so
"unfreeze, then Grab()" saves the previous image. The frame must be scanned AND
finished before Grab().

THE SINGLE-FRAME PROTOCOL (grab_fresh_frame):
  1. The scan speed is whatever the caller set ONCE for its sequence (refine, autofocus,
     the capture's coarse frame); nothing here changes it.
  2. DP_FREEZE_ON = End Frame, READ BACK. Anything else refuses (FreezeModeError):
     step 4 relies on the freeze finishing the frame.
  3. CMD_UNFREEZE_ZONE, then DP_FROZEN must read Live within 2 s (sent once more if
     not; still Frozen -> the frame is the old store: SCAN_NOT_RESTARTED).
  4. IMMEDIATELY CMD_FREEZE_ZONE: with End Frame the scan finishes the frame in
     progress and stops -- one pass.
  5. Wait for DP_FROZEN = Frozen, timeout 2 x AP_FRAME_TIME (60 s, with a log
     warning, if AP_FRAME_TIME is unreadable). A brief DP_FROZEN read failure does not
     end the wait; a frame whose end was never seen is NOT used (FRAME_NOT_FROZEN).
  6. Grab().
  7. Checks, each retried ONCE (one more frame) before giving up:
       * Live -> Frozen shorter than 0.5 x AP_FRAME_TIME: the frame did not run
         (FRAME_TOO_SHORT). Skipped when AP_FRAME_TIME is unreadable, for frames under
         0.3 s (speeds 1-2: the polls cannot time them), and by callers that scan a
         reduced raster (autofocus).
       * the file is byte-identical to the previous grab on this connection: the scan
         did not refresh (FRAME_IDENTICAL).
  8. The scan is left FROZEN (no further dose).
  9. Logged per frame: speed, AP_FRAME_TIME and the measured Live -> Frozen time (from
     the moment DP_FROZEN was seen Live to the moment it was seen Frozen).
Frame averaging is left as the operator set it (Evan, 2026-10-16: Frames to average = 1).

A frame that fails a check is still saved (for inspection) and the returned warning
contains a marker; is_stale_warning() tells callers it must NOT be used.

THE SWITCH TO FIB IMAGING (ensure_fib_imaging, bug #14): when SmartFIB hands control
back, SmartSEM goes to SEM imaging; the next switch to FIB imaging resets the FIB to its
lowest magnification (~176x), and on 2 of 21 positions (2026-10-13) that reset landed
AFTER pyFIB had set 2000x and read it back. So: send CMD_FIB_MODE_FIB only when the
column is not already in FIB imaging, wait for FIB, then wait at least 0.5 s and until
AP_FIB_MAGNIFICATION has not changed for 1 s (at most 5 s) BEFORE the caller sets the
magnification. Changes seen during the wait are logged with their timing.
"""

from __future__ import annotations

import hashlib
import math
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

FRAME_WAIT_S = 3.0        # v0.54.0: ONLY used when DP_FROZEN is unreadable (see below)
FROZEN_TIMEOUT_S = 60.0   # v0.54.0: wait-for-Frozen timeout when AP_FRAME_TIME is unreadable
LIVE_TIMEOUT_S = 2.0      # v0.53.4: the scan must report Live this soon after the unfreeze
FROZEN_TIMEOUT_FRAMES = 2.0   # wait for Frozen up to this many frame times
TOO_SHORT_FRAC = 0.5          # Live -> Frozen below this x AP_FRAME_TIME: it did not run
MIN_TIMED_FRAME_S = 0.3       # shorter frames (speeds 1-2) are not timed: the Live / Frozen
                              # polls (0.02-0.05 s + COM time) cannot resolve them
SCAN_RATE_SETTLE_S = 0.3      # after a scan-speed change, before the first frame
FRAME_TIME_MAX_MS = 6.0e5     # AP_FRAME_TIME is in ms; outside (0, 600 s) it is not believed

# Markers in the returned warning. A frame carrying any of them must NOT be used.
# v0.53.4: the scan never restarted -- the saved frame is the OLD frame store (Evan,
# 2026-10-12: position_1's 800x file was a byte copy of its 2000x one).
SCAN_NOT_RESTARTED = "the scan did not restart after the unfreeze"
FRAME_TOO_SHORT = "the frame ended too soon for this scan speed"
FRAME_IDENTICAL = "the frame is identical to the previous grab"
# v0.54.0 review: with the immediate freeze the store may still hold the PREVIOUS frame when
# the end of this one was never seen (a speed just changed, a lost freeze request), so an
# unconfirmed frame is not used either.
FRAME_NOT_FROZEN = "DP_FROZEN did not report Frozen"
STALE_MARKERS = (SCAN_NOT_RESTARTED, FRAME_TOO_SHORT, FRAME_IDENTICAL, FRAME_NOT_FROZEN)

# ensure_fib_imaging (bug #14)
FIB_SWITCH_MIN_WAIT_S = 0.5
FIB_MAG_STEADY_S = 1.0
FIB_SWITCH_MAX_SETTLE_S = 5.0
FIB_MAG_POLL_S = 0.1
FIB_MAG_SAME_FRAC = 0.001

_DIGEST_ATTR = "_pyfib_last_frame_sha1"


class FreezeModeError(RuntimeError):
    """DP_FREEZE_ON does not read End Frame: no frame was taken."""


class StaleFrameError(RuntimeError):
    """The grabbed frame failed a single-frame check (see is_stale_warning)."""


class FibSwitchTimeout(TimeoutError):
    """FIB imaging did not come on after CMD_FIB_MODE_FIB."""


def is_stale_warning(w: Optional[str]) -> bool:
    """True when a grab_fresh_frame warning says the saved frame must not be used."""
    return bool(w) and any(m in w for m in STALE_MARKERS)


# ------------------------------------------------------------------ reads
def is_frozen(sem) -> Optional[bool]:
    """True/False from DP_FROZEN (name first, number fallback); None if unreadable."""
    try:
        v = sem.get_str("DP_FROZEN").strip().lower()
        if v in ("frozen", "1", "yes"):
            return True
        if v in ("live", "0", "no"):
            return False
    except Exception:
        pass
    try:
        return int(round(float(sem.get_float("DP_FROZEN")))) == 1
    except Exception:
        return None


def wait_frozen(sem, timeout_s: float, poll_s: float = 0.2) -> Optional[bool]:
    """Block until DP_FROZEN reports Frozen. Returns True (confirmed), False (timed out)
    or None (DP_FROZEN never readable during the wait).

    v0.54.0 review: a read failure does not end the wait -- it keeps polling until the
    deadline (a single failed read used to return at once and the frame was grabbed
    mid-scan)."""
    deadline = time.time() + float(timeout_s)
    readable = False
    while True:
        st = is_frozen(sem)
        if st:
            return True
        if st is not None:
            readable = True
        if time.time() >= deadline:
            return False if readable else None
        time.sleep(poll_s)


def wait_live(sem, timeout_s: float = LIVE_TIMEOUT_S, poll_s: float = 0.05) -> Optional[bool]:
    """After CMD_UNFREEZE_ZONE: True once DP_FROZEN reads Live, False if it stays Frozen
    for ``timeout_s``, None if DP_FROZEN was never readable (cannot tell).

    v0.54.0 review: a read failure does not end the wait (as wait_frozen)."""
    deadline = time.time() + float(timeout_s)
    readable = False
    while True:
        st = is_frozen(sem)
        if st is False:
            return True
        if st is not None:
            readable = True
        if time.time() >= deadline:
            return False if readable else None
        time.sleep(poll_s)


def read_frame_time_s(sem) -> Optional[float]:
    """AP_FRAME_TIME in seconds (the API reports ms -- measured in FIB imaging at
    30kV:20pA, 2026-10-16: speed 3 = 358.54, speed 7 ~ 5100, speed 8 ~ 10100), or None
    if unreadable / not believable."""
    try:
        ft = float(sem.get_float("AP_FRAME_TIME"))
    except Exception:  # noqa: BLE001
        return None
    if ft != ft or not (0.0 < ft < FRAME_TIME_MAX_MS):
        return None
    return ft / 1000.0


def read_freeze_on(sem) -> Tuple[Optional[bool], str]:
    """(True if DP_FREEZE_ON is End Frame, False if it is something else, None if
    unreadable; what was read). Text first (End Frame / End Line / Command), number
    second (0 = End Frame, 1 = End Line, 2 = Command)."""
    text = None
    try:
        text = str(sem.get_str("DP_FREEZE_ON")).strip()
    except Exception:  # noqa: BLE001
        text = None
    norm = "".join((text or "").lower().split())
    if norm == "endframe":
        return True, repr(text)
    if norm in ("endline", "command"):
        return False, repr(text)
    try:
        n = float(sem.get_float("DP_FREEZE_ON"))
        if n == n:
            code = int(round(n))
            return code == 0, f"{code}" + (f" (text {text!r})" if text else "")
    except Exception:  # noqa: BLE001
        pass
    return None, f"unreadable (text {text!r})"


def _set_freeze_on_end_frame(sem) -> Tuple[Optional[bool], str]:
    try:
        sem.set_value("DP_FREEZE_ON", "End Frame")
    except Exception:  # noqa: BLE001
        try:
            sem.set_value("DP_FREEZE_ON", 0.0)
        except Exception:  # noqa: BLE001
            pass
    ok, shown = read_freeze_on(sem)
    for _ in range(2):                     # a set may take a moment to read back
        if ok:
            break
        time.sleep(0.1)
        ok, shown = read_freeze_on(sem)
    return ok, shown


def _freeze_request(sem, log=None) -> bool:
    """CMD_FREEZE_ZONE (CMD_FREEZE_ALL if that fails). False -- and a log warning -- if
    both failed: the scan may still be live."""
    try:
        sem.execute("CMD_FREEZE_ZONE")
        return True
    except Exception:  # noqa: BLE001
        try:
            sem.execute("CMD_FREEZE_ALL")
            return True
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"[frame] WARNING: CMD_FREEZE_ZONE and CMD_FREEZE_ALL both failed ({e!r}); "
                    f"the scan may still be live -- press Freeze in SmartSEM.")
            return False


def _file_sha1(path) -> Optional[str]:
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ one frame
def _scan_one_frame(sem, *, ft_s: Optional[float], frame_wait_s: float,
                    frozen_timeout_s: float, log) -> dict:
    """Steps 3-5: unfreeze -> Live -> immediate end-of-frame freeze -> Frozen."""
    live = None
    readable_before = is_frozen(sem) is not None
    for attempt in range(2):
        try:
            sem.execute("CMD_UNFREEZE_ZONE")
        except Exception:  # noqa: BLE001
            pass
        live = wait_live(sem)
        if live is not False:
            break
        log(f"[frame] the scan still reads Frozen {LIVE_TIMEOUT_S:g} s after the unfreeze"
            + ("; sending it again" if attempt == 0 else ""))
    t_live = time.time()                   # DP_FROZEN seen Live
    _freeze_request(sem, log)              # End Frame: finish THIS frame, then stop
    if live is None:
        # DP_FROZEN unreadable: cannot see Live or Frozen. The freeze was requested at
        # once, so one frame time (plus margin) finishes it.
        wait = max(float(frame_wait_s), 1.2 * ft_s + 0.3) if ft_s else float(frame_wait_s)
        log(f"[frame] DP_FROZEN unreadable; waiting {wait:.1f} s for the frame to finish.")
        time.sleep(wait)
        # v0.54.0 review: the speed may just have changed -- believe a longer frame time.
        ft2 = read_frame_time_s(sem)
        if ft2 and ft2 > (ft_s or 0.0):
            more = max(float(frame_wait_s), 1.2 * ft2 + 0.3) - wait
            if more > 0:
                log(f"[frame] AP_FRAME_TIME now reads {ft2:.2f} s; waiting {more:.1f} s more.")
                time.sleep(more)
        st = is_frozen(sem)
        if st is True:
            frozen = True
        elif readable_before or st is False:
            # it was readable before the unfreeze (or reads Live now): the end of this
            # frame was not seen -> not used
            frozen = False
        else:
            frozen = None                  # never readable: cannot tell (as before v0.54)
        return dict(live=None, frozen=frozen, elapsed=None, ft_after=ft2 or ft_s,
                    timeout=wait)
    if ft_s:
        timeout = FROZEN_TIMEOUT_FRAMES * ft_s
        poll = min(0.2, max(0.02, ft_s / 20.0))
    else:
        timeout = float(frozen_timeout_s)
        poll = 0.2
        log(f"[frame] WARNING: AP_FRAME_TIME unreadable; waiting up to {timeout:g} s for "
            f"the frame to finish.")
    st = wait_frozen(sem, timeout, poll)
    if st is None:
        st = False                         # readable at Live: an unseen end is not confirmed
    if st is False and ft_s:
        # The speed may have changed just before this frame: believe a longer frame time.
        ft2 = read_frame_time_s(sem)
        more = (FROZEN_TIMEOUT_FRAMES * ft2 - (time.time() - t_live)) if ft2 else 0.0
        if more > 0:
            log(f"[frame] AP_FRAME_TIME now reads {ft2:.2f} s; waiting {more:.1f} s more.")
            st = bool(wait_frozen(sem, more, poll))
    t_frozen = time.time()
    if st is False:
        _freeze_request(sem, log)          # stop the dose if the first request was lost
    ft_after = read_frame_time_s(sem) or ft_s
    return dict(live=live, frozen=st, elapsed=(t_frozen - t_live) if st else None,
                ft_after=ft_after, timeout=timeout)


def grab_fresh_frame(sem, path: str, *, xoff: int = 0, yoff: int = 0,
                     width: int = 1024, height: int = 768, reduction: int = 0,
                     frame_wait_s: float = FRAME_WAIT_S,
                     frozen_timeout_s: Optional[float] = None,
                     check_too_short: bool = True,
                     log: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Take ONE fresh frame (the single-frame protocol above) and Grab() it to `path`.

    Returns None for a confirmed frame, or a WARNING string (is_stale_warning() says
    whether the frame must not be used). Raises FreezeModeError (nothing scanned) when
    DP_FREEZE_ON does not read End Frame, and re-raises a Grab() failure after a
    best-effort freeze.

    frame_wait_s: only when DP_FROZEN is unreadable (then max(frame_wait_s,
      1.2 x AP_FRAME_TIME + 0.3 s)).
    frozen_timeout_s: the wait for Frozen when AP_FRAME_TIME is unreadable (default 60 s).
    check_too_short: False for a reduced raster (autofocus), whose frame is shorter than
      AP_FRAME_TIME.
    """
    log = log or (lambda m: None)
    fto = FROZEN_TIMEOUT_S if frozen_timeout_s is None else float(frozen_timeout_s)
    ok, shown = _set_freeze_on_end_frame(sem)
    if not ok:
        raise FreezeModeError(
            f"DP_FREEZE_ON reads {shown}, not End Frame, after setting it. A freeze would "
            f"not wait for the end of the frame, so no frame was taken. Set Freeze on = "
            f"End Frame in SmartSEM and try again.")
    speed = read_scan_rate(sem)
    ft_s = read_frame_time_s(sem)
    prev = getattr(sem, _DIGEST_ATTR, None)
    if not isinstance(prev, str):
        prev = None
    warning: Optional[str] = None
    for attempt in (1, 2):
        r = _scan_one_frame(sem, ft_s=ft_s, frame_wait_s=frame_wait_s,
                            frozen_timeout_s=fto, log=log)
        ft_chk = r["ft_after"]
        log(f"[frame] speed {speed if speed is not None else '?'}, AP_FRAME_TIME "
            + (f"{ft_chk:.2f} s" if ft_chk else "unreadable") + ", Live -> Frozen "
            + (f"{r['elapsed']:.2f} s" if r["elapsed"] is not None else "not measured")
            + (f" (attempt {attempt})" if attempt > 1 else ""))
        too_short = bool(check_too_short and r["live"] is True and r["elapsed"] is not None
                         and ft_chk and ft_chk >= MIN_TIMED_FRAME_S
                         and r["elapsed"] < TOO_SHORT_FRAC * ft_chk)
        if too_short and attempt == 1:
            log(f"[frame] the frame ended after {r['elapsed']:.2f} s but one frame takes "
                f"{ft_chk:.2f} s: it did not run. Taking one more frame.")
            continue
        try:
            sem.grab_bmp(str(path), xoff=int(xoff), yoff=int(yoff), width=int(width),
                         height=int(height), reduction=int(reduction))
        except Exception:
            _freeze_request(sem, log)
            raise
        digest = _file_sha1(path)          # None if Grab() saved elsewhere (recovered later)
        identical = digest is not None and prev is not None and digest == prev
        if identical and attempt == 1 and r["live"] is not False:
            log(f"[frame] the frame is byte-identical to the previous grab (the scan did "
                f"not refresh). Taking one more frame.")
            continue
        if digest is not None:
            try:
                setattr(sem, _DIGEST_ATTR, digest)
            except Exception:  # noqa: BLE001
                pass
        if r["live"] is False:
            warning = (f"WARNING: {SCAN_NOT_RESTARTED} (DP_FROZEN stayed Frozen); the frame "
                       f"is incomplete or stale -- it is the previous frame store.")
        elif too_short:
            warning = (f"WARNING: {FRAME_TOO_SHORT} twice (Live -> Frozen "
                       f"{r['elapsed']:.2f} s, one frame {ft_chk:.2f} s); the frame is "
                       f"incomplete or stale.")
        elif identical:
            warning = (f"WARNING: {FRAME_IDENTICAL} twice; the frame is incomplete or "
                       f"stale -- the scan did not refresh.")
        elif r["frozen"] is False:
            warning = (f"WARNING: {FRAME_NOT_FROZEN} within {r.get('timeout', fto):.1f} s; "
                       f"the frame is incomplete or stale -- the store may still hold the "
                       f"previous frame.")
        if warning:
            log(warning)
        return warning
    return warning                         # not reached


# ----------------------------- scan rate (core) ----------------------------- #
# DP_SCANRATE is read-only on the instrument; CMD_SCANRATE<n> sets it (confirmed).
def read_scan_rate(sem) -> Optional[int]:
    try:
        return int(round(float(sem.get_float("DP_SCANRATE"))))
    except Exception:
        return None


def set_scan_rate(sem, rate: int) -> bool:
    try:
        sem.execute(f"CMD_SCANRATE{int(rate)}")
        return True
    except Exception:
        return False


# ----------------------------- FIB imaging + magnification ----------------- #
def fib_imaging_on(sem) -> bool:
    """DP_FIB_IMAGING reads FIB (text first; number 1 as the fallback)."""
    try:
        if str(sem.get_str("DP_FIB_IMAGING")).strip().upper() == "FIB":
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        return int(round(float(sem.get_float("DP_FIB_IMAGING")))) == 1
    except Exception:  # noqa: BLE001
        return False


def _imaging_text(sem) -> Optional[str]:
    try:
        return str(sem.get_str("DP_FIB_IMAGING")).strip()
    except Exception:  # noqa: BLE001
        return None


def _same_mag(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= FIB_MAG_SAME_FRAC * max(abs(a), abs(b), 1e-9)


def ensure_fib_imaging(sem, *, timeout_s: float = 60.0, poll_s: float = 0.25,
                       min_wait_s: float = FIB_SWITCH_MIN_WAIT_S,
                       steady_s: float = FIB_MAG_STEADY_S,
                       max_settle_s: float = FIB_SWITCH_MAX_SETTLE_S,
                       log: Optional[Callable[[str], None]] = None,
                       clock: Optional[Callable[[], float]] = None,
                       sleep: Optional[Callable[[float], None]] = None) -> bool:
    """Put the column in FIB imaging (bug #14). Returns True if a switch was sent.

    Already in FIB imaging: nothing is sent and nothing waits. Otherwise
    CMD_FIB_MODE_FIB, wait for DP_FIB_IMAGING = FIB (FibSwitchTimeout after
    ``timeout_s``), then wait ``min_wait_s`` and until AP_FIB_MAGNIFICATION has not
    changed for ``steady_s`` (at most ``max_settle_s``), so SmartSEM's reset of the FIB
    magnification lands BEFORE the caller sets it."""
    log = log or (lambda m: None)
    clock = clock or time.time             # looked up at call time (tests patch `time`)
    sleep = sleep or time.sleep
    if fib_imaging_on(sem):
        return False
    before = _imaging_text(sem)
    mag0 = read_fib_mag(sem)
    t0 = clock()
    sem.execute("CMD_FIB_MODE_FIB")
    n_max = max(1, int(math.ceil(float(timeout_s) / max(poll_s, 1e-3))))
    i = 0
    while not fib_imaging_on(sem):
        i += 1
        if i > n_max or clock() - t0 > float(timeout_s):
            raise FibSwitchTimeout(
                f"FIB imaging did not come on within {float(timeout_s):g} s after "
                f"CMD_FIB_MODE_FIB (DP_FIB_IMAGING reads {_imaging_text(sem)!r}).")
        sleep(poll_s)
    t_fib = clock() - t0
    sleep(float(min_wait_s))
    last = read_fib_mag(sem)
    changes = []
    if not _same_mag(mag0, last):
        changes.append(f"{_mag_txt(mag0)} -> {_mag_txt(last)} by +{clock() - t0:.2f} s")
    need = max(1, int(round(float(steady_s) / FIB_MAG_POLL_S)))
    n_settle = max(need, int(round(max(0.0, float(max_settle_s) - float(min_wait_s))
                                   / FIB_MAG_POLL_S)))
    steady = 0
    for _ in range(n_settle):
        sleep(FIB_MAG_POLL_S)
        m = read_fib_mag(sem)
        if _same_mag(m, last):
            steady += 1
            if steady >= need:
                break
        else:
            changes.append(f"{_mag_txt(last)} -> {_mag_txt(m)} at +{clock() - t0:.2f} s")
            last, steady = m, 0
    settled = steady >= need
    log(f"[fib] switched to FIB imaging (was {before!r}): on after {t_fib:.2f} s; "
        f"FIB magnification " + ("; ".join(changes) if changes else
                                 f"unchanged at {_mag_txt(last)}")
        + (f"; steady for {float(steady_s):g} s by +{clock() - t0:.2f} s" if settled else
           f"; WARNING: still changing after {float(max_settle_s):g} s"))
    return True


def _mag_txt(m: Optional[float]) -> str:
    return f"{m:g}x" if m is not None else "unreadable"


def set_fib_mag_checked(sem, target: float, *, tol_frac: float = 0.01, tries: int = 2,
                        wait_s: float = 0.3, sleep: Optional[Callable[[float], None]] = None):
    """v0.53.4: set AP_FIB_MAGNIFICATION and READ IT BACK. Returns (ok, readback).

    Each try: set, read at once and, if it does not match yet, wait ``wait_s`` and read
    again; up to ``tries`` sets. ok only when the read-back is within ``tol_frac`` of the
    target (an unreadable read-back is never ok)."""
    target = float(target)
    sleep = sleep or time.sleep
    rb: Optional[float] = None
    for _ in range(max(1, int(tries))):
        sem.set_value("AP_FIB_MAGNIFICATION", target)
        for k in range(2):
            rb = read_fib_mag(sem)
            if rb is not None and abs(rb - target) <= tol_frac * target:
                return True, rb
            if k == 0:
                sleep(float(wait_s))
    return False, rb


def read_fib_mag(sem) -> Optional[float]:
    try:
        v = float(sem.get_float("AP_FIB_MAGNIFICATION"))
        return v if v == v else None
    except Exception:  # noqa: BLE001
        return None


def fib_mag_still(sem, target: float, tol_frac: float = 0.01) -> Tuple[bool, Optional[float]]:
    """After a frame: (the FIB magnification still reads ``target``, what it reads)."""
    m = read_fib_mag(sem)
    return (m is not None and abs(m - float(target)) <= tol_frac * float(target)), m
