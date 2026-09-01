#!/usr/bin/env python3
"""Safely stage or activate the Gate12 combined GPIO-SPI + PRU overlay.

Default behavior is PREPARE ONLY: upload/build/install the DTBO and report the
current boot configuration. It does not edit /boot/uEnv.txt and does not reboot.

Activation requires both --activate and the literal confirmation token. Reboot
is a separate flag so the recovery copy can be inspected first.
"""

from __future__ import annotations

import argparse
import os
import sys

from provision import connect, die, run


DTBO = "BB-RTASE-A-GPIO-SPI-PRU-00A0.dtbo"
DTS = "BB-RTASE-A-GPIO-SPI-PRU-00A0.dts"
CONFIRM_TOKEN = "APPLY_GATE12_OVERLAY"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("host", nargs="?", default=os.environ.get("BB_HOST", "192.168.50.2"))
    p.add_argument("--activate", action="store_true",
                   help="back up and replace the active uboot_overlay_pru line")
    p.add_argument("--confirm", default="",
                   help=f"required with --activate: {CONFIRM_TOKEN}")
    p.add_argument("--reboot", action="store_true",
                   help="reboot after activation (never implied)")
    return p


def main() -> int:
    args = parser().parse_args()
    if args.reboot and not args.activate:
        die("--reboot requires --activate.")
    if args.activate and args.confirm != CONFIRM_TOKEN:
        die(f"--activate requires --confirm {CONFIRM_TOKEN}")

    client = connect(host=args.host)
    try:
        rc, model, err = run(client, "tr -d '\\000' </proc/device-tree/model")
        if rc or "AM335x BeagleBone Black" not in model:
            die(f"unexpected target: {model.strip()!r}\n{err}")

        run(client, "mkdir -p ~/pru/overlay")
        sftp = client.open_sftp()
        home = sftp.normalize(".")
        sftp.put(
            os.path.join(REPO, "overlay", DTS),
            home + "/pru/overlay/" + DTS,
        )
        sftp.put(os.path.join(REPO, "host", "rtase.py"), home + "/pru/rtase.py")
        sftp.put(os.path.join(REPO, "host", "rtase_spi.py"), home + "/pru/rtase_spi.py")
        sftp.put(
            os.path.join(REPO, "scripts", "rtase-pru-service.sh"),
            home + "/pru/rtase-pru-service.sh",
        )
        sftp.put(
            os.path.join(REPO, "systemd", "rtase-pru.service"),
            home + "/pru/rtase-pru.service",
        )
        sftp.close()

        rc, out, err = run(
            client,
            f"cd ~/pru/overlay && "
            f"dtc -I dts -O dtb -@ -o {DTBO} {DTS} && "
            f"echo \"$BB_PW\" | sudo -S install -m 0644 {DTBO} /lib/firmware/{DTBO} && "
            f"sha256sum /lib/firmware/{DTBO}",
        )
        if rc:
            die("Gate12 overlay build/install failed:\n" + err + "\n" + out)
        print("PREPARED:", out.strip())
        _, current, _ = run(
            client,
            "grep -E '^(enable_uboot_overlays|uboot_overlay_pru|"
            "disable_uboot_overlay_(audio|video))=' /boot/uEnv.txt || true",
        )
        print("CURRENT BOOT LINES:\n" + current.strip())

        if not args.activate:
            print("HOST/SERVICE FILES STAGED IN ~/pru")
            print("NO BOOT FILE CHANGE; NO REBOOT")
            return 0

        # Preserve an SD-card-readable recovery copy before editing the active
        # file. The edit is idempotent and leaves exactly one active line for
        # each required setting.
        command = (
            'echo "$BB_PW" | sudo -S install -m 0755 '
            '"$HOME/pru/rtase-pru-service.sh" /usr/local/sbin/rtase-pru-service && '
            'echo "$BB_PW" | sudo -S install -m 0644 '
            '"$HOME/pru/rtase-pru.service" /etc/systemd/system/rtase-pru.service && '
            'echo "$BB_PW" | sudo -S systemctl daemon-reload && '
            'echo "$BB_PW" | sudo -S systemctl enable rtase-pru.service && '
            'echo "$BB_PW" | sudo -S cp /boot/uEnv.txt '
            '/boot/uEnv.txt.pre-gate12 && '
            'echo "$BB_PW" | sudo -S sed -i '
            '"/^#*uboot_overlay_pru=/d;'
            '/^#*disable_uboot_overlay_audio=/d;'
            '/^#*disable_uboot_overlay_video=/d" /boot/uEnv.txt && '
            'echo "$BB_PW" | sudo -S sh -c "'
            f'printf \'%s\\n\' \'uboot_overlay_pru={DTBO}\' '
            "\'disable_uboot_overlay_audio=1\' "
            "\'disable_uboot_overlay_video=1\' >> /boot/uEnv.txt"
            '"'
        )
        rc, _, err = run(client, command)
        if rc:
            die("uEnv activation failed; recovery copy is /boot/uEnv.txt.pre-gate12:\n" + err)
        _, active, _ = run(
            client,
            "grep -E '^(uboot_overlay_pru|disable_uboot_overlay_(audio|video))=' "
            "/boot/uEnv.txt",
        )
        print("ACTIVATED; RECOVERY: /boot/uEnv.txt.pre-gate12")
        print(active.strip())

        if args.reboot:
            print("REBOOT REQUESTED")
            try:
                run(client, 'echo "$BB_PW" | sudo -S systemctl reboot', timeout=15)
            except Exception:
                pass
        else:
            print("NO REBOOT; inspect the recovery copy before rebooting")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
