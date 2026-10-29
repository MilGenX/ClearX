setopt NULL_GLOB
echo "== HW/OS"; sw_vers -productVersion; sysctl -n hw.memsize | awk '{print $1/2^30" GB"}'; uptime
echo "== user LaunchAgents"; ls ~/Library/LaunchAgents
echo "== system LaunchAgents/Daemons (3rd party)"; ls /Library/LaunchAgents /Library/LaunchDaemons /Library/PrivilegedHelperTools 2>/dev/null
echo "== loaded non-apple user jobs"; launchctl list | grep -v -E 'com\.apple|^-\s+0\s+0x' | awk '$1!="-"' | head -40
echo "== top CPU (5 samples x 3s)"; top -l 5 -s 3 -o cpu -n 15 -stats pid,command,cpu,mem 2>/dev/null | awk '/^PID/{n++} n==5' | head -17
echo "== top memory"; ps -axo rss=,comm= | sort -rn | head -15 | awk '{printf "%6.0f MB %s\n",$1/1024,substr($0,index($0,$2))}' | cut -c1-120
echo "== memory"; memory_pressure -Q 2>/dev/null | tail -1; sysctl vm.swapusage
echo "== disk"; df -h / | tail -1
for d in ~/Library/CloudStorage ~/Music ~/Library/Application\ Support ~/Library/Caches ~/Library/Containers ~/Library/Group\ Containers ~/Documents ~/Downloads ~/Desktop ~/Movies ~/Pictures ~/Developer ~/.cache ~/.npm ~/Library/Developer ~/.Trash ~/Library/Logs /private/var/folders; do printf "%s\t%s\n" "$(du -sh "$d" 2>/dev/null | cut -f1)" "$d"; done
echo "== biggest in App Support / Caches / Library/Developer"; du -sh ~/Library/Application\ Support/* ~/Library/Caches/* ~/Library/Developer/* 2>/dev/null | sort -rh | head -20
echo "== Xcode junk"; du -sh ~/Library/Developer/Xcode/DerivedData ~/Library/Developer/Xcode/iOS\ DeviceSupport ~/Library/Developer/CoreSimulator ~/Library/Developer/Xcode/Archives 2>/dev/null; ls /Applications | grep -i xcode
echo "== Trash"; du -sh ~/.Trash 2>/dev/null
echo "== Spotlight"; mdutil -s / 2>/dev/null | tail -1
echo "== TM snapshots"; tmutil listlocalsnapshots / 2>/dev/null | wc -l
echo "== pmset"; pmset -g | grep -E ' sleep|displaysleep|powernap|tcpkeepalive|standby'
echo "== settings"; defaults read com.apple.universalaccess reduceTransparency 2>/dev/null; defaults read com.apple.universalaccess reduceMotion 2>/dev/null; defaults read NSGlobalDomain NSAutomaticWindowAnimationsEnabled 2>/dev/null
echo "== Brave"; for p in ~/Library/Application\ Support/BraveSoftware/Brave-Browser/*/Extensions; do echo "$p: $(ls "$p" | wc -l) ext"; done
echo "== Google Drive mode"; defaults read com.google.drivefs.settings 2>/dev/null | grep -i -E 'mirror|stream|Mode' | head
echo "== crash/hang last 3 days"; find /Library/Logs/DiagnosticReports ~/Library/Logs/DiagnosticReports -mtime -3 -type f 2>/dev/null | sed 's#.*/##' | sed 's/-20[0-9-]*\..*//' | sort | uniq -c | sort -rn | head -12
echo "== kernel_task / thermal"; pmset -g therm 2>/dev/null | tail -3
echo AUDIT-DONE
