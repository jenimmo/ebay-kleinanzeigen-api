#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-run}"
if [[ "$MODE" == "--update-extension" ]]; then
  shift
  exec "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/script/update_safari_extension.py" "$@"
fi
APP_NAME="Kleinanzeigen Safari"
PROJECT="$ROOT_DIR/safari-extension/native/$APP_NAME/$APP_NAME.xcodeproj"
BUILD_DIR="$ROOT_DIR/build/safari"
# File-provider metadata on .app/.appex directories can break code signing.
# Keep Xcode intermediates in a private local cache; publish the signed app below.
BUILD_CACHE="/private/tmp/kleinanzeigen-safari-build-${UID}"
mkdir -p "$BUILD_CACHE"
chmod 700 "$BUILD_CACHE"
APP_BUNDLE="$BUILD_CACHE/Build/Products/Debug/$APP_NAME.app"
APP_ARCHIVE="$BUILD_DIR/$APP_NAME.zip"
INSTALL_BUNDLE="$HOME/Applications/$APP_NAME.app"
export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"

case "$MODE" in
  run|--build|--install|--cleanup-cache|--debug|--logs|--telemetry|--verify) ;;
  *) echo "usage: $0 [run|--build|--install|--cleanup-cache|--debug|--logs|--telemetry|--verify|--update-extension --version X.Y.Z --build N]" >&2; exit 2 ;;
esac

if [[ "$MODE" == "--cleanup-cache" ]]; then
  "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/script/install_safari_app.py" \
    --cleanup-only "$APP_BUNDLE" "$INSTALL_BUNDLE" "$APP_ARCHIVE"
  echo "Removed verified generated cache bundle: $APP_BUNDLE"
  exit 0
fi

case "$MODE" in
  run|--debug|--logs|--telemetry|--verify)
    pkill -x "$APP_NAME" >/dev/null 2>&1 || true
    ;;
esac

# Use the existing Apple Development identities. Xcode automatic provisioning
# requires the certificate class rather than a manually pinned fingerprint.
# Never change Safari's unsigned-extension setting as part of a build.
# Fail before provisioning when no usable existing development key is present.
if ! /usr/bin/security find-identity -v -p codesigning | /usr/bin/grep -q '"Apple Development:'; then
  echo 'An existing valid Apple Development identity is required; no certificate will be created.' >&2
  exit 1
fi
xcodebuild -quiet -allowProvisioningUpdates -project "$PROJECT" -scheme "$APP_NAME" \
  -configuration Debug -destination 'platform=macOS,arch=arm64' -derivedDataPath "$BUILD_CACHE" \
  "CODE_SIGN_IDENTITY=${SAFARI_CODE_SIGN_IDENTITY:-Apple Development}" \
  CODE_SIGN_INJECT_BASE_ENTITLEMENTS=NO \
  build
codesign --verify --deep --strict "$APP_BUNDLE"
mkdir -p "$BUILD_DIR"
# A zip preserves the signed bundle in synchronized project folders without
# FileProvider attaching forbidden FinderInfo to its .app/.appex directories.
/usr/bin/ditto -c -k --norsrc --noextattr --keepParent "$APP_BUNDLE" "$APP_ARCHIVE"

if [[ "$MODE" != "--build" ]]; then
  # Verify a complete staged copy, then atomically swap directories on the same
  # filesystem. Never mutate an active bundle's files or cycle its registration.
  "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/script/install_safari_app.py" \
    "$APP_BUNDLE" "$INSTALL_BUNDLE" "$APP_ARCHIVE"
  APP_BUNDLE="$INSTALL_BUNDLE"
fi

case "$MODE" in
  --build) echo "Built: $APP_BUNDLE"; echo "Archive: $APP_ARCHIVE" ;;
  --install) echo "Installed: $APP_BUNDLE" ;;
  --debug) lldb -- "$APP_BUNDLE/Contents/MacOS/$APP_NAME" ;;
  --logs|--telemetry)
    /usr/bin/open -n "$APP_BUNDLE"
    /usr/bin/log stream --info --style compact --predicate 'process == "Kleinanzeigen Safari"'
    ;;
  --verify)
    /usr/bin/open -n "$APP_BUNDLE"
    sleep 1
    pgrep -x "$APP_NAME" >/dev/null
    ;;
  run) /usr/bin/open -n "$APP_BUNDLE" ;;
esac
