"""Apple Notes, read and arranged on request (X 2026-11-09: "look through my notes ... and organize literally everything").

`notes export` in state/keeper-run.req writes state/notes.json: every folder (with its parent) and every note (folder,
title, dates, text up to 4000 characters). Locked notes give their title only. The file is private (0600) and is read
once by the helper that asked, then removed by `notes forget`.
`notes apply` carries out state/notes-plan.json: [{"op": "folder", "name": ..., "parent": <folder id or "">},
{"op": "move", "id": <note id>, "to": <folder id or new folder name>}, {"op": "movefolder", "id": ..., "to": ...}].
Nothing is ever deleted: notes and folders are only made and moved. Each step's result goes to keeper-run.out.
"""
import json, os, subprocess

EXPORT = r"""
const N = Application('com.apple.Notes'); const folders = [], notes = [], seen = {};
function walk(f, parent, acct) {
  const id = f.id(); if (seen[id]) return; seen[id] = 1;
  let shared = false; try { shared = f.shared(); } catch (e) {}
  folders.push({id: id, name: f.name(), parent: parent, account: acct, shared: shared});
  let ids = [], names = [], mods = [], made = [], texts = [];
  try { ids = f.notes.id(); names = f.notes.name(); mods = f.notes.modificationDate(); made = f.notes.creationDate(); } catch (e) {}
  for (let i = 0; i < ids.length; i++) {
    let t = ''; let locked = false;
    try { locked = f.notes.byId(ids[i]).passwordProtected(); } catch (e) {}
    if (!locked) { try { t = f.notes.byId(ids[i]).plaintext().slice(0, 4000); } catch (e) { t = ''; } }
    notes.push({id: ids[i], folder: id, name: names[i], modified: String(mods[i]), created: String(made[i]), locked: locked, text: t});
  }
  try { f.folders().forEach(c => walk(c, id, acct)); } catch (e) {}
}
N.accounts().forEach(a => a.folders().forEach(f => {
  let p = ''; try { const c = f.container(); if (c.class && c.class() === 'folder') p = c.id(); } catch (e) {}
  walk(f, p, a.name());
}));
JSON.stringify({folders: folders, notes: notes});
"""

APPLY = r"""
function run(argv) {
  const N = Application('com.apple.Notes'); const plan = JSON.parse(argv[0]); const out = []; const made = {};
  const acct = N.defaultAccount();
  function folder(ref) {
    if (!ref) return null; if (made[ref]) return made[ref];
    try { const f = N.folders.byId(ref); f.name(); return f; } catch (e) {}
    const hit = acct.folders.whose({name: ref})(); return hit.length ? hit[0] : null;
  }
  plan.forEach(s => {
    try {
      if (s.op === 'folder') {
        const p = folder(s.parent);
        const existing = (p ? p.folders : acct.folders).whose({name: s.name})();
        let f = existing.length ? existing[0] : null;
        if (!f) { f = N.Folder({name: s.name}); (p ? p.folders : acct.folders).push(f); f = (p ? p.folders : acct.folders).whose({name: s.name})()[0]; }
        made[s.key || s.name] = f; out.push('ok folder ' + s.name);
      } else if (s.op === 'move') {
        const to = folder(s.to); if (!to) throw new Error('no folder ' + s.to);
        N.move(N.notes.byId(s.id), {to: to}); out.push('ok move ' + s.id);
      } else if (s.op === 'movefolder') {
        const to = folder(s.to); if (!to) throw new Error('no folder ' + s.to);
        N.move(N.folders.byId(s.id), {to: to}); out.push('ok movefolder ' + s.id);
      } else if (s.op === 'rename') {
        folder(s.id).name = s.name; out.push('ok rename ' + s.name);
      }
    } catch (e) { out.push('FAILED ' + JSON.stringify(s) + ': ' + e); }
  });
  return out.join('\n');
}
"""


def _osa(script, *args, timeout=3600):
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", script, *args], capture_output=True, timeout=timeout)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def via_terminal(what, state_dir):
    """ClearX's tick runs outside the desktop session, where Notes answers -600 ("isn't running"): the same step is
    run in Terminal instead (it asks once to control Notes), and its result lands in state/notes-<what>.out."""
    sh = os.path.join(state_dir, f"notes-{what}.command")
    with open(sh, "w") as f:
        f.write(f"#!/bin/sh\ncd {json.dumps(os.path.dirname(os.path.realpath(__file__)))}\n"
                f"/usr/bin/python3 notes.py {what} {json.dumps(state_dir)} > {json.dumps(os.path.join(state_dir, f'notes-{what}.out'))} 2>&1\n"
                f"rm -f {json.dumps(sh)}\nexit 0\n")
    os.chmod(sh, 0o700)
    subprocess.run(["open", "-a", "Terminal", sh], timeout=60)
    return f"notes {what}: started in Terminal (result in notes-{what}.out)"


def export(state_dir):
    code, out, err = _osa(EXPORT)
    if code != 0 and "-600" in err and not os.environ.get("TERM_PROGRAM"):
        return via_terminal("export", state_dir)
    if code != 0:
        return f"notes export failed: {err.strip()[:500]}"
    p = os.path.join(state_dir, "notes.json")
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(out)
    d = json.loads(out)
    return f"notes export: {len(d['folders'])} folders, {len(d['notes'])} notes -> {p}"


def apply(state_dir):
    p = os.path.join(state_dir, "notes-plan.json")
    if not os.path.exists(p):
        return "notes apply: no notes-plan.json"
    with open(p) as f:
        plan = f.read()
    json.loads(plan)  # (refuses a broken plan before anything moves)
    if not os.environ.get("TERM_PROGRAM") and _osa("Application('com.apple.Notes').name()")[0] != 0:
        return via_terminal("apply", state_dir)
    code, out, err = _osa(APPLY, plan)
    os.rename(p, p + ".done")
    return f"notes apply (exit {code}):\n{out}{err[:2000]}"


def test(state_dir):
    if not os.environ.get("TERM_PROGRAM"):
        return via_terminal("test", state_dir)
    a = subprocess.run(["osascript", "-e", 'tell application "Notes" to count notes'], capture_output=True, timeout=120)
    b = _osa("Application('com.apple.Notes').notes.length")
    return f"applescript: {a.returncode} {a.stdout!r} {a.stderr!r}\njxa: {b}"


TREE = r"""
const N = Application('com.apple.Notes'); const out = [];
N.folders().forEach(f => { let c = ''; try { c = f.container().name(); } catch (e) { c = '?'; }
  let n = 0; try { n = f.notes.length; } catch (e) {}
  let sh = false; try { sh = f.shared(); } catch (e) {}
  out.push([f.id(), f.name(), c, n, sh]); });
JSON.stringify(out);
"""


def tree(state_dir):
    """Every folder with the folder (or account) it sits in: state/notes-tree.json (names only, no note text)."""
    code, out, err = _osa(TREE)
    if code != 0:
        return f"notes tree failed: {err.strip()[:500]}"
    with open(os.path.join(state_dir, "notes-tree.json"), "w") as f:
        f.write(out)
    return f"notes tree: {len(json.loads(out))} folders"


def folders(state_dir):
    """Folders moved into folders with AppleScript (JXA's move reports success but leaves folders where they were):
    state/notes-folders.json = [[folder id, target folder id], ...]."""
    p = os.path.join(state_dir, "notes-folders.json")
    with open(p) as f:
        pairs = json.load(f)
    res = []
    for a, b in pairs:
        try:  # (one that hangs, e.g. a shared folder Notes won't move, never stops the rest)
            r = subprocess.run(["osascript", "-e", f'tell application id "com.apple.Notes" to move folder id "{a}" to folder id "{b}"'],
                               capture_output=True, timeout=60)
            res.append(f"{'ok' if r.returncode == 0 else 'FAILED'} {a} -> {b} {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
        except subprocess.TimeoutExpired:
            res.append(f"TIMED OUT {a} -> {b}")
    os.rename(p, p + ".done")
    return "notes folders:\n" + "\n".join(res)


def forget(state_dir):
    for n in ("notes.json",):
        try:
            os.remove(os.path.join(state_dir, n))
        except OSError:
            pass
    return "notes forget: export removed"


if __name__ == "__main__":
    import sys
    print(globals()[sys.argv[1]](sys.argv[2]))
