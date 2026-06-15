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


def _binary_dep_mtime(root):
    """Mtime of files that require a full rebuild when changed (affect the binary)."""
    latest = 0.0
    for rel in ("setup_app.py", "pyproject.toml"):
        path = root / rel
        if path.is_file():
            latest = max(latest, path.stat().st_mtime)
    return latest


_SYNCABLE = frozenset({".py", ".applescript", ".html", ".css", ".js"})


def _python_source_mtime(root):
    """Mtime of source files that can be synced into the bundle without rebuilding."""
    latest = 0.0
    pkg = root / "mac_agent"
    if pkg.is_dir():
        for path in pkg.rglob("*"):
            if path.is_file() and path.suffix in _SYNCABLE:
                latest = max(latest, path.stat().st_mtime)
    return latest


def _bundle_lib_dir(app):
    """Return the lib/pythonX.Y directory inside the app bundle, or None."""
    lib = app / "Contents" / "Resources" / "lib"
    if not lib.is_dir():
        return None
    for candidate in sorted(lib.iterdir(), reverse=True):
        if candidate.is_dir() and candidate.name.startswith("python"):
            return candidate
    return None


def _sync_python_files(root, app):
    """Copy changed Python source into the bundle and patch python3XX.zip."""
    import py_compile
    import shutil
    import tempfile
    import zipfile

    lib = _bundle_lib_dir(app)
    if lib is None:
        raise SystemExit("Cannot find lib/pythonX.Y in app bundle — run a full rebuild.")

    src_pkg = root / "mac_agent"
    dst_pkg = lib / "mac_agent"

    # Collect changed .py files.
    changed_py: list[tuple[Path, Path]] = []  # (src, dst_in_bundle)
    for src in src_pkg.rglob("*"):
        if not src.is_file() or src.suffix not in _SYNCABLE:
            continue
        rel = src.relative_to(src_pkg)
        dst = dst_pkg / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
            shutil.copy2(src, dst)
            if src.suffix == ".py":
                changed_py.append((src, dst))

    # Stale bytecode in loose dirs would shadow new source — drop it.
    for cache in dst_pkg.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)

    # py2app loads .pyc from pythonXYZ.zip, not the loose .py files.
    # Recompile changed files and patch them into the zip.
    zip_path = lib.parent / (lib.name.replace(".", "") + ".zip")  # e.g. python314.zip
    if changed_py and zip_path.is_file():
        _patch_zip(zip_path, src_pkg, changed_py)

    total = len(changed_py) + sum(
        1 for src in src_pkg.rglob("*")
        if src.is_file() and src.suffix in _SYNCABLE and src.suffix != ".py"
        and (dst_pkg / src.relative_to(src_pkg)).exists()
    )
    print(f"Synced {len(changed_py)} Python file(s) into bundle zip  (no rebuild needed)")


def _patch_zip(zip_path: Path, src_pkg: Path, changed: list[tuple[Path, Path]]) -> None:
    """Replace .pyc entries in py2app's zip for the given source files."""
    import py_compile
    import tempfile
    import zipfile

    # Compile each changed .py to a temp .pyc.
    replacements: dict[str, Path] = {}  # zip entry name → temp pyc path
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        for src, _dst in changed:
            rel = src.relative_to(src_pkg)
            zip_name = f"mac_agent/{rel.with_suffix('.pyc').as_posix()}"
            pyc = tmp_dir / rel.with_suffix(".pyc")
            pyc.parent.mkdir(parents=True, exist_ok=True)
            try:
                py_compile.compile(str(src), cfile=str(pyc), optimize=0, doraise=True)
                replacements[zip_name] = pyc
            except py_compile.PyCompileError as e:
                print(f"  [sync] compile error in {src.name}: {e}")

        if not replacements:
            return

        # Rebuild zip with replacements (zipfile has no in-place replace).
        backup = zip_path.with_suffix(".zip.bak")
        import shutil
        shutil.copy2(zip_path, backup)
        with zipfile.ZipFile(backup, "r") as old_zf:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as new_zf:
                written: set[str] = set()
                for info in old_zf.infolist():
                    if info.filename in replacements:
                        new_zf.write(str(replacements[info.filename]), info.filename)
                    else:
                        new_zf.writestr(info, old_zf.read(info.filename))
                    written.add(info.filename)
                # Add brand-new .pyc entries that don't exist in the zip yet.
                for zip_name, pyc_path in replacements.items():
                    if zip_name not in written:
                        new_zf.write(str(pyc_path), zip_name)
        backup.unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _app_is_stale(root, app):
    if not app.is_dir():
        return True
    bundle_mtime = _bundle_mtime(app)
    return (
        _binary_dep_mtime(root) > bundle_mtime
        or _python_source_mtime(root) > bundle_mtime
    )


def ensure_app(root):
    app = root / "dist" / "Daimon.app"
    build = root / "scripts" / "build_app.sh"
    if not build.is_file():
        raise SystemExit(f"No {app} and no {build}")

    if not app.is_dir():
        print(f"Building {app.name} (missing)…")
        subprocess.run([str(build)], cwd=root, check=True)
        if not app.is_dir():
            raise SystemExit(f"Build finished but {app} is missing")
        return app

    bundle_mtime = _bundle_mtime(app)

    if _binary_dep_mtime(root) > bundle_mtime:
        # pyproject.toml or setup_app.py changed — binary deps may differ, full rebuild.
        print(f"Building {app.name} (dependencies changed)…")
        subprocess.run([str(build)], cwd=root, check=True)
        if not app.is_dir():
            raise SystemExit(f"Build finished but {app} is missing")
    elif _python_source_mtime(root) > bundle_mtime:
        # Only .py/.applescript changed — sync into bundle, preserve signature + TCC grant.
        _sync_python_files(root, app)

    return app


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print("Usage: daimon start | daimon serve | daimon install")
        return 0 if not args else 0
    if args[0] == "install":
        return install_cli()
    if args[0] == "serve":
        from mac_agent.ollama_serve import ensure_serving

        ensure_serving(verbose=True)
        print("Ollama is running at http://localhost:11434")
        return 0
    if args[0] != "start":
        print("Usage: daimon start | daimon serve | daimon install")
        return 1
    root = repo_root()
    kill_stale()
    from mac_agent import settings
    from mac_agent.ollama_serve import ensure_serving

    if settings.get_backend() == "ollama":
        try:
            ensure_serving(verbose=True)
        except RuntimeError as e:
            print(f"  [ollama] {e}")
    app = ensure_app(root)
    subprocess.run(["open", str(app)], check=True)
    print(f"Launched {app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
