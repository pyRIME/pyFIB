"""Headless CLI: build an overlay from images + landmarks, no GUI required.

Examples
--------
    python -m pymap.cli --session session.json --out out/
    python -m pymap.cli --fixed fib.bmp --moving confocal.tif \
        --landmarks pairs.json --kind affine --cmap green --out out/

`pairs.json` is either a full session dict or a bare list of
{"fixed":[x,y], "moving":[x,y]} pairs.
"""

from __future__ import annotations

import argparse
import json
import os
import numpy as np
from PIL import Image

from .model import RegistrationSession, TransformKind, PreAlign
from .session import load_session
from .pipeline import register_and_warp
from .composite import to_uint8


def _session_from_args(args) -> RegistrationSession:
    if args.session:
        s = load_session(args.session)
    else:
        s = RegistrationSession(fixed_path=args.fixed, moving_path=args.moving,
                                transform=args.kind)
        with open(args.landmarks) as f:
            d = json.load(f)
        pairs = d["pairs"] if isinstance(d, dict) else d
        for p in pairs:
            s.add_pair(p["fixed"], p["moving"])
        s.prealign = PreAlign(rot90=args.rot90, flip_h=args.flip_h, flip_v=args.flip_v)
    if args.kind:
        s.transform = args.kind
    return s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pymap", description="pyMap overlay builder")
    ap.add_argument("--session")
    ap.add_argument("--fixed")
    ap.add_argument("--moving")
    ap.add_argument("--landmarks")
    ap.add_argument("--kind", choices=TransformKind.ALL, default=TransformKind.AFFINE)
    ap.add_argument("--rot90", type=int, default=0)
    ap.add_argument("--flip-h", dest="flip_h", action="store_true")
    ap.add_argument("--flip-v", dest="flip_v", action="store_true")
    ap.add_argument("--cmap", default="green")
    ap.add_argument("--opacity", type=float, default=0.5)
    ap.add_argument("--out", default="pymap_out")
    ap.add_argument("--meta", help="FIB metadata json (needed for handoff / um)")
    ap.add_argument("--handoff", metavar="DIR",
                    help="also write an elymaker handoff job to DIR")
    ap.add_argument("--ely", default="lamella.ely",
                    help="target .ely filename for the handoff")
    args = ap.parse_args(argv)

    session = _session_from_args(args)
    result = register_and_warp(session)

    os.makedirs(args.out, exist_ok=True)
    overlay = to_uint8(result.overlay(cmap=args.cmap, opacity=args.opacity))
    Image.fromarray(overlay).save(os.path.join(args.out, "overlay.png"))
    rgba = to_uint8(result.warped_rgba(cmap=args.cmap))
    # uint8 HxWx4 -> RGBA is inferred (mode= is removed in Pillow 13)
    Image.fromarray(rgba).save(os.path.join(args.out, "confocal_in_fib.png"))

    report = {
        "transform": result.registration.kind,
        "n_pairs": result.registration.n,
        "rms_px": result.registration.rms,
        "leave_one_out_rms_px": result.loo_rms,
        "per_point_residual_px": result.registration.residuals.tolist(),
    }
    with open(os.path.join(args.out, "report.json"), "w") as f:
        json.dump(report, f, indent=2)

    # Keep this Python 3.9-compatible: no nested same-quote f-strings (3.12+ only).
    loo = report["leave_one_out_rms_px"]
    loo_txt = "n/a" if loo is None else "%.3fpx" % loo
    print("transform=%s n=%d rms=%.3fpx loo_rms=%s"
          % (report["transform"], report["n_pairs"], report["rms_px"], loo_txt))
    print(f"wrote overlay.png, confocal_in_fib.png, report.json to {args.out}/")

    if args.handoff:
        if not args.meta:
            ap.error("--handoff requires --meta (FIB pixel size)")
        from .meta import FibImageMeta
        from .handoff import write_handoff
        meta = FibImageMeta.load(args.meta)
        underlay = result.overlay(cmap=args.cmap, opacity=args.opacity)
        tpx = []
        for (mx, my) in session.targets:
            fx, fy = result.registration.forward([[mx, my]])[0]
            tpx.append((float(fx), float(fy)))
        hrep = write_handoff(args.handoff, underlay, meta, ely_out=args.ely,
                             targets_fib_px=tpx)
        print(f"handoff: job.json + {hrep['n_targets']} target(s) -> {args.handoff}/ "
              f"(open with: elymaker-gui --job {args.handoff}/job.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
