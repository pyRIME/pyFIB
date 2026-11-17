"""Render an ELAYOUT to a standalone SVG for visual sanity-checking.

This is a geometry plot, not a beam simulation: it shows where each shape sits
in the layout plane (micrometres), colour-keyed by mill depth, so you can eyeball
a generated pattern before opening SmartFIB. It never touches the instrument.

Screen convention: SmartFIB layout y increases upward; SVG y increases downward,
so we flip y. A rectangle's box runs from its anchor by the signed width and height,
matching how the box grows in the file; a rotated rectangle turns counter-clockwise
about the box centre, as SmartFIB places it (geometry.rect_corners; pyFIB patch
0.9.1+pyfib.1 -- before it, rotation was drawn about the anchor).
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from .geometry import rect_center, rect_corners
from .model import Arc, Layout, Polygon, Rect, Shape, Text


def _bounds(layout: Layout) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for _st, lref, sh in layout.iter_shapes():
        if isinstance(sh, Rect):
            # the corners as SmartFIB places them (rotation about the box centre)
            pts = rect_corners(sh.x, sh.y, sh.width, sh.height, sh.angle_deg)
            xs += [p[0] for p in pts]
            ys += [p[1] for p in pts]
        elif isinstance(sh, Polygon):
            xs += [p[0] for p in sh.points]
            ys += [p[1] for p in sh.points]
        elif isinstance(sh, Arc):
            r = max(abs(sh.r1), abs(sh.r2))
            xs += [sh.cx - r, sh.cx + r]
            ys += [sh.cy - r, sh.cy + r]
        elif isinstance(sh, Text):
            xs += [sh.x, sh.x + max(1, len(sh.text)) * sh.height * 0.6]
            ys += [sh.y, sh.y + abs(sh.height)]
    if not xs:
        # fall back to frame size if present
        f = layout.structures[0].layer_refs[0].frame_size if layout.structures else 100.0
        f = f or 100.0
        return (-f / 2, -f / 2, f / 2, f / 2)
    return (min(xs), min(ys), max(xs), max(ys))


def _depth_color(depth_um: float, dmin: float, dmax: float) -> str:
    """Map depth to a blue->red ramp; deeper = redder."""
    if dmax <= dmin:
        t = 0.5
    else:
        t = (depth_um - dmin) / (dmax - dmin)
    t = max(0.0, min(1.0, t))
    r = int(60 + t * 180)
    g = int(90 + (1 - abs(t - 0.5) * 2) * 60)
    b = int(220 - t * 170)
    return "#%02x%02x%02x" % (r, g, b)


def to_svg(layout: Layout, width_px: int = 900, margin_px: int = 40) -> str:
    """Return an SVG string visualising the layout."""
    xmin, ymin, xmax, ymax = _bounds(layout)
    span_x = max(xmax - xmin, 1e-6)
    span_y = max(ymax - ymin, 1e-6)
    scale = (width_px - 2 * margin_px) / span_x
    height_px = int(span_y * scale + 2 * margin_px)

    def sx(x: float) -> float:
        return margin_px + (x - xmin) * scale

    def sy(y: float) -> float:
        # flip y (layout up-positive -> svg down-positive)
        return margin_px + (ymax - y) * scale

    depths = [sh.depth_um for _s, _l, sh in layout.iter_shapes()] or [0.0]
    dmin, dmax = min(depths), max(depths)

    parts: List[str] = []
    parts.append(
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" font-family="sans-serif">'
        % (width_px, height_px, width_px, height_px)
    )
    parts.append('<rect width="%d" height="%d" fill="#0f1115"/>' % (width_px, height_px))

    # axes at layout origin, if in view
    if xmin <= 0 <= xmax:
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#333" stroke-dasharray="4 4"/>'
                     % (sx(0), margin_px, sx(0), height_px - margin_px))
    if ymin <= 0 <= ymax:
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#333" stroke-dasharray="4 4"/>'
                     % (margin_px, sy(0), width_px - margin_px, sy(0)))

    for _st, _lref, sh in layout.iter_shapes():
        color = _depth_color(sh.depth_um, dmin, dmax)
        opacity = "0.30" if getattr(sh, "ignored", False) else "0.75"
        dash = ' stroke-dasharray="5 3"' if getattr(sh, "ignored", False) else ""

        if isinstance(sh, Rect):
            # (x,y) is the anchor corner; the box extends by signed (width,
            # height) and SmartFIB rotates it counter-clockwise about its centre.
            pts = ["%.2f,%.2f" % (sx(rx), sy(ry))
                   for rx, ry in rect_corners(sh.x, sh.y, sh.width, sh.height,
                                              sh.angle_deg)]
            parts.append('<polygon points="%s" fill="%s" fill-opacity="%s" stroke="%s"%s/>'
                         % (" ".join(pts), color, opacity, color, dash))
            if sh.imaging:
                # imaging marker at the box centre (the pivot: unchanged by rotation)
                ccx, ccy = rect_center(sh.x, sh.y, sh.width, sh.height)
                parts.append('<circle cx="%.1f" cy="%.1f" r="3" fill="#ffd166"/>'
                             % (sx(ccx), sy(ccy)))

        elif isinstance(sh, Polygon):
            pts = ["%.2f,%.2f" % (sx(px), sy(py)) for px, py in sh.points]
            parts.append('<polygon points="%s" fill="%s" fill-opacity="%s" stroke="%s"%s/>'
                         % (" ".join(pts), color, opacity, color, dash))

        elif isinstance(sh, Arc):
            # sample the annulus outline
            a1, a2 = sh.a1_deg, sh.a2_deg
            n = max(6, int(abs(a2 - a1) / 6))
            outer = []
            inner = []
            for k in range(n + 1):
                ang = math.radians(a1 + (a2 - a1) * k / n)
                outer.append((sh.cx + sh.r1 * math.cos(ang), sh.cy + sh.r1 * math.sin(ang)))
            for k in range(n + 1):
                ang = math.radians(a2 + (a1 - a2) * k / n)
                inner.append((sh.cx + sh.r2 * math.cos(ang), sh.cy + sh.r2 * math.sin(ang)))
            ring = outer + inner
            pts = ["%.2f,%.2f" % (sx(px), sy(py)) for px, py in ring]
            parts.append('<polygon points="%s" fill="%s" fill-opacity="%s" stroke="%s"%s/>'
                         % (" ".join(pts), color, opacity, color, dash))

        elif isinstance(sh, Text):
            fs = max(6.0, abs(sh.height) * scale)
            parts.append('<text x="%.1f" y="%.1f" fill="%s" font-size="%.1f">%s</text>'
                         % (sx(sh.x), sy(sh.y), color, fs, _svg_escape(sh.text)))

    # legend
    parts.append('<text x="%d" y="20" fill="#aaa" font-size="13">%s  -  %d shapes, depth %g..%g um</text>'
                 % (margin_px, _svg_escape(layout.name), len(depths), dmin, dmax))
    parts.append("</svg>")
    return "\n".join(parts)


def _svg_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def write_svg(layout: Layout, path: str, **kwargs) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(to_svg(layout, **kwargs))
