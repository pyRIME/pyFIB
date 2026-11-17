"""Headless smoke tests for the Qt GUI (skip cleanly without Qt)."""

import os
import tempfile

# v0.55.0: pyMap v0.9.2 keeps a diagnostics log (default <pyFIB>/logs) once its widget
# is built, and pyFIB's tests build it. pyMap asked (its notes, revision 3) for
# PYMAP_DIAG=0 or PYMAP_LOG_DIR -> a temporary folder. The temporary folder is used:
# PYMAP_DIAG=0 is process-wide and switched pyMap's own diagnostics tests off when the
# whole tree runs in one pytest process. Same rule as pyMap's own conftest.
os.environ.setdefault("PYMAP_LOG_DIR", tempfile.mkdtemp(prefix="pyfib_test_pymap_logs_"))
