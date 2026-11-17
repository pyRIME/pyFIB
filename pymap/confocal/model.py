"""Normalized in-memory confocal structure.

A ConfocalImage is what every importer backend produces, so the rest of pyMap
never sees a file format. It is either a list of Channels (each a 2D float image
after Z-projection, holding TRUE intensity normalized by the source dtype, plus a
per-channel display window low/high/gamma and LUT colour) or a pre-merged RGB
image (already-coloured exports like an RGB PNG).

Per-channel display is separate from true intensity: Channel.data is the real
signal; Channel.display() applies the black/white points + gamma. This is what
lets the user brighten a dim channel without touching the data used elsewhere.

Two derived views drive the app:
  merged_rgb()   - included channels, each display()-scaled, blended in their LUTs
  single_gray()  - one 2D image (mono display / future refinement)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import numpy as np

# Default LUTs assigned in order when the file carries none.
DEFAULT_PALETTE: List[Tuple[float, float, float]] = [
    (0.0, 1.0, 0.0),   # green
    (1.0, 0.0, 1.0),   # magenta
    (0.0, 1.0, 1.0),   # cyan
    (1.0, 0.0, 0.0),   # red
    (0.35, 0.55, 1.0),  # blue
    (1.0, 1.0, 0.0),   # yellow
    (1.0, 1.0, 1.0),   # gray
]


def luminance(rgb: np.ndarray) -> np.ndarray:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return (0.299 * r + 0.587 * g + 0.114 * b).astype(np.float32)


def autoscale(a: np.ndarray) -> np.ndarray:
    """Min-max stretch a 2D array to [0,1]."""
    a = np.asarray(a, dtype=np.float32)
    lo, hi = float(a.min()), float(a.max())
    if hi <= lo:
        return np.zeros_like(a)
    return np.clip((a - lo) / (hi - lo), 0.0, 1.0)


def default_window(d: np.ndarray) -> Tuple[float, float]:
    """A robust display window for a channel: [p1, p99.5], so a dim channel with
    a few hot pixels isn't crushed by a min-max stretch."""
    d = np.asarray(d, dtype=np.float32)
    d = d[np.isfinite(d)]
    if d.size == 0:
        return 0.0, 1.0
    lo = float(np.percentile(d, 1.0))
    hi = float(np.percentile(d, 99.5))
    if hi <= lo:
        lo, hi = float(d.min()), float(d.max())
    if hi <= lo:
        hi = lo + 1e-3
    return lo, hi


@dataclass
class Channel:
    data: np.ndarray                              # 2D float [0,1] true intensity
    name: str = ""
    color: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    bit_depth: int = 16
    include: bool = True
    low: float = 0.0                              # display black point in [0,1]
    high: float = 1.0                             # display white point in [0,1]
    gamma: float = 1.0

    def display(self) -> np.ndarray:
        """Apply the black/white points + gamma -> 2D [0,1] for display/merge."""
        lo, hi = self.low, self.high
        if hi <= lo:
            hi = lo + 1e-6
        g = np.clip((self.data - lo) / (hi - lo), 0.0, 1.0)
        if abs(self.gamma - 1.0) > 1e-6:
            g = np.power(g, self.gamma, dtype=np.float32)
        return g.astype(np.float32)


@dataclass
class ConfocalImage:
    channels: List[Channel] = field(default_factory=list)
    premerged_rgb: Optional[np.ndarray] = None
    pixel_size_m: Optional[float] = None
    source: str = ""
    n_z: int = 1
    projection: str = "mip"
    notes: List[str] = field(default_factory=list)   # how the file was read (shown in summary)

    def shape_hw(self) -> Tuple[int, int]:
        if self.premerged_rgb is not None:
            return self.premerged_rgb.shape[:2]
        return self.channels[0].data.shape[:2]

    def display_channels(self) -> List[Channel]:
        return [c for c in self.channels if c.include]

    def is_multicolor(self) -> bool:
        return self.premerged_rgb is not None or len(self.display_channels()) > 1

    def merged_rgb(self) -> np.ndarray:
        """Additive LUT merge of the included, display()-scaled channels."""
        if self.premerged_rgb is not None:
            return np.clip(self.premerged_rgb, 0.0, 1.0).astype(np.float32)
        chans = self.display_channels()
        h, w = self.shape_hw()
        out = np.zeros((h, w, 3), dtype=np.float32)
        for c in chans:
            g = c.display()
            out += g[..., None] * np.asarray(c.color, dtype=np.float32)
        return np.clip(out, 0.0, 1.0)

    def single_gray(self) -> np.ndarray:
        if self.premerged_rgb is not None:
            return luminance(np.clip(self.premerged_rgb, 0.0, 1.0))
        chans = self.display_channels() or self.channels
        if len(chans) == 1:
            return chans[0].display()
        return luminance(self.merged_rgb())

    def moving_display(self) -> np.ndarray:
        return self.merged_rgb() if self.is_multicolor() else self.single_gray()

    def summary(self) -> str:
        proj = "" if self.n_z <= 1 else " (%s of %d z)" % (self.projection, self.n_z)
        if self.premerged_rgb is not None:
            text = "RGB image" + proj
        else:
            nch = len(self.channels)
            text = "%d channel%s%s" % (nch, "" if nch == 1 else "s", proj)
        if self.notes:
            text += "; " + "; ".join(self.notes)
        return text
