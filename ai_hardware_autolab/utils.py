"""UTC timestamps and streamed file digests used by the platform."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
