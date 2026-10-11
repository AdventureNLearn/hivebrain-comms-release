#!/usr/bin/env python3
"""Hub-interpreter launcher for the PC scrub check.

    <hub python> -m tools.scrub_e2e
"""

from pathlib import Path
import runpy
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
runpy.run_path(str(HERE / "scrub_e2e.py"), run_name="__main__")
