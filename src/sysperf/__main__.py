"""``python -m sysperf`` entry point — see cli.py for the options."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
