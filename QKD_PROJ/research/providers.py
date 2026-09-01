"""Replaceable web-search and document-fetch boundary.

No concrete internet provider is selected here.  The Codex App Server spike
must first establish whether the installed runtime exposes a usable search
surface.  A provider implementation is responsible for redirect, DNS/private
network, byte and time limits; `ResearchTools` independently restricts what an
agent may ask it to collect.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


class ResearchProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchResult:
    result_id: str
    provider: str
    title: str
    url: str
    snippet: str = ""
    media_type: str | None = None

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        title: str,
        url: str,
        snippet: str = "",
        media_type: str | None = None,
    ) -> "SearchResult":
        identity = hashlib.sha256(f"{provider}\0{url}".encode("utf-8")).hexdigest()
        return cls(identity, provider, title, url, snippet, media_type)

    def public_dict(self) -> dict[str, object]:
        return {
            "result_id": self.result_id,
            "provider": self.provider,
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "media_type": self.media_type,
            "untrusted": True,
        }


@dataclass(frozen=True)
class FetchedDocument:
    path: Path
    final_url: str
    media_type: str
    title: str


class ResearchProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int) -> Sequence[SearchResult]: ...

    def fetch(
        self,
        result: SearchResult,
        destination: Path,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> FetchedDocument: ...
