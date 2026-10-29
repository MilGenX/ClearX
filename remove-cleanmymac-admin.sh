#!/bin/sh
# Removes CleanMyMac's root-owned helper (needs your admin password). Run:  sudo ~/Developer/ClearX/remove-cleanmymac-admin.sh
set -x
launchctl bootout system/com.macpaw.CleanMyMac4.Agent 2>/dev/null
rm -f /Library/LaunchDaemons/com.macpaw.CleanMyMac4.Agent.plist
rm -f /Library/PrivilegedHelperTools/com.macpaw.CleanMyMac4.Agent
rm -rf "/Library/Application Support/MacPaw" /Library/Application\ Support/com.macpaw.* 2>/dev/null
echo "CleanMyMac fully removed."
