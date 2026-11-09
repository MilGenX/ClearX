#!/bin/bash
# Builds clearx-gpu and installs the governor as a boot-time daemon.
# Usage: ./install.sh [max|balanced|quiet]     Uninstall: ./install.sh --uninstall
set -euo pipefail
PROFILE="${1:-balanced}"
BIN=/usr/local/bin/clearx-gpu
PLIST=/Library/LaunchDaemons/com.clearx.gpu.plist

if [[ "$PROFILE" == "--uninstall" ]]; then
  sudo launchctl bootout system "$PLIST" 2>/dev/null || true
  sudo "$BIN" restore || true
  sudo rm -f "$PLIST" "$BIN"
  echo "Removed. Fans back to stock."; exit 0
fi

swift build -c release
sudo mkdir -p /usr/local/bin
sudo cp .build/release/ClearXGPU "$BIN"
sudo tee "$PLIST" >/dev/null <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.clearx.gpu</string>
  <key>ProgramArguments</key><array><string>$BIN</string><string>govern</string><string>$PROFILE</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/var/log/clearx-gpu.log</string>
  <key>StandardErrorPath</key><string>/var/log/clearx-gpu.log</string>
</dict></plist>
EOF
sudo launchctl bootout system "$PLIST" 2>/dev/null || true
sudo launchctl bootstrap system "$PLIST"
echo "Installed. Governor running ($PROFILE). Check: clearx-gpu watch"
