"""Milling-complete confirmation window (v0.52.2).

Evan 2026-10-09: the 30 s confirmation after a mill is longer than needed; the Rough Mill /
Prepolish / Polish tabs get a "Confirm done (s)" box, default 20 s (CLI default stays 30 s).
The runner now logs how long a false "done" lasted, so the window can be chosen from the
instrument Log. All offline: no microscope.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout

from ..runner import PatternRunner


class _QuietRunner(PatternRunner):
    """PatternRunner without a microscope: status reads return None."""

    def __init__(self):
        super().__init__(sem=None)

    def _print_wait_status(self, *a, **k):  # noqa: D401 - no microscope here
        pass

    def _get_fib_mode_code(self):
        return None

    def _get_fib_blanked(self):
        return None


def _confirm(is_done_fn, confirm_s):
    import time
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = _QuietRunner()._confirm_done_stable(
            t0=time.time(), timeout_s=0, confirm_s=confirm_s, poll_s=0.01,
            status_every_s=1e9, is_done_fn=is_done_fn)
    return ok, buf.getvalue()


def test_confirm_passes_when_done_stays_stable():
    ok, out = _confirm(lambda: True, 0.05)
    assert ok is True
    assert "confirming for" in out and "Completion confirmed." in out


def test_confirm_logs_how_long_a_false_done_lasted():
    reads = iter([True, True, True, False])
    ok, out = _confirm(lambda: next(reads), 5.0)
    assert ok is False
    line = [s for s in out.splitlines() if "not stable" in s]
    assert line, out
    assert "Completion not stable after " in line[0] and " s (window 5 s)" in line[0], line[0]


def test_confirm_zero_window_returns_at_once():
    ok, out = _confirm(lambda: False, 0)
    assert ok is True and out == ""


def test_cli_default_stays_30_and_accepts_20():
    from ..cli import build_parser
    p = build_parser()
    base = ["mill-positions", "p.ely", "s1", "--positions-file", "p.json"]
    assert p.parse_args(base).done_confirm_s == 30.0
    assert p.parse_args(base + ["--done-confirm-s", "20"]).done_confirm_s == 20.0


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
