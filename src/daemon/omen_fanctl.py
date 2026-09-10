#!/usr/bin/env python3
"""Compatibility entry point for the packaged fan-control daemon."""

import sys

from omen_fanctl.cli import main


if __name__ == "__main__":
    sys.exit(main())
