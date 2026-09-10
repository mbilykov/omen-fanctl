"""Run the daemon with ``python -m omen_fanctl``."""

import sys

from .cli import main


if __name__ == "__main__":
    sys.exit(main())
