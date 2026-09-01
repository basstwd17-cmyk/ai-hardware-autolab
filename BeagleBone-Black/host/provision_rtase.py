#!/usr/bin/env python3
"""Provision the BBB at 192.168.50.2 for the RTASE_V1 RF1 demonstration.

This is intentionally separate from provision.py: it installs the additional
P9_31 PRU output required by RTASE without changing the verified generic
P8_11/P8_12/P8_16 overlay workflow.  RTASE also wires channel-B signals to
P8_43..P8_46, so both stock HDMI audio and video overlays must be disabled
before the daughterboard is attached.
"""

from __future__ import annotations

import os
import sys
import time

from provision import connect, die, put_tree, run, wait_ssh


HOST = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BB_HOST", "192.168.50.2")
PW = os.environ.get("BB_PW", "xilinx")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DTBO = "BB-RTASE-A-00A0.dtbo"
FW_NAME = "am335x-pru0-rtase.out"


def step(message: str) -> None:
    print(f"\n=== {message} ===")


def main() -> int:
    print(f"RTASE_V1 BBB provisioner -> {HOST}")
    client = connect(host=HOST)

    step("1/7 verify exact target")
    _, model, _ = run(client, "tr -d '\\000' </proc/device-tree/model")
    print("  model:", model.strip())
    if "AM335x BeagleBone Black" not in model:
        die(f"target is not an AM335x BeagleBone Black: {model.strip()!r}")

    step("2/7 upload firmware, RTASE overlay, and RTASE CLI")
    run(client, "mkdir -p ~/pru/pru0_pulse_io ~/pru/overlay ~/pru")
    sftp = client.open_sftp()
    home = sftp.normalize(".")
    put_tree(sftp, os.path.join(REPO, "firmware"), home + "/pru/pru0_pulse_io")
    sftp.put(
        os.path.join(REPO, "overlay", "BB-RTASE-A-00A0.dts"),
        home + "/pru/overlay/BB-RTASE-A-00A0.dts",
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

    step("3/7 build PRU firmware on the BBB")
    rc, out, err = run(
        client,
        "cd ~/pru/pru0_pulse_io && make clean >/dev/null 2>&1; "
        "PRU_CGT=/usr/share/ti/cgt-pru make 2>&1; "
        "test -f gen/pru0_pulse_io.out && echo BUILD_OK",
    )
    print("  " + out.strip().replace("\n", "\n  "))
    if rc or "BUILD_OK" not in out:
        die("PRU firmware build failed:\n" + err + "\n" + out)

    step("4/7 build and install RTASE pinmux overlay")
    rc, _, _ = run(client, "command -v dtc")
    if rc:
        die("dtc is missing on the BBB (install device-tree-compiler).")
    _, old_hash, _ = run(
        client,
        f"sha256sum /lib/firmware/{DTBO} 2>/dev/null | cut -d' ' -f1 || true",
    )
    rc, out, err = run(
        client,
        f"cd ~/pru/overlay && dtc -I dts -O dtb -@ -o {DTBO} BB-RTASE-A-00A0.dts && "
        f"echo \"$BB_PW\" | sudo -S cp {DTBO} /lib/firmware/{DTBO} && "
        f"sha256sum /lib/firmware/{DTBO} | cut -d' ' -f1",
    )
    if rc:
        die("overlay build/install failed:\n" + err)
    new_hash = out.strip().splitlines()[-1]
    overlay_changed = old_hash.strip() != new_hash
    print(f"  overlay sha256: {new_hash} (changed={overlay_changed})")

    step("5/7 install firmware and configure boot overlay")
    rc, _, err = run(
        client,
        f'echo "$BB_PW" | sudo -S cp "$HOME/pru/pru0_pulse_io/gen/pru0_pulse_io.out" '
        f"/lib/firmware/{FW_NAME}",
    )
    if rc:
        die("firmware install failed:\n" + err)
    rc, _, err = run(
        client,
        'echo "$BB_PW" | sudo -S install -m 0755 "$HOME/pru/rtase-pru-service.sh" '
        '/usr/local/sbin/rtase-pru-service && '
        'echo "$BB_PW" | sudo -S install -m 0644 "$HOME/pru/rtase-pru.service" '
        '/etc/systemd/system/rtase-pru.service && '
        'echo "$BB_PW" | sudo -S systemctl daemon-reload && '
        'echo "$BB_PW" | sudo -S systemctl enable rtase-pru.service',
    )
    if rc:
        die("RTASE PRU boot service installation failed:\n" + err)
    _, configured, _ = run(
        client, f"grep -c '^uboot_overlay_pru={DTBO}$' /boot/uEnv.txt 2>/dev/null || true"
    )
    _, audio_disabled, _ = run(
        client, "grep -c '^disable_uboot_overlay_audio=1$' /boot/uEnv.txt 2>/dev/null || true"
    )
    _, video_disabled, _ = run(
        client, "grep -c '^disable_uboot_overlay_video=1$' /boot/uEnv.txt 2>/dev/null || true"
    )
    needs_reboot = (
        configured.strip() != "1"
        or audio_disabled.strip() != "1"
        or video_disabled.strip() != "1"
        or overlay_changed
    )
    if configured.strip() != "1":
        run(client, 'echo "$BB_PW" | sudo -S cp /boot/uEnv.txt /boot/uEnv.txt.bak-rtase')
        run(client, 'echo "$BB_PW" | sudo -S sed -i "/^#*uboot_overlay_pru=/d" /boot/uEnv.txt')
        run(
            client,
            f'echo "$BB_PW" | sudo -S sh -c "echo uboot_overlay_pru={DTBO} >> /boot/uEnv.txt"',
        )
        run(
            client,
            'grep -q "^enable_uboot_overlays=1" /boot/uEnv.txt || '
            'echo "$BB_PW" | sudo -S sh -c "echo enable_uboot_overlays=1 >> /boot/uEnv.txt"',
        )
    if audio_disabled.strip() != "1":
        # P9_31 is mcasp0_aclkx in the stock HDMI-audio overlay, but RTASE
        # needs that pad as PRU0 R30.0. Disable only the audio virtual cape;
        # HDMI video and eMMC remain untouched.
        run(
            client,
            'echo "$BB_PW" | sudo -S sed -i "/^#*disable_uboot_overlay_audio=/d" /boot/uEnv.txt',
        )
        run(
            client,
            'echo "$BB_PW" | sudo -S sh -c "echo disable_uboot_overlay_audio=1 >> /boot/uEnv.txt"',
        )
        print("  disabled HDMI audio overlay (releases P9_31)")
    if video_disabled.strip() != "1":
        # RTASE channel B is physically wired to P8_43..P8_46.  Even though
        # this channel-A demo does not drive those BBB pins, the stock HDMI
        # video cape must release them before the daughterboard is connected.
        run(
            client,
            'echo "$BB_PW" | sudo -S sed -i "/^#*disable_uboot_overlay_video=/d" /boot/uEnv.txt',
        )
        run(
            client,
            'echo "$BB_PW" | sudo -S sh -c "echo disable_uboot_overlay_video=1 >> /boot/uEnv.txt"',
        )
        print("  disabled HDMI video overlay (releases P8_43..P8_46)")

    step("6/7 apply pinmux and start PRU firmware")
    if needs_reboot:
        print("  rebooting BBB to apply P9_31 PRU pinmux")
        try:
            run(client, 'echo "$BB_PW" | sudo -S systemctl reboot', timeout=15)
        except Exception:
            pass
        client.close()
        wait_ssh(HOST, up=False, deadline_s=60)
        if not wait_ssh(HOST, up=True, deadline_s=180):
            die("BBB did not return after reboot.")
        client = None
        for _ in range(8):
            time.sleep(3)
            try:
                client = connect(host=HOST)
                break
            except Exception:
                client = None
        if client is None:
            die("BBB SSH did not become ready after reboot.")

    if not needs_reboot:
        rc, _, err = run(
            client,
            'echo "$BB_PW" | sudo -S systemctl restart rtase-pru.service',
            timeout=150,
        )
        if rc:
            die("RTASE PRU boot service restart failed:\n" + err)
    rc, service_state, err = run(
        client,
        "for i in $(seq 1 130); do "
        "systemctl is-active --quiet rtase-pru.service && break; sleep 1; done; "
        "systemctl is-active rtase-pru.service",
        timeout=150,
    )
    if rc:
        _, journal, _ = run(
            client,
            'echo "$BB_PW" | sudo -S journalctl -u rtase-pru.service -n 80 --no-pager',
        )
        die("RTASE PRU boot service failed:\n" + err + "\n" + journal)
    print(f"  rtase-pru.service: {service_state.strip()}")

    step("7/7 verify pinmux and RTASE command protocol")
    _, pins, _ = run(
        client,
        'echo "$BB_PW" | sudo -S grep -E "pin (12 |13 |100 )" '
        "/sys/kernel/debug/pinctrl/44e10800.pinmux-pinctrl-single/pins 2>/dev/null",
    )
    print("  pinmux:\n  " + pins.strip().replace("\n", "\n  "))
    rc, status, err = run(
        client,
        "ls -l /dev/spidev0.0 2>/dev/null || true; "
        "python3 ~/pru/rtase.py --local status",
    )
    print("  " + status.strip().replace("\n", "\n  "))
    client.close()
    if rc or '"ok": true' not in status.lower():
        die("RTASE firmware status failed:\n" + err)
    print("\nPROVISION OK")
    print(f"Next: BB_HOST={HOST} python host/rtase.py demo --pattern ramp --shots 100")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
