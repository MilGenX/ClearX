# Benchmarks

Scripts that measure what ClearX changes on a real Mac. Results from the development Mac are summarized in
[docs/MEASUREMENTS.md](../docs/MEASUREMENTS.md). Raw runs you record yourself are git-ignored.

| Script | Measures |
|---|---|
| `ram-series.sh` | Runs `ram-bench.py` as ClearX on, off, a purge-style "cleaner", then on again |
| `ram-bench.py` | App-switch latency, program start time, swap and SSD writes while a background job fills memory |
| `ram-probe.c` | Stand-in apps for `ram-bench.py`: allocates memory and touches part of it on a schedule |
| `apps-test.py` | Opens about 20 of your apps, then times how fast each comes to the front (optionally during a build) |
| `slowness-sample.py` | Samples pressure, swap, CPU speed limit, load and per-process CPU and disk I/O for 15 minutes |
| `io-top.py` | Which processes write to the SSD |
| `replay-levels.py` | Replays the memory-level rules over recorded samples |
| `ram-*.json`, `selftest*.log` | Recorded results behind the published numbers |

Also built in: `clearx bench [seconds]` and `clearx bench run <label> -- <command>`.
