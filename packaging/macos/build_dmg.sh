#!/bin/bash
# Build dist/PDF-Signer-<version>-macos-<arch>.dmg from dist/PDF Signer.app.
#
# Signing is optional. Without MACOS_SIGN_IDENTITY the app is signed ad hoc
# (it runs, but Gatekeeper asks on first start). With MACOS_SIGN_IDENTITY
# (a "Developer ID Application" identity in the keychain) the app is signed
# with the hardened runtime, and with APPLE_ID, APPLE_TEAM_ID and
# APPLE_APP_PASSWORD the DMG is also notarized and stapled.
set -euo pipefail
cd "$(dirname "$0")/../.."

VERSION="${1:?usage: build_dmg.sh VERSION}"
APP="dist/PDF Signer.app"
ARCH="$(uname -m)"
DMG="dist/PDF-Signer-${VERSION}-macos-${ARCH}.dmg"

if [ -n "${MACOS_SIGN_IDENTITY:-}" ]; then
  codesign --force --deep --options runtime --timestamp \
    --entitlements packaging/macos/entitlements.plist \
    --sign "$MACOS_SIGN_IDENTITY" "$APP"
else
  codesign --force --deep --sign - "$APP"
fi
codesign --verify --deep --strict "$APP"

staging="$(mktemp -d)"
cp -R "$APP" "$staging/"
ln -s /Applications "$staging/Applications"
rm -f "$DMG"
hdiutil create -volname "PDF Signer" -srcfolder "$staging" -ov -format UDZO "$DMG"
rm -rf "$staging"

if [ -n "${MACOS_SIGN_IDENTITY:-}" ] && [ -n "${APPLE_ID:-}" ]; then
  codesign --sign "$MACOS_SIGN_IDENTITY" --timestamp "$DMG"
  xcrun notarytool submit "$DMG" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_PASSWORD" --wait
  xcrun stapler staple "$DMG"
fi
echo "built $DMG"
