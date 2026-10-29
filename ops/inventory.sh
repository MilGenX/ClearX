echo "== LaunchAgents mentioning XDAW/X DAW/mydaw"; grep -l -i -e xdaw -e 'x daw' -e mydaw ~/Library/LaunchAgents/*.plist 2>/dev/null
for f in $(grep -l -i -e xdaw -e 'x daw' ~/Library/LaunchAgents/*.plist 2>/dev/null); do echo "-- $f"; grep -n -i -e xdaw -e 'x daw' "$f"; done
echo "== launchctl"; launchctl list | grep -i -e mydaw -e xdaw
echo "== claude projects"; ls ~/.claude/projects 2>/dev/null | grep -i -e xdaw -e mydaw
echo "== shell rc"; grep -n -i -e xdaw -e 'x daw' ~/.zshrc ~/.zprofile ~/.bashrc ~/.profile 2>/dev/null
echo "== ~/.local/bin, ~/bin"; grep -l -i -e 'Developer/XDAW' ~/.local/bin/* ~/bin/* 2>/dev/null
echo "== procs with cwd/files in Developer/XDAW"; lsof -nP +c 0 2>/dev/null | grep '/Developer/XDAW' | awk '{print $1,$2,$4}' | sort -u | head -30
echo "== MyDAW.app"; ls -d /Applications/MyDAW*.app ~/Applications/MyDAW*.app 2>/dev/null
