#!/bin/sh
# Removes ClearX completely and undoes every change it made. Resumes anything it paused first.
D=~/Developer/ClearX
/usr/bin/python3 "$D/clearx.py" off            # resumes paused, removes background QoS, stops CPU throttles
for A in ClearX; do osascript -e "quit app \"$A\"" 2>/dev/null; pkill -x $A 2>/dev/null; done
for L in com.user.clearx com.user.clearx.menubar; do
  launchctl bootout gui/$(id -u)/$L 2>/dev/null; rm -f ~/Library/LaunchAgents/$L.plist
done
rm -rf ~/Applications/ClearX.app ~/.local/bin/clearx "$D/state/.probe"
defaults delete com.apple.dt.Xcode IDEBuildOperationMaxNumberOfConcurrentCompileTasks 2>/dev/null
defaults delete com.brave.Browser HighEfficiencyModeEnabled 2>/dev/null
defaults delete com.brave.Browser MemorySaverModeSavings 2>/dev/null
defaults delete com.brave.Browser BackgroundModeEnabled 2>/dev/null
defaults delete com.brave.Browser DiskCacheSize 2>/dev/null
defaults delete com.xdaw.app NSAppSleepDisabled 2>/dev/null          # App Nap back to the default for MyDAW
launchctl unsetenv CMAKE_BUILD_PARALLEL_LEVEL; launchctl unsetenv MAKEFLAGS   # (clearx off does this too)
DD=~/Library/Developer/Xcode/DerivedData
tmutil removeexclusion "$DD" 2>/dev/null; rm -f "$DD/.metadata_never_index"
for d in ~/Developer/MyDAW/auto-src/build ~/Developer/MyDAW/src/build ~/Developer/MYAPP/build ~/Developer/MYAPP/apps/macos/.build \
         ~/Developer/MYAPP/packages/core/node_modules ~/Developer/MYAPP/.worktrees/learning/build ~/Developer/MYAPP/.worktrees/builder/build \
         ~/Developer/MYAPP/.worktrees/builder/packages/core/node_modules ~/Developer/MYAPP/.worktrees/builder/apps/macos/.build ~/Developer/MYAPP/.cache; do
  [ -d "$d" ] && tmutil removeexclusion "$d" 2>/dev/null
done
# agents clearx disabled stay disabled until you re-enable them:  launchctl enable gui/$(id -u)/<label>
grep -h "MAINT disabled agent" "$D/logs/clearx.log"* 2>/dev/null | sed 's/.*agent /  disabled by clearx: /' 
echo "Note: if you clicked Fix for the sleep file, undo it with: sudo pmset -a hibernatemode 3"
echo "smartmontools (installed for the SSD wear reading) stays; remove with: brew uninstall smartmontools"
echo "ClearX uninstalled. (Folder $D left in place; trash it if you like.)"
