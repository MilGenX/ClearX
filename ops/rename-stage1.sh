set -u
D=$HOME/Developer; OLD=$D/XDAW; NEW=$D/MyDAW; PL=$HOME/Library/LaunchAgents/com.mydaw.auto-update.plist
fail() { echo "ABORT: $*"; exit 1; }
echo "== preflight"
[ -d "$OLD" ] && [ ! -L "$OLD" ] || fail "$OLD is not a real folder"
[ -e "$NEW" ] && fail "$NEW already exists (overlap)"
[ -f "$PL" ] || fail "no plist"
echo "== stop auto-update schedule"
launchctl bootout gui/$(id -u)/com.mydaw.auto-update 2>&1; sleep 1
pkill -CONT -f mydaw-auto-update.sh 2>/dev/null; sleep 1   # (held in the build queue: let the TERM land)
echo "== move"
mv "$OLD" "$NEW" || fail "mv failed"
ln -s MyDAW "$OLD" || fail "symlink failed"
echo "== repoint auto-update script + plist"
cp "$NEW/mydaw-auto-update.sh" "$NEW/mydaw-auto-update.sh.pre-mydaw-rename"
sed -i '' 's#Developer/XDAW#Developer/MyDAW#g' "$NEW/mydaw-auto-update.sh"
cp "$PL" "$NEW/auto-logs/com.mydaw.auto-update.plist.pre-mydaw-rename"
sed -i '' 's#Developer/XDAW#Developer/MyDAW#g' "$PL"
plutil -lint "$PL" || fail "plist invalid"
rmdir "$NEW/auto-logs/.lock" 2>/dev/null && echo "cleared the stopped run's lock"
echo "== git worktree"
git -C "$NEW/src" worktree repair "$NEW/auto-src" 2>&1
echo "== CMake caches (old path inside): set aside, next build reconfigures"
for c in "$NEW/auto-src/build/CMakeCache.txt" "$NEW/src/build/CMakeCache.txt"; do
  [ -f "$c" ] && grep -q 'Developer/XDAW' "$c" && mv "$c" "$c.pre-mydaw-rename" && echo "set aside $c"
done
echo "== Claude history for the new path"
P=$HOME/.claude/projects; [ -d "$P/-Users-user-Developer-XDAW" ] && [ ! -e "$P/-Users-user-Developer-MyDAW" ] && ln -s -- -Users-user-Developer-XDAW "$P/-Users-user-Developer-MyDAW" && echo linked
echo "== restart schedule"
launchctl bootstrap gui/$(id -u) "$PL" 2>&1
echo "== VERIFY"
ls -ld "$OLD" "$NEW"; readlink "$OLD"
grep -c 'Developer/XDAW' "$NEW/mydaw-auto-update.sh" "$PL"
grep -n 'Developer/MyDAW' "$PL"
launchctl print gui/$(id -u)/com.mydaw.auto-update 2>&1 | grep -E 'state|program|path =' | head -5
git -C "$NEW/auto-src" status -sb 2>&1 | head -2
cat "$NEW/src/.git/worktrees/auto-src/gitdir"; cat "$NEW/auto-src/.git"
echo DONE
