#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_DIR="$ROOT_DIR/build/safari-native-tests"
mkdir -p "$BUILD_DIR"
export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
xcrun swiftc -swift-version 5 \
  -module-cache-path "$BUILD_DIR/module-cache" \
  "$ROOT_DIR/safari-extension/native/Kleinanzeigen Safari/Shared/BridgeConfiguration.swift" \
  "$ROOT_DIR/safari-extension/native/tests/main.swift" \
  -o "$BUILD_DIR/bridge-validation"
"$BUILD_DIR/bridge-validation"
xcrun swiftc -swift-version 5 \
  -module-cache-path "$BUILD_DIR/module-cache" \
  "$ROOT_DIR/safari-extension/native/Kleinanzeigen Safari/Shared/BridgeConfiguration.swift" \
  "$ROOT_DIR/safari-extension/native/Kleinanzeigen Safari/Kleinanzeigen Safari Extension/BridgeExchange.swift" \
  "$ROOT_DIR/safari-extension/native/tests/ExchangeMain.swift" \
  -o "$BUILD_DIR/bridge-exchange-test"
"$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/safari-extension/native/tests/test_exchange.py" "$BUILD_DIR/bridge-exchange-test"
