#!/bin/sh
# Start/stop the RTASE PRU0 firmware from systemd.
set -eu

FIRMWARE=am335x-pru0-rtase.out

find_pru0() {
    for rp in /sys/class/remoteproc/remoteproc*; do
        [ -r "$rp/name" ] || continue
        case "$(cat "$rp/name")" in
            *4a334000.pru*|*pru0*) printf '%s\n' "$rp"; return 0 ;;
        esac
    done
    return 1
}

wait_for_pru0() {
    count=0
    while [ "$count" -lt 90 ]; do
        if rp=$(find_pru0); then
            printf '%s\n' "$rp"
            return 0
        fi
        count=$((count + 1))
        sleep 1
    done
    echo "RTASE PRU0 remoteproc did not appear within 90 seconds" >&2
    return 1
}

stop_pru() {
    rp=$1
    if [ "$(cat "$rp/state" 2>/dev/null || true)" = running ]; then
        printf '%s\n' stop >"$rp/state"
        sleep 1
    fi
}

case "${1:-start}" in
    start)
        test -r "/lib/firmware/$FIRMWARE"
        rp=$(wait_for_pru0)
        stop_pru "$rp"
        printf '%s' "$FIRMWARE" >"$rp/firmware"
        printf '%s\n' start >"$rp/state"

        count=0
        while [ "$count" -lt 30 ] && [ ! -e /dev/rpmsg0 ]; do
            count=$((count + 1))
            sleep 1
        done
        test -e /dev/rpmsg0
        chmod 0666 /dev/rpmsg0
        : >/run/lock/rtase-control.lock
        chmod 0666 /run/lock/rtase-control.lock
        [ ! -e /dev/spidev0.0 ] || chmod 0666 /dev/spidev0.0
        ;;
    stop)
        if rp=$(find_pru0); then
            stop_pru "$rp"
        fi
        ;;
    *)
        echo "usage: $0 {start|stop}" >&2
        exit 2
        ;;
esac
