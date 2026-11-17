"""Controller: owns registration + target state and drives the views.

Two pick modes:
  - "landmark": clicks add registration landmark pairs (fixed<->moving).
  - "target":   clicks on the confocal add targets (points of interest, e.g.
                fluorescent features). Targets are mapped moving->fixed px, and
                (with FIB metadata) to FOV-centred layout µm for elymaker.

Refits + rewarps on discrete registration actions; display param changes
re-render from the cached result without refitting. Targets never affect the
registration; they re-map whenever the registration or metadata changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple
import os
import json
import numpy as np

from qtpy.QtCore import QObject, Signal

from ..model import RegistrationSession, LandmarkPair, PreAlign, TransformKind
from ..reader import load_image, to_gray, LoadedImage
from ..warp import apply_prealign
from ..pipeline import register_and_warp, RegistrationResult
from ..transform import RegistrationError
from ..prealign_map import remap_point
from ..composite import (to_uint8, pseudocolor, blend_over,
                         compare_swipe, compare_checkerboard)
from ..session import save_session
from ..meta import FibImageMeta
from ..handoff import write_handoff
from ..confocal import import_confocal, ConfocalImportError
from ..qc import registration_qc, combine_steps
from .. import api
from .. import diag

log = diag.log.getChild("controller")


def _weak(obj):
    """A weak reference (or a callable returning None for None)."""
    if obj is None:
        return lambda: None
    import weakref
    return weakref.ref(obj)


def _file_info(path) -> str:
    try:
        return "%s (%.1f MB)" % (path, os.path.getsize(path) / 2 ** 20)
    except Exception:  # noqa: BLE001
        return str(path)

from PIL import Image


def _inside(path, folder) -> bool:
    """True when path lies inside folder (after resolving . and ..)."""
    p = os.path.normcase(os.path.abspath(str(path)))
    f = os.path.normcase(os.path.abspath(str(folder)))
    try:
        return os.path.commonpath([p, f]) == f
    except ValueError:                    # different drives on Windows
        return False


MODE_LANDMARK = "landmark"
MODE_TARGET = "target"

CMP_BLEND = "blend"
CMP_SWIPE = "swipe"
CMP_CHECKER = "checker"
CMP_FLICKER = "flicker"
COMPARE_MODES = (CMP_BLEND, CMP_SWIPE, CMP_CHECKER, CMP_FLICKER)


@dataclass
class _Slot:
    fixed: Optional[Tuple[float, float]] = None
    moving: Optional[Tuple[float, float]] = None

    def complete(self) -> bool:
        return self.fixed is not None and self.moving is not None


class LandmarkController(QObject):
    fixedImageChanged = Signal(object)     # ndarray
    movingImageChanged = Signal(object)    # ndarray (pre-aligned)
    overlayChanged = Signal(object)        # ndarray RGB
    markersChanged = Signal()              # views re-pull landmark markers
    targetsChanged = Signal()              # views re-pull target markers + table
    residualsChanged = Signal()            # overlay re-draws residual arrows
    channelsChanged = Signal()             # confocal channel list changed (rebuild UI)
    qcChanged = Signal(dict)               # {n, rms, loo, residuals, message, *_um, squash}
    targetsWritten = Signal(str)           # path of a pymap.targets.v2 file just written

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fixed: Optional[LoadedImage] = None
        self._moving: Optional[LoadedImage] = None
        self._moving_pre: Optional[np.ndarray] = None
        self.prealign = PreAlign()
        self.transform = TransformKind.AFFINE
        self.slots: List[_Slot] = []
        self.active: Optional[int] = None
        self.targets: List[Tuple[float, float]] = []     # moving (pre-aligned) coords
        self.active_target: Optional[int] = None
        self.mode = MODE_LANDMARK
        self.meta: Optional[FibImageMeta] = None
        # Set when the fixed image is a pyFIB position record (load_fixed_from_record):
        # {"positions_file", "name", "record", "warnings"}. Only then can targets be
        # written for pyFIB.
        self.fixed_source: Optional[dict] = None
        self.session_dir: Optional[str] = None
        self._qc: Optional[dict] = None
        self._confocal = None
        self.z_projection = "mip"
        self._result: Optional[RegistrationResult] = None
        # display params
        self.cmap = "green"
        self.opacity = 0.5
        self.contrast = (0.0, 1.0)
        # alignment-verification state
        self.compare_mode = CMP_BLEND
        self.split_frac = 0.5
        self.tile = 32
        self.flicker_phase = False
        self.show_residuals = False
        self.residual_scale = 1.0
        # v0.9.2: where self.meta came from ("record" | "file" | None), so a new FIB
        # image never keeps another image's pixel size
        self._meta_source: Optional[str] = None
        # v0.9.2: what the last recompute solved, so a click that leaves the complete
        # pairs unchanged (half a pair, deleting an unpaired point) skips the warp
        self._solved_key = None
        self._flicker_cache = None              # (key, {phase: image})
        self.moving_series: Optional[int] = None   # .lif image index (None = first)
        self._notices: List[str] = []           # for the status bar (pop_notices)

    # ---- mode --------------------------------------------------------------
    def set_mode(self, mode: str):
        self.mode = mode

    # ---- images ------------------------------------------------------------
    def load_fixed(self, path: str):
        """A plain image as the fixed image (no pyFIB record: targets can still be
        picked and exported, but not written for pyFIB)."""
        log.info("load FIB image %s", _file_info(path))
        img = load_image(path)
        log.info("FIB image %s %s", img.data.shape, img.data.dtype)
        self._set_plain_fixed(img)
        self.fixedImageChanged.emit(to_gray(self._fixed.data))
        self.recompute()

    def _set_plain_fixed(self, img):
        """Use img as the fixed image with no pyFIB record. Metadata from a record
        belongs to that record's image and is dropped; metadata from a file is kept
        only if its size matches (v0.9.2: target um and QC used the old pixel size)."""
        self._fixed = img
        self.fixed_source = None
        if self.meta is not None:
            h, w = img.shape_hw
            same = (int(self.meta.image_width), int(self.meta.image_height)) == (w, h)
            if self._meta_source == "record" or not same:
                why = ("it came from a pyFIB record" if self._meta_source == "record"
                       else "it was for a %sx%s image" % (self.meta.image_width,
                                                          self.meta.image_height))
                log.info("FIB metadata dropped: %s; this image is %dx%d", why, w, h)
                self._notices.append("FIB pixel size cleared (%s); um values need "
                                     "Load FIB meta" % why)
                self.meta = None
                self._meta_source = None

    def pop_notices(self) -> List[str]:
        """Notes for the user from the last action (and forget them)."""
        out, self._notices = self._notices, []
        return out

    def load_fixed_from_record(self, positions_file: str, name: str):
        """The fixed image is a pyFIB position record (e.g. the +30 deg FIB overview).

        Its BMP and its own geometry (fib_pixel_size_m, image size) are used, so no
        hand-made metadata is needed. Refuses records pyFIB would refuse as a
        targets source; returns pyMap's warnings (not at SEM normal, ...)."""
        img, meta, source = self._read_fixed_record(positions_file, name)
        self._fixed = img
        self.meta = meta
        self._meta_source = "record"
        self.fixed_source = source
        self.fixedImageChanged.emit(to_gray(self._fixed.data))
        self.recompute()
        return source["warnings"]

    def _read_fixed_record(self, positions_file: str, name: str):
        """(image, meta, fixed_source) for a pyFIB position record; changes nothing."""
        recs = api.load_positions(positions_file)
        if name not in recs:
            raise RegistrationError("no position %r in %s" % (name, positions_file))
        rec = recs[name]
        errors, warnings = api.check_overview_record(rec)
        if errors:
            raise RegistrationError("position %r cannot be used: %s" % (name, "; ".join(errors)))
        img_path = str(api.record_image_path(rec, positions_file))
        log.info("load FIB overview %r from %s: image %s", name, positions_file,
                 _file_info(img_path))
        img = load_image(img_path)
        h, w = img.shape_hw
        if (w, h) != (int(rec["image_width_px"]), int(rec["image_height_px"])):
            raise RegistrationError(
                "position %r: image is %dx%d px but the record says %sx%s"
                % (name, w, h, rec["image_width_px"], rec["image_height_px"]))
        meta = FibImageMeta(image_width=w, image_height=h,
                            fib_pixel_size_m=float(rec["fib_pixel_size_m"]))
        source = {"positions_file": str(positions_file), "name": name,
                  "record": rec, "warnings": warnings}
        return img, meta, source

    def load_moving(self, path: str, series: Optional[int] = None):
        """series: which image of a multi-image .lif (None = the first)."""
        log.info("load confocal %s%s", _file_info(path),
                 "" if series is None else " (image %d)" % (series + 1))
        ci = import_confocal(path, projection=self.z_projection, series=series)
        self.moving_series = series
        log.info("confocal %s: %s, %s", os.path.basename(path), ci.summary(), ci.shape_hw())
        self._confocal = ci
        self._apply_confocal_display()
        if ci.is_multicolor():
            self.cmap = "rgb"          # preserve the merged multicolor confocal
        self._rebuild_moving()
        self.channelsChanged.emit()
        self.recompute()

    def _apply_confocal_display(self):
        """Rebuild the moving LoadedImage from the current channel settings."""
        ci = self._confocal
        disp = ci.moving_display().astype(np.float32)
        self._moving = LoadedImage(data=disp, is_rgb=(disp.ndim == 3),
                                   bit_depth=16, path=ci.source)

    def channel_infos(self):
        """For the GUI: per-channel display state."""
        if self._confocal is None:
            return []
        out = []
        for i, c in enumerate(self._confocal.channels):
            out.append({"index": i, "name": c.name, "color": c.color,
                        "include": c.include, "low": c.low, "high": c.high,
                        "gamma": c.gamma})
        return out

    def set_channel_param(self, index, include=None, low=None, high=None,
                          gamma=None, color=None, commit=True, refresh=True):
        """Update one channel's display. commit=False updates the moving view only
        for live slider drags; commit=True also re-warps the overlay. refresh=False
        only stores the value (the panel debounces refresh_moving_display / commit)."""
        if self._confocal is None:
            return
        if not 0 <= index < len(self._confocal.channels):
            return                      # a stale channel row from a previous image
        ch = self._confocal.channels[index]
        if include is not None:
            ch.include = bool(include)
        if low is not None:
            ch.low = float(low)
        if high is not None:
            ch.high = float(high)
        if gamma is not None:
            ch.gamma = float(gamma)
        if color is not None:
            ch.color = tuple(color)
        # multichannel-ness may have changed (include toggled)
        self.cmap = "rgb" if self._confocal.is_multicolor() else self.cmap
        if not refresh:
            return
        self._apply_confocal_display()
        self._rebuild_moving()          # moving view updates live
        if commit:
            self.recompute()            # re-warp overlay

    def refresh_moving_display(self):
        """Re-merge the channels into the moving view (no re-warp)."""
        if self._confocal is None:
            return
        self._apply_confocal_display()
        self._rebuild_moving()

    def commit_channels(self):
        if self._confocal is None:
            return
        self._apply_confocal_display()  # in case only values were stored
        self._rebuild_moving()
        self.recompute()

    def load_meta(self, path: str):
        self.meta = FibImageMeta.load(path)
        self._meta_source = "file"
        log.info("FIB metadata from %s: %sx%s px, pixel %s m", path, self.meta.image_width,
                 self.meta.image_height, self.meta.fib_pixel_size_m)
        self._refresh_qc()
        self.targetsChanged.emit()

    def _refresh_qc(self):
        """Re-derive the um figures for a new pixel size, without re-warping."""
        if self._result is None:
            return
        session = self._complete_session()
        reg = self._result.registration
        self._qc = registration_qc(
            reg, [p.moving for p in session.pairs], [p.fixed for p in session.pairs],
            self.meta.fib_pixel_size_m if self.meta is not None else None)
        self._emit_qc(reg.n, reg.rms, self._result.loo_rms, reg.residuals.tolist(), "")
        self._solved_key = self._solve_key()      # the warp itself is unchanged

    def _rebuild_moving(self):
        if self._moving is None:
            return
        self._moving_pre = apply_prealign(self._moving.data, self.prealign)
        self.movingImageChanged.emit(self._moving_pre)

    def moving_dims(self):
        h, w = self._moving.data.shape[:2]
        return w, h

    # ---- prealign / transform ---------------------------------------------
    def set_prealign(self, new_pa: PreAlign):
        if self._moving is not None:
            w, h = self.moving_dims()
            for s in self.slots:
                if s.moving is not None:
                    s.moving = remap_point(s.moving, self.prealign, new_pa, w, h)
            self.targets = [remap_point(t, self.prealign, new_pa, w, h)
                            for t in self.targets]
        self.prealign = new_pa
        self._rebuild_moving()
        self.markersChanged.emit()
        self.recompute()

    def set_transform(self, kind: str):
        if kind not in TransformKind.ALL:
            raise RegistrationError("unknown transform %r" % (kind,))
        self.transform = kind
        self.recompute()

    def set_display(self, cmap=None, opacity=None, contrast=None):
        if cmap is not None:
            self.cmap = cmap
        if opacity is not None:
            self.opacity = float(opacity)
        if contrast is not None:
            self.contrast = tuple(contrast)
        self._render_overlay()   # no refit

    # ---- landmark placement ------------------------------------------------
    def place_fixed(self, x: float, y: float):
        if self.active is not None and self.slots[self.active].fixed is None:
            self.slots[self.active].fixed = (x, y)
        else:
            self.slots.append(_Slot(fixed=(x, y)))
            self.active = len(self.slots) - 1
        self._after_edit()

    def place_moving(self, x: float, y: float):
        if self.active is not None and self.slots[self.active].moving is None:
            self.slots[self.active].moving = (x, y)
        else:
            self.slots.append(_Slot(moving=(x, y)))
            self.active = len(self.slots) - 1
        self._after_edit()

    def move_fixed(self, index: int, x: float, y: float):
        self.slots[index].fixed = (x, y)

    def move_moving(self, index: int, x: float, y: float):
        self.slots[index].moving = (x, y)

    def commit_move(self):
        self.recompute()

    def select(self, index: int):
        self.active = index
        self.markersChanged.emit()

    def delete_active(self):
        if self.active is None:
            return
        self.slots.pop(self.active)
        self.active = (len(self.slots) - 1) if self.slots else None
        self._after_edit()

    def clear(self):
        self.slots.clear()
        self.active = None
        self._after_edit()

    def _after_edit(self):
        self.markersChanged.emit()
        if self._key_equal(self._solve_key(), self._solved_key):
            log.debug("complete pairs unchanged: no re-warp")
            return
        self.recompute()

    def _solve_key(self):
        """What a recompute depends on. Images are held weakly and compared by
        identity, so the key never keeps an old image alive (v0.9.2 review: a strong
        reference held a replaced 100 MB confocal array)."""
        px = self.meta.fib_pixel_size_m if self.meta is not None else None
        pairs = tuple((s.fixed, s.moving) for s in self.slots if s.complete())
        return (pairs, self.transform,
                (self.prealign.rot90, self.prealign.flip_h, self.prealign.flip_v, px),
                _weak(self._fixed), _weak(self._moving))

    @staticmethod
    def _key_equal(a, b):
        if a is None or b is None or a[:3] != b[:3]:
            return False
        return all(x() is y() for x, y in zip(a[3:], b[3:])) and \
            a[3]() is not None and a[4]() is not None

    # ---- target placement --------------------------------------------------
    def place_target(self, x: float, y: float):
        self.targets.append((x, y))
        self.active_target = len(self.targets) - 1
        self.targetsChanged.emit()

    def move_target(self, index: int, x: float, y: float):
        self.targets[index] = (x, y)

    def commit_target_move(self):
        self.targetsChanged.emit()

    def select_target(self, index: int):
        self.active_target = index
        self.targetsChanged.emit()

    def delete_active_target(self):
        if self.active_target is None:
            return
        self.targets.pop(self.active_target)
        self.active_target = (len(self.targets) - 1) if self.targets else None
        self.targetsChanged.emit()

    def clear_targets(self):
        self.targets.clear()
        self.active_target = None
        self.targetsChanged.emit()

    # ---- marker accessors --------------------------------------------------
    def fixed_markers(self):
        return [{"index": i, "x": s.fixed[0], "y": s.fixed[1]}
                for i, s in enumerate(self.slots) if s.fixed is not None]

    def moving_markers(self):
        return [{"index": i, "x": s.moving[0], "y": s.moving[1]}
                for i, s in enumerate(self.slots) if s.moving is not None]

    def target_solutions(self):
        """Per target: moving coords, mapped FIB px (if solved), layout µm (if meta)."""
        reg = self._result.registration if self._result is not None else None
        out = []
        for i, (mx, my) in enumerate(self.targets):
            fixed = None
            um = None
            if reg is not None:
                fx, fy = reg.forward(np.array([[mx, my]], dtype=float))[0]
                fixed = (float(fx), float(fy))
                if self.meta is not None:
                    xm, ym = self.meta.px_to_fov_m(fx, fy)
                    um = (xm * 1e6, ym * 1e6)
            out.append({"index": i, "moving": (mx, my), "fixed": fixed, "um": um,
                        "active": i == self.active_target})
        return out

    def map_cursor(self, x: float, y: float):
        """Map a moving-space cursor to (fib_px|None, um|None) for live readout."""
        if self._result is None:
            return None, None
        fx, fy = self._result.registration.forward(np.array([[x, y]], dtype=float))[0]
        um = None
        if self.meta is not None:
            xm, ym = self.meta.px_to_fov_m(fx, fy)
            um = (xm * 1e6, ym * 1e6)
        return (float(fx), float(fy)), um

    # ---- registration ------------------------------------------------------
    def _complete_session(self) -> RegistrationSession:
        s = RegistrationSession(
            fixed_path=self._fixed.path if self._fixed else "",
            moving_path=self._moving.path if self._moving else "",
            prealign=self.prealign, transform=self.transform, meta_path="",
        )
        for slot in self.slots:
            if slot.complete():
                s.pairs.append(LandmarkPair(slot.fixed, slot.moving))
        s.targets = list(self.targets)
        return s

    def recompute(self):
        """Fit, warp and QC the current pairs; then redraw the overlay, targets and
        residuals. Never leaves the views half-updated: an unexpected error is logged
        with its traceback and shown in the fit-quality box (v0.9.2)."""
        self._result = None
        self._qc = None
        self._flicker_cache = None
        self._solved_key = self._solve_key()
        try:
            if self._fixed is None or self._moving is None:
                self._emit_qc(0, None, None, [], "load both images")
                return
            session = self._complete_session()
            n = len(session.pairs)
            need = TransformKind.MIN_PAIRS[self.transform]
            if n < need:
                self._emit_qc(n, None, None, [],
                              "%s needs >= %d pairs" % (self.transform, need))
                return
            try:
                log.info("register %s with %d pairs: FIB %s, confocal %s",
                         self.transform, n, self._fixed.data.shape,
                         self._moving.data.shape)
                self._result = register_and_warp(session, self._fixed, self._moving)
                reg = self._result.registration
                log.info("registered: rms %.3f px, leave-one-out %s px", reg.rms,
                         "-" if self._result.loo_rms is None
                         else "%.3f" % self._result.loo_rms)
                self._qc = registration_qc(
                    reg, [p.moving for p in session.pairs],
                    [p.fixed for p in session.pairs],
                    self.meta.fib_pixel_size_m if self.meta is not None else None)
                self._emit_qc(reg.n, reg.rms, self._result.loo_rms,
                              reg.residuals.tolist(), "")
            except RegistrationError as e:
                log.info("registration refused: %s", e)
                self._result = None
                self._qc = None
                self._emit_qc(n, None, None, [], str(e))
            except Exception as e:  # noqa: BLE001
                log.exception("registration failed (%s, %d pairs)", self.transform, n)
                self._result = None
                self._qc = None
                self._emit_qc(n, None, None, [], "registration failed: %s: %s (details "
                              "in pymap.log)" % (type(e).__name__, e))
        finally:
            self._render_overlay()
            self.targetsChanged.emit()
            self.residualsChanged.emit()

    def _emit_qc(self, n, rms, loo, residuals, message):
        q = self._qc or {}
        self.qcChanged.emit({"n": n, "rms": rms, "loo": loo,
                             "residuals": residuals, "message": message,
                             "rms_um": q.get("rms_um"), "loo_um": q.get("loo_rms_um"),
                             "loo_grid_um": q.get("loo_rms_grid_um"),
                             "squash": q.get("squash_ratio")})

    def qc(self) -> Optional[dict]:
        """The latest registration QC (registration_qc), or None if unsolved."""
        return dict(self._qc) if self._qc else None

    def _underlay_rgb(self):
        """The plain blended overlay (FIB + pseudocolored confocal). Used for the
        handoff / export regardless of the on-screen compare mode."""
        if self._result is not None:
            return self._result.overlay(cmap=self.cmap, opacity=self.opacity,
                                        contrast=self.contrast)
        if self._fixed is not None:
            g = to_gray(self._fixed.data)
            return np.stack([g] * 3, axis=-1)
        return None

    def _compare_layers(self):
        fib = np.stack([to_gray(self._fixed.data)] * 3, axis=-1).astype(np.float32)
        conf = pseudocolor(self._result.warped, cmap=self.cmap, contrast=self.contrast)
        return fib, conf, self._result.alpha

    def set_compare(self, mode=None, split=None, tile=None, flicker_phase=None):
        if mode is not None:
            self.compare_mode = mode
        if split is not None:
            self.split_frac = float(split)
        if tile is not None:
            self.tile = int(tile)
        if flicker_phase is not None:
            self.flicker_phase = bool(flicker_phase)
        self._render_overlay()

    def set_residuals(self, visible=None, scale=None):
        if visible is not None:
            self.show_residuals = bool(visible)
        if scale is not None:
            self.residual_scale = float(scale)
        self.residualsChanged.emit()

    def residual_vectors(self):
        """Per complete pair: fixed point and where the moving point maps to."""
        if self._result is None or not self.show_residuals:
            return []
        reg = self._result.registration
        out = []
        for s in self.slots:
            if s.complete():
                mx, my = reg.forward(np.array([s.moving], dtype=float))[0]
                out.append({"fx": s.fixed[0], "fy": s.fixed[1],
                            "mx": float(mx), "my": float(my)})
        return out

    def _render_overlay(self):
        if self._result is None:
            if self._fixed is not None:
                g = to_gray(self._fixed.data)
                self.overlayChanged.emit(np.stack([g] * 3, axis=-1))
            return
        m = self.compare_mode
        if m == CMP_FLICKER and self._flicker_cache is not None and \
                self._flicker_cache[0] == (id(self._result), self.cmap, self.opacity,
                                           tuple(self.contrast)):
            self.overlayChanged.emit(self._flicker_cache[1][bool(self.flicker_phase)])
            return
        fib, conf, alpha = self._compare_layers()
        if m == CMP_SWIPE:
            img = compare_swipe(fib, conf, alpha, self.split_frac)
        elif m == CMP_CHECKER:
            img = compare_checkerboard(fib, conf, alpha, self.tile)
        elif m == CMP_FLICKER:
            # the two frames are rendered once and reused on every tick (v0.9.2: each
            # 450 ms tick re-rendered the whole overlay, 0.5 s at 3072x2304)
            key = (id(self._result), self.cmap, self.opacity, tuple(self.contrast))
            cache = self._flicker_cache
            if cache is None or cache[0] != key:
                cache = (key, {True: fib, False: blend_over(fib, conf * alpha[..., None],
                                                            alpha, self.opacity)})
                self._flicker_cache = cache
            img = cache[1][bool(self.flicker_phase)]
        else:
            img = self._result.overlay(cmap=self.cmap, opacity=self.opacity,
                                       contrast=self.contrast)
        self.overlayChanged.emit(img)

    # ---- io ----------------------------------------------------------------
    def save_session_to(self, path: str):
        d = self._complete_session().to_dict()
        if self.fixed_source is not None:       # so a reload re-links the pyFIB overview
            d["pyfib_source"] = {
                "positions_file": os.path.abspath(self.fixed_source["positions_file"]),
                "name": self.fixed_source["name"]}
        if self.moving_series is not None:      # which image of a multi-image .lif
            d["moving_series"] = int(self.moving_series)
        with open(path, "w") as f:
            json.dump(d, f, indent=2)

    def load_session_from(self, path: str) -> List[str]:
        """Load landmarks, targets and images from a session file. Returns notes
        for the user (an overview that could not be re-linked, a missing image).

        Everything is read and checked first; pyMap's state changes only once all of
        it loaded, so a bad file leaves the current work untouched (v0.9.2)."""
        with open(path) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("%s is not a pyMap session (no JSON object)" % path)
        session = RegistrationSession.from_dict(d)
        notes: List[str] = []
        log.info("load session %s: %s, %d pairs, %d targets", path, session.transform,
                 len(session.pairs), len(session.targets))

        # 1) read the images (nothing changes yet)
        fixed = None                     # ("record", img, meta, source) | ("plain", img)
        src = d.get("pyfib_source")
        if isinstance(src, dict) and src.get("positions_file") and src.get("name"):
            try:
                img, meta, source = self._read_fixed_record(src["positions_file"],
                                                            src["name"])
                fixed = ("record", img, meta, source)
                notes += source["warnings"]
            except Exception as e:  # noqa: BLE001
                notes.append("pyFIB overview %r not re-linked: %s" % (src.get("name"), e))
        if fixed is None and session.fixed_path:
            if os.path.isfile(session.fixed_path):
                fixed = ("plain", load_image(session.fixed_path))
            else:
                notes.append("FIB image not found: %s" % session.fixed_path)
        ci = None
        series = d.get("moving_series")
        series = int(series) if isinstance(series, int) else None
        if session.moving_path and os.path.isfile(session.moving_path):
            ci = import_confocal(session.moving_path, projection=self.z_projection,
                                 series=series)
        elif session.moving_path:
            notes.append("confocal image not found: %s" % session.moving_path)

        # 2) commit
        self.prealign = session.prealign
        self.transform = session.transform
        self.slots = [_Slot(fixed=tuple(p.fixed), moving=tuple(p.moving))
                      for p in session.pairs]
        self.active = (len(self.slots) - 1) if self.slots else None
        self.targets = [tuple(t) for t in session.targets]
        self.active_target = (len(self.targets) - 1) if self.targets else None
        if fixed is not None and fixed[0] == "record":
            _, self._fixed, self.meta, self.fixed_source = fixed
            self._meta_source = "record"
        elif fixed is not None:
            self._set_plain_fixed(fixed[1])
        elif self.fixed_source is not None:
            # these landmarks are not known to belong to the overview on screen
            self.fixed_source = None
            notes.append("pyFIB overview link cleared; reopen the overview to write targets")
        if fixed is not None:
            self.fixedImageChanged.emit(to_gray(self._fixed.data))
        if ci is not None:
            self._confocal = ci
            self.moving_series = series
            self._apply_confocal_display()
            if ci.is_multicolor():
                self.cmap = "rgb"
        self._rebuild_moving()
        self.channelsChanged.emit()
        self.markersChanged.emit()
        self.recompute()
        for n_ in notes:
            log.info("session note: %s", n_)
        return notes

    def export_to(self, out_dir: str) -> dict:
        if self._result is None:
            raise RegistrationError("nothing to export - registration not solved")
        os.makedirs(out_dir, exist_ok=True)
        overlay = to_uint8(self._result.overlay(cmap=self.cmap,
                                                opacity=self.opacity,
                                                contrast=self.contrast))
        Image.fromarray(overlay).save(os.path.join(out_dir, "overlay.png"))
        rgba = to_uint8(self._result.warped_rgba(cmap=self.cmap,
                                                 contrast=self.contrast))
        # uint8 HxWx4 -> RGBA is inferred (mode= is removed in Pillow 13)
        Image.fromarray(rgba).save(
            os.path.join(out_dir, "confocal_in_fib.png"))
        self.save_session_to(os.path.join(out_dir, "session.json"))
        reg = self._result.registration
        report = {"transform": reg.kind, "n_pairs": reg.n, "rms_px": reg.rms,
                  "leave_one_out_rms_px": self._result.loo_rms,
                  "per_point_residual_px": reg.residuals.tolist()}
        with open(os.path.join(out_dir, "report.json"), "w") as f:
            json.dump(report, f, indent=2)
        return report

    # ---- targets for pyFIB (pymap.targets.v2) -------------------------------
    def targets_dir(self, session_dir: Optional[str] = None) -> str:
        """``<session>/clem/`` when a session is known, else the overview's
        positions-file folder (which is ``<session>/clem/`` for pyFIB overviews)."""
        sd = session_dir or self.session_dir
        if sd:
            return os.path.join(str(sd), "clem")
        if self.fixed_source is None:
            raise RegistrationError("no session folder and no pyFIB overview loaded")
        return os.path.dirname(os.path.abspath(self.fixed_source["positions_file"]))

    def build_targets_doc(self, session_dir: Optional[str] = None) -> dict:
        """The pymap.targets.v2 document for the current registration and targets.
        Refuses (RegistrationError) when pyFIB could not use it."""
        if self.fixed_source is None:
            raise RegistrationError(
                "open the FIB overview from a pyFIB positions file first "
                "(targets must be tied to a pyFIB capture)")
        if self._result is None or self._qc is None:
            raise RegistrationError("registration not solved yet")
        q = self._qc
        if q.get("loo_rms_grid_um") is None and q.get("loo_error"):
            raise RegistrationError("the registration error cannot be measured: %s"
                                    % q["loo_error"])
        if q.get("loo_rms_grid_um") is None:
            need = TransformKind.MIN_PAIRS[self.transform] + 1
            raise RegistrationError(
                "the registration error cannot be measured yet: %s needs at least %d "
                "landmark pairs for the leave-one-out check" % (self.transform, need))
        sols = [t for t in self.target_solutions() if t["fixed"] is not None]
        if not sols:
            raise RegistrationError("no targets picked")
        if len(sols) > api.MAX_TARGETS:
            raise RegistrationError("%d targets: at most %d per grid"
                                    % (len(sols), api.MAX_TARGETS))
        tdir = self.targets_dir(session_dir)
        fs = self.fixed_source
        if not _inside(fs["positions_file"], os.path.dirname(tdir)):
            raise RegistrationError(
                "the FIB overview (%s) is not part of this session (%s); reopen the "
                "overview from this session's positions file"
                % (fs["positions_file"], os.path.dirname(tdir)))
        source = api.fib_source_from_record(fs["record"], positions_file=fs["positions_file"],
                                            targets_dir=tdir, source_id="ov1",
                                            position_name=fs["name"])
        step = {"from": "confocal", "to": "fib:%s" % fs["name"],
                "image_sha256": source["image_sha256"], "kind": q["kind"],
                "n_pairs": q["n_pairs"],
                "rms_um": q["rms_um"], "loo_rms_um": q["loo_rms_um"],
                "rms_grid_um": q["rms_grid_um"], "loo_rms_grid_um": q["loo_rms_grid_um"],
                "squash_ratio": q["squash_ratio"]}
        reg = {"chain": [step]}
        reg.update(combine_steps([step]))
        worst = reg["combined_loo_rms_grid_um"]
        qc_warn = []
        if worst is not None and worst > api.QC_REFUSE_UM:
            qc_warn.append("registration error %.2f um on the grid exceeds %.0f um "
                           "(pyFIB refuses unless overridden)" % (worst, api.QC_REFUSE_UM))
        elif worst is not None and worst > api.QC_WARN_UM:
            qc_warn.append("registration error %.2f um on the grid exceeds %.0f um"
                           % (worst, api.QC_WARN_UM))
        W, H = source["image_width_px"], source["image_height_px"]
        ps = source["fib_pixel_size_m"]
        targets = []
        for k, t in enumerate(sols):
            fx, fy = t["fixed"]
            warn = list(qc_warn)
            if not (0.0 <= fx < W and 0.0 <= fy < H):
                warn.append("outside the overview image")
            lu = api.layout_um((fx, fy), W, H, ps)
            targets.append({"id": "T%d" % (k + 1), "label": str(k + 1), "source": "ov1",
                            "fib_px": [round(fx, 3), round(fy, 3)],
                            "layout_um": [round(lu[0], 3), round(lu[1], 3)],
                            "warnings": warn, "note": ""})
        ci = self._confocal
        confocal = {"file": os.path.basename(ci.source) if ci else "",
                    "path": api.normalise_rel_path(ci.source) if ci else "",
                    "channels": [c.name for c in ci.display_channels()] if ci and ci.channels else [],
                    "projection": getattr(ci, "projection", None),
                    "prealign": {"rot90": self.prealign.rot90, "flip_h": self.prealign.flip_h,
                                 "flip_v": self.prealign.flip_v}}
        return api.new_targets_doc(sources=[source], targets=targets,
                                   confocal=confocal, registration=reg)

    def write_targets_file(self, session_dir: Optional[str] = None) -> str:
        """Write ``<session>/clem/targets_<overview>.json`` + its ready marker (and the
        pyMap session beside it, so the registration can be reopened). Returns the
        path and emits targetsWritten."""
        doc = self.build_targets_doc(session_dir)
        tdir = self.targets_dir(session_dir)
        name = self.fixed_source["name"]
        path = os.path.join(tdir, api.targets_filename(name))
        self.save_session_to(os.path.join(tdir, "pymap_session_%s.json"
                                          % api.targets_filename(name)[len("targets_"):-5]))
        api.write_targets(path, doc)
        self.targetsWritten.emit(path)
        return path

    def export_handoff(self, out_dir: str, ely_out: str = "lamella.ely") -> dict:
        if self.meta is None:
            raise RegistrationError(
                "load FIB metadata (pixel size) before exporting an elymaker handoff")
        underlay = self._underlay_rgb()
        if underlay is None:
            raise RegistrationError("load the FIB image first")
        tpx = [tuple(s["fixed"]) for s in self.target_solutions()
               if s["fixed"] is not None]
        return write_handoff(out_dir, underlay, self.meta, ely_out,
                             targets_fib_px=tpx)
