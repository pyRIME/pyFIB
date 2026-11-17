from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple

try:
    import pythoncom
    import pywintypes
    import win32com.client
except Exception as e:  # pragma: no cover
    # Allow importing this module on non-Windows systems for pattern parsing etc.
    pythoncom = None  # type: ignore
    pywintypes = None  # type: ignore
    win32com = None  # type: ignore
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None


# CLSID of the CZ EM API (CZEMApi) ActiveX control.
CZEMAPI_CLSID = "{71BD42C4-EBD3-11D0-AB3A-444553540000}"


class SmartSEMError(RuntimeError):
    """Raised when the SmartSEM Remote API returns a non-zero error code."""

    def __init__(self, message: str, code: Optional[int] = None, last_error: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.last_error = last_error


@dataclass(frozen=True)
class StagePosition:
    x_m: float
    y_m: float
    z_m: float
    tilt_deg: float
    rot_deg: float
    master_z_m: float


# --------------------------------------------------------------------------------------
# One SmartSEM client at a time (v0.53.3). Evan, 2026-10-12: Rough Mill Start crashed with
# ZEISS "RRemoting Error: Failed to create an instance of Managed ... unable to open port
# for incoming connections" and exit -1073741819 (access violation) while v0.39.0 worked.
# Since v0.46.0 Start connected twice in a row (a tilt-check process, then the armed run
# ~1 s later), and a hung python.exe titled "Remoting Forwarding Window" (the ZEISS API's
# hidden window = a process holding a SmartSEM connection) was seen. Before creating the
# API object, wait for any OTHER such process to go away; refuse cleanly if it does not.
# --------------------------------------------------------------------------------------
REMOTING_WINDOW_TITLE = "Remoting Forwarding Window"
CLIENT_WAIT_S = 20.0


def other_api_clients(*, _run=None, _is_windows=None, _self_pids=None, _parent_out=None):
    """Other python.exe / pythonw.exe processes holding a SmartSEM connection (their window
    is the ZEISS API's "Remoting Forwarding Window"): [{pid, image, status, title}].
    Never this process or its parent (the pyFIB window that started it). [] off Windows
    or if tasklist fails.

    v0.55.1 (bug #18): ``_parent_out`` (a list) receives the PARENT's entry if the parent
    itself holds a connection (the pyFIB window does once its GISDEP tab has connected).
    It is still not returned as "other" -- it is only reported, so the Log shows when two
    clients were connected at once."""
    import csv
    import os
    import subprocess
    if not (_is_windows if _is_windows is not None else os.name == "nt"):
        return []
    run = _run or subprocess.run
    skip = set(_self_pids) if _self_pids is not None else {os.getpid(), os.getppid()}
    parent_pid = os.getppid()
    found = []
    for image in ("python.exe", "pythonw.exe"):
        try:
            kw = {"creationflags": 0x08000000} if os.name == "nt" else {}   # no console flash
            out = run(["tasklist", "/V", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {image}"],
                      capture_output=True, text=True, timeout=15, **kw).stdout or ""
        except Exception:  # noqa: BLE001
            continue
        for rec in csv.reader(out.splitlines()):
            if len(rec) < 9:
                continue                       # "INFO: No tasks are running ..."
            try:
                pid = int(rec[1])
            except ValueError:
                continue
            if pid in skip:
                if (_parent_out is not None and pid == parent_pid and pid != os.getpid()
                        and REMOTING_WINDOW_TITLE.lower() in rec[8].lower()):
                    _parent_out.append({"pid": pid, "image": rec[0], "status": rec[5],
                                        "title": rec[8]})
                continue
            if REMOTING_WINDOW_TITLE.lower() in rec[8].lower():
                found.append({"pid": pid, "image": rec[0], "status": rec[5],
                              "title": rec[8]})
    return found


def wait_for_other_api_clients(wait_s: float = CLIENT_WAIT_S, *, poll_s: float = 1.0,
                               log=print, _find=None, _sleep=time.sleep, _clock=time.monotonic):
    """Return once no other process holds a SmartSEM connection; after ``wait_s`` raise
    SmartSEMError naming them (nothing has been sent to the microscope)."""
    find = _find or other_api_clients
    t0 = _clock()
    said = False
    first = True
    while True:
        if first and _find is None:
            # v0.55.1 (#18): note (never refuse) when the window that started this command
            # holds a connection of its own -- two clients are then connected at once.
            first = False
            parent = []
            others = find(_parent_out=parent)
            for o in parent:
                try:
                    log(f"[connect] note: the pyFIB window (PID {o['pid']}) holds a SmartSEM "
                        f"connection of its own (GISDEP tab); this command adds a second one.")
                except Exception:  # noqa: BLE001
                    pass
        else:
            others = find()
        if not others:
            return
        if _clock() - t0 >= float(wait_s):
            desc = ", ".join(f"{o['image']} PID {o['pid']} ({o['status']})" for o in others)
            raise SmartSEMError(
                f"another program holds a SmartSEM connection: {desc}. It did not let go "
                f"within {wait_s:g} s. End it in Task Manager (Details) and try again. "
                f"Nothing was sent to the microscope.")
        if not said:
            log(f"[connect] waiting for another SmartSEM client to finish "
                f"({', '.join(str(o['pid']) for o in others)}) ...")
            said = True
        _sleep(float(poll_s))


class SmartSEM:
    """Thin wrapper over the SmartSEM CZEMApi ActiveX (COM) control.

    Notes:
    - Windows only (pywin32).
    - Supports local Initialise("") and remoting InitialiseRemoting/InitialiseRemote.
    - Get/Set use VARIANT types; helper methods here request explicit float/string types.
    """

    def __init__(self, suppress_remoting_popups: bool = True,
                 client_wait_s: float = CLIENT_WAIT_S):
        if _IMPORT_ERROR is not None:
            raise RuntimeError(
                "pywin32/pythoncom not available. This package must run on Windows with pywin32 installed."
            ) from _IMPORT_ERROR

        assert pythoncom is not None
        assert win32com is not None

        # v0.53.3: never create a second SmartSEM client while another process holds one
        # (the ZEISS API then crashes this process with an access violation).
        if client_wait_s is not None and client_wait_s >= 0:
            wait_for_other_api_clients(client_wait_s)

        # SmartSEM OCX expects STA.
        pythoncom.CoInitialize()

        self.api = win32com.client.Dispatch(CZEMAPI_CLSID)

        # Optional: prevent popups when remoting connection is not ready.
        self._suppress_remoting_popups = suppress_remoting_popups
        self._connected = False
        self._mode: Optional[str] = None

    def close(self) -> None:
        if self._connected:
            try:
                self._call_ok(self.api.ClosingControl)
            except Exception:
                pass
        self._connected = False
        self._mode = None

    def __enter__(self) -> "SmartSEM":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ----------------------------
    # Connection
    # ----------------------------
    def connect_local(self, machine: str = "") -> None:
        """Connect to a local EM Server using Initialise(machine)."""
        self._call_ok(self.api.Initialise, machine)
        self._connected = True
        self._mode = "local"

    def connect_remoting(self) -> None:
        """Connect via remoting using InitialiseRemoting / InitialiseRemote."""
        init = None
        if hasattr(self.api, "InitialiseRemoting"):
            init = getattr(self.api, "InitialiseRemoting")
        elif hasattr(self.api, "InitialiseRemote"):
            init = getattr(self.api, "InitialiseRemote")
        else:
            raise SmartSEMError("CZEMApi has no InitialiseRemoting/InitialiseRemote method")

        # IMPORTANT: On some SmartSEM/SmartFIB installations the OCX requires
        # SetSuppressRemotingConnectionErrors() to be called *before* InitialiseRemoting().
        # Calling it after InitialiseRemoting() can raise an error like:
        # "You cannot call SetSuppressRemotingConnectionErrors() after calling InitialiseRemoting(),
        #  the flag must be set first".
        if self._suppress_remoting_popups and hasattr(self.api, "SetSuppressRemotingConnectionErrors"):
            try:
                self._call_ok(self.api.SetSuppressRemotingConnectionErrors)
            except Exception:
                # Not fatal; depends on API version.
                pass

        self._call_ok(init)

        self._connected = True
        self._mode = "remoting"

    # ----------------------------
    # Low-level call helpers
    # ----------------------------
    def _split_rc_outs(self, res: Any) -> Tuple[int, Tuple[Any, ...]]:
        # Some COM calls return just rc (int).
        if isinstance(res, (int, float)):
            return int(res), tuple()
        # Some wrappers return (rc, out1, out2, ...)
        if isinstance(res, tuple) and res and isinstance(res[0], (int, float)):
            return int(res[0]), tuple(res[1:])
        # If we got only an out value, treat as success.
        return 0, (res,)

    def _get_last_error(self) -> Optional[str]:
        try:
            # Signature: GetLastError(VARIANT* Error). Like Get(), win32com may return
            # the [out] string in the RETURN (as (rc, error)) rather than mutating the
            # passed VARIANT -- handle both so error detail isn't lost.
            v = win32com.client.VARIANT(pythoncom.VT_BSTR, "")  # type: ignore[attr-defined]
            res = self.api.GetLastError(v)
            if isinstance(res, tuple):
                rc = res[0] if res else 0
                err = res[1] if len(res) > 1 else getattr(v, "value", "")
            else:
                rc = res
                err = getattr(v, "value", "")
            if int(rc) == 0 and err:
                return str(err)
        except Exception:
            return None
        return None

    def _call_ok(self, func, *args) -> Tuple[Any, ...]:
        try:
            res = func(*args)
        except pywintypes.com_error as e:  # type: ignore[attr-defined]
            raise SmartSEMError(f"COM error calling {getattr(func, '__name__', str(func))}: {e}") from e

        rc, outs = self._split_rc_outs(res)
        if rc != 0:
            raise SmartSEMError(
                f"{getattr(func, '__name__', 'COM call')} failed with rc={rc}",
                code=rc,
                last_error=self._get_last_error(),
            )
        return outs

    # ----------------------------
    # Core API surface
    # ----------------------------
    def execute(self, command: str) -> None:
        """Execute CMD_... or macro MCF_/MCL_..."""
        self._call_ok(self.api.Execute, command)

    def _get_r4(self, mnemonic: str) -> float:
        """Read a parameter as a number, forcing VT_R4 and taking the RETURNED value.

        Tries a plain-float prime first (`Get(param, 0.0)` -> use the return), exactly
        like gisdep's ComSmartSEM; falls back to an explicit VT_R4 VARIANT. The value
        comes from the return tuple, never the passed VARIANT's .value (which stays
        stale -- the bug behind DP_AUTO_FUNCTION / GIS needles reading 0).
        """
        try:
            outs = self._call_ok(self.api.Get, mnemonic, 0.0)
            if outs:
                return float(outs[0])
        except (TypeError, ValueError):
            pass
        v = win32com.client.VARIANT(pythoncom.VT_R4, 0.0)  # type: ignore[attr-defined]
        outs = self._call_ok(self.api.Get, mnemonic, v)
        if outs:
            return float(outs[0])
        return float(getattr(v, "value", v))

    def get_float(self, mnemonic: str) -> float:
        """Get value requesting VT_R4 (float) when possible."""
        # Try the python-friendly calling convention first.
        try:
            outs = self._call_ok(self.api.Get, mnemonic)
            if outs:
                v = outs[0]
                if isinstance(v, (int, float)):
                    return float(v)
                # fallthrough for strings etc
        except TypeError:
            pass
        return self._get_r4(mnemonic)

    def get_str(self, mnemonic: str) -> str:
        """Get value requesting VT_BSTR (string) when possible."""
        try:
            outs = self._call_ok(self.api.Get, mnemonic)
            if outs:
                return str(outs[0])
        except TypeError:
            pass

        v = win32com.client.VARIANT(pythoncom.VT_BSTR, "")  # type: ignore[attr-defined]
        outs = self._call_ok(self.api.Get, mnemonic, v)
        if outs:
            return str(outs[0])
        return str(getattr(v, "value", v))

    def set_value(self, mnemonic: str, value: Any) -> None:
        """Set AP_/DP_/SV_ values.

        For analogue parameters, pass float in SI units (e.g., metres).
        For DP_ states, pass a state string (e.g., "Off") or a numeric code.
        For SV_ strings, pass python str.
        """
        self._call_ok(self.api.Set, mnemonic, value)

    # ----------------------------
    # Stage helpers
    # ----------------------------
    def get_stage_position(self) -> StagePosition:
        """Read the stage position.

        PRIMARY: six AP_STAGE_AT_* reads via get_float -- the path PROVEN live (prep_cryo
        and setup_move confirmed real arrival coordinates with it).

        The old implementation called GetStagePosition() and read back the passed
        VARIANTs' .value, which stays at the initial 0.0 because win32com returns the
        [out] values in the RETURN tuple, not by mutating the VARIANTs (the same
        stale-VARIANT bug as get_float/get_digital/get_str). It returned six zeros,
        which made the v0.27.0 arrival wait report "x off by <the whole target>" and
        time out. GetStagePosition is kept only as a fallback, now capturing the
        returned tuple.
        """
        try:
            return StagePosition(
                x_m=float(self.get_float("AP_STAGE_AT_X")),
                y_m=float(self.get_float("AP_STAGE_AT_Y")),
                z_m=float(self.get_float("AP_STAGE_AT_Z")),
                tilt_deg=float(self.get_float("AP_STAGE_AT_T")),
                rot_deg=float(self.get_float("AP_STAGE_AT_R")),
                master_z_m=float(self.get_float("AP_STAGE_AT_M")),
            )
        except Exception:
            pass
        # Fallback: GetStagePosition(VARIANT* x..m), taking the RETURNED values.
        if win32com is not None:
            v = [win32com.client.VARIANT(pythoncom.VT_R4, 0.0) for _ in range(6)]  # type: ignore[attr-defined]
            outs = self._call_ok(self.api.GetStagePosition, *v)
        else:
            v = []
            outs = self._call_ok(self.api.GetStagePosition)
        if outs and len(outs) >= 6:
            vals = [float(o) for o in outs[:6]]
        else:
            vals = [float(getattr(x, "value", x)) for x in v]
        return StagePosition(vals[0], vals[1], vals[2], vals[3], vals[4], vals[5])

    def move_stage(self, x_m: float, y_m: float, z_m: float, t_deg: float, r_deg: float, m_m: float) -> None:
        """Move stage using MoveStage(x,y,z,t,r,m). Non-blocking.

        v0.55.1 (bug #20): refuses a non-finite number (NaN / infinity) before anything is
        sent -- what MoveStage does with one is unknown, and the arrival check could not
        tell (a comparison with NaN is never "off target")."""
        vals = {"x_m": x_m, "y_m": y_m, "z_m": z_m, "t_deg": t_deg, "r_deg": r_deg, "m_m": m_m}
        bad = []
        for k, v in vals.items():
            try:
                if not math.isfinite(float(v)):
                    bad.append(f"{k}={v!r}")
            except (TypeError, ValueError):
                bad.append(f"{k}={v!r}")
        if bad:
            raise SmartSEMError(
                f"MoveStage refused: {', '.join(bad)} is not a finite number. Nothing was "
                f"sent to the stage.")
        self._call_ok(self.api.MoveStage, float(x_m), float(y_m), float(z_m), float(t_deg), float(r_deg), float(m_m))

    # --- gisdep SmartSEMInterface surface (delegates to the methods above) -----
    # Lets pyFIB's proven connection be used wherever a gisdep SmartSEMInterface is
    # expected, and lets pyFIB code migrate to the clearer analogue/digital split.
    def initialise(self) -> None:
        """Open the connection (remoting, falling back to local)."""
        try:
            self.connect_remoting()
        except Exception:
            self.connect_local()

    def get_analogue(self, param: str) -> float:
        """AP_* as a float in SI units."""
        return float(self.get_float(param))

    def get_digital(self, param: str) -> int:
        """DP_* as its integer state value (forced VT_R4, value from the return)."""
        return int(round(self._get_r4(param)))

    def get_digital_text(self, param: str) -> str:
        """DP_* as its state name."""
        return str(self.get_str(param))

    def set_analogue(self, param: str, value: float) -> None:
        self.set_value(param, float(value))

    def set_digital(self, param, value) -> None:
        self.set_value(param, value)

    def _get_stage_is_code(self) -> Optional[int]:
        """Best-effort getter for DP_STAGE_IS as an integer (0=Idle, 1=Busy)."""
        try:
            return int(self.get_float("DP_STAGE_IS"))
        except Exception:
            pass
        try:
            s = self.get_str("DP_STAGE_IS").strip()
            if s.isdigit():
                return int(s)
            s_low = s.lower()
            if s_low == "idle":
                return 0
            if s_low == "busy":
                return 1
        except Exception:
            return None
        return None

    def wait_stage_idle(self, timeout_s: float = 120.0, poll_s: float = 0.25) -> None:
        """Wait for DP_STAGE_IS to become Idle (0).

        CAUTION: idle ALONE is not a safe post-move wait. MoveStage is non-blocking
        and DP_STAGE_IS can read Idle *before* the stage starts moving, so this can
        return mid-travel. After commanding a move, use wait_stage_arrival() with the
        target so the read-back position is confirmed. This method is kept for callers
        that genuinely only need "not busy".
        """
        t0 = time.time()
        while True:
            code = self._get_stage_is_code()
            if code == 0:
                return
            if time.time() - t0 > timeout_s:
                raise SmartSEMError(f"Timeout waiting for stage idle (DP_STAGE_IS). Got {code!r}.")
            time.sleep(poll_s)

    def wait_stage_arrival(self, x_m=None, y_m=None, z_m=None, t_deg=None,
                           r_deg=None, m_m=None, *, tol_m: float = 0.3e-3,
                           tol_deg: float = 0.05, timeout_s: float = 120.0,
                           poll_s: float = 0.25) -> None:
        """Wait until the stage is Idle AND its read-back position is at the target.

        This is the correct post-move wait: requiring the read-back to reach the
        commanded target (within tol) makes the premature Idle harmless -- the loop
        keeps polling until the stage has actually arrived. Axes passed as None are
        not checked (e.g. an axis that was not commanded). Raises SmartSEMError on
        timeout, reporting which axis is still off.
        """
        targets = {"x_m": x_m, "y_m": y_m, "z_m": z_m,
                   "tilt_deg": t_deg, "rot_deg": r_deg, "master_z_m": m_m}
        # v0.55.1 (#20): a NaN target used to count as "arrived" (abs(cur - nan) > tol is
        # False). A non-finite target is refused; a non-finite read-back is "not arrived".
        _bad = [f"{k}={tv!r}" for k, tv in targets.items()
                if tv is not None and not math.isfinite(float(tv))]
        if _bad:
            raise SmartSEMError(
                f"wait_stage_arrival: target {', '.join(_bad)} is not a finite number.")
        linear = {"x_m", "y_m", "z_m", "master_z_m"}
        t0 = time.time()
        last_miss = "?"
        while True:
            code = self._get_stage_is_code()
            pos = None
            try:
                pos = self.get_stage_position()
            except Exception:
                pos = None
            miss = None
            if pos is not None:
                for k, tv in targets.items():
                    if tv is None:
                        continue
                    cur = float(getattr(pos, k))
                    tol = tol_m if k in linear else tol_deg
                    if not math.isfinite(cur):
                        miss = f"{k} reads {cur!r}"
                        break
                    if abs(cur - float(tv)) > tol:
                        miss = f"{k} off by {abs(cur - float(tv)):.4g}"
                        break
            else:
                miss = "position unreadable"
            if code == 0 and miss is None:
                return
            last_miss = miss or "busy"
            if time.time() - t0 > timeout_s:
                raise SmartSEMError(
                    f"Timeout waiting for stage arrival ({last_miss}; DP_STAGE_IS={code!r}).")
            time.sleep(poll_s)

    # ----------------------------
    # Imaging helper
    # ----------------------------
    def grab_bmp(
        self,
        filename: str,
        *,
        xoff: int = 0,
        yoff: int = 0,
        width: int = 1024,
        height: int = 768,
        reduction: int = 0,
    ) -> None:
        """Grab an image and save as BMP using Grab()."""
        self._call_ok(
            self.api.Grab,
            int(xoff),
            int(yoff),
            int(width),
            int(height),
            int(reduction),
            str(filename),
        )

    # ----------------------------
    # Convenience waits
    # ----------------------------
    def wait_for_fib_mode(self, desired_state_num: int, timeout_s: float = 60.0, poll_s: float = 0.25) -> None:
        """Wait until DP_FIB_MODE equals desired_state_num."""
        t0 = time.time()
        while True:
            cur = int(self.get_float("DP_FIB_MODE"))
            if cur == desired_state_num:
                return
            if time.time() - t0 > timeout_s:
                raise SmartSEMError(f"Timeout waiting for DP_FIB_MODE == {desired_state_num} (got {cur})")
            time.sleep(poll_s)
