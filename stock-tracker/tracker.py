#!/usr/bin/env python3
"""Stock & price tracker. Run `python tracker.py --help` for commands."""

import sys

from stocktracker.cli import main

if __name__ == "__main__":
    sys.exit(main())
