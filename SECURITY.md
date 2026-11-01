# Security

## Reporting a vulnerability

Please report security problems privately through GitHub: **Security › Report a vulnerability** on this repository.
Don't open a public issue. You'll get a reply within a few days.

## What ClearX can and can't do

ClearX runs as your own user, with no admin rights, and never opens a network connection.

- It sends signals (`SIGSTOP`, `SIGCONT`, `SIGTERM`) and changes priority only for processes that belong to you. It
  refuses macOS's own processes (`/System`, `/usr`, `/bin`, `/sbin`, `/Library/Apple`) and your protected list, and it
  never sends `SIGKILL`.
- It stores no passwords, tokens or keys, and uses no third-party packages: the service is the Python standard
  library, and the app is Swift with no dependencies.
- Its log (`logs/clearx.log`) and state (`state/`) stay on your Mac, inside the ClearX folder. They contain process
  names and memory figures, and they're git-ignored, so they're never committed.
- Your own settings live in `config/clearx.local.conf`, which is also git-ignored.
- `clearx bench run -- <command>` runs the command you type through your shell, on purpose: it's a benchmark of your
  own workload.

## Supported versions

Only the latest version on `main` gets fixes.
