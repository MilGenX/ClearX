#!/bin/sh
# Installs memguard (per-user, no root). Safe to re-run.
#   ./install.sh               daemon LaunchAgent + CLI + MemGuard.app in ~/Applications
#   ./install.sh --login-item  also start MemGuard.app at login (LaunchAgent com.user.memguard.menubar)
set -e
D=~/Developer/MacMemoryManager
mkdir -p "$D/logs" "$D/state" ~/.local/bin ~/Library/LaunchAgents ~/Applications
chmod +x "$D/memguard" "$D/memguard.py"
ln -sf "$D/memguard" ~/.local/bin/memguard
cp "$D/com.user.memguard.plist" ~/Library/LaunchAgents/com.user.memguard.plist
launchctl bootout gui/$(id -u)/com.user.memguard 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.memguard.plist
[ -d "$D/app/build/MemGuard.app" ] || "$D/app/build.sh"
rm -rf ~/Applications/MemGuard.app && cp -R "$D/app/build/MemGuard.app" ~/Applications/
if [ "$1" = "--login-item" ]; then
  cp "$D/com.user.memguard.menubar.plist" ~/Library/LaunchAgents/
  launchctl bootout gui/$(id -u)/com.user.memguard.menubar 2>/dev/null || true
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.memguard.menubar.plist
fi
open ~/Applications/MemGuard.app
launchctl list | grep memguard
