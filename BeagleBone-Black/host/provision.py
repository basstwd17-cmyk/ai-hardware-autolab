#!/usr/bin/env python3
"""provision.py - one-command bring-up of a FRESH BeagleBone Black for bbb_pulse_io.

Pushes firmware + overlay + host tools, builds and loads the PRU firmware, and
installs the U-Boot pinmux overlay. Idempotent (safe to re-run). Verifies the
target is an AM335x BeagleBone Black BEFORE touching anything - important because
the lab has other xilinx/xilinx boards on the same subnet.

Usage (from the repo root):
    python host/provision.py                # BB_HOST env or default beagle.local
    python host/provision.py 192.168.0.42   # explicit host
Env: BB_HOST (default beagle.local), BB_USER (xilinx), BB_PW (xilinx).
Requires: pip install paramiko   (see requirements.txt)

After provisioning, day-to-day use is via host/bb.py + scripts/ (see README).
"""
import os
import sys
import stat
import time
import socket
import paramiko

HOST = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BB_HOST", "beagle.local")
USER = os.environ.get("BB_USER", "xilinx")
PW   = os.environ.get("BB_PW", "xilinx")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FW_NAME = "am335x-pru0-pulseio.out"
DTBO    = "BB-PRUOUT-P8-00A0.dtbo"


def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"


def connect(host=HOST, timeout=12):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=USER, password=PW, timeout=timeout,
              allow_agent=False, look_for_keys=False,
              banner_timeout=timeout, auth_timeout=timeout)
    return c


def run(c, script, timeout=600):
    """Run a bash script on the board; BB_PW is exported so scripts can sudo -S."""
    full = "export BB_PW=%s\n%s\nexit\n" % (_q(PW), script)
    stdin, out, err = c.exec_command("bash -l", timeout=timeout)
    stdin.write(full)
    stdin.flush()
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    return out.channel.recv_exit_status(), o, e


def put_tree(sftp, local, remote):
    """Recursively upload a file or directory."""
    if os.path.isdir(local):
        try:
            sftp.mkdir(remote)
        except IOError:
            pass
        for name in sorted(os.listdir(local)):
            put_tree(sftp, os.path.join(local, name), remote + "/" + name)
    else:
        sftp.put(local, remote)


def step(msg):
    print("\n=== %s ===" % msg)


def die(msg):
    print("\nABORT: " + msg)
    sys.exit(1)


def wait_ssh(host, up=True, deadline_s=180):
    """Poll TCP 22 until reachable (up=True) or unreachable (up=False)."""
    end = time.time() + deadline_s
    while time.time() < end:
        ok = False
        try:
            s = socket.create_connection((host, 22), timeout=3)
            s.close(); ok = True
        except Exception:
            ok = False
        if ok == up:
            return True
        time.sleep(3)
    return False


def main():
    print("bbb_pulse_io provisioner -> %s@%s" % (USER, HOST))

    step("1/8  connect + verify target is a BeagleBone Black")
    c = connect()
    rc, model, _ = run(c, "cat /proc/device-tree/model 2>/dev/null | tr -d '\\000'")
    print("  model: %s" % model.strip())
    if "AM335x BeagleBone Black" not in model:
        die("target is not an AM335x BeagleBone Black (got %r). Set BB_HOST to the BBB."
            % model.strip())

    step("2/8  ensure PRU toolchain (clpru)")
    # clpru may be on PATH (/usr/bin/clpru) or only at the CGT install the Makefile
    # drives via PRU_CGT (/usr/share/ti/cgt-pru/bin) - accept either.
    probe = ("command -v clpru 2>/dev/null || "
             "{ test -x /usr/share/ti/cgt-pru/bin/clpru && echo /usr/share/ti/cgt-pru/bin/clpru; } "
             "|| echo MISSING")
    rc, out, _ = run(c, probe)
    if "MISSING" in out:
        print("  clpru missing -> apt-get update + install ti-pru-cgt-v2.3")
        run(c, 'echo "$BB_PW" | sudo -S apt-get update 2>&1 | tail -1')
        run(c, 'echo "$BB_PW" | sudo -S apt-get -y install ti-pru-cgt-v2.3 2>&1 | tail -3')
        rc, out, _ = run(c, probe)
        if "MISSING" in out:
            die("clpru still missing - install TI PRU CGT (ti-pru-cgt-v2.3) manually.")
    print("  clpru: %s" % out.strip().splitlines()[-1])

    step("3/8  push firmware + overlay + host tools")
    run(c, "mkdir -p ~/pru/pru0_pulse_io ~/pru/overlay ~/pru/timing_verify")
    sftp = c.open_sftp()
    home = sftp.normalize(".")
    put_tree(sftp, os.path.join(REPO, "firmware"), home + "/pru/pru0_pulse_io")
    put_tree(sftp, os.path.join(REPO, "overlay", "BB-PRUOUT-P8-00A0.dts"),
             home + "/pru/overlay/BB-PRUOUT-P8-00A0.dts")
    # AWG timebase-verification tools are optional (archived under
    # archive_pulse_io_verification/); push them only if present in host/.
    pushed_awg = False
    for f in ("awg.py", "run_awg_bbb.py"):
        local = os.path.join(REPO, "host", f)
        if os.path.exists(local):
            sftp.put(local, home + "/pru/timing_verify/" + f)
            pushed_awg = True
    sftp.close()
    print("  pushed firmware/ (with vendored pru-support/), overlay%s"
          % (", awg host tools" if pushed_awg else ""))

    step("4/8  build PRU firmware (offline, vendored support)")
    rc, out, err = run(c, "cd ~/pru/pru0_pulse_io && make clean >/dev/null 2>&1; "
                          "PRU_CGT=/usr/share/ti/cgt-pru make 2>&1 | tail -4; "
                          "ls gen/pru0_pulse_io.out")
    print("  " + out.strip().replace("\n", "\n  "))
    if "pru0_pulse_io.out" not in out:
        die("firmware build failed:\n" + err)

    step("5/8  build + install pinmux overlay (dtbo)")
    rc, dtc_out, _ = run(c, "command -v dtc || echo MISSING")
    if "MISSING" in dtc_out:
        die("dtc not found - run: sudo apt-get install device-tree-compiler")
    run(c, "cd ~/pru/overlay && dtc -I dts -O dtb -@ -o %s BB-PRUOUT-P8-00A0.dts 2>&1" % DTBO)
    # $HOME is expanded by the (xilinx) login shell BEFORE sudo; a bare ~ inside
    # `sudo sh -c` would expand to /root and the copy would silently fail.
    run(c, 'echo "$BB_PW" | sudo -S cp "$HOME/pru/overlay/%s" /lib/firmware/%s' % (DTBO, DTBO))
    rc, out, _ = run(c, "ls -l /lib/firmware/%s 2>/dev/null || echo MISSING" % DTBO)
    if "MISSING" in out:
        die("overlay dtbo not installed to /lib/firmware (dtc build or cp failed).")
    print("  " + out.strip())

    step("6/8  configure U-Boot overlay in /boot/uEnv.txt")
    # Exactly one active line, no matter how many #uboot_overlay_pru= examples exist:
    # delete every (commented or active) uboot_overlay_pru line, then append ours.
    rc, cur, _ = run(c, "grep -c '^uboot_overlay_pru=%s$' /boot/uEnv.txt 2>/dev/null || true" % DTBO)
    rc2, dupe, _ = run(c, "grep -c '^uboot_overlay_pru=' /boot/uEnv.txt 2>/dev/null || true")
    needs_reboot = not (cur.strip() == "1" and dupe.strip() == "1")
    if needs_reboot:
        run(c, 'echo "$BB_PW" | sudo -S cp /boot/uEnv.txt /boot/uEnv.txt.bak-pulseio 2>/dev/null')
        run(c, 'echo "$BB_PW" | sudo -S sed -i "/^#*uboot_overlay_pru=/d" /boot/uEnv.txt')
        run(c, 'echo "$BB_PW" | sudo -S sh -c "echo uboot_overlay_pru=%s >> /boot/uEnv.txt"' % DTBO)
        run(c, 'grep -q "^enable_uboot_overlays=1" /boot/uEnv.txt || '
               'echo "$BB_PW" | sudo -S sh -c "echo enable_uboot_overlays=1 >> /boot/uEnv.txt"')
        print("  set uboot_overlay_pru=%s (reboot required to apply pinmux)" % DTBO)
    else:
        print("  overlay already configured")

    step("7/8  deploy + (re)load firmware via remoteproc")
    run(c, 'echo "$BB_PW" | sudo -S cp '
           '"$HOME/pru/pru0_pulse_io/gen/pru0_pulse_io.out" /lib/firmware/%s' % FW_NAME)
    rc, chk, _ = run(c, "test -f /lib/firmware/%s && echo OK || echo MISSING" % FW_NAME)
    if "OK" not in chk:
        die("firmware .out not installed to /lib/firmware (cp failed).")
    if needs_reboot:
        print("  rebooting to apply the pinmux overlay ...")
        try:
            run(c, 'echo "$BB_PW" | sudo -S systemctl reboot', timeout=15)
        except Exception:
            pass  # the reboot drops our SSH channel; expected
        try:
            c.close()
        except Exception:
            pass
        if not wait_ssh(HOST, up=False, deadline_s=60):
            print("  (warning: board did not drop; continuing)")
        if not wait_ssh(HOST, up=True, deadline_s=180):
            die("board did not come back after reboot - reconnect and re-run provision.py")
        # TCP :22 opening precedes sshd auth-readiness on a cold boot; retry connect.
        c = None
        for _ in range(6):
            time.sleep(4)
            try:
                c = connect(); break
            except Exception:
                c = None
        if c is None:
            die("board is back on the LAN but SSH is not ready yet - re-run provision.py")
    RP = "/sys/class/remoteproc/remoteproc1"
    run(c, 'echo "$BB_PW" | sudo -S sh -c "echo stop > %s/state" 2>/dev/null; sleep 0.3; '
           'echo "$BB_PW" | sudo -S sh -c "echo -n %s > %s/firmware"; '
           'echo "$BB_PW" | sudo -S sh -c "echo start > %s/state"; '
           'for i in $(seq 1 25); do [ -e /dev/rpmsg0 ] && break; sleep 0.2; done; '
           'echo "$BB_PW" | sudo -S chmod 666 /dev/rpmsg0' % (RP, FW_NAME, RP, RP))

    step("8/8  verify pinmux + firmware")
    rc, pins, _ = run(c, "echo \"$BB_PW\" | sudo -S grep -E 'pin 12 |pin 13 |pin 14 ' "
                         "/sys/kernel/debug/pinctrl/44e10800.pinmux-pinctrl-single/pins 2>/dev/null")
    print("  pinmux:\n  " + (pins.strip() or "(pins not claimed - overlay may need a reboot)").replace("\n", "\n  "))
    rc, ver, _ = run(c, "python3 - <<'PY'\n"
                        "import os,select\n"
                        "fd=os.open('/dev/rpmsg0',os.O_RDWR)\n"
                        "os.write(fd,bytes([0x03]))\n"
                        "r,_,_=select.select([fd],[],[],3)\n"
                        "print((os.read(fd,512) if r else b'')[1:].decode('ascii','replace'))\n"
                        "os.close(fd)\nPY")
    v = ver.strip()
    print("  firmware VERSION: %s" % (v or "(no reply)"))
    c.close()
    if v.startswith("bbb_pulse_io"):
        print("\nPROVISION OK - board is in service. Try: BB_HOST=%s py host/bb.py scripts/demo.sh" % HOST)
    else:
        die("firmware did not respond to PING/VERSION - check dmesg / remoteproc state.")


if __name__ == "__main__":
    main()
