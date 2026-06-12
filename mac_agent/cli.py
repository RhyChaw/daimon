"""
cli.py — the terminal entry point. `macagent start` launches the loop.
"""

import argparse

from .agent import repl
from .backend import check_backend_ready, create_backend
from .env import load_dotenv


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(prog="macagent")
    parser.add_argument("cmd", nargs="?", default="start", choices=["start"])
    parser.parse_args()
    ok, message = check_backend_ready()
    if not ok:
        print(message)
        return
    repl(backend_getter=create_backend)


if __name__ == "__main__":
    main()
