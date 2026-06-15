#!/usr/bin/env bash
# Create a stable self-signed code-signing certificate for Daimon dev.
# Run this once. After that, build_app.sh uses it and TCC grants persist
# across rebuilds because the requirement is cert-based, not hash-based.
set -euo pipefail

CERT_NAME="Daimon Dev"
KEYCHAIN="${HOME}/Library/Keychains/login.keychain-db"
TMPDIR="$(mktemp -d)"
trap "rm -rf '${TMPDIR}'" EXIT

echo "Creating certificate: '${CERT_NAME}' ..."

# Generate key + self-signed cert with code-signing EKU (10 years).
# -addext works on both OpenSSL 3 and LibreSSL 3.3+.
openssl req -x509 -newkey rsa:2048 \
  -keyout "${TMPDIR}/key.pem" \
  -out    "${TMPDIR}/cert.pem" \
  -days 3650 -nodes \
  -subj "/CN=${CERT_NAME}" \
  -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=codeSigning" 2>/dev/null

# Bundle as PKCS12 (no password). Try without -legacy first; fall back for OpenSSL 3.
if openssl pkcs12 -export \
    -out "${TMPDIR}/daimon.p12" \
    -inkey "${TMPDIR}/key.pem" \
    -in    "${TMPDIR}/cert.pem" \
    -passout pass: 2>/dev/null; then
  : # ok
else
  openssl pkcs12 -legacy -export \
    -out "${TMPDIR}/daimon.p12" \
    -inkey "${TMPDIR}/key.pem" \
    -in    "${TMPDIR}/cert.pem" \
    -passout pass:
fi

# Import into login keychain and allow codesign to use it without per-use prompts.
security import "${TMPDIR}/daimon.p12" \
  -k "${KEYCHAIN}" \
  -P "" \
  -T /usr/bin/codesign \
  -A

# Allow codesign access without a password prompt for this key.
security set-key-partition-list \
  -S "apple-tool:,apple:,codesign:" \
  -k "" \
  "${KEYCHAIN}" 2>/dev/null || true

echo
echo "Certificate '${CERT_NAME}' is ready."
echo "Now run:  ./scripts/build_app.sh"
echo
echo "After the build, grant Accessibility once in:"
echo "  System Settings → Privacy & Security → Accessibility"
echo "It will persist across all future rebuilds."
