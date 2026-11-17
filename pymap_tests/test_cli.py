import json
import numpy as np
from PIL import Image

from pymap.cli import main
from pymap.model import RegistrationSession, TransformKind
from pymap.session import save_session
from pymap.meta import FibImageMeta


def _session(tmp_path):
    fib = np.zeros((80, 100), np.float32)
    conf = np.zeros((80, 100), np.float32)
    marks = [(20, 15), (70, 18), (25, 60), (80, 55)]
    sh = (8.0, -4.0)
    for x, y in marks:
        fib[y, x] = 1.0
        conf[int(y - sh[1]), int(x - sh[0])] = 1.0
    fp = tmp_path / "fib.png"
    cp = tmp_path / "conf.png"
    Image.fromarray((fib * 255).astype(np.uint8)).save(str(fp))
    Image.fromarray((conf * 255).astype(np.uint8)).save(str(cp))
    s = RegistrationSession(fixed_path=str(fp), moving_path=str(cp),
                            transform=TransformKind.AFFINE)
    for x, y in marks:
        s.add_pair((x, y), (x - sh[0], y - sh[1]))
    s.targets = [(20 - sh[0], 15 - sh[1])]
    sp = tmp_path / "session.json"
    save_session(s, str(sp))
    mp = tmp_path / "meta.json"
    FibImageMeta(image_width=100, image_height=80, fov_width_m=20e-6).save(str(mp))
    return sp, mp


def test_cli_overlay_and_handoff(tmp_path, capsys):
    sp, mp = _session(tmp_path)
    out = tmp_path / "out"
    hand = tmp_path / "hand"
    rc = main(["--session", str(sp), "--meta", str(mp), "--out", str(out),
               "--handoff", str(hand), "--ely", "lamella.ely"])
    assert rc == 0
    for name in ("overlay.png", "confocal_in_fib.png", "report.json"):
        assert (out / name).exists()
    job = json.loads((hand / "job.json").read_text())
    assert job["schema"] == "elymaker.handoff.v1"      # contract unchanged
    side = json.loads((hand / "targets.json").read_text())
    assert side["schema"] == "pymap.targets.v1"
    assert len(side["targets"]) == 1
    printed = capsys.readouterr().out
    assert "transform=affine" in printed and "loo_rms=" in printed


def test_cli_loo_na_with_minimum_pairs(tmp_path, capsys):
    # exactly 3 affine pairs -> leave-one-out unavailable -> prints n/a
    sp, _ = _session(tmp_path)
    d = json.loads(sp.read_text())
    d["pairs"] = d["pairs"][:3]
    sp.write_text(json.dumps(d))
    assert main(["--session", str(sp), "--out", str(tmp_path / "o")]) == 0
    assert "loo_rms=n/a" in capsys.readouterr().out
