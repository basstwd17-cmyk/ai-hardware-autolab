/* Read-only AM335x McSPI0 channel-0 register snapshot. */
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>
#include <unistd.h>

#define MCSPI0_BASE 0x48030000u
#define MAP_BYTES   0x1000u
#define CH0CONF     0x12cu
#define CH0STAT     0x130u
#define CH0CTRL     0x134u
#define MODULCTRL   0x128u

int main(void) {
    const int fd = open("/dev/mem", O_RDONLY | O_SYNC);
    if (fd < 0) { perror("open /dev/mem"); return 2; }
    volatile uint8_t *map = mmap(NULL, MAP_BYTES, PROT_READ, MAP_SHARED, fd, MCSPI0_BASE);
    if (map == MAP_FAILED) { perror("mmap McSPI0"); close(fd); return 2; }
    const uint32_t module = *(volatile uint32_t *)(map + MODULCTRL);
    const uint32_t conf = *(volatile uint32_t *)(map + CH0CONF);
    const uint32_t stat = *(volatile uint32_t *)(map + CH0STAT);
    const uint32_t ctrl = *(volatile uint32_t *)(map + CH0CTRL);
    printf("MODULCTRL=0x%08x CH0CONF=0x%08x CH0STAT=0x%08x CH0CTRL=0x%08x\n",
           module, conf, stat, ctrl);
    printf("CH0CONF PHA=%u POL=%u TRM=%u DPE0=%u DPE1=%u IS=%u FORCE=%u\n",
           conf & 1u, (conf >> 1) & 1u, (conf >> 12) & 3u,
           (conf >> 16) & 1u, (conf >> 17) & 1u,
           (conf >> 18) & 1u, (conf >> 20) & 1u);
    munmap((void *)map, MAP_BYTES);
    close(fd);
    return 0;
}
