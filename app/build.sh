#!/bin/sh
# Builds ClearX.app (no Xcode project needed) and ad-hoc signs it.
set -e
cd "$(dirname "$0")"
APP=build/ClearX.app
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS"
xcrun swiftc -O -parse-as-library -target x86_64-apple-macos13.0 -o "$APP/Contents/MacOS/ClearX" Sources/*.swift
cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.user.clearx</string>
  <key>CFBundleName</key><string>ClearX</string>
  <key>CFBundleExecutable</key><string>ClearX</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>3.1</string>
  <key>CFBundleDisplayName</key><string>ClearX</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PL
codesign --force -s - "$APP"
echo "built $APP"
