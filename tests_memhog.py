# Test hog: allocate in steps (MB per step, steps) and hold. Never > 6 GB.
import sys, time
step, n = int(sys.argv[1]), int(sys.argv[2])
assert step * n <= 6144
blocks = []
for i in range(n):
    blocks.append(bytearray(b"\x01" * (step << 20)))
    print(f"allocated {(i+1)*step} MB", flush=True); time.sleep(1)
time.sleep(3600)
