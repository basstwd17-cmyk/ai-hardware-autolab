#!/usr/bin/env python3
"""bb.py - reusable SSH runner for the BeagleBone Black at 192.168.0.4.

Usage:
  py bb.py "remote one-liner"        # run a command string
  py bb.py path\\to\\script.sh         # run a script file (CRLF-safe)
  echo "cmd" | py bb.py               # read remote script from stdin
  py bb.py --put LOCAL REMOTE         # upload a file (sftp)
  py bb.py --get REMOTE LOCAL         # download a file (sftp)

Connection from env (defaults are the lab board): BB_HOST/BB_USER/BB_PW.
Remote sudo: scripts can use:  echo "$BB_PW" | sudo -S <cmd>   (PW exported on remote).
"""
import os, sys, shlex, paramiko
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOST = os.environ.get("BB_HOST", "192.168.0.4")
USER = os.environ.get("BB_USER", "xilinx")
PW   = os.environ.get("BB_PW",   "xilinx")


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PW, timeout=12,
              allow_agent=False, look_for_keys=False, banner_timeout=12, auth_timeout=12)
    return c


def run(script, timeout=180):
    script = script.replace("\r\n", "\n")
    c = connect()
    # export BB_PW on the remote so scripts can drive sudo -S non-interactively
    full = "export BB_PW=%s\n%s" % (shlex.quote(PW), script)
    stdin, out, err = c.exec_command("bash -l", get_pty=False, timeout=timeout)
    stdin.write(full + "\nexit\n")
    stdin.flush()
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    c.close()
    sys.stdout.write(o)
    if e.strip():
        sys.stderr.write("\n--- stderr ---\n" + e)
    return rc


def sftp_put(local, remote):
    c = connect(); s = c.open_sftp(); s.put(local, remote); s.close(); c.close()
    print("PUT %s -> %s OK" % (local, remote))


def sftp_get(remote, local):
    c = connect(); s = c.open_sftp(); s.get(remote, local); s.close(); c.close()
    print("GET %s -> %s OK" % (remote, local))


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "--put":
        sftp_put(a[1], a[2]); sys.exit(0)
    if a and a[0] == "--get":
        sftp_get(a[1], a[2]); sys.exit(0)
    if not a:
        sys.exit(run(sys.stdin.read()))
    if os.path.exists(a[0]):
        sys.exit(run(open(a[0], "r", encoding="utf-8").read()))
    sys.exit(run(" ".join(a)))
