#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

ROOT="$(pwd)"
VENV="${ROOT}/.venv"

if [[ ! -x "${VENV}/bin/python" ]]; then
  echo "Missing .venv — run: python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[app]'"
  exit 1
fi

echo "Building Daimon.app …"
"${VENV}/bin/pip" install -q -e ".[app]"
rm -rf build dist
"${VENV}/bin/python" setup_app.py py2app

APP="dist/Daimon.app"
if [[ ! -d "${APP}" ]]; then
  echo "Build failed — dist/Daimon.app not found."
  exit 1
fi

# py2app's package-data copier silently skips unknown binary extensions (e.g.
# .glb model files), so sync the full web/ dir into the bundle to guarantee parity.
echo "Syncing web/ assets into bundle …"
BUNDLE_WEB="$(find "${APP}/Contents/Resources/lib" -type d -path '*/mac_agent/web' | head -1)"
if [[ -n "${BUNDLE_WEB}" ]]; then
  rsync -a --delete "${ROOT}/mac_agent/web/" "${BUNDLE_WEB}/"
  echo "  → ${BUNDLE_WEB}"
else
  echo "  WARNING: could not locate mac_agent/web inside the bundle"
fi

echo "Clearing quarantine (so Finder can open the app) …"
xattr -cr "${APP}" 2>/dev/null || true

# Sign with Apple Development cert — stable across rebuilds, so TCC grants persist.
CERT_NAME="Apple Development: Shrivas Mangalampalli (3BS39AS9X8)"
echo "Signing with Apple Development certificate …"
codesign --force --deep --sign "${CERT_NAME}" "${APP}"
codesign --verify --deep "${APP}"

echo
echo "Bundle id: com.rhychaw.daimon"
echo "Built: ${APP}"
echo
echo "Launch:"
echo "  open \"${APP}\""
echo
echo "If macOS blocks the first launch: right-click Daimon.app → Open → Open."
echo "Or run directly:  dist/Daimon.app/Contents/MacOS/Daimon"
echo
echo "Quit old copies first:  pkill -f 'Daimon.app/Contents/MacOS/Daimon'"
