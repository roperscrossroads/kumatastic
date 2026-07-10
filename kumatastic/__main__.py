"""Module entry point so `python -m kumatastic` runs the CLI.

Used by the container image, whose distroless base has no shell or console
scripts on PATH — the entrypoint invokes `python3.13 -m kumatastic`.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
