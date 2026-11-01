# ClearX

**A memory manager for macOS that keeps the app you're using fast, and stays out of the way when your Mac is fine.**

[![tests](https://github.com/MilGenX/ClearX/actions/workflows/test.yml/badge.svg)](https://github.com/MilGenX/ClearX/actions/workflows/test.yml)
![macOS 13+](https://img.shields.io/badge/macOS-13%2B-black)
![License: MIT](https://img.shields.io/badge/license-MIT-7CF5C4)

Created by **[MilGenX](https://github.com/MilGenX)**.

<p align="center">
  <img src="docs/images/menu.png" width="300" alt="ClearX menu: health score, Speed Up, memory, processor and storage">
  &nbsp;&nbsp;
  <img src="docs/images/details.png" width="500" alt="ClearX Details: memory pool, graphics memory, processor and top apps">
</p>

Most "memory cleaners" purge caches that macOS refills seconds later. ClearX does something different. It decides
**who waits** when memory, heat or the disk run short. What you're using goes first. Hidden idle apps, background
builds and batch jobs step back, and they come back the moment you need them.

## Why it's different

| | macOS alone | Typical "RAM cleaner" | **ClearX** |
|---|---|---|---|
| When it acts | Always (compressor, swap) | When you click | **Only under strain**: memory, heat or a slow disk |
| What it does | Compresses and swaps whatever is coldest | Purges caches (macOS refills them) | **Freezes hidden idle apps, backgrounds big jobs, keeps a RAM reserve** |
| Getting an app back | Pages it back in from swap | Same | **Wakes it in about 1 ms on Cmd-Tab, Dock or click** |
| Builds (Xcode, CMake, Cargo…) | No special handling | No | **Sets job counts from free memory, queues builds, caps hot compilers** |
| Graphics memory (VRAM) | Hidden | Hidden | **Run as one pool with RAM; GPU spill into RAM is tracked** |
| On a healthy Mac | — | Purges anyway | **Does nothing.** Builds and apps run at full speed |

### Measured (2019 iMac, 16 GB RAM, Intel i9, details in [docs/MEASUREMENTS.md](docs/MEASUREMENTS.md))

With a heavy background job (an AI fine-tune) filling memory:

| | macOS alone | Cleaner method (purge) | **ClearX** |
|---|---|---|---|
| Switching back to a hidden app (median) | 1378 ms | 1141 ms | **225 ms** (6× faster) |
| Background job's progress | 100 % | 100 % | **80 %** (it yields only while you work) |
| SSD writes in the run | 28.4 GB | 28.6 GB | **20.6 GB** |

Builds with memory to spare: **as fast as without ClearX** (120 and 114 objects with it, 111 without, within noise).
These are single runs on one Mac. Treat differences under about 20 % as noise.

## Features

- **Memory pool controller.** RAM and VRAM run as one pool with green, yellow, orange and red levels, set from swap
  rate, available memory, GPU spill, heat and disk latency. Every 10 seconds it does the least that fixes the
  problem.
- **App Freezer.** Under strain, a hidden app that has been idle for 5 minutes and uses over 300 MB is paused, so it
  stops using CPU and memory churn. It wakes the instant you switch to it. It never freezes the app in front, apps
  playing audio or downloading, a DAW, or anything on your never-list.
- **RAM reserve.** It keeps 3 GB ready to use (you can change this), so opening something new doesn't stall.
- **Build-aware.** It sets Xcode's and CMake's parallel job count from free memory. It runs one build at a time
  across projects. It caps compiler memory and heat only when needed. While you're at the Mac, automatic builders run
  at background priority.
- **Studio mode.** When an audio app (Logic, Ableton, REAPER and others) is open and the Mac is strained, everything
  else gets out of its way.
- **Heat-aware.** It reads macOS's CPU speed limit. While the Mac is heat-throttling and you're using it, it caps
  parallel compiles.
- **Speed Up.** One click does real, measured actions: it compares before and after, and offers to politely quit
  idle apps (they ask to save first).
- **Maintenance and storage.** It shows a health score, storage cleanup with sizes, login-item and security checks,
  and a full report of every limit on your Mac (`clearx optimize`).
- **Native menu-bar app.** Built in SwiftUI, with a black theme and glass surfaces. With its windows closed, it
  refreshes only every 10 seconds.

## Safety

- It **never** sends SIGKILL, never touches root or system processes, and never changes kernel, sysctl or SIP
  settings. It needs no admin rights.
- Everything it pauses resumes on its own: when memory recovers, after a time limit, when you switch to the app, if
  the menu-bar app quits, or with `clearx off`.
- `./uninstall.sh` resumes everything and reverses every setting it changed.
- `clearx tick --dry-run` shows what it would do right now without doing it.

## Requirements

- macOS 13 Ventura or later
- Xcode Command Line Tools (for `python3` and `swiftc`): `xcode-select --install`
- Tested on an Intel iMac (2019) running macOS 15. Apple silicon should work: the code reads the page size and VRAM
  from the Mac, and there's no VRAM on unified memory. It hasn't been tested there yet, and reports are welcome.

## Install

```bash
git clone https://github.com/MilGenX/ClearX.git
```

```bash
cd ClearX && ./install.sh --login-item
```

This installs, for your user only:

- the daemon, a LaunchAgent that runs every 10 seconds
- the `clearx` command in `~/.local/bin`
- `ClearX.app` in `~/Applications`, which starts at login with `--login-item`

Keep the folder where you cloned it: the daemon runs from there.

## Use

Click the turbine in the menu bar. **Speed Up** gives you a measured boost, and **Details…** opens the full window.
From Terminal:

```bash
clearx status
```

```text
clearx status | log [N] | json        what it sees and did
clearx tick --dry-run                 what it would do right now, without doing it
clearx off | on                       off = resume everything and stop acting
clearx boost [--quit "App, App"]      Speed Up from the Terminal
clearx run "<label>" <command…>       queue a heavy job: runs when memory allows, at background priority
clearx optimize [--apply]             measure every limit of this Mac; --apply sets the safe user-level ones
clearx junk                           read-only report of reclaimable disk space
```

The full list is in [docs/REFERENCE.md](docs/REFERENCE.md#commands).

## Configure

`clearx.conf` holds the shared defaults, with every option explained. Put your own changes in
**`clearx.local.conf`**. Start from [`clearx.local.conf.example`](clearx.local.conf.example), or use **Open
settings file** in the app. It's read after the defaults, isn't tracked by git, and applies on the next tick. Common
changes:

- `[freezer] never`: apps that must never be frozen
- `[protected] apps`: whole apps (and everything they start) that ClearX never slows
- `[modes] studio_apps`: your audio app
- `[reserve] target`: how much memory to keep ready
- `[queue] scripts`: your own build scripts, so they wait their turn

## Uninstall

```bash
./uninstall.sh
```

## How it works

```
LaunchAgent (every 10 s)            ClearX.app (menu bar)
  clearx.py tick                      reads state/state.json, shows it
   ├─ measure: vm_stat, swap,         wakes frozen apps on activation (SIGCONT)
   │  VRAM/GPU, pmset therm, disk     calls `clearx` for every button
   ├─ pool level: green…red
   ├─ act, least first: background QoS → build caps → freeze hidden idle apps → pause the biggest job
   └─ release in reverse once the Mac is fine again
```

A deeper walkthrough of every rule, threshold and command is in [docs/REFERENCE.md](docs/REFERENCE.md). Why it
can't be on the Mac App Store, and what Developer ID distribution needs, is in
[docs/DISTRIBUTION.md](docs/DISTRIBUTION.md).

## Project layout

```
clearx.py          the daemon and the `clearx` command (Python 3, standard library only)
maint.py           maintenance, storage, checks and the optimize report
clearx.conf        defaults; clearx.local.conf.example shows how to override them
app/               ClearX.app (SwiftUI, built by app/build.sh, no Xcode project)
test/              unit tests on a simulated Mac, plus stress tools for real self-tests
bench/             benchmark scripts and their recorded results
docs/              reference, measurements, distribution notes, screenshots
```

## Development

```bash
python3 -m unittest discover -s test
```

```bash
app/build.sh
```

```bash
app/build/ClearX.app/Contents/MacOS/ClearX --snapshot /tmp/shots
```

The first command runs the unit tests: whole ticks on a fake Mac, with no real signals. The second builds the app.
The third renders the menu and every Details page to PNG files.

Issues and pull requests are welcome. Please include `clearx status` output and your Mac model with bug reports.

## Roadmap

- A Swift daemon inside the app (no Python), then signed and notarized Developer ID releases
- Testing on Apple silicon
- First-run onboarding and settings in the app

## License

[MIT](LICENSE) © 2026 MilGenX. ClearX is provided as is. It pauses and reprioritizes your own processes; read
**Safety** above, and use `clearx tick --dry-run` if you want to see what it would do first.
