"""FIB-image metadata and pixel <-> FOV-meters conversion.

    *** PENDING RECONCILIATION - DO NOT TRUST FOR PLACEMENT UNTIL VERIFIED ***

This module ties FIB *pixel* coordinates (what pyMap produces natively) to
FOV-centered *meters* (what elymaker / pyFIB consume for lamella placement).

The FOV-centered convention implemented here is chosen to MATCH elymaker's
confirmed convention: FOV-centered origin, +X right, +Y up, no mirror. Image
pixel space is +x right, +y DOWN, so the Y axis is flipped in the conversion.

Per the project's core principle, signs/conventions must be CONFIRMED on the
instrument / against elymaker, never derived. The conversion below is an
explicit, regression-tested definition that must be cross-checked against
elymaker before it is used to place a lamella. The registration core does not
depend on any of this - it works entirely in FIB pixel space.

Fields mirror the pyFIB v2 position-record contract (fov_width_m,
fib_pixel_size_m, image_width/height). Reconcile field names with the actual
v2 record at integration time; unknown/extra fields are preserved in `extra`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple
import json


@dataclass
class FibImageMeta:
    image_width: int                       # px
    image_height: int                      # px
    fov_width_m: Optional[float] = None     # field-of-view width in metres
    fov_height_m: Optional[float] = None
    fib_pixel_size_m: Optional[float] = None
    extra: dict = field(default_factory=dict)   # carry-through of v2 fields

    def __post_init__(self):
        self.image_width = int(self.image_width)
        self.image_height = int(self.image_height)
        # Derive pixel size / fov where possible; cross-check when both given.
        if self.fib_pixel_size_m is None and self.fov_width_m:
            self.fib_pixel_size_m = self.fov_width_m / self.image_width
        if self.fov_width_m is None and self.fib_pixel_size_m:
            self.fov_width_m = self.fib_pixel_size_m * self.image_width
        if self.fov_height_m is None and self.fib_pixel_size_m:
            self.fov_height_m = self.fib_pixel_size_m * self.image_height
        if self.fib_pixel_size_m is None:
            raise ValueError(
                "FibImageMeta needs fib_pixel_size_m or fov_width_m to define scale.")
        # Consistency check (square-pixel assumption for FIB).
        if self.fov_width_m:
            implied = self.fov_width_m / self.image_width
            if abs(implied - self.fib_pixel_size_m) > 1e-3 * self.fib_pixel_size_m:
                raise ValueError(
                    "Inconsistent metadata: fov_width_m/image_width "
                    f"({implied:.3e}) != fib_pixel_size_m ({self.fib_pixel_size_m:.3e}).")

    # --- pixel <-> FOV-centered metres (Y flipped; see module docstring) -----
    def px_to_fov_m(self, x_px: float, y_px: float) -> Tuple[float, float]:
        pix = self.fib_pixel_size_m
        cx = self.image_width / 2.0
        cy = self.image_height / 2.0
        x_m = (x_px - cx) * pix
        y_m = (cy - y_px) * pix           # +Y up
        return (x_m, y_m)

    def fov_m_to_px(self, x_m: float, y_m: float) -> Tuple[float, float]:
        pix = self.fib_pixel_size_m
        cx = self.image_width / 2.0
        cy = self.image_height / 2.0
        x_px = x_m / pix + cx
        y_px = cy - y_m / pix
        return (x_px, y_px)

    # --- io ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "image_width": self.image_width,
            "image_height": self.image_height,
            "fov_width_m": self.fov_width_m,
            "fov_height_m": self.fov_height_m,
            "fib_pixel_size_m": self.fib_pixel_size_m,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FibImageMeta":
        known = {"image_width", "image_height", "fov_width_m",
                 "fov_height_m", "fib_pixel_size_m", "extra"}
        extra = dict(d.get("extra", {}))
        # preserve any unrecognised top-level keys as carry-through
        for k, v in d.items():
            if k not in known:
                extra[k] = v
        return cls(
            image_width=d["image_width"],
            image_height=d["image_height"],
            fov_width_m=d.get("fov_width_m"),
            fov_height_m=d.get("fov_height_m"),
            fib_pixel_size_m=d.get("fib_pixel_size_m"),
            extra=extra,
        )

    @classmethod
    def load(cls, path: str) -> "FibImageMeta":
        with open(path, "r") as f:
            return cls.from_dict(json.load(f))

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
