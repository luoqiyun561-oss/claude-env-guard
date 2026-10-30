"""Frozen-entry wrapper.

PyInstaller needs a concrete script, so this file exists only to hand the package
to `envguard.__main__.main`. Keeping the real logic inside the package means the
frozen exe and `python -m envguard` run the same code path — a `run.py` that
duplicated the argument parsing would be a second place for the two to disagree.
"""

import sys

from envguard.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
