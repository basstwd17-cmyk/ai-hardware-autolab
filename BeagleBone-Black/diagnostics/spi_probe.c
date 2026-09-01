// Reference SPI0 transfer probe using Linux's native spidev ABI.
#include <errno.h>
#include <fcntl.h>
#include <linux/spi/spidev.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

static int fd;
static uint32_t transfer_speed_hz = 100000;

static int transfer(const uint8_t tx[3], uint8_t rx[3]) {
    struct spi_ioc_transfer tr = {
        .tx_buf = (uintptr_t)tx,
        .rx_buf = (uintptr_t)rx,
        .len = 3,
        .speed_hz = transfer_speed_hz,
        .bits_per_word = 8,
    };
    memset(rx, 0, 3);
    return ioctl(fd, SPI_IOC_MESSAGE(1), &tr);
}

static int read_code(unsigned address, uint16_t *code) {
    uint8_t tx[3] = {(uint8_t)(0x80u | address), 0, 0};
    uint8_t rx[3];
    if (transfer(tx, rx) < 0) return -1;
    *code = (uint16_t)(((rx[1] & 0x3fu) << 8) | rx[2]);
    printf("READ%u tx=%02x%02x%02x rx=%02x%02x%02x code=0x%04x\n",
           address, tx[0], tx[1], tx[2], rx[0], rx[1], rx[2], *code);
    return 0;
}

static int write_code(unsigned address, uint16_t code) {
    uint8_t tx[3] = {
        (uint8_t)address,
        (uint8_t)((code >> 8) & 0x3f),
        (uint8_t)code,
    };
    uint8_t rx[3];
    if (transfer(tx, rx) < 0) return -1;
    printf("WRITE%u tx=%02x%02x%02x rx=%02x%02x%02x\n",
           address, tx[0], tx[1], tx[2], rx[0], rx[1], rx[2]);
    return 0;
}

int main(int argc, char **argv) {
    unsigned requested_mode = argc > 1 ? (unsigned)strtoul(argv[1], NULL, 0) : 0;
    if (requested_mode > 3) {
        fprintf(stderr, "mode must be 0..3\n");
        return 2;
    }
    if (argc > 2) transfer_speed_hz = (uint32_t)strtoul(argv[2], NULL, 0);
    uint8_t mode = (uint8_t)requested_mode;
    uint8_t bits = 8;
    uint32_t speed = transfer_speed_hz;
    const uint16_t expected[4] = {0x0001, 0x0123, 0x2345, 0x3ffe};
    uint16_t observed;
    int errors = 0;

    fd = open("/dev/spidev0.0", O_RDWR);
    if (fd < 0) { perror("open"); return 2; }
    if (ioctl(fd, SPI_IOC_WR_MODE, &mode) < 0 ||
        ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed) < 0) {
        perror("SPI setup"); close(fd); return 2;
    }

    printf("MODE=%u SPEED_HZ=%u\n", requested_mode, transfer_speed_hz);

    puts("BEFORE");
    for (unsigned i = 0; i < 4; ++i)
        if (read_code(i, &observed) < 0) { perror("read"); return 2; }
    puts("WRITE");
    for (unsigned i = 0; i < 4; ++i)
        if (write_code(i, expected[i]) < 0) { perror("write"); return 2; }
    puts("AFTER");
    for (unsigned i = 0; i < 4; ++i) {
        if (read_code(i, &observed) < 0) { perror("read"); return 2; }
        if (observed != expected[i]) ++errors;
    }
    close(fd);
    printf("BIT_ERRORS=%d\n", errors);
    return errors ? 1 : 0;
}
