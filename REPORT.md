# memguard v3: report for the iMac (2026-10-26)

iMac19,1 · i9-9900K · 16 GB RAM (2 × 8 GB, 2 of 4 slots) · Radeon Pro 575X 4 GB · macOS 15.8.1 → 15.8.2 (installing)

## A. Health score

| Component | Before (17:58) | Now | What changed |
|---|---|---|---|
| Memory (30) | 0 | 0 | green |
| Swap (15) | 0 | 0 | — |
| Thermal (15) | 0 | 0 | throttles during builds (76 % measured), but not at the hourly sample |
| Storage (15, new) | — | 0 | 72.2 GB free (≥ 40 GB for swap), SMART Verified, SSD |
| Updates (10) | −10 (macOS 15.8.2, Safari 27.2, brew cmake) | −9 → **0 after the restart** | cmake upgraded; macOS + Safari installing now |
| Security (15) | −2 (CleanMyMac root helper, ad-hoc signed) | **0** | helper removed by `remove-cleanmymac-admin.sh`; its two dead login jobs booted out |
| **Total** | **88** | **91 → 100 expected** | scoring unchanged except the stricter storage component |

Only you can fix: (1) finish the macOS 15.8.2 + Safari 27.2 install and restart (in progress);
(2) System Settings → General → Login Items & Extensions: turn off the two **CleanMyMac X** entries
(BTM still lists them because the app is in the Trash; they vanish when you empty it).

## B. RAM + VRAM as one 20 GB pool (live in the daemon)

- One pool state per tick from RAM pressure, swap-out rate, free/compressed, predicted exhaustion,
  VRAM, GPU spill rate (`inUseSysMemoryBytes` growth), CPU load, thermal limit and the disk.
- Priority: foreground app + DAW (never touched) > builds > background. Pressure never pauses a
  compiler; only the build guard does, oldest job always running.
- GPU spilling under pressure → GPU-heavy background apps backgrounded first, compilers get a 20 s grace.
  Thermal throttling → compiler parallelism cut first (4 jobs, Xcode cap 2), UI apps only after 60 s.
  Healthy → everything released.
- The 90 % VRAM reaction is gone; per-app budgets act only at yellow+ (or when the disk is tight).
- Xcode parallelism for the next build follows the pool: 8 / 4 / 2.
- Seen live during the MyDAW auto-update build at 18:28: old v2 had paused three clang jobs (3.7 GB) as
  "largest process"; v3 paused a Brave renderer instead and handed the clangs to the build guard, which
  resumed them as room appeared.
- `vm.compressor_mode` = **4** (compression + swap, the macOS default; not changed).
- Free disk for swap: **72.2 GB** (+ 45.7 GB purgeable) ≥ 40 GB ✔.

## Storage

- Drive: **SSD APPLE SSD SM0512L** (PCI-Express), internal NVMe, **not a Fusion Drive**. SMART Verified, TRIM Yes.
- Speed (2 GB file): write **884 MB/s** (dd: 1,130 MB/s); read 9930 MB/s is cached. No external SSD needed.
- 72.2 GB free + 45.7 GB purgeable of 465.6 GB. Local snapshots: 0 (only the sealed OS snapshot).
- Biggest folders: ~/Library/CloudStorage 110 GB (Google Drive), ~/Music 79 GB, ~/Library/Application Support 32 GB,
  ~/Documents 26 GB, ~/Developer/XDAW 16 GB, ~/.cache 9.6 GB (whisper models), ~/Developer/MYAPP 8.9 GB.
- Duplicates: the first scan read cloud and app-bundle folders and over-counted (73 GB); it now skips
  CloudStorage (no downloads), app bundles, node_modules, package stores and .git. Real candidates seen:
  the MyDAW song stems exist 3× (XDAW/gold/work/swap, ~/Music/X DAW Songs, Google Drive), ~150 MB each;
  the 1.2 GB whisper encoder weights are duplicated in ~/.cache/xdaw. Nothing was deleted: review in the Storage view.
- **SSD writes:** 346.3 GB written and 510.6 GB read since boot at 14:49, nearly all swap.
  Tracked per day in the Storage view. Lifetime writes need `brew install smartmontools` (your OK).
- Pool is storage-aware: < 40 GB free or a slow disk (fsync ≥ 50 ms) → budgets and yellow kick in earlier.

## C. Benchmark: memguard OFF vs ON (clean MyDAW Release build + Brave with tabs)

The OFF run started at 18:48 and was **stopped by me at 229 s** because the macOS update was about to restart
the Mac (not by the safety guard). What it showed in those 229 s with no memguard (Xcode default = 16 compiles):

| | OFF (229 s, partial) | ON |
|---|---|---|
| CPU speed limit (min) | **76 %** (thermal throttling) | after the restart |
| Load (max) | 29.7 | |
| Swap growth / peak | +962 MB / 2.4 GB | |
| Swapped out | 1024 MB | |
| SSD written | **3178 MB in 229 s** | |
| Min available memory | 5.5 GB | |
| Peak compressed | 2.3 GB | |
| Pressure peak | 1 (normal) | |
| Peak VRAM / GPU spill | 1.9 GB / 48 MB | |

After the restart, run `~/Developer/MacMemoryManager/bench/mydaw-bench.sh` (or ask Claude to): it rebuilds the
workspace from XDAW read-only, waits for a quiet Mac, runs OFF then ON with the same safety guard, and
writes `bench/run-*-mydaw-{off,on}.json`. This table gets filled from those.

## Max potential: every item measured

| Area | Item | Measured | Status | Action / command |
|---|---|---|---|---|
| Power | Computer sleep / display sleep / disk sleep | sleep 1 min, displaysleep 0, disksleep 0 | **needs OK** | sudo pmset -a sleep 0  # a desktop: never sleep mid-build (display sleep still saves power) |
| Power | Power Nap (wakes the Mac for background work) | powernap 0 | **ok** | sudo pmset -a powernap 0 |
| Power | Standby / hibernate | standby 1, hibernatemode 3 | **needs OK** | sudo pmset -a hibernatemode 0  # desktop: no RAM image written to disk on sleep (saves 16 GB of writes) |
| Power | High Power mode | not offered on iMac19,1 (Apple Silicon/MacBook Pro only) | **n/a** | — |
| Startup | Third-party kernel extensions | 1: com.waves.driver.soundgrid 14.26.48 | **info** | kmutil showloaded --list-only | grep -v com.apple |
| Startup | System extensions | 2 enabled | **ok** | systemextensionsctl list |
| Startup | Uptime | booted 2026-10-26 14:49 (0.2 days) | **info** | — |
| Startup | Login items of apps in the Trash (stale BTM records) | 1: CleanMyMac X.app | **needs you** | System Settings > General > Login Items & Extensions: turn off the CleanMyMac X items (they disappear once the Trash is emptied) |
| Background | Homebrew services running | none | **ok** | brew services stop <name> |
| Background | Virtual machines | 0 running, 0 B | **ok** | cap the VM's memory in its app (e.g. 2 GB for a Linux VM; Docker Desktop: Settings > Resources) |
| Background | node / python processes left running | node 1 (57.8 MB), python 3 (93.0 MB) | **info** | memguard status  # Quit the ones you don't need from the menu |
| Background | Google Drive | not running | **ok** | Google Drive > Settings > Google Drive: Stream files (not Mirror) keeps the disk and RAM lower |
| Dev | Xcode/xcodebuild parallel compiles | 8 (set by memguard from the pool: 8 healthy, 4 yellow, 2 pressure/hot) | **applied** | — |
| Dev | Xcode DerivedData location | default (~/Library/Developer/Xcode/DerivedData, internal SSD) | **ok** | — |
| Dev | Xcode indexing | on (Xcode pauses it while building) | **ok** | — |
| Dev | ccache for the MyDAW build | not installed | **needs OK** | brew install ccache; then in XDAW's build script: cmake -G Ninja -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache (the Xcode generator ignores compiler launchers) |
| Dev | MyDAW auto-update interval | every 2 min | **info** | suggestion only (XDAW is not edited by memguard): 15-30 min is plenty; each check that finds a new commit starts a full Release build |
| Browser | Brave Memory Saver (tab discarding) | policy on | **applied** | defaults write com.brave.Browser HighEfficiencyModeEnabled -bool true |
| Browser | Brave hardware acceleration | on | **ok** | brave://settings/system > Use graphics acceleration |
| Browser | Brave extensions / RAM | 0 extensions, 25 processes, 1.8 GB | **ok** | brave://extensions: each extension is a process |
| Spotlight | mds / mds_stores / mdworker CPU now | 1% | **ok** | System Settings > Spotlight > Search Privacy: add ~/Developer/XDAW/src/build, ~/Developer/XDAW/auto-src/build, ~/Developer/MYAPP/build, node_modules folders |
| Network | Default route | Wi-Fi (en1) | **info** | Ethernet is lower latency and doesn't share airtime with the Wi-Fi of your other devices |
| Network | DNS lookup time (uncached) | current 25 ms, 1.1.1.1 23 ms | **ok** | keep current DNS (1.1.1.1 isn't meaningfully faster here) |
| Display | WindowServer CPU / Reduce transparency | 14% CPU, reduce transparency on | **ok** | System Settings > Accessibility > Display > Reduce transparency |
| Thermal | Macs Fan Control | installed | **your call** | see the bench: if CPU_Speed_Limit stays 100 during a full build, the stock fan curve is enough |
| Storage | SSD traffic since boot (reads + writes, mostly swap) | 883.6 GB in 4.1 h (5.1 TB/day) | **hardware** | swap traffic is SSD wear; more RAM is the fix (see RAM below) |
| Hardware | RAM | 8 GB, 8 GB in 2 of 4 slots, 2667 MHz; 278.3 GB swapped out since boot (1.6 TB/day) | **hardware** | 32 GB: 2x16 GB DDR4-2666 PC4-21300 260-pin SO-DIMM in the empty slots, or 4x8/4x16 GB (Apple's max is 64 GB); rear hatch above the power port, no tools beyond a coin |
| Hardware | Boot drive | SSD APPLE SSD SM0512L PCI-Express, write 884 MB/s, read 9930 MB/s | **ok** | — |

### What raises the ceiling (hardware)
- **RAM to 32 GB**: 2 × 16 GB DDR4-2666 (PC4-21300) 260-pin SO-DIMMs into the 2 empty slots (~$60–90), rear
  hatch, no tools. Measured: 346.3 GB written to the SSD in ~4 h, mostly swap, during ordinary builds + Brave.
  With 32 GB the build + browser working set fits in RAM: builds stop being paused, swap writes drop to ~0.
  64 GB = 4 × 16 GB (replace the 8s).
- **Boot drive**: already an internal NVMe SSD at ~1 GB/s write. An external drive would not help.

### Recommendations that need your OK (not done)
- `sudo pmset -a hibernatemode 0` (desktop: no 16 GB RAM image written at each sleep) and `sudo pmset -a sleep 0`.
- ccache for MyDAW: `brew install ccache`, then build MyDAW with `-G Ninja -DCMAKE_C_COMPILER_LAUNCHER=ccache
  -DCMAKE_CXX_COMPILER_LAUNCHER=ccache` (the Xcode generator ignores compiler launchers). Rebuilds after a pull
  recompile only what changed. XDAW was not edited.
- MyDAW auto-update checks every 2 min and full-builds each new commit; 15–30 min would cut build churn (XDAW's call).
- Spotlight → Search Privacy: add the XDAW/MYAPP build folders and node_modules.
- Ethernet instead of Wi-Fi if a cable is near (DNS is fine: 23 ms vs 18 ms for 1.1.1.1).
- Macs Fan Control: your call. The partial OFF build already hit a 76 % CPU speed limit; the ON run will
  show whether memguard's job cap keeps it at 100 % without a fan curve.

## Files
- `memguard.py` (pool controller, boost, bench run), `maint.py` (storage, optimize, health), `memguard.conf` ([pool]),
  `app/Sources/main.swift` (pool bar, Speed Up, Storage, Optimize), `bench/mydaw-bench.sh`, README.md, uninstall.sh.
- Backups of v2: `state/*.v2bak`.
