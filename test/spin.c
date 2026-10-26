// test: 2 busy threads (~200% CPU) for up to 90 s
#include <pthread.h>
#include <unistd.h>
static void *spin(void *a) { volatile unsigned long x = 0; for (;;) x++; return 0; }
int main(void) { pthread_t t; pthread_create(&t, 0, spin, 0); pthread_create(&t, 0, spin, 0); sleep(90); return 0; }
