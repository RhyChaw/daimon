"""
launcher.py — `daimon start`: kill stale runs, find the app from repo root, launch.
"""

import os
import subprocess
import sys
import time
from pathlib import Path


def repo_root():
    return Path(__file__).resolve().parent.parent


def kill_stale():
    for pattern in (
        "Daimon.app/Contents/MacOS/Daimon",
        "macagent start",
    ):
        subprocess.run(
            ["pkill", "-f", pattern],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    time.sleep(0.3)


def install_cli():
    root = repo_root()
    script = root / "scripts" / "daimon"
    if not script.is_file():
        raise SystemExit(f"Missing launcher script: {script}")
    dest_dir = Path.home() / ".local" / "bin"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "daimon"
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    dest.symlink_to(script)
    print(f"Installed: {dest} -> {script}")
    path = os.environ.get("PATH", "")
    if str(dest_dir) not in path.split(":"):
        print(f"Add to ~/.zshrc:  export PATH=\"$HOME/.local/bin:$PATH\"")
    print("Then run:  daimon start")
    return 0


def _bundle_mtime(app):
    binary = app / "Contents" / "MacOS" / "Daimon"
    if binary.is_file():
        return binary.stat().st_mtime
    if app.is_dir():
        return app.stat().st_mtime
    return 0


def _source_mtime(root):
    latest = 0.0
    for rel in ("setup_app.py", "pyproject.toml"):
        path = root / rel
        if path.is_file():
            latest = max(latest, path.stat().st_mtime)
    pkg = root / "mac_agent"
    if pkg.is_dir():
        for path in pkg.rglob("*.py"):
            if path.is_file():
                latest = max(latest, path.stat().st_mtime)
    return latest


def _app_is_stale(root, app):
    if not app.is_dir():
        return True
    return _source_mtime(root) > _bundle_mtime(app)


def ensure_app(root):
    app = root / "dist" / "Daimon.app"
    build = root / "scripts" / "build_app.sh"
    if not build.is_file():
        raise SystemExit(f"No {app} and no {build}")
    if _app_is_stale(root, app):
        reason = "missing" if not app.is_dir() else "source changed"
        print(f"Building {app.name} ({reason})…")
        subprocess.run([str(build)], cwd=root, check=True)
        if not app.is_dir():
            raise SystemExit(f"Build finished but {app} is missing")
    return app


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print("Usage: daimon start | daimon install")
        return 0 if not args else 0
    if args[0] == "install":
        return install_cli()
    if args[0] != "start":
        print("Usage: daimon start | daimon install")
        return 1
    root = repo_root()
    kill_stale()
    app = ensure_app(root)
    subprocess.run(["open", str(app)], check=True)
    print(f"Launched {app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
