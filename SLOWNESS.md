# Why the Mac feels slow: measured 2026-10-27 00:31–00:58 (EDT)

Method: a 15-minute sampler during normal use (`bench/slowness-sample.py`): every 10 s it recorded memory pressure, swap
in/out, compressor, ready memory, CPU speed limit, load, GPU %, total SSD bytes (IOKit), and per-process CPU
and disk I/O (`proc_pid_rusage`, including short-lived processes). Then fixes, then a 5-minute re-sample.
Root/kernel I/O can't be split per process without `sudo fs_usage`; it's shown as "system" (total minus user processes).

**Correction of an earlier claim.** "Swap ~3 MB per 5 min" was one quiet 5-minute window. Over 15 minutes swap came
in **bursts**: 6.1 GB out and 4.8 GB in, almost all in two 1-minute spikes. Quiet minutes really are ~0.

## Ranked causes

| # | Cause | Evidence (measured) | How it feels | Fix | Before → after |
|---|---|---|---|---|---|
| 1 | **MyApp's builder and test runs** (`~/Developer/MYAPP/.worktrees/builder`, node) | 489 node processes started in 15 min; node used 83 % of a core and wrote **5.8 GB** (all identifiable user-process writes). SSD bursts of **1.6–2.7 GB/min** for 3 minutes; one burst of 5.8 GB/min with 3.3 GB/min swap-out. A hung orphan test runner (pid 15622, `--test-timeout=0`, working folder in the Trash, a test stuck for 2 h 50 m) | Whole-system stalls for a minute at a time: the SSD is saturated, and apps wait for the disk | ClearX: builder runs at background QoS (lowest CPU and I/O tier) while you use the Mac. **Proposed for the MyApp repo (not edited):** (a) end the orphan run (`kill -TERM 15622`; it's from a deleted build); (b) give tests a timeout (`--test-timeout=120000`) so a stuck test can't live for hours; (c) cap test concurrency (`--test-concurrency=2`); (d) run the builder only when the Mac is idle, or less often | node writes 5.8 GB/15 min → 1.46 GB/5 min (same rate; it's the builder's workload) |
| 2 | **MyApp's local AI** (`llama-server`, Qwen3-8B, 5.3 GB, `-t 8`) | **87 % of a core on average** over 15 min (top CPU user), 8 threads at once; 5.3 GB resident, the single biggest memory user | Typing and scrolling lag while it thinks; memory pressure leads to swap bursts | ClearX: background QoS while you're in another app (never paused); full speed when MyApp is in front or you're away. **Proposed for MyApp:** `-t 4` (leave half the cores for you), and the smaller Qwen3-4B (2.5 GB, already downloading) would free ~2.8 GB of RAM | top CPU 87 % → not in the top 8 during the after-sample (it was idle then: not a like-for-like comparison) |
| 3 | **ClearX pausing the wrong things** | 443 pauses yesterday, including **com.apple.WebKit.GPU ×15** (draws every web view: MyApp, Safari), the Claude VM ×6, llama-server ×10, Apple speech ×5, Brave renderers ×16 for pressure; MyApp's node and cloudflared frozen with MyApp | Frozen or blank windows; beachballs in any app waiting on a paused service; MyApp features stalling | **Fixed:** pressure pauses never touch any process of an app on screen or any macOS service (/System, /usr); the Freezer honors `[protected] apps` (MyApp); resume only after 60 s steady, then 10 min with no re-pause; AI servers never paused | pauses of protected/GUI/system processes: many/day → **0** since the fix (0 pauses of any kind in the after window) |
| 4 | **Memory swap bursts** (16 GB RAM) | Pressure avg 29 %, max 33 %; ready memory min 3.1 GB; 6.1 GB swapped out in 15 min, in 2 bursts aligned with #1/#2 | Short freezes when switching to an app whose memory was swapped out | Fixes #1–#3 reduce the triggers; freezer stops at 4 GB compressed; builds 2 jobs while you're active | pressure avg 29 % → 23 %; ready min 3.1 → 4.1 GB; swap out 6.1 GB/15 min → 1.8 GB/5 min |
| 5 | **CPU heat throttling** (i9-9900K in an iMac) | `CPU_Speed_Limit` down to **74 %** in 7 of 90 samples, all when load hit 15–37 (builder + AI at once) | Everything ~25 % slower during those bursts | Less parallel work while you're active (#1, #2) | min 74 % → **100 %** in the after window |
| 6 | **Computer sleep after 1 minute** | `pmset sleep 1` | Builds, downloads and the AI paused or interrupted; wake-up delays | **Fixed** with your password: `sleep 0` (the screen still sleeps on its own setting) | 1 min → never |
| 7 | **ClearX's own cost** | Normal tick 0.37 s of CPU every 10 s (~4 % of one core); every 5 min a 1.7 s bookkeeping tick | Small | Acceptable; noted for a later optimization | — |
| 8 | Spotlight | mds + mds_stores + mdworker ≈ 6 % of a core; MYAPP, MyDAW and the Trash are **not** indexed (checked) | Minor | Nothing needed | — |
| — | Not causes (checked) | Graphics: GPU avg 3 %, WindowServer ~5 %, native 5K "default" scaling, Reduce Transparency **on** (ClearX's page wrongly said off: fixed). App launch: TextEdit 0.12 s, Safari 1.8 s cold / 0.13 s warm. No hang or spin reports today. Time Machine not running. Disk fsync latency 0.3–2.7 ms. | | | |

## What you'll feel
1. No more frozen or blank MyApp/Safari pages caused by ClearX, and no more beachballs from paused system services.
2. While you use the Mac, MyApp's builder and AI step back; when you walk away they run at full speed.
3. The Mac no longer sleeps after 1 minute, so long jobs don't stall.
4. The remaining slow moments are the bursts when MyApp's builder runs tests and its AI thinks at the same time. The fixes
   for that are in the MyApp repo (listed in #1 and #2). That thread can apply them with your OK.
5. Hardware limits of this 2019 Intel iMac: 16 GB RAM (an 8B local AI model plus builds plus browsers is more than
   16 GB can hold without swapping), an i9 that throttles under long all-core load in the iMac's case, and no Apple
   Silicon efficiency. Software can choose **who waits**; it can't make 16 GB hold 22 GB.

## After (b801): measured 2026-10-27 03:17–03:37 (EDT), 20 minutes, same sampler and metrics

MyApp b801 is live: tests have a 2-minute limit and run 2 at a time, the builder runs only when you're idle or MyApp is in
front, and the local AI is Qwen3-4B with `-t 4`. Same method as above (`bench/slowness-sample.py 1200`). Rates are
normalised to the first sample's 15 minutes where marked.

| Metric | Before (15 min, 00:31) | After b801 (20 min, 03:17) | Change |
|---|---|---|---|
| Drive writes | 18.9 GB (75 GB/h) | 1.9 GB (5.6 GB/h) | **−93 %** |
| Worst minute of writes | 5.8 GB/min | 0.9 GB/min | −84 % |
| Swap-out (per 15 min) | 6.1 GB | 0.65 GB | **−89 %** |
| Swap-in (per 15 min) | 4.8 GB | 0.2 GB | −96 % |
| node processes started | 489 | 0 among the top spawners | gone |
| node CPU (share of one core) | 83 % | 1.3 % | −98 % |
| llama-server CPU | 87 % (8B, 8 threads) | below 0.3 % (4B, 4 threads) | not like-for-like: the AI was idle during this window |
| Heat throttling (CPU speed limit) | min 74 %, 7 of 90 samples | 100 % in all 120 samples | **none** |
| Load average | avg 9.1, max 37.5 | avg 1.6, max 3.1 | −82 % |
| Ready memory (free + cache) | min 3.1, avg 5.1 GB | min 3.6, avg 5.9 GB | better |
| Memory pressure index | avg 29 %, max 33 % | avg 35 %, max 39 % | **worse** (see below) |

**About the pressure index.** It's macOS's "how hard it's working to find memory" figure. It rose while ready memory,
swap and writes all improved. The likely reason is a different mix of open apps: an MyDAW build and its tests were running in
this window, and Brave was open. 35–39 % is still in macOS's normal range (no warning level was reached, and no orange
or red pool state occurred). It is the one metric that didn't improve, so it's reported as is.

### Still slow, ranked
1. **MyDAW's auto-update build and tests.** clang used 26 % of a core on average; MyDAW's test programs wrote 0.35 GB.
   The only swap burst of the window (0.9 GB in one minute at 03:22) lines up with it. Proposal for MyDAW (another
   thread, not edited here): run its builder only when you're idle, like MyApp b801.
2. **16 GB with everything open.** Ready memory dips to 3.6 GB with Brave, Claude, MyApp and an MyDAW build at once. That's
   the hardware ceiling; software decides who waits.
3. **Minor:** Spotlight started 99 mdworker processes (0.7 % CPU in total); Brave's tab renderers used 5 %. Nothing to fix.

In short, the big stalls (drive-write storms, heat throttling, swap bursts from MyApp's tests and AI) are gone. What's
left is MyDAW's own build when it runs, and 16 GB's limit when everything is open at once.

## MyDAW build, ClearX ON vs OFF (4-minute windows, 2026-10-30)

Same clean MyDAW Release build, cut off after 240 s, run ON, OFF, ON (`bench/mydaw-window.sh`), so heat or other work
drifting over the hour shows up as a difference between the two ON runs.

| Metric | ON (1st) | OFF | ON (2nd) |
|---|---|---|---|
| Objects built in the window | 94 | 97 | 94 |
| Swap-out | 0.75 GB | 2.23 GB | 0.20 GB |
| Drive writes | 1.0 GB | 2.5 GB | 1.5 GB |
| Worst memory pressure | normal | **warn** | normal |
| Lowest ready memory | 3.4 GB | 2.7 GB | 3.3 GB |
| Process start, p95 | 8.9 ms | 13.2 ms | 18.2 ms |

ClearX costs about 3 % of build speed in a window this short and cuts swap-out by 66–91 %; only the OFF run reached
the warn pressure level. Process start times overlap between runs (the 2nd ON run had the highest), so the Mac's
feel during a build is within noise either way.

## What more RAM would change, measured (2026-10-30, `bench/ram-series.sh`)

Each run is 5 minutes on top of what the Mac was doing: you were away and MyApp's LoRA fine-tune was running (it needs
~11.6 GB on its own and swapped 60-190 MB/s). Stand-ins (`bench/ram-probe.c`): the app in use (1 GB, a tenth touched
every second) and 4 hidden apps (400 MB each, one switched to every 20 s). "Cleaner" = ClearX off plus what memory
cleaner apps do without admin rights (allocate and touch 2 GB so macOS drops caches when free memory < 1 GB).

| With the fine-tune running | macOS alone (off) | Cleaner method | ClearX before | **ClearX now** |
|---|---|---|---|---|
| Switching back to a hidden app (median) | 1378 ms | 1141 ms | 691 ms / 1 ms | **225 ms** |
| Fine-tune progress (CPU s in 5 min) | 1009 | 1016 | 573 / 212 | **809** |
| Starting a small program, p95 | 10.3 ms | 10.1 ms | 351 ms / 100 ms | **10.6 ms** |
| Swap in + out | 57.9 GB | 57.9 GB | 29.3 / 17.6 GB | **39.1 GB** |
| SSD writes | 28.4 GB | 28.6 GB | 15.2 / 7.8 GB | **20.6 GB** |

- One run per column ("ClearX now" ran 30 min after the off run; the fine-tune stopped when you came back, before a
  second round could run), so treat differences under ~20 % as noise.
- **ClearX before** paused the fine-tune at orange (two runs: it paused it for part of the first and most of the
  second), which is why switching was fast and the fine-tune got 21-57 % of its progress. It also lowered the
  benchmark's own 15 MB Python process, which is the 351 ms start time. Both fixed: orange doesn't pause the big job
  while you're away, and processes under 200 MB aren't lowered.
- **ClearX now** switches 6x faster than macOS alone and keeps 80 % of the fine-tune's progress: the job runs at
  background QoS, so its paging waits behind the apps.
- **Cleaner method**: no better than macOS alone. macOS refills what it drops.
- **Healthy Mac** (the fine-tune stopped when you came back; three more runs): off, cleaner and ClearX all switch in
  1 ms and start programs in 4.6 ms p95. ClearX adds nothing when nothing is wrong.
- **Where RAM still wins**: with macOS alone the fine-tune already gets 84 % of its 4 threads while swapping, so more
  RAM would make it at most ~19 % faster. What 16 GB costs here is the **SSD**: 20-28 GB written per 5 minutes of
  training (~240-340 GB/hour). Only less memory demand stops that: MyApp's server locks its 2.6 GB model in RAM
  (`--load-mode mmap+mlock`, visible as 2.6 GB wired) and the trainer loads a second copy of the same model at Q8
  (4.2 GB). Those are MyApp's settings (not changed).
- ClearX's own tick under pressure: 1.2 s → 0.6 s (footprints from `proc_pid_rusage` instead of `top`).

## MyDAW build, ClearX ON vs OFF after the fixes (4-minute windows, 2026-10-31 15:07)

Same clean MyDAW Release build, cut off after 240 s, ON, OFF, ON (`bench/mydaw-window.sh`). MyDAW was open (studio mode).

| Metric | ON (1st) | OFF | ON (2nd) | Before the fixes (09:31: ON / OFF / ON) |
|---|---|---|---|---|
| Objects built in the window | **120** | 111 | **114** | 58 / 116 / 103 |
| Compile jobs | 16 | 16 (Xcode default) | 16 | 8 / 16 / 8 |
| Swap-out | 0.7 GB | 0 | 0.7 GB | 0 / 0 / 1.3 GB |
| Process start, p95 | 104 ms | 4.9 ms | 46 ms | 4.0 / 3.5 / 8.0 ms |

Builds now run as fast with ClearX as without it (+3 to +8 %, within noise). The slower process starts in the ON runs
came from studio mode: with MyDAW open, ClearX kept every Python script and Brave at background QoS all the time, the
benchmark's own script included. Fixed right after (studio mode now lowers them only when the Mac is strained); that
fix is covered by a unit test, not re-measured.

## 4 GB VRAM with real apps open (2026-10-31 15:30)

MyDAW (open), Brave, Claude and MyApp's local AI on the Radeon (Vulkan build, as many layers as fit), sampled every 5 s
for a minute: VRAM in use **2.8-3.5 GB of 4 GB**, GPU memory spilled into RAM steady at **~0.1 GB** (not growing), so
everything runs inside the card's 4 GB. When something does push it over while memory is short, ClearX backgrounds
the GPU-heavy background apps first (seen today at 15:08: spill 24.8 MB/s at yellow -> WebKit GPU and
MTLCompilerService lowered, pool green again 43 s later).
