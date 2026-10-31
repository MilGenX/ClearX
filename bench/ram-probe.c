// ram-probe: stand-ins for apps, to time what more RAM would change.
//   ram-probe active <MB> <interval_ms> <seconds> <out.csv>  the app you're using: touches a tenth of its memory every
//                                                            interval (all of it every 10 intervals) and logs each pass (ms)
//   ram-probe idle <MB> <out.csv>                            a hidden app: sits still; on SIGUSR1 ("switch to it")
//                                                            touches all of its memory and logs how long that took
//   ram-probe clean <MB>                                     what memory-cleaner apps do without admin rights:
//                                                            allocate and touch MB so macOS drops caches, then exit
// Pages are half random, half zero, so they compress about 2:1 like real app memory.
// Build: cc -O2 -o bench/ram-probe bench/ram-probe.c
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static double now_ms(void) {
  struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

static char *fill(long mb) {
  long n = mb << 20;
  char *p = malloc(n);
  if (!p) exit(1);
  for (long i = 0; i < n; i += 4096) { arc4random_buf(p + i, 2048); memset(p + i + 2048, 0, 2048); }
  return p;
}

static double touch_range(volatile char *p, long from, long to) {
  double t = now_ms();
  for (long i = from; i < to; i += 4096) p[i] += 1;
  return now_ms() - t;
}
static double touch(volatile char *p, long mb) { return touch_range(p, 0, mb << 20); }

static volatile sig_atomic_t go;
static void on_usr1(int s) { (void)s; go = 1; }

int main(int c, char **v) {
  if (c < 3) { fprintf(stderr, "usage: see source\n"); return 2; }
  long mb = atol(v[2]);
  if (!strcmp(v[1], "clean")) { char *p = fill(mb); touch(p, mb); return 0; }
  if (!strcmp(v[1], "active") && c >= 6) {
    long iv = atol(v[3]); double end = now_ms() + atol(v[4]) * 1e3;
    FILE *f = fopen(v[5], "w");
    char *p = fill(mb);
    long slice = (mb << 20) / 10, k = 0;
    while (now_ms() < end) {  // a tenth of its memory per pass: each page is used every 10 s, like a real app's state
      double ms = touch_range(p, (k % 10) * slice, (k % 10 + 1) * slice); k++;
      fprintf(f, "%.0f,%.1f\n", now_ms(), ms); fflush(f);
      usleep(iv * 1000);
    }
    return 0;
  }
  if (!strcmp(v[1], "idle") && c >= 4) {
    signal(SIGUSR1, on_usr1);
    FILE *f = fopen(v[3], "w");
    char *p = fill(mb);
    for (;;) {
      pause();
      if (go) { go = 0; fprintf(f, "%.0f,%.1f\n", now_ms(), touch(p, mb)); fflush(f); }
    }
  }
  return 2;
}
