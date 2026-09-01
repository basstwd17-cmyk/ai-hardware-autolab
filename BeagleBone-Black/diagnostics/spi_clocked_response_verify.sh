#!/bin/bash
# Verify the fixed response from the non-DAC clocked SPI return diagnostic.
set -u

python3 - <<'PY'
import array
import ctypes
import fcntl
import os

DEVICE = "/dev/spidev0.0"
TX = bytes(6)
EXPECTED = bytes.fromhex("a55a3cc3f00f")
SPEED = 100000
REPEATS = int(os.environ.get("SPI_REPEATS", "32"))

def ioc(direction, kind, number, size):
    return (direction << 30) | (size << 16) | (kind << 8) | number

RD_MODE, WR_MODE = ioc(2, ord("k"), 1, 1), ioc(1, ord("k"), 1, 1)
RD_BITS, WR_BITS = ioc(2, ord("k"), 3, 1), ioc(1, ord("k"), 3, 1)
RD_SPEED, WR_SPEED = ioc(2, ord("k"), 4, 4), ioc(1, ord("k"), 4, 4)

class Transfer(ctypes.Structure):
    _fields_ = [("tx_buf", ctypes.c_uint64), ("rx_buf", ctypes.c_uint64), ("len", ctypes.c_uint32), ("speed_hz", ctypes.c_uint32), ("delay_usecs", ctypes.c_uint16), ("bits_per_word", ctypes.c_uint8), ("cs_change", ctypes.c_uint8), ("tx_nbits", ctypes.c_uint8), ("rx_nbits", ctypes.c_uint8), ("word_delay_usecs", ctypes.c_uint8), ("pad", ctypes.c_uint8)]

MESSAGE = ioc(1, ord("k"), 0, ctypes.sizeof(Transfer))

def read(fd, request, code):
    value = array.array(code, [0])
    fcntl.ioctl(fd, request, value, True)
    return value[0]

def write(fd, request, code, value):
    fcntl.ioctl(fd, request, array.array(code, [value]))

def transfer(fd):
    tx = ctypes.create_string_buffer(TX, len(TX))
    rx = ctypes.create_string_buffer(len(TX))
    message = Transfer(ctypes.addressof(tx), ctypes.addressof(rx), len(TX), SPEED, 0, 8, 0, 0, 0, 0, 0)
    fcntl.ioctl(fd, MESSAGE, bytes(message))
    return bytes(rx.raw)

fd = os.open(DEVICE, os.O_RDWR)
try:
    original = (read(fd, RD_MODE, "B"), read(fd, RD_BITS, "B"), read(fd, RD_SPEED, "I"))
    write(fd, WR_MODE, "B", 0)
    write(fd, WR_BITS, "B", 8)
    write(fd, WR_SPEED, "I", SPEED)
    replies = [transfer(fd) for _ in range(REPEATS)]
    print("tx=%s expected=%s matches=%d/%d rx_distinct=%s" % (TX.hex(), EXPECTED.hex(), sum(reply == EXPECTED for reply in replies), REPEATS, ",".join(sorted({reply.hex() for reply in replies}))))
finally:
    write(fd, WR_MODE, "B", original[0])
    write(fd, WR_BITS, "B", original[1])
    write(fd, WR_SPEED, "I", original[2])
    os.close(fd)
raise SystemExit(0)
PY
