#!/bin/bash
# Compare the two CPHA choices using only the already-approved non-DAC
# loopback frame.  This neither programs the CPLD nor changes pinmux/overlay
# state.  The spidev settings observed on entry are restored before exit.
set -euo pipefail

python3 - <<'PY'
import array
import ctypes
import fcntl
import os

DEVICE = "/dev/spidev0.0"
SPEED_HZ = 100_000
TX = bytes.fromhex("00ffaa553cc3")
TRANSFERS_PER_MODE = 64


def ioc(direction, kind, number, size):
    return (direction << 30) | (size << 16) | (kind << 8) | number


SPI_IOC_RD_MODE = ioc(2, ord("k"), 1, 1)
SPI_IOC_WR_MODE = ioc(1, ord("k"), 1, 1)
SPI_IOC_RD_BITS_PER_WORD = ioc(2, ord("k"), 3, 1)
SPI_IOC_WR_BITS_PER_WORD = ioc(1, ord("k"), 3, 1)
SPI_IOC_RD_MAX_SPEED_HZ = ioc(2, ord("k"), 4, 4)
SPI_IOC_WR_MAX_SPEED_HZ = ioc(1, ord("k"), 4, 4)


class Transfer(ctypes.Structure):
    _fields_ = [
        ("tx_buf", ctypes.c_uint64),
        ("rx_buf", ctypes.c_uint64),
        ("len", ctypes.c_uint32),
        ("speed_hz", ctypes.c_uint32),
        ("delay_usecs", ctypes.c_uint16),
        ("bits_per_word", ctypes.c_uint8),
        ("cs_change", ctypes.c_uint8),
        ("tx_nbits", ctypes.c_uint8),
        ("rx_nbits", ctypes.c_uint8),
        ("word_delay_usecs", ctypes.c_uint8),
        ("pad", ctypes.c_uint8),
    ]


SPI_IOC_MESSAGE_1 = ioc(1, ord("k"), 0, ctypes.sizeof(Transfer))


def read_u8(fd, request):
    value = array.array("B", [0])
    fcntl.ioctl(fd, request, value, True)
    return value[0]


def read_u32(fd, request):
    value = array.array("I", [0])
    fcntl.ioctl(fd, request, value, True)
    return value[0]


def write_u8(fd, request, value):
    fcntl.ioctl(fd, request, array.array("B", [value]))


def write_u32(fd, request, value):
    fcntl.ioctl(fd, request, array.array("I", [value]))


def transfer(fd):
    tx_buffer = ctypes.create_string_buffer(TX, len(TX))
    rx_buffer = ctypes.create_string_buffer(len(TX))
    message = Transfer(
        tx_buf=ctypes.addressof(tx_buffer),
        rx_buf=ctypes.addressof(rx_buffer),
        len=len(TX),
        speed_hz=SPEED_HZ,
        bits_per_word=8,
    )
    fcntl.ioctl(fd, SPI_IOC_MESSAGE_1, bytes(message))
    return bytes(rx_buffer.raw)


fd = os.open(DEVICE, os.O_RDWR)
try:
    original = (
        read_u8(fd, SPI_IOC_RD_MODE),
        read_u8(fd, SPI_IOC_RD_BITS_PER_WORD),
        read_u32(fd, SPI_IOC_RD_MAX_SPEED_HZ),
    )
    print(
        "entry mode=%d bits=%d speed_hz=%d" % original
    )
    for mode in (0, 1):
        write_u8(fd, SPI_IOC_WR_MODE, mode)
        write_u8(fd, SPI_IOC_WR_BITS_PER_WORD, 8)
        write_u32(fd, SPI_IOC_WR_MAX_SPEED_HZ, SPEED_HZ)
        observed = [transfer(fd) for _ in range(TRANSFERS_PER_MODE)]
        matches = sum(reply == TX for reply in observed)
        distinct = sorted({reply.hex() for reply in observed})
        print(
            "mode=%d tx=%s matches=%d/%d rx_distinct=%s"
            % (mode, TX.hex(), matches, TRANSFERS_PER_MODE, ",".join(distinct))
        )
finally:
    try:
        write_u8(fd, SPI_IOC_WR_MODE, original[0])
        write_u8(fd, SPI_IOC_WR_BITS_PER_WORD, original[1])
        write_u32(fd, SPI_IOC_WR_MAX_SPEED_HZ, original[2])
        print("restored mode=%d bits=%d speed_hz=%d" % original)
    finally:
        os.close(fd)
PY
