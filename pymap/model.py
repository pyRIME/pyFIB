"""Serializable data model for a pyMap registration session.

Coordinate frames
-----------------
- Fixed frame  : FIB image pixels, origin top-left, +x right, +y down.
- Moving frame : the *pre-aligned* confocal image pixels (same pixel convention).
                 i.e. landmarks on the moving side are picked AFTER the coarse
                 rot90/flip pre-align has been applied, matching the BigWarp-style
                 workflow (coarse align, then pick corresponding points).

The registration transform maps  pre-aligned-moving px -> fixed px.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Tuple


class TransformKind:
    RIGID = "rigid"            # Euclidean: rotation + translation (>=2 pairs)
    SIMILARITY = "similarity"  # + uniform scale (>=2 pairs)
    AFFINE = "affine"          # + shear/anisotropic scale (>=3 pairs)
    TPS = "tps"                # thin-plate spline, deformable (>=3 pairs)

    ALL = (RIGID, SIMILARITY, AFFINE, TPS)
    MIN_PAIRS = {RIGID: 2, SIMILARITY: 2, AFFINE: 3, TPS: 3}


def _valid_transform(kind) -> str:
    """A session's transform name, checked (v0.9.2: an unknown name such as "TPS"
    used to be stored and then broke every later click with a KeyError)."""
    k = str(kind).strip().lower()
    if k not in TransformKind.ALL:
        raise ValueError("unknown transform %r in the session (expected one of %s)"
                         % (kind, ", ".join(TransformKind.ALL)))
    return k


@dataclass
class PreAlign:
    """Coarse orientation applied to the moving (confocal) image before picking.

    Order of application (fixed, deterministic): rot90 first, then flips.
    rot90 is the number of counter-clockwise 90-degree rotations (numpy np.rot90).
    """
    rot90: int = 0          # 0..3
    flip_h: bool = False    # mirror left<->right (columns)
    flip_v: bool = False    # mirror top<->bottom (rows)

    def __post_init__(self):
        self.rot90 = int(self.rot90) % 4

    def is_identity(self) -> bool:
        return self.rot90 == 0 and not self.flip_h and not self.flip_v


@dataclass
class LandmarkPair:
    """A corresponding point: fixed (FIB px) <-> moving (pre-aligned confocal px)."""
    fixed: Tuple[float, float]   # (x, y)
    moving: Tuple[float, float]  # (x, y)

    def __post_init__(self):
        self.fixed = (float(self.fixed[0]), float(self.fixed[1]))
        self.moving = (float(self.moving[0]), float(self.moving[1]))


@dataclass
class RegistrationSession:
    """Everything needed to reproduce an overlay from the two source images."""
    fixed_path: str = ""
    moving_path: str = ""
    prealign: PreAlign = field(default_factory=PreAlign)
    pairs: List[LandmarkPair] = field(default_factory=list)
    targets: List[Tuple[float, float]] = field(default_factory=list)
    transform: str = TransformKind.AFFINE
    meta_path: str = ""          # optional sidecar json with FibImageMeta
    schema_version: int = 1

    def add_pair(self, fixed_xy, moving_xy) -> LandmarkPair:
        p = LandmarkPair(tuple(fixed_xy), tuple(moving_xy))
        self.pairs.append(p)
        return p

    def min_pairs(self) -> int:
        return TransformKind.MIN_PAIRS[self.transform]

    def is_solvable(self) -> bool:
        return len(self.pairs) >= self.min_pairs()

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "fixed_path": self.fixed_path,
            "moving_path": self.moving_path,
            "prealign": asdict(self.prealign),
            "transform": self.transform,
            "meta_path": self.meta_path,
            "pairs": [{"fixed": list(p.fixed), "moving": list(p.moving)} for p in self.pairs],
            "targets": [list(t) for t in self.targets],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RegistrationSession":
        pa = d.get("prealign", {}) or {}
        s = cls(
            fixed_path=d.get("fixed_path", ""),
            moving_path=d.get("moving_path", ""),
            prealign=PreAlign(rot90=pa.get("rot90", 0),
                              flip_h=pa.get("flip_h", False),
                              flip_v=pa.get("flip_v", False)),
            transform=_valid_transform(d.get("transform", TransformKind.AFFINE)),
            meta_path=d.get("meta_path", ""),
            schema_version=int(d.get("schema_version", 1)),
        )
        for pr in d.get("pairs", []):
            s.add_pair(pr["fixed"], pr["moving"])
        s.targets = [(float(t[0]), float(t[1])) for t in d.get("targets", [])]
        return s
