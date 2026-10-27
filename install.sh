#!/bin/sh
# Installs ClearX (formerly memguard): per-user, no root. Safe to re-run; removes the old memguard names if present.
#   ./install.sh               daemon LaunchAgent + CLI (clearx, alias memguard) + ClearX.app in ~/Applications
#   ./install.sh --login-item  also start ClearX.app at login (LaunchAgent com.user.clearx.menubar)
set -e
D=~/Developer/MacMemoryManager
U=$(id -u)
mkdir -p "$D/logs" "$D/state" ~/.local/bin ~/Library/LaunchAgents ~/Applications
# old names (memguard v1-v3): stop and remove, so only one daemon and one menu app ever run
for L in com.user.memguard com.user.memguard.menubar; do
  launchctl bootout gui/$U/$L 2>/dev/null || true; rm -f ~/Library/LaunchAgents/$L.plist
done
osascript -e 'quit app "MemGuard"' 2>/dev/null || true; pkill -x MemGuard 2>/dev/null || true
rm -rf ~/Applications/MemGuard.app
chmod +x "$D/clearx" "$D/memguard" "$D/memguard.py"
ln -sf "$D/clearx" ~/.local/bin/clearx
ln -sf "$D/clearx" ~/.local/bin/memguard          # old name, kept as an alias
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
launchctl list | grep -E "clearx|memguard"
