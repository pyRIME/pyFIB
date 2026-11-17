"""Markup trenches at SEM normal for CLEM targets (v0.53.0).

Evan, 2026-10-10: "We often mill trenches at SEM normal with the FIB. These trenches
help center a lamella at a position when we take it to tilt." The standard pattern is
Markup_Standard.ely: two vertical trenches (1.04 x ~28 um, 27.76 um apart centre to
centre) and a small tick on the RIGHT trench that marks where the final lamella goes
(midway between the trenches, at the tick's height). It is milled at the correlation
magnification, and several markups go into the same low-mag correlative image.

So pyFIB does what the operator does by hand in SmartFIB: the stage stays at the FIB
overview position, and ONE SmartFIB layout holds a copy of the markup for every marked
target, each shifted so its origin lands on the target in the overview field. No stage
motion, no stage model, no 400x stage calibration.

Conventions (locked elsewhere, tested here):
  * A markup pattern's ORIGIN is the target (where the lamella goes). The bundled
    assets/patterns/markup_standard.ely is Markup_Standard.ely translated so that the
    origin is midway between the trench centres at the tick's height; shapes, probe,
    dose and depth are byte-for-byte the operator's.
  * Overview pixel -> layout um (elymaker coords.ImageCalibration, FOV-centred, +X right,
    +Y up, BMP top row = +Y):  x = (px - W/2) * ps,  y = (H/2 - py) * ps.

Checks (files only; refuse = not milled):
  * the markup file has exactly one STRUCTURE with one LAYER_REFERENCE (frame centre 0,
    no instances) and only RECTs at 0 deg and POLYGONs (others refused for now);
  * every shape is at least MARKUP_FIELD_MARGIN_UM inside the overview field;
  * no shape comes within MARKUP_TARGET_CLEARANCE_UM of ANY marked target point (its own
    included): a trench never mills over a lamella site;
  * the targets file passed clem.targets.plan_targets (ready marker, hashes, overview
    record, registration error), and only its ok/warn rows can be marked.

The combined file is <session>/clem/markup/<overview>_markup.ely with a sidecar
<file>.pyfib.json recording what pyFIB generated (sha256, inputs). If the operator edits
the file in ELYMaker, pyFIB keeps the edit (state "edited"): it is milled as saved, after
the same checks against the targets it was generated for; if the targets file or the
overview changed since, it is refused until "Reset markup" regenerates it.

Milling (mill_markup, ARM-gated through the CLI): not milling for 5 s, stage at SEM
normal AND still at the overview record's position (X/Y/Z/M within 1 um, T/R within
0.05 deg), FIB beam shift zeroed and read back 0, scan rotation 0, FIB magnification =
the overview's (read back), objective = the overview's; then one SmartFIB layout through
the standard drop-folder path, then one after image at the overview magnification.
Recorded in <session>/clem/pyfib_clem_markups.json (pyfib.clem_markups.v1).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import IMAGES_DIRNAME, clem_dir
from . import limits as L

MARKUP_DIRNAME = "markup"
MARKUPS_LOG = "pyfib_clem_markups.json"
MARKUP_FILE_SCHEMA = "pyfib.clem_markup_file.v1"
MARKUPS_SCHEMA = "pyfib.clem_markups.v1"
SIDECAR_SUFFIX = ".pyfib.json"

DEFAULT_MARKUP = (Path(__file__).resolve().parent.parent / "assets" / "patterns"
                  / "markup_standard.ely")


class MarkupError(ValueError):
    """The markup pattern or plan is not acceptable (files only; nothing was milled)."""


class MarkupRefused(RuntimeError):
    """A live check refused milling (nothing was milled)."""


# ============================================================ pattern file handling
@dataclass
class ShapeBox:
    tag: str
    xmin: float
    xmax: float
    ymin: float
    ymax: float

    def shifted(self, dx: float, dy: float) -> "ShapeBox":
        return ShapeBox(self.tag, self.xmin + dx, self.xmax + dx, self.ymin + dy,
                        self.ymax + dy)


_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_NUM_RE = re.compile(r"^\s*" + _NUM + r"\s*$")
_POINT_RE = re.compile(r"\(\s*(" + _NUM + r")[\s,]+(" + _NUM + r")\s*\)")
_POINTS_FULL_RE = re.compile(r"^\s*(?:\(\s*" + _NUM + r"[\s,]+" + _NUM + r"\s*\)\s*)+$")
_SHAPE_TAG_RE = re.compile(r"<(RECT|POLYGON)\b[^>]*>")
_LAYER_REF_RE = re.compile(r"(<LAYER_REFERENCE\b[^>]*>)(.*?)(</LAYER_REFERENCE>)", re.S)


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _read_text(path) -> Tuple[str, str]:
    raw = Path(path).read_bytes()
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise MarkupError(f"{path}: cannot decode")             # pragma: no cover


def _num(v, what: str) -> float:
    if not _NUM_RE.match(str(v)):
        raise MarkupError(f"{what}: not a plain number ({v!r})")
    try:
        x = float(str(v).strip())
    except Exception:  # noqa: BLE001
        raise MarkupError(f"{what}: not a number ({v!r})")
    if not math.isfinite(x):
        raise MarkupError(f"{what}: not finite ({v!r})")
    return x


def _angle_deg(v) -> float:
    s = str(v or "0").strip().lower().replace("deg", "").strip()
    return _num(s or "0", "RECT angle")


def _points(raw: str) -> List[Tuple[float, float]]:
    if not _POINTS_FULL_RE.match(raw or ""):
        raise MarkupError(f"POLYGON points are not a plain list of (x y) pairs: {raw!r}")
    return [(_num(a, "POLYGON point"), _num(b, "POLYGON point"))
            for a, b in _POINT_RE.findall(raw or "")]


def shapes_from_text(text: str, *, what: str = "markup") -> List[ShapeBox]:
    """Bounding boxes (um) of every shape. Raises MarkupError for anything outside the
    supported subset (see module doc)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise MarkupError(f"{what}: not a well-formed .ely ({e})")
    if root.tag != "ELAYOUT":
        raise MarkupError(f"{what}: root element {root.tag!r}, expected ELAYOUT")
    structs = root.findall("./STRUCTURE_LIST/STRUCTURE")
    if len(structs) != 1:
        raise MarkupError(f"{what}: {len(structs)} structures; a markup has exactly one")
    st = structs[0]
    il = st.find("INSTANCE_LIST")
    if il is not None and len(list(il)):
        raise MarkupError(f"{what}: structure instances are not supported in a markup")
    lrs = st.findall("LAYER_REFERENCE")
    if len(lrs) != 1:
        raise MarkupError(f"{what}: {len(lrs)} layer references; a markup has exactly one")
    lr = lrs[0]
    for k in ("frame_cx", "frame_cy"):
        if abs(_num(lr.get(k, "0"), k)) > 1e-9:
            raise MarkupError(f"{what}: {k} = {lr.get(k)}; a markup's frame centre must be 0")
    out: List[ShapeBox] = []
    for el in lr:
        if el.tag == "RECT":
            if abs(_angle_deg(el.get("angle"))) > 1e-9:
                raise MarkupError(f"{what}: a rotated RECT ({el.get('angle')}) is not "
                                  f"supported in a markup")
            x, y = _num(el.get("x"), "RECT x"), _num(el.get("y"), "RECT y")
            w, h = _num(el.get("width"), "RECT width"), _num(el.get("height"), "RECT height")
            out.append(ShapeBox("RECT", min(x, x + w), max(x, x + w),
                                min(y, y + h), max(y, y + h)))
        elif el.tag == "POLYGON":
            pts = _points(el.get("points", ""))
            if len(pts) < 3:
                raise MarkupError(f"{what}: a POLYGON with {len(pts)} points")
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            out.append(ShapeBox("POLYGON", min(xs), max(xs), min(ys), max(ys)))
        else:
            raise MarkupError(f"{what}: {el.tag} is not supported in a markup (RECT at "
                              f"0 deg and POLYGON only)")
    if not out:
        raise MarkupError(f"{what}: no shapes")
    return out


def read_markup(path) -> Tuple[str, List[ShapeBox]]:
    text, _enc = _read_text(path)
    return text, shapes_from_text(text, what=Path(path).name)


def bbox(shapes: Sequence[ShapeBox]) -> Tuple[float, float, float, float]:
    return (min(s.xmin for s in shapes), max(s.xmax for s in shapes),
            min(s.ymin for s in shapes), max(s.ymax for s in shapes))


def _fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _shift_attr(tag: str, name: str, dv: float) -> str:
    pat = re.compile(r'(\s' + name + r'=")([^"]*)(")')
    new, n = pat.subn(lambda m: m.group(1) + _fmt(_num(m.group(2), name) + dv) + m.group(3),
                      tag, count=1)
    if n != 1:
        raise MarkupError(f"shape without a {name} attribute: {tag[:60]}...")
    return new


def _shift_tag(tag: str, dx: float, dy: float) -> str:
    if tag.startswith("<RECT"):
        return _shift_attr(_shift_attr(tag, "x", dx), "y", dy)
    pat = re.compile(r'(\spoints=")([^"]*)(")')

    def pts(m):
        inner = _POINT_RE.sub(lambda p: f"({_fmt(_num(p.group(1), 'x') + dx)} "
                                        f"{_fmt(_num(p.group(2), 'y') + dy)})", m.group(2))
        return m.group(1) + inner + m.group(3)
    new, n = pat.subn(pts, tag, count=1)
    if n != 1:
        raise MarkupError(f"POLYGON without points: {tag[:60]}...")
    return new


def combine(base_text: str, offsets_um: Sequence[Tuple[float, float]]) -> str:
    """One layout with a copy of the base's shapes at every offset (um). Everything else
    in the file (exposure, probe, dose, depth, header) is kept byte for byte; only the
    RECT x/y and POLYGON points of the copies change. Verified by re-reading."""
    base_shapes = shapes_from_text(base_text, what="base markup")
    m = list(_LAYER_REF_RE.finditer(base_text))
    if len(m) != 1:
        raise MarkupError("base markup: expected one LAYER_REFERENCE block")
    m = m[0]
    inner = m.group(2)
    body = inner.rstrip()
    tail = inner[len(body):]
    copies = [_SHAPE_TAG_RE.sub(lambda t: _shift_tag(t.group(0), dx, dy), body)
              for dx, dy in offsets_um]
    out = base_text[:m.start(2)] + "".join(copies) + tail + base_text[m.end(2):]
    got = shapes_from_text(out, what="combined markup")
    n = len(base_shapes)
    if len(got) != n * len(offsets_um):
        raise MarkupError(f"combined markup has {len(got)} shapes, expected "
                          f"{n * len(offsets_um)}")
    for k, (dx, dy) in enumerate(offsets_um):
        for j, s in enumerate(base_shapes):
            g, e = got[k * n + j], s.shifted(dx, dy)
            if max(abs(g.xmin - e.xmin), abs(g.xmax - e.xmax), abs(g.ymin - e.ymin),
                   abs(g.ymax - e.ymax)) > 1e-5:
                raise MarkupError("combined markup failed its self-check (shape moved by "
                                  "the wrong amount)")
    return out


def pixel_to_layout_um(px: float, py: float, *, width_px: int, height_px: int,
                       pixel_size_m: float) -> Tuple[float, float]:
    """elymaker's ImageCalibration.pixel_to_layout with the FOV centre at 0, in um."""
    ps = float(pixel_size_m) * 1e6
    return ((float(px) - width_px / 2.0) * ps, (height_px / 2.0 - float(py)) * ps)


def match_markups(shapes: Sequence[ShapeBox], base: Sequence[ShapeBox], rows,
                  tol_um: float = 5.0) -> Tuple[List[Any], int]:
    """Which targets carry a copy of the base markup in ``shapes`` (each base shape found
    within ``tol_um`` of where it belongs, same kind and size), and how many shapes belong
    to no such copy. Used for a layout edited in ELYMaker: what is milled is decided by
    the file's content, not by what pyFIB generated."""
    used, found = set(), []
    for r in rows:
        if r.x_um is None:
            continue
        idx = []
        for b in base:
            e = b.shifted(r.x_um, r.y_um)
            ec = ((e.xmin + e.xmax) / 2, (e.ymin + e.ymax) / 2)
            hit = None
            for k, s in enumerate(shapes):
                if k in used or k in idx or s.tag != e.tag:
                    continue
                if abs((s.xmin + s.xmax) / 2 - ec[0]) <= tol_um and \
                        abs((s.ymin + s.ymax) / 2 - ec[1]) <= tol_um and \
                        abs((s.xmax - s.xmin) - (e.xmax - e.xmin)) <= tol_um and \
                        abs((s.ymax - s.ymin) - (e.ymax - e.ymin)) <= tol_um:
                    hit = k
                    break
            if hit is None:
                idx = None
                break
            idx.append(hit)
        if idx:
            used.update(idx)
            found.append(r)
    return found, len(shapes) - len(used)


def check_shapes(shapes: Sequence[ShapeBox], field_um: Tuple[float, float],
                 points: Dict[str, Tuple[float, float]], *,
                 margin_um: float = L.MARKUP_FIELD_MARGIN_UM,
                 clearance_um: float = L.MARKUP_TARGET_CLEARANCE_UM) -> List[str]:
    """Problems with milling these shapes in this field next to these target points."""
    hw, hh = field_um[0] / 2.0 - margin_um, field_um[1] / 2.0 - margin_um
    probs: List[str] = []
    for s in shapes:
        if s.xmin < -hw or s.xmax > hw or s.ymin < -hh or s.ymax > hh:
            probs.append(f"a {s.tag} at x {s.xmin:.1f}..{s.xmax:.1f}, y {s.ymin:.1f}.."
                         f"{s.ymax:.1f} um is not {margin_um:g} um inside the "
                         f"{field_um[0]:.0f} x {field_um[1]:.0f} um field")
            break
    c = clearance_um
    for tid, (tx, ty) in points.items():
        for s in shapes:
            if s.xmin - c <= tx <= s.xmax + c and s.ymin - c <= ty <= s.ymax + c:
                probs.append(f"a {s.tag} would mill within {c:g} um of target {tid}")
                break
    return probs


# ============================================================ plan
@dataclass
class MarkRow:
    id: str
    label: str
    fib_px: Tuple[float, float]
    x_um: Optional[float] = None
    y_um: Optional[float] = None
    selected: bool = False
    status: str = "not selected"          # marked | refused | not selected
    reasons: List[str] = field(default_factory=list)


@dataclass
class MarkupPlan:
    ok: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    targets_file: Optional[str] = None
    targets_sha256: Optional[str] = None
    overview: Any = None                  # PositionRecord
    overview_positions_file: Optional[str] = None
    overview_image: Optional[str] = None
    field_um: Tuple[float, float] = (0.0, 0.0)
    pixel_size_um: Optional[float] = None
    base_path: Optional[str] = None
    base_sha256: Optional[str] = None
    base_bbox_um: Optional[Tuple[float, float, float, float]] = None
    probes: List[str] = field(default_factory=list)
    rows: List[MarkRow] = field(default_factory=list)
    file_path: Optional[str] = None
    file_state: str = "none"              # none | generated | edited | refused
    file_sha256: Optional[str] = None
    file_targets: List[Dict[str, Any]] = field(default_factory=list)
    extra_shapes: int = 0                 # edited layout: shapes on no target's markup

    def refuse(self, why: str) -> None:
        self.ok = False
        self.errors.append(why)

    def marked(self) -> List[MarkRow]:
        return [r for r in self.rows if r.status == "marked"]

    def milled_ids(self) -> List[str]:
        """The targets the file on disk marks (generated or as edited)."""
        return [str(t["id"]) for t in self.file_targets]

    def lines(self) -> List[str]:
        out = []
        ov = self.overview
        if ov is not None:
            out.append(f"[markup] overview {ov.name}: {ov.fib_magnification_x:g}x, field "
                       f"{self.field_um[0]:.1f} x {self.field_um[1]:.1f} um")
        if self.base_path:
            b = self.base_bbox_um
            out.append(f"[markup] pattern {Path(self.base_path).name}"
                       + (f" (x {b[0]:.1f}..{b[1]:.1f}, y {b[2]:.1f}..{b[3]:.1f} um around "
                          f"the target)" if b else "")
                       + (f", probe {', '.join(self.probes)}" if self.probes else ""))
        for r in self.rows:
            pos = "" if r.x_um is None else f"at x {r.x_um:+.1f}, y {r.y_um:+.1f} um"
            why = "; ".join(r.reasons)
            out.append(f"[markup] {r.id} ({r.label}): {r.status.upper():12s} {pos}"
                       + (f" -- {why}" if why else ""))
        for w in self.warnings:
            out.append(f"[markup] WARNING: {w}")
        for e in self.errors:
            out.append(f"[markup] REFUSED: {e}")
        if self.file_path:
            out.append(f"[markup] file {self.file_path} ({self.file_state}; marks "
                       f"{', '.join(self.milled_ids()) or 'nothing'})")
        out.append(f"CLEM-MARKUP-PLAN: marked={len(self.milled_ids()) if self.ok else 0} "
                   f"refused={sum(1 for r in self.rows if r.status == 'refused')} "
                   f"state={self.file_state} ok={'yes' if self.ok else 'no'}")
        return out


def _probes(text: str) -> List[str]:
    names = []
    for m in re.finditer(r'<PROBE\b[^>]*\bname="([^"]*)"', text):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


def markup_paths(session_dir, overview_name: str) -> Tuple[Path, Path]:
    d = clem_dir(session_dir) / MARKUP_DIRNAME
    f = d / f"{overview_name}_markup.ely"
    return f, f.with_name(f.name + SIDECAR_SUFFIX)


def plan_markup(tplan, *, base=None, selected: Optional[Sequence[str]] = None,
                margin_um: float = L.MARKUP_FIELD_MARGIN_UM,
                clearance_um: float = L.MARKUP_TARGET_CLEARANCE_UM) -> MarkupPlan:
    """Which targets get a markup and where (files only). ``selected``: target ids
    (None = none selected)."""
    mp = MarkupPlan(targets_file=tplan.targets_file, targets_sha256=tplan.targets_sha256,
                    overview=tplan.overview,
                    overview_positions_file=tplan.overview_positions_file)
    sel = {str(s) for s in (selected or [])}
    base_path = Path(base) if base else DEFAULT_MARKUP
    mp.base_path = str(base_path)
    base_shapes: List[ShapeBox] = []
    try:
        text, base_shapes = read_markup(base_path)
        mp.base_sha256 = file_sha256(base_path)
        mp.base_bbox_um = bbox(base_shapes)
        mp.probes = _probes(text)
    except FileNotFoundError:
        mp.refuse(f"markup pattern not found: {base_path}")
    except MarkupError as e:
        mp.refuse(str(e))
    if not tplan.ok:
        mp.refuse("the targets file is refused: " + "; ".join(tplan.errors))
    rec = tplan.overview
    if rec is None or not (rec.image_width_px and rec.image_height_px and rec.fib_pixel_size_m):
        mp.refuse("no usable FIB overview record (image size / pixel size)")
        rec = None
    W = H = 0
    if rec is not None:
        W, H = int(rec.image_width_px), int(rec.image_height_px)
        mp.pixel_size_um = float(rec.fib_pixel_size_m) * 1e6
        mp.field_um = (W * mp.pixel_size_um, H * mp.pixel_size_um)
        if tplan.overview_positions_file and rec.image_bmp:
            from .targets import _resolve            # Windows separators on the PC
            mp.overview_image = str(_resolve(Path(tplan.overview_positions_file).parent,
                                             rec.image_bmp))
    for t in tplan.rows:
        row = MarkRow(id=t.id, label=t.label, fib_px=tuple(t.fib_px), selected=t.id in sel)
        mp.rows.append(row)
        if rec is not None and all(math.isfinite(v) for v in t.fib_px):
            row.x_um, row.y_um = pixel_to_layout_um(
                t.fib_px[0], t.fib_px[1], width_px=W, height_px=H,
                pixel_size_m=float(rec.fib_pixel_size_m))
        if not row.selected:
            continue
        if t.status == "refused":
            row.status = "refused"
            row.reasons.append("target refused: " + "; ".join(t.reasons))
        elif not mp.ok or row.x_um is None:
            row.status = "refused"
            row.reasons.append("plan refused")
        else:
            row.status = "marked"
    cand = [r for r in mp.rows if r.status == "marked"]
    pts = {r.id: (r.x_um, r.y_um) for r in cand}
    for r in cand:
        probs = check_shapes([s.shifted(r.x_um, r.y_um) for s in base_shapes], mp.field_um,
                             pts, margin_um=margin_um, clearance_um=clearance_um)
        own = f"of target {r.id}"
        r.reasons.extend((p[:-len(own)] + "of its own target") if p.endswith(own) else p
                         for p in probs)
    for r in cand:
        if r.reasons:
            r.status = "refused"
    # A trench must not come near ANY target of the file (ticked or not, refused rows
    # included): every one is a fluorescent site someone may want a lamella at.
    allpts = {r.id: (r.x_um, r.y_um) for r in mp.rows if r.x_um is not None}
    for r in cand:
        if r.reasons:
            continue
        probs = check_shapes([s.shifted(r.x_um, r.y_um) for s in base_shapes], mp.field_um,
                             {k: v for k, v in allpts.items() if k not in pts},
                             margin_um=margin_um, clearance_um=clearance_um)
        probs = [p for p in probs if "field" not in p]
        if probs:
            r.status = "refused"
            r.reasons.extend(p + " (not ticked)" for p in probs)
    for i in sel - {r.id for r in mp.rows}:
        mp.warnings.append(f"selected target {i} is not in the targets file")
    if mp.ok and not mp.marked():
        mp.refuse("no target to mark (select at least one OK target)")
    return mp


# ============================================================ file on disk
def _inputs(mp: MarkupPlan) -> Dict[str, Any]:
    ov = mp.overview
    return {"overview": getattr(ov, "name", None),
            "overview_captured_unix_s": getattr(ov, "captured_unix_s", None),
            "targets_sha256": mp.targets_sha256, "base_sha256": mp.base_sha256,
            "targets": [{"id": r.id, "label": r.label, "fib_px": list(r.fib_px),
                         "offset_um": [round(r.x_um, 6), round(r.y_um, 6)]}
                        for r in mp.marked()]}


def _load_sidecar(p: Path) -> Optional[Dict[str, Any]]:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if d.get("schema") == MARKUP_FILE_SCHEMA else None
    except Exception:  # noqa: BLE001
        return None


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(str(tmp), str(path))


def prepare_file(mp: MarkupPlan, session_dir, *, reset: bool = False,
                 write: bool = True) -> MarkupPlan:
    """Generate / keep / validate <session>/clem/markup/<overview>_markup.ely.

    generated (sha = what pyFIB wrote): regenerated whenever the inputs changed;
    edited (the operator saved it in ELYMaker): kept and milled as saved, re-checked
    against the targets it was generated for; refused if the targets file or the
    overview changed since (Reset regenerates). ``reset`` regenerates regardless."""
    if mp.overview is None:
        return mp
    f, side = markup_paths(session_dir, mp.overview.name)
    mp.file_path = str(f)
    sc = _load_sidecar(side) if side.exists() else None
    cur = _inputs(mp)
    if f.exists() and sc is None and not reset:
        mp.file_state = "refused"
        mp.refuse(f"{f.name} exists without its pyFIB record ({side.name}); Reset markup "
                  f"to regenerate it")
        return mp
    if f.exists() and sc and not reset:
        sha = file_sha256(f)
        if sha != sc.get("generated_sha256"):
            mp.file_state = "edited"
            mp.file_sha256 = sha
            mp.file_targets = list(sc.get("inputs", {}).get("targets") or [])
            old = sc.get("inputs", {})
            changed = [k for k in ("overview", "overview_captured_unix_s", "targets_sha256")
                       if old.get(k) != cur.get(k)]
            if changed:
                mp.file_state = "refused"
                mp.refuse("the markup was edited in ELYMaker for an older targets file or "
                          "overview (" + ", ".join(changed) + " changed); Reset markup to "
                          "regenerate it")
                return mp
            try:
                text, shapes = read_markup(f)
                pts = {r.id: (r.x_um, r.y_um) for r in mp.rows if r.x_um is not None}
                probs = check_shapes(shapes, mp.field_um, pts)
                base_shapes = read_markup(mp.base_path)[1] if mp.base_path else []
            except (MarkupError, OSError) as e:
                probs, shapes, base_shapes = [str(e)], [], []
            if probs:
                mp.file_state = "refused"
                mp.refuse("the edited markup fails its checks: " + "; ".join(probs))
            else:
                mp.probes = _probes(text) or mp.probes
                found, extra = match_markups(shapes, base_shapes, mp.rows)
                mp.file_targets = [{"id": r.id, "label": r.label, "fib_px": list(r.fib_px),
                                    "offset_um": [round(r.x_um, 6), round(r.y_um, 6)]}
                                   for r in found]
                mp.extra_shapes = extra
                mp.warnings.append(
                    f"the markup was edited in ELYMaker: it is milled AS SAVED -- "
                    f"{len(shapes)} shapes, markups on "
                    + (", ".join(mp.milled_ids()) or "no target")
                    + (f", plus {extra} shape(s) that are not a markup on a target"
                       if extra else "")
                    + " (the ticks are ignored; Reset markup to use them)")
                if mp.errors and all("no target to mark" in e for e in mp.errors):
                    mp.ok, mp.errors = True, []
            return mp
        if sc.get("inputs") == cur:
            mp.file_state, mp.file_sha256 = "generated", sha
            mp.file_targets = list(cur["targets"])
            return mp
    if not mp.ok:
        mp.file_state = "none"
        return mp
    base_text, enc = _read_text(mp.base_path)
    offs = [(r.x_um, r.y_um) for r in mp.marked()]
    data = combine(base_text, offs).encode(enc)
    mp.file_targets = list(cur["targets"])
    mp.file_state = "generated"
    if not write:
        mp.file_sha256 = "sha256:" + hashlib.sha256(data).hexdigest()
        return mp
    _atomic_write(f, data)
    mp.file_sha256 = file_sha256(f)
    try:
        from .. import __version__ as ver
    except Exception:  # noqa: BLE001
        ver = None
    side_data = {"schema": MARKUP_FILE_SCHEMA, "pyfib_version": ver,
                 "created": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "base": mp.base_path, "generated_sha256": mp.file_sha256, "inputs": cur}
    _atomic_write(side, json.dumps(side_data, indent=1).encode("utf-8"))
    return mp


def annotate_overview(mp: MarkupPlan, out_path) -> Optional[Path]:
    """A copy of the overview with each target's cross + label burnt in (same size and
    pixel size, so ELYMaker places shapes exactly as on the original). None without
    Pillow or an image; the original is never changed."""
    if not mp.overview_image or not Path(mp.overview_image).exists():
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001
        return None
    try:
        im = Image.open(mp.overview_image).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    dr = ImageDraw.Draw(im)
    marked = set(mp.milled_ids()) if mp.file_targets else {r.id for r in mp.marked()}
    for r in mp.rows:
        if not all(math.isfinite(v) for v in r.fib_px):
            continue
        x, y = r.fib_px
        col = ((255, 210, 0) if r.id in marked else
               (255, 80, 80) if r.status == "refused" else (150, 150, 150))
        s = 7
        dr.line([(x - s, y), (x + s, y)], fill=col, width=1)
        dr.line([(x, y - s), (x, y + s)], fill=col, width=1)
        dr.text((x + s + 2, y - s - 10), str(r.label), fill=col)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    return out


# ============================================================ milling (ARM-gated)
def _f(sem, mnemonic) -> Optional[float]:
    try:
        v = float(sem.get_float(mnemonic))
        return v if math.isfinite(v) else None
    except Exception:  # noqa: BLE001
        return None


def check_at_overview(sem, rec) -> Dict[str, Optional[float]]:
    """Refuse unless the stage still reads the overview record's position."""
    from .overview import stage_readings
    live = stage_readings(sem)
    st = rec.stage_at
    probs = []
    for k, ref, tol, unit, scale in (
            ("x_m", st.x_m, L.MARKUP_STAGE_TOL_M, "um", 1e6),
            ("y_m", st.y_m, L.MARKUP_STAGE_TOL_M, "um", 1e6),
            ("z_m", st.z_m, L.MARKUP_STAGE_TOL_M, "um", 1e6),
            ("m_m", st.m_m, L.MARKUP_STAGE_TOL_M, "um", 1e6),
            ("t_deg", st.t_deg, L.MARKUP_ANGLE_TOL_DEG, "deg", 1.0),
            ("r_deg", st.r_deg, L.MARKUP_ANGLE_TOL_DEG, "deg", 1.0)):
        v = live.get(k)
        if ref is None:
            if k in ("x_m", "y_m", "z_m", "t_deg", "r_deg"):
                probs.append(f"{rec.name} has no recorded {k}")
            continue
        if v is None:
            probs.append(f"stage {k} unreadable")
            continue
        d = v - float(ref)
        if k == "r_deg":
            d = (d + 180.0) % 360.0 - 180.0
        if abs(d) > tol:
            probs.append(f"{k[0].upper()} is {d * scale:+.2f} {unit} from the overview "
                         f"(limit {tol * scale:g})")
    if probs:
        raise MarkupRefused(
            f"the stage is not at {rec.name}'s position ({'; '.join(probs)}). The markup is "
            f"placed on the overview image, so the stage must not have moved since it was "
            f"taken. Nothing was milled.")
    return live


def _set_fib_mag(sem, target: float, log, sleep=time.sleep) -> float:
    """The run --fib-mag sequence: FIB mode first (a set in post-milling mode does not
    stick), set, read back (twice). The read-back must be within 1 %: the markup is
    placed for the overview's field, so an unreadable read-back refuses too.

    v0.54.0 (bug #14): the shared switch -- CMD_FIB_MODE_FIB only if not already in FIB
    imaging, then wait until SmartSEM's magnification reset has landed. A switch that
    fails refuses (nothing milled)."""
    from ..frame_grab import ensure_fib_imaging
    try:
        ensure_fib_imaging(sem, log=log, sleep=sleep)
    except Exception as e:  # noqa: BLE001
        raise MarkupRefused(f"could not switch to FIB imaging ({e}); nothing was milled")
    actual = None
    for _ in range(2):
        sem.set_value("AP_FIB_MAGNIFICATION", float(target))
        sleep(0.3)
        actual = _f(sem, "AP_FIB_MAGNIFICATION")
        if actual is not None and abs(actual - target) <= L.MARKUP_MAG_TOL_FRAC * target:
            return actual
    raise MarkupRefused(f"FIB magnification reads {actual}x after setting {target:g}x; "
                        f"nothing was milled")


def _rel(path, base: Path) -> str:
    try:
        return Path(path).resolve().relative_to(base.resolve()).as_posix()
    except Exception:  # noqa: BLE001
        return str(path)


def _append_log(session_dir, entry: Dict[str, Any]) -> Path:
    p = clem_dir(session_dir) / MARKUPS_LOG
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if d.get("schema") != MARKUPS_SCHEMA:
            d = {}
    except Exception:  # noqa: BLE001
        d = {}
    d = {"schema": MARKUPS_SCHEMA, "runs": list(d.get("runs", [])) + [entry]}
    _atomic_write(p, json.dumps(d, indent=1).encode("utf-8"))
    return p


def load_markups_log(session_dir) -> List[Dict[str, Any]]:
    p = clem_dir(session_dir) / MARKUPS_LOG
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return list(d.get("runs", [])) if d.get("schema") == MARKUPS_SCHEMA else []
    except Exception:  # noqa: BLE001
        return []


def mill_markup(sem, session_dir, mp: MarkupPlan, *, drop_dir, drop_name: str = "ApiLayout.ely",
                load_delay_s: float = 1.0, prepare_delay_s: float = 1.0,
                wait_strategy: str = "auto", wait_timeout_s: float = 5400.0,
                done_confirm_s: float = 30.0, after_image: bool = True,
                guard_s: Optional[float] = None, log: Callable[[str], None] = print,
                _runner_cls=None, _grab_fn=None, sleep=time.sleep) -> Dict[str, Any]:
    """Mill the prepared markup file at the overview position. Returns the log entry.

    The checked bytes are copied to <session>/clem/markup/milled/<name>_<time>.ely first
    and THAT copy is milled, so a later save in ELYMaker (or a regenerate) can never swap
    in an unchecked layout; the copy stays as the record of what was milled."""
    from ..batch import _is_milling_active, _zero_fib_beam_shift
    from ..runner import PatternJob, PatternRunner
    from ..site_ops import GUARD_S, SiteRefused, _milling_guard
    from .overview import OverviewRefused, check_sem_normal
    rec = mp.overview
    if not mp.ok or rec is None or mp.file_state not in ("generated", "edited"):
        raise MarkupRefused("the markup plan is refused: " + "; ".join(mp.errors or
                                                                       ["no markup file"]))
    f = Path(mp.file_path)
    data = f.read_bytes() if f.exists() else b""
    if "sha256:" + hashlib.sha256(data).hexdigest() != mp.file_sha256:
        raise MarkupRefused(f"{f.name} changed after it was checked; check it again. "
                            f"Nothing was milled.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    shapes = shapes_from_text(text, what=f.name)
    pts = {r.id: (r.x_um, r.y_um) for r in mp.rows if r.x_um is not None}
    probs = check_shapes(shapes, mp.field_um, pts)
    if probs:
        raise MarkupRefused("; ".join(probs) + ". Nothing was milled.")
    # ---- read-only checks: refuse before anything is set or written
    try:
        _milling_guard(sem, "Mark targets", GUARD_S if guard_s is None else guard_s, log=log)
        check_sem_normal(sem)
    except (SiteRefused, OverviewRefused) as e:
        raise MarkupRefused(str(e))
    live = check_at_overview(sem, rec)
    rot = _f(sem, "AP_FIB_SCAN_ROTATION")
    if rot is None or abs(rot) > L.TARGET_MAX_SCAN_ROTATION_DEG:
        raise MarkupRefused(f"FIB scan rotation reads {rot}; it must be 0 (the overview "
                            f"was taken without it). Nothing was milled.")
    log(f"[markup] stage at {rec.name} (X {live['x_m'] * 1e3:.4f} Y {live['y_m'] * 1e3:.4f} "
        f"Z {live['z_m'] * 1e3:.4f} mm, T {live['t_deg']:.2f}, R {live['r_deg']:.2f}); "
        f"it is not moved.")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    copy = f.parent / "milled" / f"{f.stem}_{stamp}.ely"
    _atomic_write(copy, data)
    if file_sha256(copy) != mp.file_sha256:
        raise MarkupRefused("the private copy of the markup does not match; nothing milled")
    mag = float(rec.fib_magnification_x)
    ids = mp.milled_ids()
    entry: Dict[str, Any] = {
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
        "overview": {"positions_file": mp.overview_positions_file, "position": rec.name,
                     "magnification_x": mag},
        "targets_file": mp.targets_file, "targets_sha256": mp.targets_sha256,
        "pattern": {"path": _rel(f, clem_dir(session_dir)),
                    "milled_copy": _rel(copy, clem_dir(session_dir)),
                    "sha256": mp.file_sha256, "state": mp.file_state, "shapes": len(shapes),
                    "extra_shapes": mp.extra_shapes, "base": mp.base_path,
                    "base_sha256": mp.base_sha256, "probes": mp.probes},
        "targets": mp.file_targets,
        "live": {"stage": live, "fib_scan_rotation_deg": rot},
    }
    try:
        from .. import __version__ as ver
        entry["pyfib_version"] = ver
    except Exception:  # noqa: BLE001
        pass
    phase = "setup"
    t0 = time.time()
    try:
        # ---- settings (no motion): beam shift 0, overview magnification + objective
        log("[markup] zeroing the FIB beam shift (the overview was taken at 0).")
        if not _zero_fib_beam_shift(sem, logger=log):
            raise MarkupRefused("the FIB beam shift does not read 0 after zeroing. Nothing "
                                "was milled.")
        for mn in ("AP_FIB_BEAM_SHIFT_X", "AP_FIB_BEAM_SHIFT_Y"):
            v = _f(sem, mn)
            if v is None or abs(v) > L.TARGET_MAX_BEAM_SHIFT_M:
                raise MarkupRefused(f"{mn} reads {v}; it must read 0. Nothing was milled.")
        mag_rb = _set_fib_mag(sem, mag, log, sleep=sleep)
        sem.set_value("AP_FIB_OBJECTIVE_POTENTIAL", float(rec.fib_objective_potential_v))
        entry["live"]["fib_mag_readback"] = mag_rb
        log(f"[markup] FIB {mag:g}x (read back {mag_rb:g}), objective "
            f"{rec.fib_objective_potential_v:.1f} V (as at the overview)")
        log(f"[markup] milling {copy.name}: markups on {', '.join(ids) or 'no target'} "
            f"({len(shapes)} shapes, {mp.file_state})")
        if file_sha256(copy) != mp.file_sha256:
            raise MarkupRefused("the private copy changed; nothing milled")
        # v0.54.0 (bug #14): SmartSEM's magnification reset can land late -- read the
        # magnification once more right before milling.
        from ..frame_grab import fib_mag_still
        mag_ok, mag_now = fib_mag_still(sem, float(mag), L.MARKUP_MAG_TOL_FRAC)
        if not mag_ok:
            raise MarkupRefused(f"the FIB magnification reads {mag_now}x just before milling, "
                                f"not {mag:g}x; nothing was milled")
        job = PatternJob(ely_local_path=copy, load_method="smartfib_drop",
                         smartfib_drop_dir=Path(drop_dir), smartfib_drop_name=str(drop_name),
                         smartfib_load_delay_s=float(load_delay_s),
                         smartfib_prepare_delay_s=float(prepare_delay_s),
                         arm=True, start_milling=True, wait_strategy=wait_strategy,
                         wait_timeout_s=float(wait_timeout_s),
                         done_confirm_s=float(done_confirm_s))
        phase = "milling"
        t0 = time.time()
        (_runner_cls or PatternRunner)(sem).run(job)
    except BaseException as e:
        entry.update(status="refused" if isinstance(e, MarkupRefused) else "error",
                     phase=phase, message=f"{type(e).__name__}: {e}",
                     duration_s=round(time.time() - t0, 1),
                     finished=datetime.now().astimezone().isoformat(timespec="seconds"))
        _append_log(session_dir, entry)
        raise
    entry.update(status="milled", duration_s=round(time.time() - t0, 1))
    if after_image:
        from types import SimpleNamespace
        from ..after_image import grab_after_mill_image
        named = SimpleNamespace(name=f"{rec.name}_markup", fib_magnification_x=mag,
                                image_width_px=rec.image_width_px,
                                image_height_px=rec.image_height_px)
        grab = _grab_fn or grab_after_mill_image
        path, warn = grab(sem, named, clem_dir(session_dir) / IMAGES_DIRNAME,
                          milling_active=lambda: _is_milling_active(sem), log=log,
                          objective_v=float(rec.fib_objective_potential_v))
        entry["after_image"] = None if path is None else _rel(path, clem_dir(session_dir))
        entry["after_image_warning"] = warn
        if warn:
            log(f"*** WARNING: {warn} ***")
        if path is not None:
            log(f"[markup] after image {entry['after_image']}")
    entry["finished"] = datetime.now().astimezone().isoformat(timespec="seconds")
    p = _append_log(session_dir, entry)
    log(f"[markup] recorded in {p}")
    return entry
