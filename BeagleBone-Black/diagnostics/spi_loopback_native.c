/* Native Linux spidev loopback probe: excludes Python/ctypes entirely. */
#include <errno.h>
#include <fcntl.h>
#include <linux/spi/spidev.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

static void print_hex(const uint8_t *data, size_t size) {
    for (size_t i = 0; i < size; ++i) printf("%02x", data[i]);
}

int main(int argc, char **argv) {
    const unsigned mode_arg = argc > 1 ? strtoul(argv[1], NULL, 0) : 0;
    const uint32_t speed = argc > 2 ? strtoul(argv[2], NULL, 0) : 10000;
    const uint8_t tx[] = {0x00, 0xff, 0xaa, 0x55, 0x3c, 0xc3};
    uint8_t rx[sizeof tx] = {0};
    uint8_t mode = (uint8_t)mode_arg;
    uint8_t actual_mode = 0;
    uint8_t bits = 8;

    if (mode_arg > 3) {
        fprintf(stderr, "mode must be 0..3\n");
        return 2;
    }
    const int fd = open("/dev/spidev0.0", O_RDWR);
    if (fd < 0) { perror("open /dev/spidev0.0"); return 2; }
    if (ioctl(fd, SPI_IOC_WR_MODE, &mode) < 0 ||
        ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed) < 0 ||
        ioctl(fd, SPI_IOC_RD_MODE, &actual_mode) < 0) {
        perror("SPI setup");
        close(fd);
        return 2;
    }

    struct spi_ioc_transfer tr = {
        .tx_buf = (uintptr_t)tx,
        .rx_buf = (uintptr_t)rx,
        .len = sizeof tx,
        .speed_hz = speed,
        .bits_per_word = bits,
    };
    const int transferred = ioctl(fd, SPI_IOC_MESSAGE(1), &tr);
    if (transferred < 0) {
        perror("SPI_IOC_MESSAGE");
        close(fd);
        return 2;
    }
    printf("mode_requested=%u mode_effective=%u speed_hz=%u transferred=%d tx=",
           mode, actual_mode, speed, transferred);
    print_hex(tx, sizeof tx);
    printf(" rx=");
    print_hex(rx, sizeof rx);
    printf(" ok=%s\n", memcmp(tx, rx, sizeof tx) == 0 ? "true" : "false");
    close(fd);
    return memcmp(tx, rx, sizeof tx) == 0 ? 0 : 1;
}
