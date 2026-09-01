/* McSPI controller-only loopback probe; it does not rely on the CPLD return wire. */
#include <fcntl.h>
#include <linux/spi/spidev.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

static void hex(const uint8_t *p, size_t n) {
    for (size_t i = 0; i < n; ++i) printf("%02x", p[i]);
}

int main(void) {
    const uint8_t tx[] = {0x00, 0xff, 0xaa, 0x55, 0x3c, 0xc3};
    uint8_t rx[sizeof tx] = {0};
    uint8_t mode = SPI_LOOP;  /* CPOL=0, CPHA=0, controller-internal return. */
    uint8_t actual = 0, bits = 8;
    uint32_t speed = 100000;
    int fd = open("/dev/spidev0.0", O_RDWR);
    if (fd < 0) { perror("open /dev/spidev0.0"); return 2; }
    if (ioctl(fd, SPI_IOC_WR_MODE, &mode) < 0 ||
        ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed) < 0 ||
        ioctl(fd, SPI_IOC_RD_MODE, &actual) < 0) {
        perror("SPI setup"); close(fd); return 2;
    }
    struct spi_ioc_transfer tr = {
        .tx_buf = (uintptr_t)tx, .rx_buf = (uintptr_t)rx,
        .len = sizeof tx, .speed_hz = speed, .bits_per_word = bits,
    };
    int count = ioctl(fd, SPI_IOC_MESSAGE(1), &tr);
    if (count < 0) { perror("SPI_IOC_MESSAGE"); close(fd); return 2; }
    int ok = memcmp(tx, rx, sizeof tx) == 0;
    printf("mode_effective=0x%02x transferred=%d tx=", actual, count);
    hex(tx, sizeof tx); printf(" rx="); hex(rx, sizeof rx);
    printf(" internal_loop_ok=%s\n", ok ? "true" : "false");
    close(fd);
    return ok ? 0 : 1;
}
