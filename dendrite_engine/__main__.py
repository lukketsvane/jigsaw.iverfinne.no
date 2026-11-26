"""Module entry point for `python -m dendrite_engine`."""

import sys
from .cli import main

if __name__ == "__main__":
    sys.exit(main())
