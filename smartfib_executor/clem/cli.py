"""CLI for CLEM targeting (v0.52.0): clem-targets (files only) and clem-overview.

    python -m smartfib_executor.cli clem-targets --positions-file S\\positions.json
    python -m smartfib_executor.cli clem-overview --kind fib --grid Grid1 --positions-file S\\positions.json --arm
    python -m smartfib_executor.cli clem-overview --kind sem --grid Grid1 --mag 200 --positions-file S\\positions.json --arm

--positions-file names the SESSION (its folder holds clem/); the milling positions file
itself is never read or written by these commands.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import CLEM_DIRNAME, GRID_FIXTURES, OVERVIEW_POSITIONS, SEM_OVERVIEWS, clem_dir
from . import limits as L


def _session_dir(args) -> Path:
    from ..cli import _resolve_user_path
    return _resolve_user_path(args.positions_file).parent


def cmd_clem_targets(args: argparse.Namespace) -> int:
    from ..cli import _project_root, _resolve_user_path
    from .targets import find_targets_file, plan_targets, write_plan
    session = _session_dir(args)
    if args.targets:
        tpath = _resolve_user_path(args.targets)
    else:
        tpath = find_targets_file(clem_dir(session))
        if tpath is None:
            print(f"[clem] no pymap.targets.v2 file in {clem_dir(session)}")
            return 2
    plan = plan_targets(tpath, accept_qc=bool(args.accept_qc),
                        cal_dirs=[clem_dir(session), session, _project_root()])
    for line in plan.lines():
        print(line)
    if not args.no_write:
        out = write_plan(plan)
        print(f"[clem] plan written: {out}")
    return 0 if plan.ok else 2


def cmd_clem_overview(args: argparse.Namespace) -> int:
    from ..smartsem_com import SmartSEM
    from .overview import OverviewRefused, capture_fib_overview, capture_sem_overview
    session = _session_dir(args)
    kind, grid = args.kind, args.grid
    mag = float(args.mag if args.mag is not None else
                (L.FIB_OVERVIEW_MAG if kind == "fib" else L.SEM_OVERVIEW_MAG))
    out = clem_dir(session) / (OVERVIEW_POSITIONS if kind == "fib" else SEM_OVERVIEWS)
    if not args.arm:
        lo, hi = L.SEM_NORMAL_TILT_RANGE_DEG
        print(f"[DRY RUN] {kind.upper()} overview of {grid} at SEM normal. Would do:")
        print(f"  - Refuse unless SmartSEM reads not-milling for 5 s and the stage is at "
              f"T {lo:g}-{hi:g} deg, R {L.SEM_NORMAL_ROT_DEG:g} +/- {L.SEM_NORMAL_ROT_TOL_DEG:g} "
              f"(go to {GRID_FIXTURES.get(grid, grid)} first). The stage is never moved.")
        if kind == "fib":
            print("  - Zero the FIB beam shift (refuse if it does not read 0)")
            print(f"  - Capture one FIB frame at {mag:g}x, {L.OVERVIEW_WIDTH_PX} x "
                  f"{L.OVERVIEW_HEIGHT_PX}, no 800x frame")
        else:
            print("  - CMD_FIB_MODE_SEM, set AP_MAG = %g, grab ONE fresh SEM frame at "
                  "%d x %d (detector, scan speed, brightness, contrast unchanged)"
                  % (mag, L.OVERVIEW_WIDTH_PX, L.OVERVIEW_HEIGHT_PX))
            print("  - Record the stage, field width/height, WD, EHT, detector, scan "
                  "rotation with it")
        print(f"  - Save under {clem_dir(session) / 'images'} and record in {out}")
        return 0
    with SmartSEM() as sem:
        if args.connect == "local":
            sem.connect_local(args.machine)
        else:
            sem.connect_remoting()
        try:
            if kind == "fib":
                rec = capture_fib_overview(sem, session, grid, mag=mag, name=args.name)
                print(f"CLEM-OVERVIEW: fib {rec.name} {rec.image_bmp}")
            else:
                e = capture_sem_overview(sem, session, grid, mag=mag, name=args.name)
                print(f"CLEM-OVERVIEW: sem {e['name']} {e['image']}")
        except OverviewRefused as e:
            print(f"[clem] REFUSED: {e}")
            return 4
    return 0


def _markup_plan(args, session):
    """(targets plan, markup plan) for clem-markup, files only."""
    from ..cli import _project_root, _resolve_user_path
    from .markup import plan_markup, prepare_file
    from .targets import find_targets_file, plan_targets
    tpath = (_resolve_user_path(args.targets_file) if args.targets_file
             else find_targets_file(clem_dir(session)))
    if tpath is None:
        return None, None
    tplan = plan_targets(tpath, accept_qc=bool(args.accept_qc),
                         cal_dirs=[clem_dir(session), session, _project_root()])
    if args.all:
        sel = [r.id for r in tplan.rows if r.status in ("ok", "warn")]
    else:
        sel = [s.strip() for s in (args.targets or "").split(",") if s.strip()]
    base = _resolve_user_path(args.markup) if args.markup else None
    mp = plan_markup(tplan, base=base, selected=sel)
    mp = prepare_file(mp, session, reset=bool(args.reset), write=not args.no_write)
    return tplan, mp


def cmd_clem_markup(args: argparse.Namespace) -> int:
    """Mark targets: one SmartFIB layout with a markup copy on every marked target,
    milled at the FIB overview position (the stage never moves)."""
    from .markup import MarkupError, MarkupRefused, mill_markup
    session = _session_dir(args)
    if not args.all and not args.targets:
        print("[markup] choose the targets: --targets T1,T2,... or --all (OK + warning rows)")
        return 2
    try:
        tplan, mp = _markup_plan(args, session)
    except MarkupError as e:
        print(f"[markup] REFUSED: {e}")
        return 2
    if mp is None:
        print(f"[markup] no pymap.targets.v2 file in {clem_dir(session)}")
        return 2
    for line in mp.lines():
        print(line)
    if not mp.ok or mp.file_state not in ("generated", "edited"):
        return 2
    if args.expect_sha256 and args.expect_sha256 != mp.file_sha256:
        print(f"[markup] REFUSED: the layout is not the one confirmed ({mp.file_sha256} vs "
              f"{args.expect_sha256}): the targets or the file changed. Check it again.")
        return 2
    rec = mp.overview
    if not args.arm:
        print("[DRY RUN] Not armed. Would do:")
        print(f"  - Refuse unless SmartSEM reads not-milling for 5 s, the stage is at SEM "
              f"normal and still at {rec.name}'s position (X/Y/Z/M within "
              f"{L.MARKUP_STAGE_TOL_M * 1e6:g} um, T/R within {L.MARKUP_ANGLE_TOL_DEG:g} deg). "
              f"The stage is never moved.")
        print("  - Zero the FIB beam shift (refuse unless it reads 0); refuse unless the "
              "FIB scan rotation reads 0")
        print(f"  - FIB magnification {rec.fib_magnification_x:g}x (read back), objective "
              f"{rec.fib_objective_potential_v:.1f} V (as at the overview)")
        print(f"  - Mill {Path(mp.file_path).name} through the SmartFIB drop folder "
              f"({args.smartfib_drop_dir}\\{args.smartfib_drop_name}), wait {args.wait}, "
              f"confirm done {args.done_confirm_s:g} s")
        if not args.no_after_image:
            print(f"  - One after image at {rec.fib_magnification_x:g}x into clem/images")
        print(f"  - Record in clem/pyfib_clem_markups.json")
        return 0
    from ..smartsem_com import SmartSEM
    with SmartSEM() as sem:
        if args.connect == "local":
            sem.connect_local(args.machine)
        else:
            sem.connect_remoting()
        try:
            entry = mill_markup(sem, session, mp, drop_dir=args.smartfib_drop_dir,
                            drop_name=args.smartfib_drop_name,
                            load_delay_s=args.smartfib_load_delay_s,
                            prepare_delay_s=args.smartfib_prepare_delay_s,
                            wait_strategy=args.wait, wait_timeout_s=float(args.timeout),
                            done_confirm_s=float(args.done_confirm_s),
                            after_image=not args.no_after_image)
        except (MarkupRefused, MarkupError) as e:
            print(f"[markup] REFUSED: {e}")
            return 4
        except Exception as e:  # noqa: BLE001
            print(f"[markup] ERROR: {type(e).__name__}: {e} -- check SmartFIB and the Log "
                  f"(a failure after the checks is recorded in clem/pyfib_clem_markups.json)")
            return 1
    print(f"CLEM-MARKUP: milled {len(mp.milled_ids())} file={Path(mp.file_path).name} "
          f"after={entry.get('after_image')}")
    return 0


def add_parsers(sub, add_connection_args) -> None:
    p = sub.add_parser(
        "clem-targets",
        help="CLEM (v0.52.0, files only): check pyMap's targets file against its FIB "
             "overview and the safety limits, compute each target's stage X/Y, print the "
             "plan and write <session>/clem/pyfib_clem_plan.json. Moves nothing.")
    p.add_argument("targets", nargs="?", default=None,
                   help="pymap.targets.v2 file (default: the newest one in <session>/clem).")
    p.add_argument("--positions-file", default="positions.json",
                   help="The session's positions file (its folder holds clem/).")
    p.add_argument("--accept-qc", action="store_true",
                   help=f"Operator override: accept a registration error above "
                        f"{L.QC_REFUSE_UM:g} um (or none given). Recorded in the plan.")
    p.add_argument("--no-write", action="store_true", help="Print only; do not write the plan.")
    p.set_defaults(func=cmd_clem_targets)

    p = sub.add_parser(
        "clem-overview",
        help="CLEM (v0.52.0): capture a FIB (400x) or SEM overview of a grid at SEM "
             "normal into <session>/clem/. Refuses unless at T 29-31, R 12 and not "
             "milling. Never moves the stage. Dry run without --arm.")
    p.add_argument("--kind", choices=("fib", "sem"), required=True)
    p.add_argument("--grid", choices=tuple(GRID_FIXTURES), required=True)
    p.add_argument("--mag", type=float, default=None,
                   help=f"Magnification (default FIB {L.FIB_OVERVIEW_MAG:g}x, SEM "
                        f"{L.SEM_OVERVIEW_MAG:g}x).")
    p.add_argument("--name", default=None,
                   help="Record name (default clem_ov_<grid>_<n> / clem_sem_<grid>_<n>).")
    p.add_argument("--positions-file", default="positions.json",
                   help="The session's positions file (its folder holds clem/).")
    p.add_argument("--arm", action="store_true",
                   help="Actually execute on the instrument. Without it: dry run.")
    add_connection_args(p)
    p.set_defaults(func=cmd_clem_overview)

    p = sub.add_parser(
        "clem-markup",
        help="CLEM (v0.53.0): mark targets -- one SmartFIB layout with a copy of the "
             "markup pattern (default Markup_Standard, origin = lamella) on every chosen "
             "target, milled at the FIB overview position and magnification. The stage "
             "never moves. Writes <session>/clem/markup/<overview>_markup.ely (an edit "
             "saved in ELYMaker is kept). Dry run without --arm.")
    p.add_argument("targets_file", nargs="?", default=None,
                   help="pymap.targets.v2 file (default: the newest one in <session>/clem).")
    p.add_argument("--positions-file", default="positions.json",
                   help="The session's positions file (its folder holds clem/).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--targets", default=None, help="Comma-separated target ids to mark.")
    g.add_argument("--all", action="store_true", help="Mark every OK and warning target.")
    p.add_argument("--markup", default=None,
                   help="Markup .ely whose origin is the lamella (default: the bundled "
                        "markup_standard.ely).")
    p.add_argument("--reset", action="store_true",
                   help="Regenerate the markup file (discards an edit made in ELYMaker).")
    p.add_argument("--accept-qc", action="store_true",
                   help=f"Accept a registration error above {L.QC_REFUSE_UM:g} um.")
    p.add_argument("--no-write", action="store_true",
                   help="Plan only; do not write the markup file (dry run).")
    p.add_argument("--expect-sha256", default=None,
                   help="Refuse unless the layout is exactly this one (the GUI passes the "
                        "layout the operator confirmed).")
    p.add_argument("--wait", choices=["none", "mode_change", "blanked", "auto", "sleep"],
                   default="auto")
    p.add_argument("--timeout", type=float, default=5400.0,
                   help="Milling wait timeout (s); 0 = none.")
    p.add_argument("--done-confirm-s", type=float, default=30.0,
                   help="Milling-complete confirmation window (s). Default 30 (the tabs "
                        "pass their Confirm done value).")
    p.add_argument("--no-after-image", action="store_true",
                   help="Skip the after image at the overview magnification.")
    p.add_argument("--smartfib-drop-dir", default=r"C:\ProgramData\Carl Zeiss\SmartFIB\API\Drop")
    p.add_argument("--smartfib-drop-name", default="ApiLayout.ely")
    p.add_argument("--smartfib-load-delay-s", type=float, default=1.0)
    p.add_argument("--smartfib-prepare-delay-s", type=float, default=1.0)
    p.add_argument("--arm", action="store_true",
                   help="Actually execute on the instrument. Without it: dry run.")
    add_connection_args(p)
    p.set_defaults(func=cmd_clem_markup)
