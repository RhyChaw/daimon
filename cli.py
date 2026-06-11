"""
cli.py — the terminal entry point. `macagent start` launches the loop.
"""

import argparse

from .agent import repl
from .ollama_client import ping


def _check_ollama():
    if ping():
        return True
    print("Could not reach Ollama at http://localhost:11434")
    print("  1. install Ollama:  https://ollama.com")
    print("  2. pull a model:    ollama pull llama3.2")
    print("  3. make sure it's running, then try again.")
    return False


def main():
    parser = argparse.ArgumentParser(prog="macagent")
    parser.add_argument("cmd", nargs="?", default="start", choices=["start"])
    parser.parse_args()
    if _check_ollama():
        repl()


if __name__ == "__main__":
    main()
