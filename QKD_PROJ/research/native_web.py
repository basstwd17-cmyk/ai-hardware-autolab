"""Codex native-web events bridged to immutable, page-cited PDF evidence.

The installed Codex App Server can expose the native Responses ``web_search``
tool.  Search is still only discovery: snippets and URLs are untrusted until a
researcher selects a result and this module downloads, hashes and ingests the
PDF into :class:`ReferenceStore`.

The bridge is deliberately per turn.  A URL can be collected only when it was
present in a completed ``webSearch`` item observed in that same researcher
turn.  That prevents a model from using the collection tool as an arbitrary
URL fetcher.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .providers import FetchedDocument, ResearchProviderError, SearchResult
from .references import ReferenceError, ReferenceStore
from .tools import ResearchToolError, ResearchToolPolicy, ResearchTools


PROVIDER_NAME = "codex-native-web"
MAX_REDIRECTS = 5
USER_AGENT = "QKD-Agent-Platform-Research/0.1"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class HttpsPdfProvider:
    """Fetch a captured HTTPS result with bounded redirects and bytes.

    Search itself is supplied by App Server.  ``search`` therefore refuses;
    only ``fetch`` is meaningful on this provider.  Every hop is checked for a
    public DNS destination before urllib opens it.  Proxies are disabled so a
    system proxy cannot silently reinterpret a public URL as an internal
    request.
    """

    name = PROVIDER_NAME

    def search(self, query: str, *, limit: int):  # pragma: no cover - contract refusal
        raise ResearchProviderError("native App Server search arrives as protocol events")

    def fetch(
        self,
        result: SearchResult,
        destination: Path,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> FetchedDocument:
        if result.provider != self.name:
            raise ResearchProviderError("result belongs to a different provider")
        current = result.url
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        for _hop in range(MAX_REDIRECTS + 1):
            self._require_public_https(current)
            request = Request(
                current,
                headers={"Accept": "application/pdf", "User-Agent": USER_AGENT},
                method="GET",
            )
            try:
                response = opener.open(request, timeout=timeout_seconds)
            except HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    location = exc.headers.get("Location")
                    if not location:
                        raise ResearchProviderError("PDF redirect has no Location") from exc
                    current = urljoin(current, location)
                    continue
                raise ResearchProviderError(f"PDF server returned HTTP {exc.code}") from exc
            except (OSError, URLError) as exc:
                raise ResearchProviderError(f"PDF download failed: {exc}") from exc

            with response:
                final_url = response.geturl()
                self._require_public_https(final_url)
                media_type = response.headers.get_content_type().lower()
                if media_type != "application/pdf":
                    raise ResearchProviderError(
                        f"selected source is {media_type!r}, not application/pdf"
                    )
                declared = response.headers.get("Content-Length")
                if declared:
                    try:
                        if int(declared) > max_bytes:
                            raise ResearchProviderError("PDF exceeds the configured byte limit")
                    except ValueError as exc:
                        raise ResearchProviderError("PDF has an invalid Content-Length") from exc

                destination = Path(destination)
                total = 0
                prefix = b""
                with destination.open("wb") as handle:
                    while True:
                        chunk = response.read(min(64 * 1024, max_bytes - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise ResearchProviderError("PDF exceeds the configured byte limit")
                        if len(prefix) < 5:
                            prefix += chunk[: 5 - len(prefix)]
                        handle.write(chunk)
                if total == 0 or prefix != b"%PDF-":
                    raise ResearchProviderError("downloaded bytes do not have a PDF signature")
                return FetchedDocument(destination, final_url, media_type, result.title)
        raise ResearchProviderError(f"PDF redirected more than {MAX_REDIRECTS} times")

    @staticmethod
    def _require_public_https(value: str) -> None:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ResearchProviderError("source must be credential-free HTTPS on port 443")
        hostname = parsed.hostname.rstrip(".")
        if hostname.lower() == "localhost" or hostname.lower().endswith(".local"):
            raise ResearchProviderError("source host is not public")
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
            }
        except OSError as exc:
            raise ResearchProviderError(f"source host could not be resolved: {exc}") from exc
        if not addresses or any(not address.is_global for address in addresses):
            raise ResearchProviderError("source host resolves outside the public Internet")


@dataclass
class NativeWebResearchSession:
    """One researcher turn's observed searches and evidence tools."""

    store: ReferenceStore
    policy: ResearchToolPolicy = field(default_factory=ResearchToolPolicy)
    provider: HttpsPdfProvider = field(default_factory=HttpsPdfProvider)
    _by_url: dict[str, SearchResult] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.tools = ResearchTools(self.provider, self.store, policy=self.policy)

    def observe(self, message: dict[str, Any]) -> None:
        """Accept only completed native web-search items from App Server."""
        if message.get("method") != "item/completed":
            return
        params = message.get("params")
        item = params.get("item") if isinstance(params, dict) else None
        if not isinstance(item, dict) or item.get("type") != "webSearch":
            return
        raw_results = item.get("results")
        query = item.get("query")
        if not isinstance(raw_results, list) or not isinstance(query, str) or not query.strip():
            return
        captured: list[SearchResult] = []
        for raw in raw_results:
            if not isinstance(raw, dict) or raw.get("type") != "text_result":
                continue
            title, url = raw.get("title"), raw.get("url")
            if not isinstance(title, str) or not title.strip() or not isinstance(url, str):
                continue
            result = SearchResult.create(
                provider=self.provider.name,
                title=title.strip(),
                url=url,
                snippet=str(raw.get("snippet") or ""),
                media_type=None,
            )
            captured.append(result)
        if not captured:
            return
        normalized = self.tools.register_search_results(
            query,
            "native Codex web search selected by the researcher",
            captured,
        )
        for result in normalized:
            self._by_url[str(result["url"])] = next(
                candidate for candidate in captured if candidate.url == result["url"]
            )

    def tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": "collect_source",
                "description": (
                    "Download and immutably ingest one PDF URL that appeared in this turn's "
                    "native web-search results. Returns its content SHA-256 document id."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "search_reference",
                "description": "Search extracted page text in one collected PDF.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["document_id", "query"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "read_reference_page",
                "description": "Read one page of extracted text from a collected PDF.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string"},
                        "page": {"type": "integer", "minimum": 1},
                    },
                    "required": ["document_id", "page"],
                    "additionalProperties": False,
                },
            },
        ]

    def handle_dynamic_call(self, params: dict[str, Any]) -> dict[str, Any]:
        tool, arguments = params.get("tool"), params.get("arguments")
        try:
            if not isinstance(arguments, dict):
                raise ResearchToolError("dynamic tool arguments must be an object")
            if tool == "collect_source":
                if set(arguments) != {"url"} or not isinstance(arguments["url"], str):
                    raise ResearchToolError("collect_source requires exactly one string URL")
                try:
                    result = self._by_url[arguments["url"]]
                except KeyError as exc:
                    raise ResearchToolError(
                        "URL was not present in this turn's completed native web-search results"
                    ) from exc
                payload = self.tools.collect_source(result.result_id)
            elif tool == "search_reference":
                if not {"document_id", "query"} <= set(arguments):
                    raise ResearchToolError("search_reference needs document_id and query")
                payload = self.tools.search_reference(
                    arguments["document_id"], arguments["query"], limit=arguments.get("limit", 10)
                )
            elif tool == "read_reference_page":
                if set(arguments) != {"document_id", "page"}:
                    raise ResearchToolError("read_reference_page needs document_id and page")
                payload = self.tools.read_reference_page(arguments["document_id"], arguments["page"])
            else:
                raise ResearchToolError(f"unknown researcher tool {tool!r}")
            return self._response(True, payload)
        except (ResearchToolError, ReferenceError, ResearchProviderError, TypeError) as exc:
            return self._response(False, {"error": str(exc)})

    def validate_packet_documents(self, packet: dict[str, Any]) -> None:
        for document in packet.get("documents", []):
            if not isinstance(document, dict) or not isinstance(document.get("document_id"), str):
                raise ResearchToolError("SourcePacket has a malformed document entry")
            self.store.get(document["document_id"])
        for claim in packet.get("claims", []):
            if not isinstance(claim, dict):
                continue
            for citation in claim.get("citations", []):
                if not isinstance(citation, dict):
                    continue
                pages = self.store.pages(citation.get("document_id", ""))
                page = citation.get("page")
                if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= len(pages):
                    raise ResearchToolError("SourcePacket citation page is outside its PDF")

    @staticmethod
    def _response(success: bool, payload: Any) -> dict[str, Any]:
        return {
            "success": success,
            "contentItems": [
                {
                    "type": "inputText",
                    "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                }
            ],
        }
