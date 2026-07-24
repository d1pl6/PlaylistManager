import sys
import argparse
import logging
from app import App  # type: ignore


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
