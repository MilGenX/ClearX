cd ~/Library/Application\ Support; du -sh "MyApp Local"/* 2>/dev/null; ls -l "MyApp Local/models"; du -sh Claude/* 2>/dev/null | sort -rh | head -4
echo "== who calls the local AI"; lsof -nP -iTCP:47900 2>/dev/null | awk 'NR>1{print $1,$2}' | sort -u
echo DONE2
