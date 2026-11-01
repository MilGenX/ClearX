#!/bin/sh
# Builds ClearX.app for this Mac (Intel or Apple silicon; no Xcode project needed) and ad-hoc signs it.
set -e
cd "$(dirname "$0")"
APP=build/ClearX.app
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp ClearX.icns "$APP/Contents/Resources/ClearX.icns"
xcrun swiftc -O -parse-as-library -target "$(uname -m)-apple-macos13.0" -o "$APP/Contents/MacOS/ClearX" Sources/*.swift
cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.user.clearx</string>
  <key>CFBundleName</key><string>ClearX</string>
  <key>CFBundleExecutable</key><string>ClearX</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>3.2</string>
  <key>CFBundleIconFile</key><string>ClearX</string>
  <key>CFBundleDisplayName</key><string>ClearX</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PL
codesign --force -s - "$APP"
echo "built $APP"
