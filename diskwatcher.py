#!/usr/bin/env python3
"""mu2edaq-diskwatcher entry point.

The application itself lives in ``src/mu2edaq_diskwatcher/``.  This shim exists
so the control-room start script can keep invoking ``python diskwatcher.py``.

``src`` is inserted at the *front* of ``sys.path``: a plain ``PYTHONPATH=./src``
is not enough, because the directory holding this script always precedes
``PYTHONPATH`` and would shadow the package.
"""

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from mu2edaq_diskwatcher.cli import main

if __name__ == "__main__":
    main()
