"""
Entry point (legacy).  ``python app/main.py`` still works, but
``python main.py`` or ``python -m app`` is preferred.

Kept for backward compatibility - will be removed in a future release.
"""

import sys
import argparse
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app import App


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def configure_logging(debug: bool):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s: %(message)s")


def main():
    args = parse_args()
    configure_logging(args.debug)
    app = App(args)
    app.run()
    sys.exit(0)


if __name__ == "__main__":
    main()
