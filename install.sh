#!/bin/sh
# Installs ClearX: per-user, no root. Safe to re-run.
#   ./install.sh               daemon LaunchAgent + CLI (clearx) + ClearX.app in ~/Applications
#   ./install.sh --login-item  also start ClearX.app at login (LaunchAgent com.user.clearx.menubar)
# Works from wherever you cloned it. Needs macOS 13+ and the Xcode Command Line Tools (xcode-select --install).
set -e
D=$(cd "$(dirname "$0")" && pwd)
[ -x /usr/bin/python3 ] && /usr/bin/python3 -c 1 2>/dev/null || { echo "Install the Command Line Tools first: xcode-select --install"; exit 1; }
U=$(id -u)
mkdir -p "$D/logs" "$D/state" ~/.local/bin ~/Library/LaunchAgents ~/Applications
chmod +x "$D/bin/clearx" "$D/src/clearx.py"
ln -sf "$D/bin/clearx" ~/.local/bin/clearx
sed "s|__CLEARX__|$D|g" "$D/config/launchd/com.user.clearx.plist" > ~/Library/LaunchAgents/com.user.clearx.plist
launchctl bootout gui/$U/com.user.clearx 2>/dev/null || true
launchctl bootstrap gui/$U ~/Library/LaunchAgents/com.user.clearx.plist
"$D/app/build.sh"
defaults write com.user.clearx home "$D"   # where ClearX.app finds the daemon's state and the clearx command
osascript -e 'quit app "ClearX"' 2>/dev/null || true; pkill -x ClearX 2>/dev/null || true
rm -rf ~/Applications/ClearX.app && cp -R "$D/app/build/ClearX.app" ~/Applications/
if [ "$1" = "--login-item" ]; then
  sed "s|__HOME__|$HOME|g" "$D/config/launchd/com.user.clearx.menubar.plist" > ~/Library/LaunchAgents/com.user.clearx.menubar.plist
  launchctl bootout gui/$U/com.user.clearx.menubar 2>/dev/null || true
  launchctl bootstrap gui/$U ~/Library/LaunchAgents/com.user.clearx.menubar.plist
fi
open ~/Applications/ClearX.app
launchctl list | grep clearx
