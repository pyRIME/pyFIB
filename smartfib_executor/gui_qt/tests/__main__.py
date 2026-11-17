from __future__ import annotations
import sys
from . import test_crash_safety, test_embedding, test_smoke
if __name__ == "__main__":
    print("="*60); print("gui_qt smoke test"); print("="*60)
    rc = test_smoke._run_all()
    print("="*60); print("gui_qt embedding test (v0.53.2)"); print("="*60)
    rc = test_embedding._run_all() or rc
    print("="*60); print("gui_qt crash safety test (v0.55.0)"); print("="*60)
    rc = test_crash_safety._run_all() or rc
    sys.exit(rc)
