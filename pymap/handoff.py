"""Write an elymaker handoff job so the CLEM overlay opens straight in elymaker.

Emits the `elymaker.handoff.v1` contract (mirrored from elymaker/handoff.py):
a job.json pointing at the overlay image, its authoritative sample-plane pixel
size, and the target .ely path. elymaker opens preloaded with the image centred
on the FOV centre = layout origin, so the operator places the lamella on the
fluorescence. Launch with:  elymaker-gui --job job.json

Also writes a pyMap targets.json sidecar (FIB px + FOV-centred layout µm) as a
record; the v1 handoff schema itself carries no target points, so targets are
additionally burned onto the overlay as small crosses for visibility.

The pixel<->µm convention (centre = W/2, +Y up) matches elymaker/gui/coords.py
exactly (verified), so a target's layout µm here equals where elymaker places it.
Pure/stdlib + numpy/PIL; no Qt.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple
import json
import os
import numpy as np
from PIL import Image

from .meta import FibImageMeta
from .composite import to_uint8

HANDOFF_SCHEMA = "elymaker.handoff.v1"
TARGETS_SCHEMA = "pymap.targets.v1"


@dataclass
class HandoffJob:
    image: str                       # relative to the job file
    pixel_size_m: float              # authoritative sample-plane m/px (from FIB image)
    out: str                         # .ely to write (relative -> next to job)
    load: Optional[str] = None
    fov_width_m: Optional[float] = None
    image_width_px: Optional[int] = None
    image_height_px: Optional[int] = None
    center: str = "origin"           # FOV centre at layout (0,0)
    schema: str = HANDOFF_SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)


def _draw_crosses(rgb: np.ndarray, pts, color=(1.0, 0.0, 1.0), r: int = 7) -> np.ndarray:
    h, w, _ = rgb.shape
    for (x, y) in pts:
        xi, yi = int(round(x)), int(round(y))
        for dx in range(-r, r + 1):
            xx = xi + dx
            if 0 <= xx < w and 0 <= yi < h:
                rgb[yi, xx] = color
        for dy in range(-r, r + 1):
            yy = yi + dy
            if 0 <= xi < w and 0 <= yy < h:
                rgb[yy, xi] = color
    return rgb


def target_layout_um(fib_px: Tuple[float, float], meta: FibImageMeta) -> Tuple[float, float]:
    """FIB pixel -> FOV-centred layout µm (elymaker layout space, +Y up)."""
    x_m, y_m = meta.px_to_fov_m(fib_px[0], fib_px[1])
    return (x_m * 1e6, y_m * 1e6)


def write_handoff(out_dir: str,
                  underlay_rgb: np.ndarray,
                  meta: FibImageMeta,
                  ely_out: str = "lamella.ely",
                  targets_fib_px: Optional[List[Tuple[float, float]]] = None,
                  mark_targets: bool = True) -> dict:
    """Write overlay.png + job.json (+ targets.json). Returns a summary dict."""
    os.makedirs(out_dir, exist_ok=True)
    rgb = np.clip(np.asarray(underlay_rgb, dtype=np.float32), 0.0, 1.0)
    if rgb.ndim == 2:
        rgb = np.stack([rgb] * 3, axis=-1)
    else:
        rgb = rgb.copy()
    if targets_fib_px and mark_targets:
        _draw_crosses(rgb, targets_fib_px)

    img_name = "overlay.png"
    Image.fromarray(to_uint8(rgb)).save(os.path.join(out_dir, img_name))

    job = HandoffJob(image=img_name,
                     pixel_size_m=float(meta.fib_pixel_size_m),
                     out=ely_out,
                     fov_width_m=meta.fov_width_m,
                     image_width_px=meta.image_width,
                     image_height_px=meta.image_height,
                     center="origin")
    with open(os.path.join(out_dir, "job.json"), "w") as f:
        json.dump(job.to_dict(), f, indent=2)

    tinfo = []
    if targets_fib_px:
        for i, (x, y) in enumerate(targets_fib_px):
            xu, yu = target_layout_um((x, y), meta)
            tinfo.append({"index": i + 1,
                          "fib_px": [float(x), float(y)],
                          "layout_um": [xu, yu]})
        with open(os.path.join(out_dir, "targets.json"), "w") as f:
            json.dump({"schema": TARGETS_SCHEMA,
                       "center": "origin",
                       "note": "layout_um: FOV-centred, +X right, +Y up "
                               "(elymaker layout space)",
                       "targets": tinfo}, f, indent=2)

    return {"job": "job.json", "image": img_name, "ely_out": ely_out,
            "n_targets": len(tinfo), "pixel_size_m": float(meta.fib_pixel_size_m)}
