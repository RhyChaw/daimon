"""Resolve bundled resource paths (AppleScripts) in dev and in Daimon.app."""

import os
import sys


def _frozen():
    return bool(getattr(sys, "frozen", False))


def _bundle_resources_dir():
    resource = os.environ.get("RESOURCEPATH")
    if resource and os.path.isdir(resource):
        return resource
    exe = getattr(sys, "executable", "")
    if exe.endswith("/MacOS/Daimon") or exe.endswith("/MacOS/python"):
        return os.path.join(os.path.dirname(os.path.dirname(exe)), "Resources")
    return None


def scripts_dir():
    """Directory containing *.applescript files."""
    if _frozen():
        resource = _bundle_resources_dir()
        if resource:
            lib = os.path.join(resource, "lib")
            if os.path.isdir(lib):
                for name in sorted(os.listdir(lib)):
                    if not name.startswith("python"):
                        continue
                    candidate = os.path.join(lib, name, "mac_agent", "scripts")
                    if os.path.isdir(candidate):
                        return candidate
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")


def script_path(filename):
    path = os.path.join(scripts_dir(), filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"script not found: {path}")
    return path


def web_dir():
    """Directory containing web/index.html (works in dev and in the frozen bundle)."""
    if _frozen():
        resource = _bundle_resources_dir()
        if resource:
            lib = os.path.join(resource, "lib")
            if os.path.isdir(lib):
                for name in sorted(os.listdir(lib)):
                    if not name.startswith("python"):
                        continue
                    candidate = os.path.join(lib, name, "mac_agent", "web")
                    if os.path.isdir(candidate):
                        return candidate
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
