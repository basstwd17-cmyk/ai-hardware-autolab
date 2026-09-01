"""MCP server: the same layer 3 tools, reachable from any runtime.

The point of this package is that the agent above it is replaceable. Claude
Code, Codex and anything else that speaks MCP call identical tools with
identical contracts and identical run records, so swapping the model is a
configuration change rather than a rewrite.

    protocol.py  MCP over stdio, dependency-free
    registry.py  which tools exist, and which of them a person has unlocked
    server.py    entry point
"""

from .protocol import Prompt, Resource, Server, Tool, ToolError
from .registry import ServerPolicy, build_prompts, build_resources, build_tools

__all__ = [
    "Prompt",
    "Resource",
    "Server",
    "ServerPolicy",
    "Tool",
    "ToolError",
    "build_prompts",
    "build_resources",
    "build_tools",
]
