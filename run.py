#!/usr/bin/env python3
"""Run session receipts from wherever this folder is installed.

    python3 run.py read today
    python3 run.py serve --open
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from receipts.__main__ import main  # noqa: E402

raise SystemExit(main())
