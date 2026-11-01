# Measurements

All on the development Mac: a 2019 iMac (Intel i9-9900K, 16 GB RAM, Radeon Pro 575X with 4 GB VRAM, macOS 15).
Each table is one run per column, so treat differences under about 20 % as noise. The scripts are in `bench/` and
the raw results of the memory benchmark are in `bench/ram-*.json`.

## A busy Mac: switching apps while a big background job fills memory

`bench/ram-series.sh`, 5 minutes per run. A local AI fine-tune (`llama-finetune-lora`, about 11.6 GB on its own,
swapping 60-190 MB/s) runs in the background. Stand-ins from `bench/ram-probe.c` play the app in use (1 GB, a tenth
touched every second) and 4 hidden apps (400 MB each, one switched to every 20 s). "Cleaner method" is ClearX off
plus what memory-cleaner apps do without admin rights: allocate and touch 2 GB so macOS drops its caches when free
memory falls under 1 GB.

| | macOS alone | Cleaner method | **ClearX** |
|---|---|---|---|
| Switching back to a hidden app (median) | 1378 ms | 1141 ms | **225 ms** |
| Background job's progress (CPU s in 5 min) | 1009 | 1016 | **809** |
| Starting a small program, p95 | 10.3 ms | 10.1 ms | **10.6 ms** |
| Swap in + out | 57.9 GB | 57.9 GB | **39.1 GB** |
| SSD writes | 28.4 GB | 28.6 GB | **20.6 GB** |

- **ClearX** switches 6× faster than macOS alone and keeps 80 % of the background job's progress. The job runs at
  background priority, so its paging waits behind the apps instead of the other way round.
- **The cleaner method** is no better than macOS alone: macOS refills what it drops.
- **Where more RAM still wins:** macOS alone already gives the job 84 % of its 4 threads while swapping, so more RAM
  mostly speeds up the background job, not the apps.

## A healthy Mac: builds must not get slower

A clean C++ (CMake/Xcode, JUCE) Release build, cut off after 240 s, run ON, OFF, ON so drift over the hour shows up
as a difference between the two ON runs.

| | ClearX ON (1st) | OFF | ClearX ON (2nd) |
|---|---|---|---|
| Objects built in the window | **120** | 111 | **114** |
| Compile jobs | 16 | 16 (Xcode's default) | 16 |
| Swap-out | 0.7 GB | 0 | 0.7 GB |

With memory to spare, ClearX leaves builds at full speed (+3 to +8 %, within noise). It only steps in under strain.

## Graphics memory (4 GB VRAM) with real apps open

A browser, a DAW, a chat app and a local AI model offloaded to the Radeon (Vulkan), sampled every 5 s for a minute.
VRAM in use was **2.8-3.5 GB of 4 GB**. GPU memory spilled into RAM held steady at about **0.1 GB** and didn't
grow, so nothing needed to move.

## A day of use: why the Mac felt slow, before and after

`bench/slowness-sample.py` recorded memory, swap, CPU speed limit, load and per-process CPU and disk I/O every 10 s.
Over 15 minutes of normal use it found that most stalls came from background builds and a local AI server running
during work. They were fixed by letting those step back while you're at the Mac (ClearX), and by the projects
themselves. Over the same length of time afterwards:

| | Before | After |
|---|---|---|
| Drive writes | 75 GB/h | 5.6 GB/h |
| Swap-out (per 15 min) | 6.1 GB | 0.65 GB |
| Heat throttling (lowest CPU speed limit) | 74 % | 100 % |
| Load average | 9.1 | 1.6 |

Part of that "after" came from changes inside those projects, not ClearX alone. It's shown as the combined result.
