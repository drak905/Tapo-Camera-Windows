"""``python -m tapo_desktop [--selftest]``."""
from __future__ import annotations

import sys

from tapo_desktop.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
