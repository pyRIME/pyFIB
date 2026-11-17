"""One SmartSEM client at a time (v0.53.3), offline.

Evan 2026-10-12: Rough Mill Start crashed in the ZEISS API ("Failed to create an instance
of Managed ... unable to open port for incoming connections", exit -1073741819) while
v0.39.0 worked; a hung python.exe titled "Remoting Forwarding Window" was seen. pyFIB now
waits for any other process holding a SmartSEM connection and refuses cleanly.
"""

from __future__ import annotations

from types import SimpleNamespace

from ..smartsem_com import SmartSEMError, other_api_clients, wait_for_other_api_clients

CSV_PY = (
    '"python.exe","24264","Console","1","45,804 K","Not Responding","XB550-0328\\\\sem",'
    '"0:00:00","Remoting Forwarding Window"\n'
    '"python.exe","7280","Console","1","10,176 K","Unknown","XB550-0328\\\\sem","0:00:00","N/A"\n'
    '"python.exe","5000","Console","1","90,000 K","Running","XB550-0328\\\\sem","0:00:03",'
    '"pyFIB v0.53.3"\n')
CSV_NONE = "INFO: No tasks are running which match the specified criteria.\n"


def _run(table):
    def run(argv, **kw):
        image = argv[-1].split()[-1]
        return SimpleNamespace(stdout=table.get(image, CSV_NONE))
    return run


def test_finds_only_other_processes_holding_a_connection():
    got = other_api_clients(_run=_run({"python.exe": CSV_PY}), _is_windows=True,
                            _self_pids={7280})
    assert got == [{"pid": 24264, "image": "python.exe", "status": "Not Responding",
                    "title": "Remoting Forwarding Window"}]
    # this process / its parent never count; nothing off Windows; tasklist failing -> []
    assert other_api_clients(_run=_run({"python.exe": CSV_PY}), _is_windows=True,
                             _self_pids={24264}) == []
    assert other_api_clients(_run=_run({"python.exe": CSV_PY}), _is_windows=False) == []

    def boom(*a, **k):
        raise OSError("no tasklist")
    assert other_api_clients(_run=boom, _is_windows=True, _self_pids=set()) == []
    pyw = CSV_PY.replace('"python.exe","24264"', '"pythonw.exe","24264"')
    got = other_api_clients(_run=_run({"pythonw.exe": pyw}), _is_windows=True, _self_pids=set())
    assert [g["pid"] for g in got] == [24264]


def test_waits_for_the_other_client_then_connects():
    seen = iter([[{"pid": 1, "image": "python.exe", "status": "Running", "title": "x"}],
                 [{"pid": 1, "image": "python.exe", "status": "Running", "title": "x"}], []])
    t = [0.0]
    lines = []
    wait_for_other_api_clients(20, _find=lambda: next(seen), _sleep=lambda s: t.__setitem__(0, t[0] + s),
                               _clock=lambda: t[0], log=lines.append)
    assert t[0] == 2.0 and len(lines) == 1 and "waiting" in lines[0]


def test_refuses_cleanly_naming_the_stuck_process():
    stuck = [{"pid": 24264, "image": "python.exe", "status": "Not Responding", "title": "x"}]
    t = [0.0]
    try:
        wait_for_other_api_clients(20, _find=lambda: stuck,
                                   _sleep=lambda s: t.__setitem__(0, t[0] + s),
                                   _clock=lambda: t[0], log=lambda m: None)
    except SmartSEMError as e:
        msg = str(e)
    else:
        raise AssertionError("not refused")
    assert "PID 24264 (Not Responding)" in msg and "Nothing was sent" in msg and t[0] >= 20


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
