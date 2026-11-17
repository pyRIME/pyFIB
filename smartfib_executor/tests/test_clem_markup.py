"""Mark targets (v0.53.0): markup trenches at SEM normal, offline, no instrument.

Evan 2026-10-10: Markup_Standard.ely is the standard markup (two trenches + a tick on the
right trench marking where the lamella goes: midway between the trenches, at the tick's
height), milled at the correlation magnification, several in the same low-mag image.
pyFIB writes ONE layout with a copy per target, placed on the FIB overview, and mills it
with the stage at the overview position (never moved).
"""

from __future__ import annotations

import io
import json
import math
import re
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from ..clem import limits as L
from ..clem import markup as M
from ..clem import targets as T
from .test_clem import PS_400, Scope, _cli, _overview, _session

HERE = Path(__file__).resolve().parent
OPERATOR_FILE = HERE.parents[1] / "elymaker_tests" / "fixtures" / "Markup_Standard.ely"


def _numbers_out(text: str) -> str:
    """The text with RECT x/y and POLYGON points blanked: what must stay identical."""
    text = re.sub(r'(\spoints=")[^"]*(")', r"\1#\2", text)
    return re.sub(r"<RECT\b[^>]*>",
                  lambda m: re.sub(r'(\s[xy]=")[^"]*(")', r"\1#\2", m.group(0)), text)


def test_bundled_markup_is_the_operators_file_centred_on_the_lamella():
    op_text, op = M.read_markup(OPERATOR_FILE)
    text, sh = M.read_markup(M.DEFAULT_MARKUP)
    assert [s.tag for s in sh] == ["POLYGON", "POLYGON", "RECT"]
    left, right, tick = sh
    cl, cr = (left.xmin + left.xmax) / 2, (right.xmin + right.xmax) / 2
    assert abs(cl + cr) < 1e-5, (cl, cr)                    # origin midway between trenches
    assert abs((cr - cl) - 27.7575) < 1e-4                    # 27.76 um centre to centre
    assert abs(tick.ymin + tick.ymax) < 1e-5                 # origin at the tick's height
    assert tick.xmin > right.xmin and tick.xmax > right.xmax  # tick on the RIGHT trench
    for a, b in zip(op, sh):                                  # same shapes, only moved
        assert abs((a.xmax - a.xmin) - (b.xmax - b.xmin)) < 1e-5
        assert abs((a.ymax - a.ymin) - (b.ymax - b.ymin)) < 1e-5
    assert _numbers_out(text) == _numbers_out(op_text)        # probe, dose, depth, header
    assert M._probes(text) == ["30kV:1.5nA"] and 'z_extent="1e-005 m"' in text
    b = M.bbox(sh)
    assert b[0] > -15 and b[1] < 19 and abs(b[2]) < 15 and b[3] < 15


def test_combine_places_exact_copies_and_keeps_everything_else():
    text, base = M.read_markup(M.DEFAULT_MARKUP)
    offs = [(-100.0, 50.0), (0.25, -0.125), (120.5, -80.0)]
    out = M.combine(text, offs)
    got = M.shapes_from_text(out)
    assert len(got) == 9
    for k, (dx, dy) in enumerate(offs):
        for j, s in enumerate(base):
            g, e = got[3 * k + j], s.shifted(dx, dy)
            assert abs(g.xmin - e.xmin) < 1e-6 and abs(g.ymax - e.ymax) < 1e-6
    head, tail = text.split("<LAYER_REFERENCE")[0], text.split("</LAYER_REFERENCE>")[1]
    assert out.startswith(head) and out.endswith(tail)
    assert out.count('<PROBE name="30kV:1.5nA"') == text.count('<PROBE name="30kV:1.5nA"') - 3 + 9


def test_only_simple_markups_are_accepted():
    text = M.DEFAULT_MARKUP.read_text()
    bad = {
        "rotated rect": text.replace('angle="0 deg"', 'angle="15 deg"'),
        "arc": text.replace("<RECT ", '<ARC cx="0" cy="0" r1="1" r2="2" a1="0" a2="90" ', 1)
                   .replace("</RECT>", "</ARC>", 1),
        "frame centre": text.replace('frame_cx="0"', 'frame_cx="12"'),
        "instances": text.replace("<INSTANCE_LIST/>", "<INSTANCE_LIST><INSTANCE/></INSTANCE_LIST>"),
        "two layer refs": text.replace("</LAYER_REFERENCE>",
                                       '</LAYER_REFERENCE><LAYER_REFERENCE ref="Layer">'
                                       '</LAYER_REFERENCE>'),
    }
    for what, t in bad.items():
        try:
            M.shapes_from_text(t)
        except M.MarkupError:
            continue
        raise AssertionError(f"{what} was accepted")


def test_pixel_to_layout_matches_elymaker():
    x, y = M.pixel_to_layout_um(612.4, 301.8, width_px=1024, height_px=768, pixel_size_m=PS_400)
    ps = PS_400 * 1e6
    assert abs(x - 100.4 * ps) < 1e-9 and abs(y - 82.2 * ps) < 1e-9   # +Y up, top row = +Y
    try:
        from elymaker.gui.coords import ImageCalibration
    except Exception:  # noqa: BLE001  (no Qt here)
        return
    cal = ImageCalibration(pixel_size_um=ps, image_w_px=1024, image_h_px=768,
                           center_x_um=0.0, center_y_um=0.0)
    ex, ey = cal.pixel_to_layout(612.4, 301.8)
    assert abs(ex - x) < 1e-9 and abs(ey - y) < 1e-9


def _targets(*pxs):
    return [{"id": f"T{i + 1}", "label": str(i + 1), "source": "ov1", "fib_px": list(p)}
            for i, p in enumerate(pxs)]


def _plans(tp, sel, **kw):
    tplan = T.plan_targets(tp, cal_dirs=[])
    return tplan, M.plan_markup(tplan, selected=sel, **kw)


def test_plan_places_each_target_and_refuses_edges_and_neighbours():
    ps = PS_400 * 1e6
    near = 512 + 18.0 / ps + 14.0 / ps                      # 14 um right of T1: on its trench
    d, clem, tp = _session(targets=_targets((512 + 18.0 / ps, 384), (100.0, 700.0),
                                            (near, 384), (300.0, 200.0)))
    tplan, mp = _plans(tp, ["T1", "T2", "T4"])
    rows = {r.id: r for r in mp.rows}
    assert abs(rows["T1"].x_um - 18.0) < 1e-6 and abs(rows["T1"].y_um) < 1e-9
    # T3 is not ticked but still protected: T1's trench would cut it.
    assert rows["T1"].status == "refused" and "of target T3 (not ticked)" in rows["T1"].reasons[0]
    assert rows["T2"].status == "refused" and "inside" in rows["T2"].reasons[0]   # field edge
    assert rows["T3"].status == "not selected" and rows["T4"].status == "marked" and mp.ok
    _, mp = _plans(tp, ["T1", "T3"])                         # neighbours 14 um apart
    rows = {r.id: r for r in mp.rows}
    assert rows["T1"].status == "refused" and "of target T3" in rows["T1"].reasons[0]
    assert rows["T3"].status == "refused" and not mp.ok      # nothing left to mark
    _, mp = _plans(tp, [])
    assert not mp.ok and "no target" in mp.errors[0]


def test_plan_refuses_with_the_targets_file_and_for_refused_targets():
    d, clem, tp = _session(qc=4.0)                           # registration 4 um > 3
    _, mp = _plans(tp, ["T1"])
    assert not mp.ok and "targets file is refused" in mp.errors[0]
    d, clem, tp = _session(targets=_targets((2000.0, 10.0)))
    _, mp = _plans(tp, ["T1"])
    assert mp.rows[0].status == "refused" and "outside the overview" in mp.rows[0].reasons[0]


def test_markup_file_generated_kept_regenerated_edited_and_reset():
    ps = PS_400 * 1e6
    d, clem, tp = _session(targets=_targets((512, 384), (700, 300), (300, 500)))
    _, mp = _plans(tp, ["T1", "T2"])
    mp = M.prepare_file(mp, d)
    f, side = M.markup_paths(d, "clem_ov_grid1_1")
    assert mp.file_state == "generated" and f.exists() and side.exists()
    assert mp.milled_ids() == ["T1", "T2"] and len(M.read_markup(f)[1]) == 6
    sha = mp.file_sha256
    _, mp = _plans(tp, ["T1", "T2"])
    assert M.prepare_file(mp, d).file_sha256 == sha                         # kept
    _, mp = _plans(tp, ["T1", "T2", "T3"])
    mp = M.prepare_file(mp, d)
    assert mp.file_sha256 != sha and len(M.read_markup(f)[1]) == 9          # regenerated
    # The operator nudges T1's markup in ELYMaker (0.5 um right) and saves.
    text = f.read_text()
    text = text.replace('x="14.10975"', 'x="14.60975"', 1)
    f.write_text(text)
    _, mp = _plans(tp, ["T1"])                                               # selection changed
    mp = M.prepare_file(mp, d)
    assert mp.ok and mp.file_state == "edited" and mp.milled_ids() == ["T1", "T2", "T3"]
    assert any("AS SAVED" in w for w in mp.warnings) and mp.extra_shapes == 0
    assert 'x="14.60975"' in f.read_text()                                   # edit kept
    # An edit that puts a trench on another target is refused.
    f.write_text(text.replace("(-14.39925 ", f"({(700 - 512) * ps - 0.5:.5f} ", 1))
    _, mp = _plans(tp, ["T1"])
    mp = M.prepare_file(mp, d)
    assert not mp.ok and "edited markup fails" in mp.errors[0]
    # pyMap writes new targets: the edited file belongs to the old ones -> Reset.
    f.write_text(text)
    data = json.loads(tp.read_text()); data["targets"][0]["fib_px"] = [520, 384]
    tp.write_text(json.dumps(data))
    T.ready_marker_path(tp).write_text(json.dumps(
        {"schema": "pymap.ready.v1", "sha256": T.file_sha256(tp)}))
    _, mp = _plans(tp, ["T1"])
    mp = M.prepare_file(mp, d)
    assert not mp.ok and "Reset markup" in mp.errors[0]
    _, mp = _plans(tp, ["T1"])
    mp = M.prepare_file(mp, d, reset=True)
    assert mp.ok and mp.file_state == "generated" and mp.milled_ids() == ["T1"]
    side.unlink()                                                            # record lost
    _, mp = _plans(tp, ["T1"])
    assert M.prepare_file(mp, d).file_state == "refused"


def test_edited_layout_is_judged_by_its_content_not_by_the_record():
    """Review finding: ELYMaker still holding an older 3-target layout saves it after
    pyFIB regenerated for 2 targets -> the file decides: 3 markups, all checked."""
    d, clem, tp = _session(targets=_targets((512, 384), (700, 300), (300, 500)))
    _, mp = _plans(tp, ["T1", "T2", "T3"])
    mp = M.prepare_file(mp, d)
    old_layout = Path(mp.file_path).read_text()
    _, mp = _plans(tp, ["T1", "T2"])
    mp = M.prepare_file(mp, d)                                   # regenerated for 2
    Path(mp.file_path).write_text(old_layout)                    # the stale save
    _, mp = _plans(tp, ["T1", "T2"])
    mp = M.prepare_file(mp, d)
    assert mp.file_state == "edited" and mp.milled_ids() == ["T1", "T2", "T3"]
    # An extra trench that is no target's markup is milled as saved but reported.
    Path(mp.file_path).write_text(old_layout.replace("</LAYER_REFERENCE>", EXTRA_RECT))
    _, mp = _plans(tp, ["T1"])
    mp = M.prepare_file(mp, d)
    assert mp.ok and mp.extra_shapes == 1 and "1 shape(s) that are not a markup" in mp.warnings[-1]


EXTRA_RECT = '\n        <RECT dose_factor="1" z_extent="1e-006 m" height="-1" width="1" angle="0 deg" x="-60" y="-60"/>\n      </LAYER_REFERENCE>'


def test_parser_refuses_anything_it_cannot_read_exactly():
    text = M.DEFAULT_MARKUP.read_text()
    for bad in (text.replace("(-14.39925 14.08553)", "(900 900", 1),
                text.replace("(-14.39925 14.08553)", "(900;900)", 1),
                text.replace('x="14.10975"', 'x="1_4.1"', 1)):
        try:
            M.shapes_from_text(bad)
        except M.MarkupError:
            continue
        raise AssertionError("accepted: " + bad[700:900])


def test_annotated_overview_is_a_same_size_copy():
    from PIL import Image
    d, clem, tp = _session(targets=_targets((512, 384), (700, 300)))
    img = clem / "images" / "clem_ov_grid1_1.bmp"
    Image.new("L", (1024, 768), 60).save(img)
    data = json.loads(tp.read_text()); data["sources"][0]["image_sha256"] = T.file_sha256(img)
    tp.write_text(json.dumps(data))
    T.ready_marker_path(tp).write_text(json.dumps(
        {"schema": "pymap.ready.v1", "sha256": T.file_sha256(tp)}))
    _, mp = _plans(tp, ["T1"])
    before = img.read_bytes()
    out = M.annotate_overview(mp, clem / "markup" / "a.png")
    im = Image.open(out)
    assert im.size == (1024, 768) and img.read_bytes() == before
    assert im.getpixel((512, 384)) != (60, 60, 60)            # cross drawn on T1


# ------------------------------------------------------------------ milling
class _Runner:
    jobs = []

    def __init__(self, sem):
        self.sem = sem

    def run(self, job):
        _Runner.jobs.append(job)
        self.sem.calls.append(("mill", Path(job.ely_local_path).name))


def _grab(sem, rec, images_dir, **kw):
    p = Path(images_dir) / f"{rec.name}_after_x.bmp"
    p.write_bytes(b"BM")
    sem.calls.append(("grab", p.name))
    return p, None


def _ready_scope(**kw):
    s = Scope(beam=(0.0, 0.0), **kw)
    st = _overview().stage_at
    s.v.update(AP_STAGE_AT_X=st.x_m, AP_STAGE_AT_Y=st.y_m, AP_STAGE_AT_Z=st.z_m,
               AP_STAGE_AT_M=st.m_m, AP_FIB_MAGNIFICATION=400.0)
    return s


def _prepared(sel=("T1", "T2")):
    d, clem, tp = _session(targets=_targets((512, 384), (700, 300)))
    _, mp = _plans(tp, list(sel))
    return d, M.prepare_file(mp, d)


def _mill(sem, d, mp, **kw):
    _Runner.jobs = []
    with redirect_stdout(io.StringIO()):
        return M.mill_markup(sem, d, mp, drop_dir=d / "drop", guard_s=0, log=lambda m: None,
                             _runner_cls=_Runner, _grab_fn=_grab, sleep=lambda s: None, **kw)


def test_mill_checks_then_mills_one_layout_in_place_and_records_it():
    d, mp = _prepared()
    sem = _ready_scope()
    e = _mill(sem, d, mp, done_confirm_s=20)
    assert sem.moves == []                                    # the stage never moves
    names = [c[1] for c in sem.calls if c[0] in ("set", "cmd")]
    assert names.index("AP_FIB_BEAM_SHIFT_X") < names.index("AP_FIB_MAGNIFICATION")
    assert ("set", "AP_FIB_MAGNIFICATION", 400.0) in sem.calls
    assert ("set", "AP_FIB_OBJECTIVE_POTENTIAL", 16400.0) in sem.calls
    mills = [c for c in sem.calls if c[0] == "mill"]
    assert len(mills) == 1 and mills[0][1].startswith("clem_ov_grid1_1_markup_")
    job = _Runner.jobs[0]
    assert Path(job.ely_local_path).parent.name == "milled"       # the private checked copy
    assert Path(job.ely_local_path).read_bytes() == Path(mp.file_path).read_bytes()
    assert job.arm and job.start_milling and job.done_confirm_s == 20 and job.wait_strategy == "auto"
    i_mill = sem.calls.index(mills[0])
    assert [c for c in sem.calls[i_mill:] if c[0] == "grab"]  # after image after milling
    assert e["status"] == "milled" and [t["id"] for t in e["targets"]] == ["T1", "T2"]
    log = M.load_markups_log(d)
    assert len(log) == 1 and log[0]["pattern"]["sha256"] == mp.file_sha256
    assert log[0]["after_image"].startswith("images/clem_ov_grid1_1_markup_after")
    assert log[0]["pattern"]["milled_copy"].startswith("markup/milled/")


def test_mill_refuses_before_anything_when_the_stage_moved_or_scope_is_busy():
    for what, tweak in (("moved 2 um", lambda s: s.v.update(AP_STAGE_AT_X=s.v["AP_STAGE_AT_X"] + 2e-6)),
                        ("Z moved", lambda s: s.v.update(AP_STAGE_AT_Z=s.v["AP_STAGE_AT_Z"] - 5e-6)),
                        ("tilted", lambda s: s.v.update(AP_STAGE_AT_T=30.2)),
                        ("milling", lambda s: setattr(s, "milling", True))):
        d, mp = _prepared()
        sem = _ready_scope()
        tweak(sem)
        try:
            _mill(sem, d, mp)
        except M.MarkupRefused:
            pass
        else:
            raise AssertionError(f"{what}: not refused")
        assert not [c for c in sem.calls if c[0] in ("set", "mill")], (what, sem.calls)
        assert M.load_markups_log(d) == []
        assert not (Path(mp.file_path).parent / "milled").exists()     # nothing written


def test_mill_refuses_scan_rotation_stuck_beam_shift_and_a_changed_file():
    d, mp = _prepared()
    sem = _ready_scope()
    sem.v["AP_FIB_SCAN_ROTATION"] = 0.2
    try:
        _mill(sem, d, mp)
    except M.MarkupRefused as e:
        assert "scan rotation" in str(e)
    else:
        raise AssertionError("scan rotation")
    assert not [c for c in sem.calls if c[0] in ("set", "cmd", "mill")]   # before any set
    sem = _ready_scope()
    sem.v.pop("AP_FIB_MAGNIFICATION")
    real_get = sem.get_float

    def no_mag(m):
        if m == "AP_FIB_MAGNIFICATION":
            raise RuntimeError("COM read failed")
        return real_get(m)
    sem.get_float = no_mag
    try:
        _mill(sem, d, mp)
    except M.MarkupRefused as e:
        assert "magnification" in str(e)
    else:
        raise AssertionError("unreadable magnification accepted")
    assert not [c for c in sem.calls if c[0] == "mill"]
    assert M.load_markups_log(d)[-1]["status"] == "refused"
    sem = _ready_scope(sticky_beam=True)
    sem.v["AP_FIB_BEAM_SHIFT_X"] = 2e-6
    try:
        _mill(sem, d, mp)
    except M.MarkupRefused as e:
        assert "beam shift" in str(e)
    else:
        raise AssertionError("stuck beam shift")
    Path(mp.file_path).write_text(Path(mp.file_path).read_text() + " ")
    sem = _ready_scope()
    try:
        _mill(sem, d, mp)
    except M.MarkupRefused as e:
        assert "changed after it was checked" in str(e)
    else:
        raise AssertionError("changed file")
    assert not [c for c in sem.calls if c[0] in ("set", "mill")]


def test_mill_switches_to_fib_only_if_needed_and_rechecks_the_mag_before_milling():
    # v0.54.0 (bug #14): CMD_FIB_MODE_FIB only when not already in FIB imaging ...
    for imaging, sent in (("FIB", 0), ("SEM", 1)):
        d, mp = _prepared()
        sem = _ready_scope()
        sem.imaging = imaging
        assert _mill(sem, d, mp)["status"] == "milled"
        assert sem.calls.count(("cmd", "CMD_FIB_MODE_FIB")) == sent, (imaging, sem.calls)
    # ... and SmartSEM's late magnification reset (after the read-back) refuses the mill.
    d, mp = _prepared()
    sem = _ready_scope()
    real_set = sem.set_value

    def late_reset(m, v):
        real_set(m, v)
        if m == "AP_FIB_OBJECTIVE_POTENTIAL":          # set after the mag read-back
            sem.v["AP_FIB_MAGNIFICATION"] = 175.9
    sem.set_value = late_reset
    try:
        _mill(sem, d, mp)
    except M.MarkupRefused as e:
        assert "just before milling" in str(e) and "175.9" in str(e)
    else:
        raise AssertionError("late magnification reset not caught")
    assert not [c for c in sem.calls if c[0] == "mill"]
    assert M.load_markups_log(d)[-1]["status"] == "refused"


def test_cli_dry_run_needs_a_selection_and_moves_nothing():
    d, clem, tp = _session(targets=_targets((512, 384), (700, 300)))
    pf = str(d / "positions.json")
    rc, out, conn = _cli(["clem-markup", "--positions-file", pf])
    assert rc == 2 and "--targets" in out and not conn
    rc, out, conn = _cli(["clem-markup", "--positions-file", pf, "--all"])
    assert rc == 0 and not conn and "[DRY RUN]" in out and "never moved" in out
    assert "CLEM-MARKUP-PLAN: marked=2" in out
    assert (clem / "markup" / "clem_ov_grid1_1_markup.ely").exists()


def test_cli_armed_mills_through_the_real_runner_with_the_stage_in_place():
    d, clem, tp = _session(targets=_targets((512, 384), (700, 300)))
    pf = str(d / "positions.json")
    sem = _ready_scope()
    drop = d / "drop"
    drop.mkdir()
    from .. import site_ops
    old_guard, site_ops.GUARD_S = site_ops.GUARD_S, 0.0     # the 5 s watch, for test speed
    try:
        _cli_armed(pf, sem, sem2_offset=3e-6, drop=drop, clem=clem)
    finally:
        site_ops.GUARD_S = old_guard


def _cli_armed(pf, sem, *, sem2_offset, drop, clem):
    fast = ["--smartfib-load-delay-s", "0", "--smartfib-prepare-delay-s", "0"]
    rc, out, conn = _cli(["clem-markup", "--positions-file", pf, "--targets", "T1",
                          "--wait", "none", "--done-confirm-s", "0", "--no-after-image",
                          "--smartfib-drop-dir", str(drop), "--arm"] + fast, sem=sem)
    assert rc == 0 and conn, out
    assert "CLEM-MARKUP: milled 1" in out
    assert sem.moves == []
    assert (drop / "ApiLayout.ely").read_bytes() == (clem / "markup" /
                                                     "clem_ov_grid1_1_markup.ely").read_bytes()
    assert ("cmd", "CMD_SMARTFIB_LOAD_ELY") in sem.calls
    sem2 = _ready_scope()
    sem2.v["AP_STAGE_AT_Y"] += sem2_offset
    rc, out, _ = _cli(["clem-markup", "--positions-file", pf, "--targets", "T1", "--wait",
                       "none", "--smartfib-drop-dir", str(drop), "--arm"] + fast, sem=sem2)
    assert rc == 4 and "REFUSED" in out and "not at clem_ov_grid1_1" in out
    rc, out, conn = _cli(["clem-markup", "--positions-file", pf, "--targets", "T1",
                          "--expect-sha256", "sha256:0", "--arm"] + fast, sem=_ready_scope())
    assert rc == 2 and "not the one confirmed" in out and not conn


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc()
            failed += 1; print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
