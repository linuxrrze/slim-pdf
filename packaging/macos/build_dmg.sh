#!/bin/bash
# Build dist/Slim-PDF-<version>-macos-<arch>.dmg from dist/Slim PDF.app.
#
# Signing is optional. Without MACOS_SIGN_IDENTITY the app is signed ad hoc
# (it runs, but Gatekeeper asks on first start). With MACOS_SIGN_IDENTITY
# (a "Developer ID Application" identity in the keychain) the app is signed
# with the hardened runtime, and with APPLE_ID, APPLE_TEAM_ID and
# APPLE_APP_PASSWORD the DMG is also notarized and stapled.
set -euo pipefail
cd "$(dirname "$0")/../.."

VERSION="${1:?usage: build_dmg.sh VERSION}"
APP="dist/Slim PDF.app"
ARCH="$(uname -m)"
DMG="dist/Slim-PDF-${VERSION}-macos-${ARCH}.dmg"

# --deep is deprecated by Apple for signing (signing each nested binary
# inside-out is preferred), but it still produces a valid, notarizable
# signature for a PyInstaller bundle.
if [ -n "${MACOS_SIGN_IDENTITY:-}" ]; then
  codesign --force --deep --options runtime --timestamp \
    --entitlements packaging/macos/entitlements.plist \
    --sign "$MACOS_SIGN_IDENTITY" "$APP"
else
  codesign --force --deep --sign - "$APP"
fi
codesign --verify --deep --strict "$APP"

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
ditto "$APP" "$staging/Slim PDF.app"  # keeps signatures, symlinks and metadata
ln -s /Applications "$staging/Applications"
rm -f "$DMG"
hdiutil create -volname "Slim PDF" -srcfolder "$staging" -ov -format UDZO "$DMG"

if [ -n "${MACOS_SIGN_IDENTITY:-}" ] && [ -n "${APPLE_ID:-}" ]; then
  codesign --sign "$MACOS_SIGN_IDENTITY" --timestamp "$DMG"
  xcrun notarytool submit "$DMG" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_PASSWORD" --wait
  xcrun stapler staple "$DMG"
fi
echo "built $DMG"
