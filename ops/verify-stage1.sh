N=$HOME/Developer/MyDAW
echo "== git"; git -C $N/src status -sb | head -1; git -C $N/auto-src status -sb | head -1; git -C $N/src worktree list
echo "== auto-update last lines"; tail -4 $N/auto-logs/updates.log; cat $N/auto-logs/STATUS.txt; tail -3 $N/auto-logs/launchd.log
echo "== MyDAW hub check path"; [ -d ~/Developer/XDAW ] && echo "old path resolves (MyDAW still sees the hub)"
echo "== overlap: files reachable twice?"; [ "$(stat -f %i $N)" = "$(stat -L -f %i ~/Developer/XDAW)" ] && echo "XDAW is only a pointer to MyDAW (one copy)"
du -sh $N | cut -f1
