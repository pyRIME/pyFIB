"""Module entry point: python -m pyfib ..."""

from smartfib_executor.cli import _enable_fault_log, main

if __name__ == "__main__":
    _enable_fault_log()                 # v0.53.3: hard crashes -> logs/pyfib_faults.log
    raise SystemExit(main(prog_name="pyfib"))
