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
