// test hog: allocate <step_mb> every <ms> until <max_mb> (capped 6144), touch pages, then hold
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int c, char **v) {
  long step = atol(v[1]), max = atol(v[2]), ms = c > 3 ? atol(v[3]) : 500;
  if (max > 6144) max = 6144;
  for (long got = 0; got < max; got += step) { char *p = malloc(step << 20); arc4random_buf(p, step << 20); usleep(ms * 1000); }
  sleep(600); return 0;
}
