# memguard v3

A per-user resource governor for this iMac (iMac19,1: Intel i9-9900K, 16 GB RAM, Radeon Pro 575X
with 4 GB VRAM, 5K display, macOS 15). It has two parts:

- **`memguard.py`**: a daemon run every 10 s by a LaunchAgent. It runs RAM + VRAM as **one 20 GB
  pool** and acts on it, together with swap, CPU load, thermals and the disk.
- **MemGuard.app**: a native menu-bar app (SwiftUI, about 35 MB RSS, refreshes every 2 s). It
  shows the live numbers and has buttons for every action.

**Safety rules:** memguard never sends SIGKILL, never touches root or system processes, and
never changes kernel, sysctl or SIP settings. Everything it pauses is resumed automatically, and
`uninstall.sh` reverses every change it made.

## Memory on this Mac: 16 GB RAM + 4 GB VRAM, run as one 20 GB pool

The Radeon Pro 575X has its **own 4 GB of video memory** on the graphics card. Graphics does
**not** take any of your 16 GB of system RAM, so you have about **20 GB of memory in total**:
16 GB for apps and 4 GB for the screen and GPU work. (The "half of RAM is VRAM" idea applies to
Apple Silicon Macs and to Intel Macs with integrated graphics only.) At 5K, macOS and the
browser's GPU process typically use about 1 GB of that 4 GB.

When the 4 GB of VRAM is full, the GPU doesn't fail: macOS maps system RAM over PCIe (GART) and keeps
going. That "spill" is visible in `ioreg` as `inUseSysMemoryBytes`. So the two memories really are one
budget, and memguard v3 controls them as one:

```
20 GB pool (16.0 RAM + 4.0 VRAM)  [AAAAAAAAAAAWWWWWWWWCCCCVVVVVVcccccccccccccccc......]####### swap
  A=app RAM 3.7  W=wired 2.7  C=compressed 1.2  S=GPU spill 0.0  V=VRAM 1.9  c=cached 8.4  .=free 2.1
```

### The pool controller (each tick)

One state (green/yellow/orange/red) is computed from RAM pressure, the swap-out rate, free and
compressed memory, predicted exhaustion, the GPU spill rate, the CPU speed limit and the disk. Then
one priority order decides who gives way: **foreground app and DAW** (never touched) > **builds** >
**background apps**. The foreground app is the frontmost app and its helper processes (a build started
from the foreground Terminal still counts as a build).

| Situation | What memguard does, in order |
|---|---|
| Pool healthy (green, not swapping, ≥ 1.5 GB available) | Releases everything: resumes paused processes one per tick (smallest first), removes background QoS after 60 s calm, and sets Xcode to **8** parallel compiles for the next build |
| Yellow (swap ≥ 2 GB while swapping, or pressure warn) | Soft budgets apply; heavy and ≥ 500 MB background processes get `renice 10` + background QoS. Xcode cap **4**. Build mode backgrounds Brave |
| Orange (swap ≥ 3 GB while swapping, free < 1 GB while swapping, or exhaustion predicted < 60 s) | Also pauses the largest background process (≥ 200 MB). The compiler memory budget halves. Xcode cap **2** |
| Red (swap ≥ 3.5 GB while swapping, or pressure critical) | Pauses every background process ≥ 500 MB; compiler budget is a quarter (the oldest job always runs) |
| GPU spilling into RAM (≥ 8 MB/s over 30 s) while the pool is yellow+ | Backgrounds the GPU-heavy *background* apps first (WebKit GPU, Brave GPU, MTLCompilerService, VMs); the build guard waits 20 s before tightening the compiler budget |
| CPU thermal throttling (`CPU_Speed_Limit` < 100) | First cuts compiler parallelism (at most 4 compile jobs at once, Xcode cap 2); if it's still throttling after 60 s, backgrounds the GPU-heavy/browser background apps too |
| VRAM full but pool healthy | Nothing. Full VRAM is fine |
| Disk tight (< 40 GB free on the swap volume, or a 4 KB write + fsync ≥ 50 ms) | The pool goes yellow as soon as RAM is tight (available < 4 GB or swapping), and budgets apply even at green |

The state only steps down one level per tick, so it doesn't flap between orange and green when
swap-outs pause for a moment. The Xcode cap goes down at once and up only after 60 s.

**Xcode parallelism.** memguard writes `IDEBuildOperationMaxNumberOfConcurrentCompileTasks` (8 / 4 / 2)
from the pool. Xcode and xcodebuild read it when a build *starts*, so it sets the next build's parallelism.
For a build that's already running, the build guard does the live work (pausing the newest compile jobs).
`memguard off` deletes the key (Xcode's own default: one compile per CPU thread).

## Maintenance (replaces CleanMyMac)

`memguard maintain …` on the command line, or the **Maintain** tab in MemGuard.app. It always
previews with sizes first. Removing something moves it to `~/.Trash` (recoverable), and every
move is logged in `logs/memguard.log` as `MAINT`. It never empties the Trash and never uses sudo.

| Command | What it does |
|---|---|
| `maintain scan` | Junk: app caches (skips apps that are running), logs > 7 days, Xcode DerivedData, old archives and DeviceSupport, unavailable simulators, brew/pip/npm/yarn caches, installers in Downloads > 30 days, Trash size. A separate **Optional** list covers big iOS dev data: simulator runtimes, simulator devices, and DeviceSupport for your iPhone. Language files are skipped on purpose, because removing them breaks app code signatures. |
| `maintain clean caches,logs,xcode,devcaches,installers` | Moves the chosen categories to the Trash |
| `maintain large` | Files over 500 MB, or over 50 MB and unused for a year, under your home folder. Report only; use Reveal in the app |
| `maintain uninstall "<App>" [--vendor com.x] [--apply]` | Finds an app's leftovers (Application Support, Caches, Preferences, Containers, Group Containers, LaunchAgents, Saved State, HTTPStorages, WebKit, Logs, Cookies), previews them, and with `--apply` moves the app and its leftovers to the Trash |
| `maintain startup` / `disable <label>` / `enable <label>` | Login items and LaunchAgents/Daemons with measured CPU/RAM. Disable/enable works for user agents (launchctl, reversible); system ones need admin |
| `maintain updates` | `softwareupdate -l` plus `brew outdated` (and `mas outdated` if installed). Nothing is installed automatically; macOS updates need a restart |
| `maintain security` | XProtect and XProtect Remediator versions, Gatekeeper, SIP, FileVault, firewall, Spotlight, plus a **persistence audit**: every LaunchAgent/Daemon/login item is checked with `codesign`, and the actual script is audited for zsh/python agents. Unsigned, ad-hoc, invalid or missing items are flagged unless they're yours. Corrupt preference files are found with `plutil -lint` (privacy-protected Apple files are skipped, since they're unreadable, not corrupt) |
| `maintain storage` | Boot drive type (SSD / Fusion / HDD), protocol, SMART, TRIM, a 2 GB write/read speed test (temp file deleted afterwards), free + purgeable space, APFS local snapshots (report only), the top 25 folders under ~ and /Applications, **duplicates over 50 MB** in your own Documents, Downloads, Desktop, Music, Movies and Pictures only (same size, then a sampled hash, then a full BLAKE2 hash; preview only, the oldest copy is listed first). Never inside apps, frameworks, plug-ins, libraries, models, node_modules, .git or build folders, and never in cloud folders, where a copy is usually needed or reading it would download it, and iOS backups, Mail downloads and Messages attachments (those need Full Disk Access to measure) |
| SSD writes | Bytes written/read on the internal SSD since boot (IOKit) and per day (tracked every 5 min in `state/maint.json`), shown in the Storage view. Lifetime writes need SMART data: `brew install smartmontools` then `smartctl -a disk0` |
| `maintain trash <file>` | Moves one file under your home folder to the Trash (used by the Storage view's duplicate list) |
| `optimize [--apply]` (or `maintain optimize`) | The "max potential" checklist: measures power settings, kexts/system extensions, stale login items, background services and VMs, Xcode settings, ccache, Brave, Spotlight, network/DNS, 5K scaling and its VRAM cost, Reduce transparency, SSD traffic, RAM slots. Each row is ok / applied / needs OK / needs you / hardware, with the exact command. `--apply` sets only the safe user-level items |
| `maintain health` | One score from 0 to 100: memory 30, swap 15, thermal 15 (these three are the **average over the last 10 minutes**, so one heavy moment doesn't swing it), storage 15 (free % under 14 %, < 40 GB free for swap, SMART, drive type), updates 10, security 15. Scans never change the score by themselves. Recorded every 5 min, with 7 days of hourly history |
| `maintain all` | All of the above except `large`, `clean`, `optimize` and the slow parts of `storage` |

**Honest note on malware.** Moonlock went away with CleanMyMac. memguard is **not a signature
antivirus**. It relies on Apple's built-in XProtect and XProtect Remediator, which macOS updates
automatically, plus Gatekeeper, SIP and the persistence audit above. On a Mac that only runs
signed software from known developers, that's the main protection anyway.

**Memory "free up".** CleanMyMac's button runs a `purge`-style flush. That empties the file cache,
which macOS refills immediately, so it's cosmetic and makes the next app launches slower.
memguard instead stops the processes that cause the pressure (pause, background QoS, build
guard, predictive) while it's happening.

### Side-by-side with CleanMyMac (2026-10-26)

| | CleanMyMac X | memguard maintain |
|---|---|---|
| Junk found | 18.89 GB (its figure, mostly system and dev data) | 3.8 GB safe + 16.8 GB optional iOS dev data = **20.6 GB**, itemized with what each one is |
| Removal | Deletes | Moves to Trash (recoverable) and logs it |
| Memory | `purge` flush (cosmetic) | Real-time governor, predictive, build guard |
| CPU / GPU / thermal | Menu monitor | Monitor plus a CPU governor, thermal response, VRAM/GPU response, modes |
| Malware | Moonlock signature engine | XProtect + Gatekeeper/SIP/FileVault status + persistence audit (no signature engine) |
| Its own cost | about 335 MB RAM in 3 always-on processes plus a root helper | about 34 MB app + a daemon running about 0.2 s every 10 s, no root |

CleanMyMac X was removed with memguard's own uninstaller (app + 19 leftovers = 208.5 MB to the
Trash). Its root helper needs your password: `sudo ~/Developer/MacMemoryManager/remove-cleanmymac-admin.sh`.

## What's fixed by hardware and what software can tune

| Fixed by hardware (no software can change it) | Tunable (what memguard does) |
|---|---|
| **4 GB of VRAM** on the Radeon. It's physical memory on the graphics card. **System RAM can't become VRAM** on an Intel Mac. The Apple Silicon `iogpu.wired_limit_mb` trick doesn't exist here. When VRAM is full, macOS pages textures over PCIe (GART), which is slow. | Run RAM + VRAM as one 20 GB pool: full VRAM is fine; when the GPU spills into RAM *under pressure*, the GPU-heavy background apps give way first |
| **16 GB RAM** (2 × 8 GB DDR4-2666 in 2 of the 4 SO-DIMM slots). Not soldered: the rear hatch takes 260-pin DDR4-2666 SO-DIMMs, up to 64 GB (Apple's max). Measured 2026-10-26: 650 GB of SSD traffic in 3.7 h since boot, almost all swap. Adding 2 × 16 GB (32 GB total) is the only real fix. | Pause/renice/background hogs, cap compiler memory, predict exhaustion, auto-resume |
| **i9-9900K thermal and power envelope.** The iMac's cooling can't hold all-core turbo, so it throttles under long loads. memguard never undervolts or disables Turbo. | CPU governor (background QoS, then duty-cycling), thermal watch, Xcode capped at 4 parallel compiles |
| No GPU switching (iMac19,1 always uses the Radeon; `pmset gpuswitch` is irrelevant here) | Reduce Transparency/Motion lowers WindowServer GPU load at 5K (optional, your call) |

## Other controls (each tick)

- **Swap gating.** macOS keeps swap allocated after pressure ends, so swap thresholds only count
  while the system is actively swapping (at least 8 MB swapped out per tick) or pressure isn't normal.
- **Predictive.** memguard takes available memory (free + inactive + purgeable) and the swap-out
  rate over the last 30 s and projects the time to exhaustion. If that's under 60 s and less than
  3 GB is available, it goes to ORANGE early.
- **Build guard.** It sums the memory of running compiler jobs (clang, swift-frontend, ld,
  rustc, cc1plus …). Above 6 GB (3 GB in Studio mode; halved at orange, quartered at red), the newest
  jobs are paused and then resumed oldest-first as others finish. The oldest job always keeps
  running, so a build finishes instead of thrashing. Pressure never pauses a compiler directly: compilers
  are only paused by the build guard.
- **CPU governor.** A non-protected process above 300 % (3 cores) for 60 s gets background QoS.
  If it's still hot after another 60 s, it's duty-cycled with SIGSTOP/SIGCONT to 50 % of its time
  (cpulimit-style). It's released automatically when it calms down. `[cpu] watch` apps are only
  logged, never touched.
- **Thermal.** memguard reads `pmset -g therm` every tick (see the pool table for what it does).
  The app also shows the CPU temperature from the SMC (no root needed).
- **One mode: Max.** There's nothing to pick. memguard detects by itself when a DAW (MyDAW/XDAW, Logic,
  Ableton …) or a build (xcodebuild, SWBBuildService, ninja, make, cargo + compilers) is running, and
  that only changes priorities: with a DAW open, Brave, Google Drive, node, Python and VMs are kept at
  background QoS, per-app budgets are halved and the compiler budget is 3 GB; during a build, Brave is
  backgrounded while the pool is yellow or worse.
- **Safety nets.** Anything paused longer than 15 min (60 min for build-guard jobs) is always
  resumed, with a notification. pid reuse is checked before any signal.
- **Protected list.** WindowServer, Finder, Dock, loginwindow, kernel_task, Claude*, Xcode (the
  app, not its compilers), Terminal, MemGuard, Spotlight/mds and other system UI processes. Add
  your own on `[protected] user =`.

## The menu-bar app

The menu bar shows a colored dot and the available memory in GB. Clicking it opens a simple menu:
- the **health score** (hover it for what's taking points off) and one plain sentence ("Your Mac is
  running smoothly")
- a big **Speed Up** button (below)
- three bars, **Memory**, **Processor**, **Storage**, each with a word: Good / Busy / Strained (or Warm /
  Too hot, Getting full / Almost full)
- **Needs you**: at most 3 items, each one sentence and one button (install the macOS update, stop the
  16 GB sleep file, turn off CleanMyMac leftovers, clean old app caches, Reduce transparency, drive
  getting full). A button that needs admin rights shows the normal macOS password prompt, only when you
  click it.
- **Details…**, the MemGuard on/off switch, and Quit

**Details…** opens a window with everything else: the live 20 GB pool bar, GPU, per-core CPU, the top
processes with Pause/Bg/Quit, the paused list (tab "Live"), and Cleanup, Storage, Optimize, Startup items,
Updates, Security, Large files and the app uninstaller (tab "Clean up & checks").

It reads its numbers natively (Mach, libproc, IOKit, SMC) and calls the `memguard` CLI for every
action, so the daemon's state file stays the single source of truth.

Source: `app/Sources/main.swift`. Build: `app/build.sh` (swiftc, no Xcode project).

### Speed Up (`memguard boost`)

CleanMyMac's "Speed up" mostly ran a `purge`, which empties the file cache that macOS refills at once.
memguard's button does only real things, then shows the **measured** before/after (available, app
memory, compressed, swap, pressure, load):

1. Resumes anything memguard paused that's no longer needed (when the pool isn't orange/red).
2. Moves every non-foreground app over 300 MB to background QoS. Each one is released the moment you
   switch to it.
3. Ends orphaned compile jobs (clang/ld/swift-frontend whose build is gone) with SIGTERM.
4. Restarts third-party user LaunchAgents that auto-restart (KeepAlive) and whose memory grew more than 2x
   since memguard first saw them.
5. Stops idle Homebrew services (`brew services start <name>` undoes it).
6. Lists idle apps (not frontmost for 30+ min, over 300 MB) with checkboxes. **Quit selected** asks each
   app to quit through AppleScript, so it can ask you to save; nothing is force-quit. Only simple
   apps from `[boost] safe_quit` are pre-ticked; Finder, Xcode, Claude, Terminal and DAWs are never quit.

Tab discarding stays Brave's own job (the Memory Saver policy is on); no outside switch exists for it.

It reads its numbers natively (Mach, libproc, IOKit, SMC) and calls the `memguard` CLI for every
action, so the daemon's state file stays the single source of truth.

Source: `app/Sources/main.swift`. Build: `app/build.sh` (swiftc, no Xcode project).

## Commands

```
memguard status | log [N] | json
memguard tick --dry-run            # what it would do right now, without doing it
memguard pause <name>              # matches EVERY process with that name; be specific
memguard resume all | resume <name>
memguard pause-pid|resume-pid|bg-pid|fg-pid|quit-pid|throttle-pid|unthrottle-pid <pid>
memguard off | on                  # off = resume all, remove background QoS, stop throttles
memguard bench [seconds]           # CSV in bench/: speed limit, load, swap rate, free, VRAM, GPU%
memguard bench run <label> [--guard-swap 3G] [--guard-avail 500M] -- <command>
                                   # runs a workload, samples every 2 s, aborts it (SIGINT, then SIGTERM)
                                   # if swap grows > guard or available memory < guard; CSV + JSON in bench/
memguard optimize [--apply]        # the max-potential checklist (see Maintenance)
memguard boost [--quit "App, App"] # Speed Up (see above)
memguard junk                      # READ-ONLY report of reclaimable disk space + delete commands
```

## Editing thresholds

Edit `memguard.conf`. It's re-read every tick, so there's nothing to restart. Sections:
`[levels] [pool] [resume] [predict] [build_guard] [cpu] [modes] [mode.normal|build|studio] [heavy]
[budgets] [protected] [vram]`. Set `dry_run = true` to watch decisions in the log without any
signals being sent.

## Other system changes (all reversed by uninstall.sh)

- `com.apple.dt.Xcode IDEBuildOperationMaxNumberOfConcurrentCompileTasks` is now set by the daemon from
  the pool (8 / 4 / 2) and read by Xcode/xcodebuild when each build starts. `memguard off` and
  `uninstall.sh` delete it.
- `defaults write com.brave.Browser HighEfficiencyModeEnabled -bool true` (Memory Saver policy).
  Brave's own setting was already on (`high_efficiency_mode state=2`). Check `brave://policy`
  after restarting Brave to see whether the policy is listed.
- `tmutil addexclusion ~/Library/Developer/Xcode/DerivedData` plus `.metadata_never_index` there.
  Newer macOS versions may ignore that file, so for certainty, also add the folder under System
  Settings → Spotlight → Search Privacy.

## Install / uninstall

```
./install.sh                # daemon + CLI + MemGuard.app (launched now)
./install.sh --login-item   # same, plus start MemGuard.app at login
./uninstall.sh              # resume everything, remove both LaunchAgents, the app, the CLI link,
                            # the Xcode/Brave keys and the Time Machine exclusion
```

Manual uninstall, equivalent to `uninstall.sh`:
```
memguard off
launchctl bootout gui/$(id -u)/com.user.memguard
launchctl bootout gui/$(id -u)/com.user.memguard.menubar
rm ~/Library/LaunchAgents/com.user.memguard*.plist ~/.local/bin/memguard
rm -rf ~/Applications/MemGuard.app
defaults delete com.apple.dt.Xcode IDEBuildOperationMaxNumberOfConcurrentCompileTasks
defaults delete com.brave.Browser HighEfficiencyModeEnabled
tmutil removeexclusion ~/Library/Developer/Xcode/DerivedData
```

## Tests

`test/selftest.log` (v1) and `test/selftest-v2.log` (v2) hold real runs. Each test used a config
whose `target_regex` limited memguard to the test processes only:
- build guard: three fake 600 MB `clang` jobs (`test/hog.c`) under a 1 GB budget
- predictive: an incompressible hog growing 500 MB/s (memguard went orange and paused it at 500 MB)
- CPU governor: a 2-thread spinner (`test/spin.c`) was moved to background QoS, then duty-cycled
  to about 75 %, then manually unthrottled

Cost: a calm tick takes about 0.18 s (≈1.6 % of one core every 10 s). Under pressure it reads the
phys footprint via `top`, which takes about 0.6 s.
