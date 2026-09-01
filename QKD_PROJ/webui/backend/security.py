"""Why a loopback server still needs a token, and what it checks.

The first version of the plan said a loopback-only server owned by the same
logon as the daemon's pipe does not need authentication. That was wrong, and
wrong in the direction that matters here, because the door it leaves open ends
at ``approval.decide``.

``127.0.0.1`` is not a private address from a browser's point of view. Any page
the user has open can reach it, and three separate mechanisms let it try:

**A cross-origin POST fires before it is refused.** CORS governs whether the
*response* may be read, not whether the *request* runs. A ``fetch`` with
``Content-Type: text/plain`` is a "simple request": no preflight, the body
arrives, the handler runs. An attacker who cannot read a single byte of the
answer can still have caused a job to start. So a mutating route cannot rely on
the browser refusing to show the reply.

**A WebSocket is not subject to CORS at all.** ``new WebSocket('ws://127.0.0.1:8787/ws')``
from any origin connects, and the browser sends an ``Origin`` header the server
is expected to check itself. Nothing checks it for us.

**DNS rebinding walks past an origin check.** A domain the attacker controls
can resolve to ``127.0.0.1`` on its second lookup, after which their page's
origin *is* their domain and the request is same-origin as far as the browser
is concerned. The header that still tells the truth is ``Host``: it carries the
name the client dialled, and a rebound request carries the attacker's hostname,
not ``127.0.0.1``.

So there are three checks, and they cover different attacks rather than
repeating one:

| Check | Stops |
| --- | --- |
| bearer token | a cross-origin request that fires but cannot read the reply |
| ``Origin`` allowlist | a WebSocket opened from another site |
| ``Host`` allowlist | DNS rebinding, which defeats the origin check |

The token is required for **everything**, not only the mutating routes. Splitting
read from write would mean deciding, for every future endpoint, which side it
falls on -- and ``GET /api/jobs/{id}`` already returns a job's whole history.
One rule is easier to keep than a taxonomy.

An absent ``Origin`` is allowed. Non-browser clients (curl, the launcher, the
tests) do not send one, and a browser page cannot suppress it, so absence is
not something an attacker can arrange.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

# Hostnames a client may have dialled. Anything else means the request reached
# us under a name that is not this machine's loopback -- which is what a
# rebound DNS record looks like from in here.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# Where the token is left for a launcher (and for the user) to find. Same
# trust boundary as the ledger: a file only this logon's processes read.
DEFAULT_SESSION_FILE = Path(__file__).resolve().parents[1] / "state" / "session.json"

_BEARER = "bearer "


class Unauthorized(Exception):
    """A request that did not carry the session token."""


class Forbidden(Exception):
    """A request that carried a Host or Origin this server will not serve."""


@dataclass
class Session:
    """One run's token, and the file that tells a launcher what it is.

    The token is 32 bytes of ``secrets`` output, generated per run and never
    persisted across restarts. A restart invalidating every open tab is the
    right trade: the tab reloads, and a token that outlives the process it
    authorised is a token nobody remembers is still valid.
    """

    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    file: Path | None = None

    @classmethod
    def start(cls, file: Path | None = DEFAULT_SESSION_FILE, port: int | None = None) -> "Session":
        session = cls(file=file)
        if file is not None:
            session.write(port)
        return session

    def write(self, port: int | None = None) -> None:
        if self.file is None:
            return
        self.file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "token": self.token, "pid": os.getpid(), "port": port}
        temporary = self.file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        # Atomic on Windows too: os.replace is MoveFileEx with REPLACE_EXISTING.
        os.replace(temporary, self.file)

    def clear(self) -> None:
        if self.file is not None and self.file.exists():
            self.file.unlink()

    def matches(self, presented: str | None) -> bool:
        """Constant-time, so a wrong token cannot be narrowed by timing it."""
        if not presented:
            return False
        return secrets.compare_digest(presented, self.token)

    def url(self, host: str, port: int, path: str = "/") -> str:
        return f"http://{host}:{port}{path}?token={self.token}"


@dataclass
class Policy:
    """What this server will answer, and to whom."""

    session: Session
    allowed_origins: frozenset[str] = frozenset()
    allowed_hosts: frozenset[str] = LOOPBACK_HOSTS

    @classmethod
    def build(
        cls,
        session: Session,
        port: int,
        extra_origins: Iterable[str] = (),
        public_origins: Iterable[str] = (),
    ) -> "Policy":
        origins = {f"http://{host}:{port}" for host in ("127.0.0.1", "localhost")}
        origins |= {o.rstrip("/") for o in extra_origins if o}
        hosts = set(LOOPBACK_HOSTS)
        for value in public_origins:
            if not value:
                continue
            origin, hostname = _public_origin(value)
            origins.add(origin)
            hosts.add(hostname)
        return cls(
            session=session,
            allowed_origins=frozenset(origins),
            allowed_hosts=frozenset(hosts),
        )

    # -- the three checks --------------------------------------------------

    def check_host(self, host_header: str | None) -> None:
        if host_header is None:
            return  # HTTP/1.0 and the ASGI test transport; no name was dialled
        hostname = _hostname(host_header)
        if hostname not in self.allowed_hosts:
            raise Forbidden(
                f"Host {host_header!r} is not configured for this server. Allowed names: "
                f"{sorted(self.allowed_hosts)} (loopback plus explicitly configured HTTPS "
                "proxy hosts). An unexpected name is what DNS rebinding "
                "looks like from here."
            )

    def check_origin(self, origin: str | None) -> None:
        if origin is None:
            return  # curl, the launcher, the tests. A browser page cannot omit it.
        if origin == "null" or origin.rstrip("/") not in self.allowed_origins:
            raise Forbidden(
                f"origin {origin!r} is not allowed. Allowed: "
                f"{sorted(self.allowed_origins)}"
            )

    def check_token(self, headers: dict[str, str], query_token: str | None) -> None:
        presented = _bearer(headers.get("authorization")) or headers.get("x-qkd-token")
        if presented is None:
            presented = query_token
        if not self.session.matches(presented):
            raise Unauthorized(
                "this endpoint needs the session token, as a bearer header or ?token=. "
                "It is printed at startup and written to webui/state/session.json."
            )

    def check(self, headers: dict[str, str], query_token: str | None) -> None:
        self.check_host(headers.get("host"))
        self.check_origin(headers.get("origin"))
        self.check_token(headers, query_token)


def _hostname(host_header: str) -> str:
    """The name half of a Host header, IPv6 brackets and port removed."""
    parsed = urlsplit(f"//{host_header}")
    return (parsed.hostname or host_header).lower()


def _public_origin(value: str) -> tuple[str, str]:
    """Validate one exact HTTPS reverse-proxy origin and return its host."""
    candidate = value.rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError(
            "public origins must be exact HTTPS origins without a path, query, or credentials"
        )
    return candidate, parsed.hostname.lower()


def _bearer(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith(_BEARER):
        return None
    return authorization[len(_BEARER) :].strip() or None


def headers_of(scope: dict[str, Any]) -> dict[str, str]:
    """ASGI raw headers as a lowercase dict. Last value wins, as HTTP says."""
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def query_token_of(scope: dict[str, Any]) -> str | None:
    """``?token=`` from a raw ASGI scope.

    A browser cannot set a header on a WebSocket handshake, so the token has to
    ride in the query string for ``/ws``. That puts it in this process's logs
    if it ever logs URLs, which is why the access log is off by default in
    ``app.main``.
    """
    from urllib.parse import parse_qs

    values = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("token")
    return values[0] if values else None


class Guard:
    """Pure ASGI middleware, because a WebSocket never reaches an HTTP one.

    Starlette's ``BaseHTTPMiddleware`` only sees ``http`` scopes. The route
    that most needs checking is a WebSocket, so the guard has to sit at the
    ASGI layer and handle both scope types itself.
    """

    def __init__(
        self, app: Any, policy: Policy, protected_prefixes: Iterable[str] = ("/api", "/ws")
    ) -> None:
        self.app = app
        self.policy = policy
        # Stated as what is *protected*, not what is open. A new route under
        # /api is guarded the day it is written; a list of exemptions would
        # have to be remembered.
        self.protected = tuple(protected_prefixes)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = headers_of(scope)
        path = scope.get("path", "")
        try:
            # Host is checked even for the static bundle: a probe that answers
            # a rebound name still tells an attacker this port is here and
            # what it is. The bundle itself is code, not data, so it does not
            # need the token -- the page reads the token out of its own URL and
            # every call it then makes is guarded.
            self.policy.check_host(headers.get("host"))
            if path.startswith(self.protected):
                self.policy.check_origin(headers.get("origin"))
                self.policy.check_token(headers, query_token_of(scope))
        except Unauthorized as exc:
            await self._refuse(scope, send, 401, str(exc))
            return
        except Forbidden as exc:
            await self._refuse(scope, send, 403, str(exc))
            return

        await self.app(scope, receive, send)

    async def _refuse(self, scope: dict[str, Any], send: Any, status: int, detail: str) -> None:
        if scope["type"] == "websocket":
            # Closing in answer to the connect message is how ASGI says
            # "rejected"; the browser sees the handshake fail.
            await send({"type": "websocket.close", "code": 1008, "reason": detail[:120]})
            return
        body = json.dumps({"ok": False, "detail": detail}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
