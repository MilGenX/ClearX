#!/bin/sh
# Installs ClearX: per-user, no root. Safe to re-run.
#   ./install.sh               daemon LaunchAgent + CLI (clearx) + ClearX.app in ~/Applications
#   ./install.sh --login-item  also start ClearX.app at login (LaunchAgent com.user.clearx.menubar)
set -e
D=~/Developer/ClearX
U=$(id -u)
mkdir -p "$D/logs" "$D/state" ~/.local/bin ~/Library/LaunchAgents ~/Applications
chmod +x "$D/clearx" "$D/clearx.py"
ln -sf "$D/clearx" ~/.local/bin/clearx
cp "$D/com.user.clearx.plist" ~/Library/LaunchAgents/
launchctl bootout gui/$U/com.user.clearx 2>/dev/null || true
launchctl bootstrap gui/$U ~/Library/LaunchAgents/com.user.clearx.plist
[ -d "$D/app/build/ClearX.app" ] || "$D/app/build.sh"
osascript -e 'quit app "ClearX"' 2>/dev/null || true; pkill -x ClearX 2>/dev/null || true
rm -rf ~/Applications/ClearX.app && cp -R "$D/app/build/ClearX.app" ~/Applications/
if [ "$1" = "--login-item" ]; then
  cp "$D/com.user.clearx.menubar.plist" ~/Library/LaunchAgents/
  launchctl bootout gui/$U/com.user.clearx.menubar 2>/dev/null || true
  launchctl bootstrap gui/$U ~/Library/LaunchAgents/com.user.clearx.menubar.plist
fi
open ~/Applications/ClearX.app
launchctl list | grep clearx
