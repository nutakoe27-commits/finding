#!/usr/bin/env python3
"""Standalone browser collector: Python 3.10+ and requirements-capture.txt only."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matchlab.capture import main

if __name__ == "__main__":
    main()
