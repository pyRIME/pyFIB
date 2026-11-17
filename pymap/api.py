"""pyMap's file-level API for pyFIB.

Importing this module pulls in only the standard library: no Qt, no scikit-image,
no tifffile. ``locate_site`` / ``locate_targets`` import numpy and Pillow when they
are called (pyFIB already depends on both). pyFIB's command-line tools can therefore
read and check pyMap's files without pyMap's GUI or registration stack.

What lives here (agreed in the pyMap <-> pyFIB plan and pyFIB's review, 2026-10-09):

* ``pymap.targets.v2`` targets files, with a ``sources`` list (tiling-ready; one
  ``fib_position`` source for now) and a ``pymap.ready.v1`` ready marker beside each.
* Reading pyFIB position records (``smartfib_executor.positions.v1/v2``), with the
  overview checks pyFIB also applies, and Windows path normalisation.
* Reading pyFIB's SEM overview records (``pyfib.sem_overviews.v1``).
* ``locate_targets``: where the targets sit in a site image taken at the same tilt
  as the overview (pyMap finds; pyFIB moves).

Conventions
-----------
* Pixel coordinates are CORNER convention: pixel (0, 0) spans [0, 1); x right, y
  down; the image centre is (W/2, H/2). Same as elymaker's ``gui/coords.py`` and
  pyFIB's ``report/map_export.py``.
* ``layout_um`` is FOV-centred, +X right, +Y up (elymaker layout space).
* Paths written into a targets file are relative to the targets file's folder and
  use '/' separators.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from . import __version__

TARGETS_SCHEMA = "pymap.targets.v2"
READY_SCHEMA = "pymap.ready.v1"
PYFIB_POSITIONS_SCHEMAS = ("smartfib_executor.positions.v2",
                           "smartfib_executor.positions.v1")
SEM_OVERVIEWS_SCHEMA = "pyfib.sem_overviews.v1"
SOURCE_KINDS = ("fib_position",)          # sem_tiff etc. only once pyFIB accepts them
MAX_TARGETS = 30
PIXEL_CONVENTION = ("corner: pixel (0,0) spans [0,1); x right, y down; "
                    "image centre = (W/2, H/2)")

# Overview limits from pyFIB's review (pyFIB enforces them; pyMap reports them early).
MAX_SCAN_ROTATION_DEG = 0.05
MAX_BEAM_SHIFT_M = 0.01e-6
SEM_NORMAL_TILT_DEG = (29.0, 31.0)
SEM_NORMAL_ROT_DEG = 12.0
ROT_TOL_DEG = 1.0
# Registration QC limits (on the grid plane, worst axis): warn / refuse-unless-override.
QC_WARN_UM = 1.0
QC_REFUSE_UM = 3.0


class PyMapApiError(ValueError):
    """A pyMap file or record is missing, malformed, or fails a check."""


# --------------------------------------------------------------------------- #
# Hashing, atomic writes, ready markers
# --------------------------------------------------------------------------- #
def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write_json(path, obj) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))
    return path


def ready_marker_path(path) -> Path:
    """``<file>.ready.json`` beside the file (same pattern as elymaker.ready.v1)."""
    return Path(str(path) + ".ready.json")


def write_ready_marker(path, extra: Optional[dict] = None) -> Path:
    """Publish the ready marker for ``path``: 'this file is complete', nothing more."""
    path = Path(path)
    marker = {"schema": READY_SCHEMA, "file": path.name,
              "content_hash": sha256_file(path), "pymap_version": __version__,
              "created": _now_iso()}
    if extra:
        marker.update(extra)
    return _atomic_write_json(ready_marker_path(path), marker)


def read_ready_marker(path) -> dict:
    mp = ready_marker_path(path)
    m = json.loads(mp.read_text(encoding="utf-8"))
    if m.get("schema") != READY_SCHEMA:
        raise PyMapApiError("%s is not a %s marker (schema=%r)"
                            % (mp, READY_SCHEMA, m.get("schema")))
    return m


def ready_marker_ok(path) -> Optional[bool]:
    """True if the marker matches the file's current hash, False if it does not,
    None if there is no marker."""
    if not ready_marker_path(path).exists():
        return None
    try:
        return read_ready_marker(path).get("content_hash") == sha256_file(path)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def normalise_rel_path(p: str) -> str:
    """Windows separators -> '/', and no leading './'. ``images\\position_10.bmp``
    becomes ``images/position_10.bmp``."""
    s = str(p).replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def _is_absolute(p: str) -> bool:
    return os.path.isabs(p) or PureWindowsPath(p).is_absolute()


def resolve_path(p, base_dir) -> Path:
    """Resolve a stored path (any separator style) against ``base_dir``."""
    s = str(p)
    if _is_absolute(s):
        return Path(s)
    return Path(base_dir) / normalise_rel_path(s)


def relpath_posix(target, start) -> str:
    return normalise_rel_path(os.path.relpath(str(target), str(start)))


# --------------------------------------------------------------------------- #
# pyFIB position records
# --------------------------------------------------------------------------- #
def _as_dict(rec) -> dict:
    if isinstance(rec, dict):
        return rec
    if is_dataclass(rec):
        return asdict(rec)
    return dict(vars(rec))


def load_positions(positions_file) -> Dict[str, dict]:
    """``{name: record dict}`` from a pyFIB positions file (v1 or v2)."""
    data = json.loads(Path(positions_file).read_text(encoding="utf-8"))
    if data.get("schema") not in PYFIB_POSITIONS_SCHEMAS:
        raise PyMapApiError("%s: unexpected schema %r (expected one of %s)"
                            % (positions_file, data.get("schema"),
                               PYFIB_POSITIONS_SCHEMAS))
    return {str(k): dict(v) for k, v in data.get("positions", {}).items()}


def record_image_path(record, positions_file) -> Path:
    """The record's BMP, resolved like pyFIB does (relative to the positions file's
    folder), with Windows separators normalised."""
    r = _as_dict(record)
    img = r.get("image_bmp")
    if not img:
        raise PyMapApiError("position %r has no image_bmp" % r.get("name"))
    return resolve_path(img, Path(positions_file).resolve().parent)


def check_overview_record(record) -> Tuple[List[str], List[str]]:
    """(errors, warnings) for using a pyFIB record as a targets source.

    Errors mirror pyFIB's refusals (v1 record, scan rotation > 0.05 deg, beam shift
    > 0.01 um, missing image or tilt). Warnings flag what pyFIB's stage model would
    refuse later (not at SEM normal, rotation not 12 deg)."""
    r = _as_dict(record)
    errors: List[str] = []
    warnings: List[str] = []
    if not r.get("image_bmp"):
        errors.append("no reference image (image_bmp)")
    if r.get("fib_pixel_size_m") is None:
        errors.append("no fib_pixel_size_m (v1 record?): re-capture with pyFIB")
    if not r.get("image_width_px") or not r.get("image_height_px"):
        errors.append("no image_width_px / image_height_px")
    rot = r.get("fib_scan_rotation_deg")
    if rot is None:
        warnings.append("FIB scan rotation not recorded")
    elif abs(float(rot)) > MAX_SCAN_ROTATION_DEG:
        errors.append("FIB scan rotation %.3f deg (limit %.2f)"
                      % (float(rot), MAX_SCAN_ROTATION_DEG))
    for ax in ("x", "y"):
        v = r.get("fib_beam_shift_%s_m" % ax)
        if v is None:
            warnings.append("FIB beam shift %s not recorded" % ax.upper())
        elif abs(float(v)) > MAX_BEAM_SHIFT_M:
            errors.append("FIB beam shift %s %.3f um (limit %.2f)"
                          % (ax.upper(), float(v) * 1e6, MAX_BEAM_SHIFT_M * 1e6))
    st = r.get("stage_at") or {}
    t, rr = st.get("t_deg"), st.get("r_deg")
    if t is None:
        errors.append("no stage tilt recorded")
    elif not (SEM_NORMAL_TILT_DEG[0] <= float(t) <= SEM_NORMAL_TILT_DEG[1]):
        warnings.append("stage tilt %.2f deg is not SEM normal (%g-%g)"
                        % (float(t), SEM_NORMAL_TILT_DEG[0], SEM_NORMAL_TILT_DEG[1]))
    if rr is not None and abs(float(rr) - SEM_NORMAL_ROT_DEG) > ROT_TOL_DEG:
        warnings.append("stage rotation %.2f deg (stage model needs %.0f +/- %.0f)"
                        % (float(rr), SEM_NORMAL_ROT_DEG, ROT_TOL_DEG))
    return errors, warnings


def fib_source_from_record(record, *, positions_file, targets_dir, source_id: str = "ov1",
                           position_name: Optional[str] = None) -> dict:
    """A ``sources[]`` entry for a pyFIB FIB overview record. Raises PyMapApiError
    if the record fails ``check_overview_record``."""
    r = _as_dict(record)
    errors, _warn = check_overview_record(r)
    if errors:
        raise PyMapApiError("position %r cannot be a targets source: %s"
                            % (r.get("name"), "; ".join(errors)))
    img = record_image_path(r, positions_file)
    st = r["stage_at"]
    return {
        "id": str(source_id),
        "kind": "fib_position",
        "positions_file": relpath_posix(Path(positions_file).resolve(), Path(targets_dir).resolve()),
        "position": str(position_name or r.get("name")),
        "image": relpath_posix(img.resolve(), Path(targets_dir).resolve()),
        "image_sha256": sha256_file(img),
        "image_width_px": int(r["image_width_px"]),
        "image_height_px": int(r["image_height_px"]),
        "fib_pixel_size_m": float(r["fib_pixel_size_m"]),
        "captured_unix_s": (float(r["captured_unix_s"])
                            if r.get("captured_unix_s") is not None else None),
        "stage_at": {k: (float(st[k]) if st.get(k) is not None else None)
                     for k in ("x_m", "y_m", "z_m", "t_deg", "r_deg")},
    }


# --------------------------------------------------------------------------- #
# Targets files
# --------------------------------------------------------------------------- #
def layout_um(fib_px: Sequence[float], width_px: int, height_px: int,
              pixel_size_m: float) -> Tuple[float, float]:
    """Image px (corner convention) -> FOV-centred um, +Y up (elymaker layout)."""
    x, y = float(fib_px[0]), float(fib_px[1])
    ps_um = float(pixel_size_m) * 1e6
    return ((x - width_px / 2.0) * ps_um, (height_px / 2.0 - y) * ps_um)


def targets_filename(source_position: str) -> str:
    """``targets_<overview position>.json`` (one file per overview source)."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(source_position))
    return "targets_%s.json" % safe


def new_targets_doc(*, sources: List[dict], targets: List[dict], confocal: dict,
                    registration: dict) -> dict:
    return {
        "schema": TARGETS_SCHEMA,
        "pymap_version": __version__,
        "created": _now_iso(),
        "sources": list(sources),
        "confocal": dict(confocal),
        "registration": dict(registration),
        "pixel_convention": PIXEL_CONVENTION,
        "targets": list(targets),
    }


def validate_targets_doc(doc: dict) -> List[str]:
    """Structural problems in a targets document (empty list = fine)."""
    errs: List[str] = []
    if doc.get("schema") != TARGETS_SCHEMA:
        errs.append("schema is %r, expected %r" % (doc.get("schema"), TARGETS_SCHEMA))
    sources = doc.get("sources")
    if not isinstance(sources, list) or not sources:
        errs.append("sources must be a non-empty list")
        sources = []
    ids = set()
    for s in sources:
        sid = s.get("id")
        if not sid:
            errs.append("a source has no id")
        elif sid in ids:
            errs.append("duplicate source id %r" % sid)
        ids.add(sid)
        if s.get("kind") not in SOURCE_KINDS:
            errs.append("source %r: unknown kind %r" % (sid, s.get("kind")))
        for k in ("image", "image_sha256", "image_width_px", "image_height_px",
                  "fib_pixel_size_m", "positions_file", "position"):
            if s.get(k) in (None, ""):
                errs.append("source %r: missing %s" % (sid, k))
    targets = doc.get("targets")
    if not isinstance(targets, list):
        errs.append("targets must be a list")
        targets = []
    if len(targets) > MAX_TARGETS:
        errs.append("%d targets (at most %d per grid)" % (len(targets), MAX_TARGETS))
    tids = set()
    for t in targets:
        tid = t.get("id")
        if not tid:
            errs.append("a target has no id")
        elif tid in tids:
            errs.append("duplicate target id %r" % tid)
        tids.add(tid)
        if t.get("source") not in ids:
            errs.append("target %r: unknown source %r" % (tid, t.get("source")))
        px = t.get("fib_px")
        if (not isinstance(px, (list, tuple)) or len(px) != 2
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in px)):
            errs.append("target %r: fib_px must be two finite numbers" % tid)
    reg = doc.get("registration")
    if not isinstance(reg, dict) or not isinstance(reg.get("chain"), list):
        errs.append("registration.chain must be a list")
    return errs


def write_targets(path, doc: dict) -> Path:
    """Validate, write atomically, then publish the ready marker. Returns the path."""
    errs = validate_targets_doc(doc)
    if errs:
        raise PyMapApiError("invalid targets document: " + "; ".join(errs))
    path = _atomic_write_json(path, doc)
    write_ready_marker(path, extra={"n_targets": len(doc["targets"])})
    return path


def read_targets(path, *, require_ready: bool = True) -> dict:
    """Load and validate a targets file. With ``require_ready`` the ready marker must
    exist and match the file's hash (a file still being written, or edited since,
    is refused)."""
    path = Path(path)
    if require_ready:
        ok = ready_marker_ok(path)
        if ok is None:
            raise PyMapApiError("%s has no ready marker" % path)
        if not ok:
            raise PyMapApiError("%s does not match its ready marker" % path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    errs = validate_targets_doc(doc)
    if errs:
        raise PyMapApiError("%s: %s" % (path, "; ".join(errs)))
    return doc


def check_source_against_record(source: dict, record, *, targets_dir) -> List[str]:
    """Does a targets source still describe this record? (image hash and size,
    capture time and stage echoes). Returns mismatches; empty = same capture."""
    r = _as_dict(record)
    out: List[str] = []
    img = resolve_path(source["image"], targets_dir)
    if not img.exists():
        out.append("image %s not found" % img)
    elif sha256_file(img) != source.get("image_sha256"):
        out.append("image hash changed")
    for k in ("image_width_px", "image_height_px"):
        if r.get(k) is not None and int(r[k]) != int(source.get(k, -1)):
            out.append("%s %s != %s" % (k, r.get(k), source.get(k)))
    cap = source.get("captured_unix_s")
    if cap is not None and r.get("captured_unix_s") is not None \
            and abs(float(r["captured_unix_s"]) - float(cap)) > 1e-3:
        out.append("captured_unix_s differs (re-captured?)")
    st, se = r.get("stage_at") or {}, source.get("stage_at") or {}
    for k, tol in (("x_m", 1e-9), ("y_m", 1e-9), ("z_m", 1e-9), ("t_deg", 1e-6), ("r_deg", 1e-6)):
        if st.get(k) is not None and se.get(k) is not None \
                and abs(float(st[k]) - float(se[k])) > tol:
            out.append("stage %s differs" % k)
    return out


# --------------------------------------------------------------------------- #
# pyFIB SEM overview records (file owned by pyFIB)
# --------------------------------------------------------------------------- #
def load_sem_overviews(path) -> List[dict]:
    """Entries of a ``pyfib.sem_overviews.v1`` file, each with ``pixel_size_m``
    filled in (from ``field_width_m / image_width_px`` if not given) and a
    ``pixel_size_nominal`` flag (True unless the file says it was measured)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != SEM_OVERVIEWS_SCHEMA:
        raise PyMapApiError("%s: unexpected schema %r (expected %r)"
                            % (path, data.get("schema"), SEM_OVERVIEWS_SCHEMA))
    out = []
    raw = data.get("overviews", data.get("entries", []))
    for e in raw:
        e = dict(e)
        if not e.get("name") or not e.get("image") or not e.get("image_width_px"):
            raise PyMapApiError("%s: an SEM overview lacks name / image / image_width_px" % path)
        if e.get("pixel_size_m") is None:
            fw = e.get("field_width_m")
            if not fw:
                raise PyMapApiError("SEM overview %r has neither pixel_size_m nor "
                                    "field_width_m" % e["name"])
            e["pixel_size_m"] = float(fw) / float(e["image_width_px"])
        e["pixel_size_nominal"] = bool(e.get("pixel_size_nominal", True))
        e["image"] = normalise_rel_path(e["image"])
        out.append(e)
    return out


# --------------------------------------------------------------------------- #
# Locating targets on a site image (pyMap finds; pyFIB moves)
# --------------------------------------------------------------------------- #
class LocatedTarget(NamedTuple):
    id: str
    x_px: float       # site-image px, corner convention
    y_px: float
    score: float      # match margin, 0..1 (see locate_site); 0 = do not trust


@dataclass
class SiteMatch:
    """Where a site image sits inside the overview (same tilt; translation only)."""
    x0: float          # overview px of the site image's top-left corner
    y0: float
    fx: float          # overview px per site px (x, y)
    fy: float
    peak: float        # normalised cross-correlation at the match (-1..1)
    second: float      # best competing peak outside the match neighbourhood
    at_edge: bool      # match on the border of the search box (unreliable)

    @property
    def score(self) -> float:
        """Match margin: peak minus the best competing peak, 0 if at the search edge.
        A periodic grid that matches equally well one square over scores ~0."""
        if self.at_edge:
            return 0.0
        return float(max(0.0, min(1.0, self.peak - max(self.second, 0.0))))

    def overview_to_site(self, x: float, y: float) -> Tuple[float, float]:
        return ((x - self.x0) / self.fx, (y - self.y0) / self.fy)

    def site_to_overview(self, u: float, v: float) -> Tuple[float, float]:
        return (self.x0 + u * self.fx, self.y0 + v * self.fy)


def _load_gray255(path):
    import numpy as np
    from PIL import Image
    with Image.open(str(path)) as im:
        if im.mode in ("I;16", "I;16B", "I;16L", "I"):
            a = np.asarray(im, dtype=np.float64)
            m = a.max()
            return a * (255.0 / m) if m > 255 else a
        return np.asarray(im.convert("L"), dtype=np.float64)


def _databar_row(img, min_keep_frac: float = 0.5) -> Optional[int]:
    """First row of the SmartSEM data bar (bright separator), as pyFIB detects it
    (refine/registration.detect_databar_row): a row mean > 250 in the lower half."""
    import numpy as np
    start = int(img.shape[0] * min_keep_frac)
    bright = np.where(img[start:, :].mean(axis=1) > 250.0)[0]
    return None if bright.size == 0 else int(start + bright[0])


def _crop_bar(img):
    row = _databar_row(img)
    return img if (row is None or row < 3) else img[:row, :]


def _resize(img, w: int, h: int):
    import numpy as np
    from PIL import Image
    shrink = w < img.shape[1] or h < img.shape[0]
    filt = Image.Resampling.BOX if shrink else Image.Resampling.BILINEAR
    out = Image.fromarray(img.astype(np.float32)).resize((int(w), int(h)), filt)  # mode F
    return np.asarray(out, dtype=np.float64)


def _ncc_valid(search, templ):
    """Normalised cross-correlation of ``templ`` at every top-left offset where it
    fits inside ``search`` (FFT + integral images)."""
    import numpy as np
    sh, sw = search.shape
    th, tw = templ.shape
    t = templ - templ.mean()
    tnorm = math.sqrt(float((t * t).sum()))
    if tnorm == 0:
        raise PyMapApiError("site image has no contrast to match")
    shape = (sh + th - 1, sw + tw - 1)
    conv = np.fft.irfft2(np.fft.rfft2(search, shape) * np.fft.rfft2(t[::-1, ::-1], shape), shape)
    corr = conv[th - 1:sh, tw - 1:sw]
    ii = np.zeros((sh + 1, sw + 1)); ii[1:, 1:] = search.cumsum(0).cumsum(1)
    ii2 = np.zeros((sh + 1, sw + 1)); ii2[1:, 1:] = (search * search).cumsum(0).cumsum(1)

    def wsum(a):
        return a[th:, tw:] - a[:-th, tw:] - a[th:, :-tw] + a[:-th, :-tw]
    n = th * tw
    s1, s2 = wsum(ii), wsum(ii2)
    var = np.maximum(s2 - s1 * s1 / n, 0.0)
    denom = np.sqrt(var) * tnorm
    with np.errstate(invalid="ignore", divide="ignore"):
        ncc = np.where(denom > 1e-12, corr / denom, 0.0)
    return ncc


def _parabolic(vm: float, v0: float, vp: float) -> float:
    d = vm - 2.0 * v0 + vp
    return 0.0 if abs(d) < 1e-12 else max(-0.5, min(0.5, 0.5 * (vm - vp) / d))


def locate_site(overview, overview_px_m: float, site, site_px_m: float, *,
                expected_centre: Optional[Tuple[float, float]] = None,
                search_radius_px: Optional[float] = None) -> SiteMatch:
    """Find a site image inside an overview taken at the SAME tilt and rotation.

    ``overview`` / ``site`` are 2-D arrays or image paths. The site image is shrunk
    to the overview's pixel size (scale from the two pixel sizes, no fitting) and
    matched by normalised cross-correlation. ``expected_centre`` (overview px) and
    ``search_radius_px`` restrict the search, which matters on a periodic grid.
    The SmartSEM data bar is cropped from both images first."""
    import numpy as np
    ov = _crop_bar(_load_gray255(overview) if not hasattr(overview, "shape")
                   else np.asarray(overview, dtype=np.float64))
    st = _crop_bar(_load_gray255(site) if not hasattr(site, "shape")
                   else np.asarray(site, dtype=np.float64))
    scale = float(site_px_m) / float(overview_px_m)          # overview px per site px
    tw = int(round(st.shape[1] * scale))
    th = int(round(st.shape[0] * scale))
    if tw < 16 or th < 16:
        raise PyMapApiError("site image is only %dx%d overview px: too small to match"
                            % (tw, th))
    if tw > ov.shape[1] or th > ov.shape[0]:
        raise PyMapApiError("site image (%dx%d overview px) is larger than the overview"
                            % (tw, th))
    templ = _resize(st, tw, th)
    fx, fy = tw / float(st.shape[1]), th / float(st.shape[0])

    # search box (top-left offsets of the template)
    x_lo, y_lo, x_hi, y_hi = 0, 0, ov.shape[1] - tw, ov.shape[0] - th
    if expected_centre is not None:
        r = float(search_radius_px if search_radius_px is not None else max(tw, th))
        ex = expected_centre[0] - tw / 2.0
        ey = expected_centre[1] - th / 2.0
        x_lo, x_hi = max(0, int(math.floor(ex - r))), min(x_hi, int(math.ceil(ex + r)))
        y_lo, y_hi = max(0, int(math.floor(ey - r))), min(y_hi, int(math.ceil(ey + r)))
        if x_hi < x_lo or y_hi < y_lo:
            raise PyMapApiError("expected site position is outside the overview")
    search = ov[y_lo:y_hi + th, x_lo:x_hi + tw]
    ncc = _ncc_valid(search, templ)
    iy, ix = np.unravel_index(int(np.argmax(ncc)), ncc.shape)
    peak = float(ncc[iy, ix])
    at_edge = (expected_centre is not None and
               (ix == 0 or iy == 0 or ix == ncc.shape[1] - 1 or iy == ncc.shape[0] - 1)
               and ncc.shape[0] > 2 and ncc.shape[1] > 2)
    dx = _parabolic(ncc[iy, ix - 1], peak, ncc[iy, ix + 1]) if 0 < ix < ncc.shape[1] - 1 else 0.0
    dy = _parabolic(ncc[iy - 1, ix], peak, ncc[iy + 1, ix]) if 0 < iy < ncc.shape[0] - 1 else 0.0
    # best competing peak outside a quarter-template neighbourhood
    ry, rx = max(2, th // 4), max(2, tw // 4)
    masked = ncc.copy()
    masked[max(0, iy - ry):iy + ry + 1, max(0, ix - rx):ix + rx + 1] = -np.inf
    second = float(masked.max()) if np.isfinite(masked).any() else -1.0
    return SiteMatch(x0=x_lo + ix + dx, y0=y_lo + iy + dy, fx=fx, fy=fy,
                     peak=peak, second=second, at_edge=bool(at_edge))


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def locate_targets(overview_source: dict, site_record, targets: Iterable[dict], *,
                   targets_dir=None, site_positions_file=None,
                   centre_target: Optional[str] = None,
                   search_fraction: float = 0.5,
                   overview_image=None, site_image=None) -> List[LocatedTarget]:
    """Where each target lies in a site image taken at the overview's tilt.

    overview_source : a ``sources[]`` entry of the targets file.
    site_record     : the pyFIB record of the site image (dict or PositionRecord);
                      its image is resolved against ``site_positions_file``.
    targets         : target dicts (``id``, ``fib_px`` in overview px, ``source``).
                      Only those on ``overview_source`` are located.
    centre_target   : id of the target pyFIB centred on (default: the first); the
                      search is limited to ``search_fraction`` of the site field
                      around it, so a periodic grid cannot pull the match a square
                      over.
    overview_image / site_image : pre-loaded arrays (testing, or images already in
                      memory); otherwise images are read from disk.

    Returns ``[(id, x_px, y_px, score)]`` in site-image px (corner convention). The
    score is the match margin (0..1, see SiteMatch.score) and is the same for every
    target of one site image; pyFIB thresholds it and falls back to the image centre
    + an operator click below the threshold.
    """
    sid = overview_source.get("id")
    tlist = [t for t in targets if t.get("source", sid) == sid]
    if not tlist:
        return []
    if overview_image is None:
        if targets_dir is None:
            raise PyMapApiError("targets_dir is needed to find the overview image")
        overview_image = resolve_path(overview_source["image"], targets_dir)
    if site_image is None:
        if site_positions_file is None:
            raise PyMapApiError("site_positions_file is needed to find the site image")
        site_image = record_image_path(site_record, site_positions_file)
    site_px = _get(site_record, "fib_pixel_size_m")
    if site_px is None:
        raise PyMapApiError("site record has no fib_pixel_size_m")
    centre = next((t for t in tlist if t.get("id") == centre_target), tlist[0])
    ov_px = float(overview_source["fib_pixel_size_m"])
    sw = _get(site_record, "image_width_px")
    if sw is None and hasattr(site_image, "shape"):
        sw = site_image.shape[1]
    radius = None
    if sw:
        radius = float(search_fraction) * float(sw) * float(site_px) / ov_px
    m = locate_site(overview_image, ov_px, site_image, float(site_px),
                    expected_centre=(float(centre["fib_px"][0]), float(centre["fib_px"][1])),
                    search_radius_px=radius)
    out = []
    for t in tlist:
        u, v = m.overview_to_site(float(t["fib_px"][0]), float(t["fib_px"][1]))
        out.append(LocatedTarget(str(t["id"]), u, v, m.score))
    return out
