"""Test-wide settings: pyMap's diagnostics log goes to a temporary folder, not to
<repo>/logs (pymap.diag writes there by default)."""

import os
import tempfile

os.environ.setdefault("PYMAP_LOG_DIR", tempfile.mkdtemp(prefix="pymap_test_logs_"))
