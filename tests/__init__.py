"""Shared test-package bootstrap and repository paths."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "src" / "config" / "omen-fanctl.toml"
DAEMON_PATH = PROJECT_ROOT / "src" / "daemon"

if str(DAEMON_PATH) not in sys.path:
    sys.path.insert(0, str(DAEMON_PATH))
