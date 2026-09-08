"""Run the daemon with ``python -m hp_fan_control``."""

import sys

from .cli import main


if __name__ == "__main__":
    sys.exit(main())
