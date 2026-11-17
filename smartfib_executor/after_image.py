"""After-mill image (v0.41.0): one fresh FIB frame of a site once its milling is done.

Off by default (mill-positions --after-image, or the Mill tab box). Called by the
batch right after milling completes and BEFORE the beam shift is reset, so the frame
is in the same beam-shifted view the pattern was milled in and the report can draw
the pattern on it.

The sequence is the capture tab's, which the instrument already runs after every
"Mill fiducial here" (mill, then capture):
  1. only if SmartSEM no longer indicates milling (otherwise skip -- never interfere);
  2. frame_grab.ensure_fib_imaging (v0.54.0, bug #14): CMD_FIB_MODE_FIB only if not
     already in FIB imaging, wait for "FIB", then until SmartSEM's magnification reset
     has landed;
  3. AP_FIB_MAGNIFICATION = the position's capture magnification;
  4. frame_grab.grab_fresh_frame (the single-frame protocol): one pass, finished by an
     end-of-frame freeze, then Grab(); the scan is left FROZEN (no further dose).
  5. v0.54.0: if the FIB magnification changed during the frame (FIB imaging still on,
     SmartSEM not indicating milling -- checked again), steps 2-4 run ONCE more (one more
     frame). Any other change, or a second one, keeps it as *_suspect.bmp.
Dose: one FIB frame at the current scan rate (two if the column changed), as a capture.

It NEVER raises: any problem freezes the scan (best effort), returns a warning, and
the batch carries on -- the site was already milled.

v0.51.0: with ``stage`` (rough | prepolish | polish) the file is named
<name>_<n>-<stage>_<time>.bmp (mill_stages.after_image_path); without it, the
pre-v0.51.0 <name>_after_<time>.bmp.

v0.53.1 (Evan, 2026-10-11: "when SmartFIB completes a mill it often resets to SEM imaging
and changes the FIB magnification to very low"):
  * Before anything is changed, what SmartFIB left is logged: imaging mode, FIB
    magnification, objective, and the FIB probe readings that answer (DP_FIB_PROBE,
    DP_FIB_IMAGE_PROBE, DP_FIB_MILL_PROBE, AP_FIB_PROBE_CURRENT -- read only; which of
    them works on this instrument is not known yet).
  * The magnification is set, READ BACK and retried once; the objective (``objective_v``:
    the value in effect before milling) is set and read back. If either does not read
    back (magnification within 1 %, objective within OBJECTIVE_TOL_V) the image is
    SKIPPED with a warning -- never a wrong-scale or defocused reference.
  * After the grab, the magnification and imaging mode are read again; a change during
    the frame renames it <...>_suspect.bmp (never used as a reference).
  * A sidecar <image>.bmp.json (pyfib.after_image.v1) records all of it; the next stage
    refuses a reference whose recorded magnification is not the position's.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

META_SCHEMA = "pyfib.after_image.v1"
MAG_TOL_FRAC = 0.01        # magnification read-back vs the position's capture mag
# Objective read-back tolerance. NOT MEASURED: the read-back precision of
# AP_FIB_OBJECTIVE_POTENTIAL is unknown; the Log prints set vs read-back so it can be
# measured and tightened (v0.53.1).
OBJECTIVE_TOL_V = 2.0
# Read-only FIB probe readings logged after milling (Remote API manual 2007: the DP_ ones
# are 0-15 enums "1 pA" ... "50 nA"; AP_FIB_PROBE_CURRENT is unlisted). Which of these
# answer on the Crossbeam 550 is to be found -- see the project bug list.
PROBE_READS = (("DP_FIB_PROBE", "str"), ("DP_FIB_IMAGE_PROBE", "str"),
               ("DP_FIB_MILL_PROBE", "str"), ("AP_FIB_PROBE_CURRENT", "float"))


def after_image_path(images_dir, name: str, t: Optional[float] = None) -> Path:
    stamp = datetime.fromtimestamp(time.time() if t is None else t).strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(name))
    return Path(images_dir) / f"{safe}_after_{stamp}.bmp"


def meta_path(image_path) -> Path:
    """The sidecar of an after image: <image>.bmp.json."""
    return Path(str(image_path) + ".json")


def read_meta(image_path) -> Optional[Dict[str, Any]]:
    try:
        d = json.loads(meta_path(image_path).read_text(encoding="utf-8"))
        return d if d.get("schema") == META_SCHEMA else None
    except Exception:  # noqa: BLE001
        return None


def meta_problem(image_path, rec) -> Optional[str]:
    """Why this after image must not be a reference for ``rec`` (None = fine, or no
    sidecar -- images from before v0.53.1 are accepted as they were)."""
    m = read_meta(image_path)
    if m is None:
        return None
    want = float(getattr(rec, "fib_magnification_x", 0) or 0)
    got = m.get("fib_magnification_readback")
    if got is None or want <= 0 or abs(float(got) - want) > MAG_TOL_FRAC * want:
        return f"was taken at {got}x, not the position's {want:g}x"
    return None


def _fval(sem, mnemonic) -> Optional[float]:
    try:
        v = float(sem.get_float(mnemonic))
        return v if v == v else None
    except Exception:  # noqa: BLE001
        return None


def _sval(sem, mnemonic) -> Optional[str]:
    try:
        return str(sem.get_str(mnemonic)).strip()
    except Exception:  # noqa: BLE001
        return None


def smartfib_state(sem) -> Dict[str, Any]:
    """What the column reads right now (read only): imaging mode, FIB magnification,
    objective, and the FIB probe readings that answer."""
    st: Dict[str, Any] = {"imaging": _sval(sem, "DP_FIB_IMAGING"),
                          "fib_magnification": _fval(sem, "AP_FIB_MAGNIFICATION"),
                          "objective_v": _fval(sem, "AP_FIB_OBJECTIVE_POTENTIAL")}
    probes = {}
    for mn, kind in PROBE_READS:
        probes[mn] = _sval(sem, mn) if kind == "str" else _fval(sem, mn)
    st["probe"] = probes
    return st


def _state_text(st: Dict[str, Any]) -> str:
    def f(v, fmt):
        return "unreadable" if v is None else fmt.format(v)
    pr = ", ".join(f"{k}=" + ("unreadable" if v is None else repr(v))
                   for k, v in st["probe"].items())
    return (f"imaging {f(st['imaging'], '{!r}')}, FIB mag {f(st['fib_magnification'], '{:g}x')}, "
            f"objective {f(st['objective_v'], '{:.1f} V')}; probe: {pr}")


def _write_meta(image_path, data: Dict[str, Any], log) -> None:
    try:
        meta_path(image_path).write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log(f"[after-image] WARNING: could not write {meta_path(image_path).name} ({e})")


def _freeze(sem) -> None:
    try:
        sem.execute("CMD_FREEZE_ZONE")
    except Exception:  # noqa: BLE001
        try:
            sem.execute("CMD_FREEZE_ALL")
        except Exception:  # noqa: BLE001
            pass


def grab_after_mill_image(sem, rec, images_dir, *,
                          milling_active: Callable[[], bool],
                          fib_image_timeout_s: float = 15.0,
                          settle_s: float = 0.25,
                          frame_wait_s: float = 3.0,
                          frozen_timeout_s: float = 60.0,
                          clock: Callable[[], float] = time.time,
                          sleep: Callable[[float], None] = time.sleep,
                          log: Callable[[str], None] = print,
                          stage: Optional[str] = None,
                          objective_v: Optional[float] = None) -> Tuple[Optional[Path], Optional[str]]:
    """Grab the after-mill frame. Returns (path or None, warning or None). Never raises.

    ``objective_v``: the objective to restore before the frame (the value in effect
    before milling); None leaves the objective as it is (and says so)."""
    name = getattr(rec, "name", "?")
    try:
        if milling_active():
            return None, (f"after-mill image for {name} skipped: SmartSEM still indicates "
                          f"milling.")
    except Exception as e:  # noqa: BLE001
        return None, f"after-mill image for {name} skipped: milling state unreadable ({e})."

    try:
        if stage:
            from .mill_stages import after_image_path as _stage_path
            target = _stage_path(images_dir, name, stage, clock())
        else:
            target = after_image_path(images_dir, name, clock())
    except Exception as e:  # noqa: BLE001
        return None, f"after-mill image for {name} skipped: {e}"
    meta: Dict[str, Any] = {"schema": META_SCHEMA, "position": name, "stage": stage}
    try:
        left = smartfib_state(sem)
        meta["smartfib_left"] = left
        log(f"[after-image] SmartFIB left: {_state_text(left)}")
    except Exception as e:  # noqa: BLE001
        log(f"[after-image] could not read the column state ({e})")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)

        from .frame_grab import (FibSwitchTimeout, ensure_fib_imaging, grab_fresh_frame,
                                 is_stale_warning)
        want_mag = float(rec.fib_magnification_x)
        w = int(getattr(rec, "image_width_px", None) or 1024)
        h = int(getattr(rec, "image_height_px", None) or 768)
        warn: Optional[str] = None
        changed = False
        post_mag = post_img = None
        for attempt in (1, 2):
            # v0.54.0 (bug #14): the shared switch -- CMD_FIB_MODE_FIB only if not already
            # in FIB imaging, then wait until SmartSEM's magnification reset has landed.
            try:
                ensure_fib_imaging(sem, timeout_s=float(fib_image_timeout_s), log=log,
                                   clock=clock, sleep=sleep)
            except FibSwitchTimeout as e:
                _freeze(sem)
                return None, f"after-mill image for {name} skipped: {e}"

            # Magnification: set, read back, retry once (a set can be ignored right after
            # milling). The frame is the next stage's reference: never at the wrong scale.
            mag_rb = None
            for _ in range(2):
                sem.set_value("AP_FIB_MAGNIFICATION", want_mag)
                sleep(0.3)
                mag_rb = _fval(sem, "AP_FIB_MAGNIFICATION")
                if mag_rb is not None and abs(mag_rb - want_mag) <= MAG_TOL_FRAC * want_mag:
                    break
            meta.update(fib_magnification_set=want_mag, fib_magnification_readback=mag_rb)
            if mag_rb is None or abs(mag_rb - want_mag) > MAG_TOL_FRAC * want_mag:
                _freeze(sem)
                return None, (f"after-mill image for {name} skipped: FIB magnification "
                              f"reads {mag_rb}x after setting {want_mag:g}x (twice).")
            # Objective: back to the value used before milling.
            if objective_v is not None:
                sem.set_value("AP_FIB_OBJECTIVE_POTENTIAL", float(objective_v))
                obj_rb = _fval(sem, "AP_FIB_OBJECTIVE_POTENTIAL")
                meta.update(objective_set_v=float(objective_v), objective_readback_v=obj_rb)
                if obj_rb is None or abs(obj_rb - float(objective_v)) > OBJECTIVE_TOL_V:
                    _freeze(sem)
                    return None, (f"after-mill image for {name} skipped: objective reads "
                                  f"{obj_rb} V after setting {float(objective_v):.1f} V.")
                log(f"[after-image] FIB {want_mag:g}x (read back {mag_rb:g}x), objective "
                    f"{float(objective_v):.1f} V (read back {obj_rb:.1f} V)")
            else:
                meta["objective_readback_v"] = _fval(sem, "AP_FIB_OBJECTIVE_POTENTIAL")
                log(f"[after-image] FIB {want_mag:g}x (read back {mag_rb:g}x); objective "
                    f"left as it is ({meta['objective_readback_v']} V)")
            if settle_s > 0:
                sleep(float(settle_s))

            warn = grab_fresh_frame(sem, str(target), width=w, height=h,
                                    frame_wait_s=frame_wait_s,
                                    frozen_timeout_s=frozen_timeout_s, log=log)
            try:
                from .positions import _recover_grab_file
                w2 = _recover_grab_file(sem, target)
                if w2:
                    warn = (warn + " " if warn else "") + w2
            except Exception:  # noqa: BLE001
                pass
            if not target.exists():
                return None, (f"after-mill image for {name}: Grab() produced no file at "
                              f"{target}.")
            # Did anything change during the frame?
            post_mag = _fval(sem, "AP_FIB_MAGNIFICATION")
            post_img = _sval(sem, "DP_FIB_IMAGING")
            changed = (post_mag is None or abs(post_mag - want_mag) > MAG_TOL_FRAC * want_mag
                       or (post_img is not None and post_img.upper() != "FIB"))
            if changed and attempt == 1:
                # v0.54.0: once more from the switch (bug #14: SmartSEM's late reset) --
                # but ONLY for a magnification change with FIB imaging still on, and only
                # if SmartSEM still does not indicate milling (review: a mill resuming
                # between pattern parts also changes the imaging mode).
                meta["first_frame_changed"] = {"fib_magnification_after_grab": post_mag,
                                               "imaging_after_grab": post_img}
                still_fib = post_img is not None and post_img.upper() == "FIB"
                try:
                    milling_now = bool(milling_active())
                except Exception:  # noqa: BLE001
                    milling_now = True              # unreadable counts as milling
                if still_fib and not milling_now:
                    log(f"[after-image] the FIB magnification changed during the frame "
                        f"({post_mag}x); setting it up again and taking one more frame.")
                    continue
                log(f"[after-image] the column changed during the frame (FIB mag "
                    f"{post_mag}x, imaging {post_img!r})"
                    + ("; SmartSEM indicates milling" if milling_now else "")
                    + " -- not retaking it.")
                break
            break
        meta.update(fib_magnification_after_grab=post_mag, imaging_after_grab=post_img,
                    grab_warning=warn, time=datetime.fromtimestamp(clock()).isoformat(
                        timespec="seconds"))
        if changed:
            sus = target.with_name(target.stem + "_suspect.bmp")
            try:
                target.replace(sus)
            except Exception:  # noqa: BLE001
                sus = target
            _write_meta(sus, meta, log)
            return None, (f"after-mill image for {name}: the column changed during the "
                          f"frame (FIB mag {post_mag}x, imaging {post_img!r}); saved as "
                          f"{sus.name}, NOT used as a reference.")
        if warn and ((stage and "incomplete or stale" in warn) or is_stale_warning(warn)):
            # v0.53.4: a frame whose scan never restarted is the PREVIOUS frame store.
            # v0.54.0: so is a frame that ended too soon or repeats the previous grab.
            # v0.51.0: a staged after image is the NEXT stage's refine reference; an
            # unconfirmed frame is kept for inspection but renamed so it is never used.
            sus = target.with_name(target.stem + "_suspect.bmp")
            try:
                target.replace(sus)
            except Exception:  # noqa: BLE001
                sus = target
            _write_meta(sus, meta, log)
            return None, (f"{warn} Saved as {sus.name}, NOT used as the next stage's "
                          f"reference -- record the after image again.")
        _write_meta(target, meta, log)
        return target, warn
    except Exception as e:  # noqa: BLE001
        _freeze(sem)
        return None, f"after-mill image for {name} FAILED ({type(e).__name__}: {e}); scan frozen."
