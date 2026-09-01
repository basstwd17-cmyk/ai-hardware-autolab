"""Recording SSH transport for the BeagleBone Black.

The fixed half of board-host control, mirroring ``scpi.py``: connect, send,
read back, hash, record. It does not know what a deploy or a diagnostic is --
those live in ``bbb.py`` on top of this.

Two rules shape this module.

**Everything that crosses the wire is recorded.** A remote command whose
output went to a console and was lost is not evidence, and the BBB is the one
part of the rig whose state a later reader cannot re-derive from files on this
machine.

**The password never enters the record.** Remote scripts need ``BB_PW`` to
drive ``sudo -S``, so the secret really is present in the text sent to the
board. That means redaction has to happen here, in the one place every byte
passes through, rather than in each caller that might forget.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

REDACTED = "***BB_PW***"

# No default host. bb.py defaults to 192.168.0.4, provision.py to
# beagle.local and provision_rtase.py to 192.168.50.2 -- three different
# boards. A tool that picks one of those on its own can reboot hardware
# somebody else is using, so this layer refuses instead of guessing.
REQUIRED_ENV = ("BB_HOST", "BB_USER", "BB_PW")


class SshError(RuntimeError):
    pass


@dataclass(frozen=True)
class SshTarget:
    host: str
    user: str
    password: str = field(repr=False)
    port: int = 22

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SshTarget":
        source = os.environ if env is None else env
        missing = [name for name in REQUIRED_ENV if not (source.get(name) or "").strip()]
        if missing:
            raise SshError(
                "Missing BBB connection environment: "
                + ", ".join(missing)
                + ". Set them for the board this session is allowed to touch; "
                "there is no default host."
            )
        port_text = (source.get("BB_PORT") or "22").strip()
        try:
            port = int(port_text)
        except ValueError as exc:
            raise SshError(f"BB_PORT is not a number: {port_text!r}") from exc
        return cls(
            host=source["BB_HOST"].strip(),
            user=source["BB_USER"].strip(),
            password=source["BB_PW"],
            port=port,
        )

    def describe(self) -> dict[str, Any]:
        """Identity of the target, without the secret."""
        return {"host": self.host, "user": self.user, "port": self.port}


@dataclass
class SshExchange:
    """One command or one upload, as it will appear in the run record."""

    at_s: float
    kind: str  # "exec" | "put"
    label: str
    command: str  # redacted, safe to store and to print
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    sha256: str | None = None
    size_bytes: int | None = None
    remote: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "at_s": self.at_s,
            "kind": self.kind,
            "label": self.label,
            "command": self.command,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_s": self.duration_s,
        }
        if self.sha256 is not None:
            data["sha256"] = self.sha256
            data["size_bytes"] = self.size_bytes
            data["remote"] = self.remote
        return data


class SshClient(Protocol):
    """The three operations a transport needs from a connection.

    Tests supply a fake implementing this, so nothing else in layer 3 needs to
    know whether paramiko is installed or whether a board is on the bench.
    """

    def exec_script(self, script: str, timeout_s: float) -> tuple[int, str, str]: ...

    def put_file(self, local: Path, remote: str) -> None: ...

    def close(self) -> None: ...


class ParamikoClient:
    """Real SSH. paramiko is imported here so offline tests never need it."""

    def __init__(self, target: SshTarget, timeout_s: float = 15.0) -> None:
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise SshError(
                "paramiko is required for live BBB access: pip install paramiko"
            ) from exc
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                target.host,
                port=target.port,
                username=target.user,
                password=target.password,
                timeout=timeout_s,
                allow_agent=False,
                look_for_keys=False,
                banner_timeout=timeout_s,
                auth_timeout=timeout_s,
            )
        except Exception as exc:
            raise SshError(
                f"SSH connection to {target.user}@{target.host}:{target.port} failed: {exc}"
            ) from exc
        self._client = client
        self._sftp: Any = None

    def exec_script(self, script: str, timeout_s: float) -> tuple[int, str, str]:
        stdin, stdout, stderr = self._client.exec_command("bash -l", timeout=timeout_s)
        stdin.write(script.replace("\r\n", "\n") + "\nexit\n")
        stdin.flush()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        return stdout.channel.recv_exit_status(), out, err

    def put_file(self, local: Path, remote: str) -> None:
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        self._sftp.put(str(local), remote)

    def close(self) -> None:
        try:
            if self._sftp is not None:
                self._sftp.close()
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SshTransport:
    """Recording SSH session against one target.

    ``client`` is injectable: a fake client makes every tool in ``bbb.py``
    testable with no board attached, which is the only way this code can be
    written while the bench is occupied by somebody else's debugging session.
    """

    def __init__(
        self,
        target: SshTarget,
        client: SshClient | None = None,
        *,
        secrets: Iterable[str] = (),
        connect_timeout_s: float = 15.0,
    ) -> None:
        self.target = target
        self.transcript: list[SshExchange] = []
        self._connect_timeout_s = connect_timeout_s
        self._client = client
        self._owns_client = client is None
        self._t0 = time.monotonic()
        candidates = [target.password, shlex.quote(target.password), *secrets]
        # Longest first: redacting "pw" before "pw123" would leave "***123"
        # in the record. Very short secrets are not redacted at all -- masking
        # every occurrence of a two-character string would corrupt the log
        # without hiding anything an attacker could not guess anyway.
        self._secrets = sorted(
            {value for value in candidates if value and len(value) >= 3},
            key=len,
            reverse=True,
        )

    # -- redaction -----------------------------------------------------------

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    # -- connection ----------------------------------------------------------

    def connect(self) -> SshClient:
        if self._client is None:
            self._client = ParamikoClient(self.target, timeout_s=self._connect_timeout_s)
            self._owns_client = True
        return self._client

    # -- primitives ----------------------------------------------------------

    def run(self, script: str, *, label: str, timeout_s: float = 180.0) -> SshExchange:
        """Run a bash script on the board with ``BB_PW`` exported, as bb.py does."""
        client = self.connect()
        body = script.replace("\r\n", "\n")
        full = f"export BB_PW={shlex.quote(self.target.password)}\n{body}\n"
        started = time.monotonic()
        try:
            returncode, stdout, stderr = client.exec_script(full, timeout_s)
        except SshError:
            raise
        except Exception as exc:
            raise SshError(f"Remote command '{label}' failed to execute: {exc}") from exc
        exchange = SshExchange(
            at_s=round(started - self._t0, 4),
            kind="exec",
            label=label,
            command=self.redact(body.strip()),
            returncode=int(returncode),
            stdout=self.redact(stdout),
            stderr=self.redact(stderr),
            duration_s=round(time.monotonic() - started, 3),
        )
        self.transcript.append(exchange)
        return exchange

    def put(self, local: Path, remote: str, *, label: str | None = None) -> SshExchange:
        """Upload one file and pin it by the hash of exactly what was sent."""
        local = Path(local)
        if not local.is_file():
            raise SshError(f"Upload source does not exist: {local}")
        payload = local.read_bytes()
        client = self.connect()
        started = time.monotonic()
        try:
            client.put_file(local, remote)
        except SshError:
            raise
        except Exception as exc:
            raise SshError(f"Upload of {local.name} to {remote} failed: {exc}") from exc
        exchange = SshExchange(
            at_s=round(started - self._t0, 4),
            kind="put",
            label=label or local.name,
            command=f"sftp put {local.as_posix()} -> {remote}",
            returncode=0,
            duration_s=round(time.monotonic() - started, 3),
            sha256=sha256_bytes(payload),
            size_bytes=len(payload),
            remote=remote,
        )
        self.transcript.append(exchange)
        return exchange

    # -- record --------------------------------------------------------------

    def commands(self) -> list[str]:
        """Redacted command lines, for ToolResult.commands."""
        return [exchange.command for exchange in self.transcript]

    def to_dict(self) -> list[dict[str, Any]]:
        return [exchange.to_dict() for exchange in self.transcript]

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> "SshTransport":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
