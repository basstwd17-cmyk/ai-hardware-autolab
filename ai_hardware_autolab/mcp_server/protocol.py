"""MCP over stdio, with no third-party dependency.

The stdio transport is newline-delimited JSON-RPC 2.0: one complete JSON
object per line, requests carry an ``id``, notifications do not. That is small
enough to implement directly, and doing so keeps this package installable
anywhere the rest of ``hwtools`` runs -- no pydantic, no anyio, no async
runtime. The same reasoning that keeps a framework out of layer 3 keeps one
out of here.

Everything in this module is transport and dispatch. What the tools *are*
lives in ``registry.py``, so the tool layer stays testable without speaking a
protocol.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, TextIO

# Versions this server understands. The client's version is echoed back when
# we know it; otherwise we answer with our newest and let the client decide.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(RuntimeError):
    """A tool refused or failed.

    Raised by handlers and reported as ``isError`` on the tool result rather
    than as a JSON-RPC error: the model is supposed to read it and react, not
    treat it as a broken connection.
    """


class DevicePreparationError(ToolError):
    """Host validation failed before hardware dispatch; Builder may correct input."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    touches_hardware: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass(frozen=True)
class Prompt:
    name: str
    description: str
    loader: Callable[[dict[str, Any]], str]
    arguments: tuple[dict[str, Any], ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "arguments": list(self.arguments),
        }


@dataclass(frozen=True)
class Resource:
    uri: str
    name: str
    description: str
    mime_type: str
    loader: Callable[[], str]

    def describe(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
        }


@dataclass
class Server:
    name: str
    version: str
    tools: dict[str, Tool] = field(default_factory=dict)
    prompts: dict[str, Prompt] = field(default_factory=dict)
    resources: dict[str, Resource] = field(default_factory=dict)
    instructions: str | None = None

    # -- registration --------------------------------------------------------

    def add_tools(self, tools: Iterable[Tool]) -> None:
        for tool in tools:
            self.tools[tool.name] = tool

    def add_prompts(self, prompts: Iterable[Prompt]) -> None:
        for prompt in prompts:
            self.prompts[prompt.name] = prompt

    def add_resources(self, resources: Iterable[Resource]) -> None:
        for resource in resources:
            self.resources[resource.uri] = resource

    # -- dispatch ------------------------------------------------------------

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one JSON-RPC message. Returns None for notifications."""
        message_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        is_notification = "id" not in message

        if not isinstance(method, str):
            if is_notification:
                return None
            return _error(message_id, INVALID_REQUEST, "Missing method")

        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method in ("notifications/initialized", "notifications/cancelled"):
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [tool.describe() for tool in self.tools.values()]}
            elif method == "tools/call":
                result = self._call_tool(params)
            elif method == "prompts/list":
                result = {"prompts": [p.describe() for p in self.prompts.values()]}
            elif method == "prompts/get":
                result = self._get_prompt(params)
            elif method == "resources/list":
                result = {"resources": [r.describe() for r in self.resources.values()]}
            elif method == "resources/read":
                result = self._read_resource(params)
            else:
                if is_notification:
                    return None
                return _error(message_id, METHOD_NOT_FOUND, f"Unknown method: {method}")
        except ToolError as exc:
            if is_notification:
                return None
            return _error(message_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # never let a traceback kill the connection
            if is_notification:
                return None
            return _error(
                message_id,
                INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
                data={"traceback": traceback.format_exc(limit=4)},
            )

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    # -- individual methods --------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else DEFAULT_PROTOCOL_VERSION
        )
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "prompts": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
            },
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or name not in self.tools:
            raise ToolError(f"Unknown tool: {name!r}")
        if not isinstance(arguments, dict):
            raise ToolError("arguments must be an object")
        tool = self.tools[name]
        try:
            payload = tool.handler(arguments)
            is_error = False
        except ToolError as exc:
            payload = {"error": str(exc), "tool": name}
            is_error = True
        except Exception as exc:
            payload = {
                "error": f"{type(exc).__name__}: {exc}",
                "tool": name,
                "traceback": traceback.format_exc(limit=4),
            }
            is_error = True
        return {"content": [_text(payload)], "isError": is_error}

    def _get_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or name not in self.prompts:
            raise ToolError(f"Unknown prompt: {name!r}")
        prompt = self.prompts[name]
        text = prompt.loader(params.get("arguments") or {})
        return {
            "description": prompt.description,
            "messages": [
                {"role": "user", "content": {"type": "text", "text": text}}
            ],
        }

    def _read_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or uri not in self.resources:
            raise ToolError(f"Unknown resource: {uri!r}")
        resource = self.resources[uri]
        return {
            "contents": [
                {
                    "uri": resource.uri,
                    "mimeType": resource.mime_type,
                    "text": resource.loader(),
                }
            ]
        }

    # -- transport -----------------------------------------------------------

    def serve(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
        """Read newline-delimited JSON from stdin until EOF.

        Nothing but protocol messages may reach stdout, so every diagnostic in
        this package goes to stderr. A single stray print would corrupt the
        stream and the client would report the server as broken.
        """
        source = stdin if stdin is not None else sys.stdin
        sink = stdout if stdout is not None else sys.stdout
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                _write(sink, _error(None, PARSE_ERROR, f"Invalid JSON: {exc}"))
                continue
            if isinstance(message, list):  # batch
                responses = [r for r in (self.handle(m) for m in message) if r]
                for response in responses:
                    _write(sink, response)
                continue
            if not isinstance(message, dict):
                _write(sink, _error(None, INVALID_REQUEST, "Message must be an object"))
                continue
            response = self.handle(message)
            if response is not None:
                _write(sink, response)
        return 0


def _text(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        return {"type": "text", "text": payload}
    return {
        "type": "text",
        "text": json.dumps(payload, indent=2, ensure_ascii=False, default=str),
    }


def _error(
    message_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


def _write(sink: TextIO, message: dict[str, Any]) -> None:
    sink.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
    sink.flush()
