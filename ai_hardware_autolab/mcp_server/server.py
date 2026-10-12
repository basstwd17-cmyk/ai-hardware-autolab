"""Entry point: expose the RTASE_V1 hardware tools over MCP stdio.

    py -3 -m ai_hardware_autolab.mcp_server                    # offline tools only
    py -3 -m ai_hardware_autolab.mcp_server --allow-hardware   # adds JTAG, SRAM, scope, BBB

``--allow-hardware`` is the one place a person decides that this session may
drive the board, which is where the architecture already puts that decision.
Without it the board-touching tools are not in the tool list at all, so an
agent connected to this server cannot reach them by any prompt.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `py -3 ai_hardware_autolab/mcp_server/server.py` as well as `-m`.
if __package__ in (None, ""):  # pragma: no cover - direct-script convenience
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ai_hardware_autolab.mcp_server.protocol import Server
    from ai_hardware_autolab.mcp_server.registry import (
        CONTRACTS_DIR,
        INSTRUCTIONS,
        ROLE_TOOLS,
        RUNS_DIR,
        ServerPolicy,
        build_prompts,
        build_resources,
        build_tools,
    )
else:
    from .protocol import Server
    from .registry import (
        CONTRACTS_DIR,
        INSTRUCTIONS,
        ROLE_TOOLS,
        RUNS_DIR,
        ServerPolicy,
        build_prompts,
        build_resources,
        build_tools,
    )

SERVER_NAME = "rtase-hwtools"
SERVER_VERSION = "0.1.0"


def build_server(policy: ServerPolicy) -> Server:
    server = Server(name=SERVER_NAME, version=SERVER_VERSION, instructions=INSTRUCTIONS)
    server.add_tools(build_tools(policy))
    server.add_prompts(build_prompts(policy))
    server.add_resources(build_resources(policy))
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rtase-hwtools", description=__doc__)
    parser.add_argument(
        "--allow-hardware",
        action="store_true",
        help="register the tools that drive the board. Off by default.",
    )
    parser.add_argument(
        "--role",
        choices=sorted(ROLE_TOOLS),
        default=None,
        help=(
            "narrow the tool list to one agent role. Composes with --allow-hardware: "
            "a role that lists board tools still gets none of them without the flag. "
            "Omit for the historical profile."
        ),
    )
    parser.add_argument("--contracts-dir", type=Path, default=CONTRACTS_DIR)
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--hardware-tool", default=None)
    parser.add_argument("--hardware-contract", default=None)
    parser.add_argument("--hardware-contract-sha256", default=None)
    parser.add_argument("--workspace-recipe", default=None)
    parser.add_argument("--workspace-recipe-sha256", default=None)
    parser.add_argument("--device-root", type=Path, default=None,
                        help="approved connection-map root, separate from mutable job source")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="code workspace for this job; evidence may still use a shared --runs-dir",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="print the tool list and exit, without serving. For checking a profile.",
    )
    args = parser.parse_args(argv)

    policy = ServerPolicy(
        allow_hardware=args.allow_hardware,
        contracts_dir=args.contracts_dir,
        runs_dir=args.runs_dir,
        project_root=args.project_root,
        device_root=args.device_root,
        role=args.role,
        hardware_tool=args.hardware_tool,
        hardware_contract=args.hardware_contract,
        hardware_contract_sha256=args.hardware_contract_sha256,
        workspace_recipe=args.workspace_recipe,
        workspace_recipe_sha256=args.workspace_recipe_sha256,
    )
    server = build_server(policy)

    if args.list_tools:
        for tool in server.tools.values():
            mark = "HW" if tool.touches_hardware else "  "
            print(f"[{mark}] {tool.name}")
        print(f"\nprompts: {', '.join(server.prompts) or '(none)'}")
        print(f"resources: {len(server.resources)}")
        print(f"allow_hardware: {policy.allow_hardware}")
        print(f"role: {policy.role or '(none)'}")
        print(f"run_bbb effects: {', '.join(sorted(policy.allowed_effects()))}")
        return 0

    # stdout carries protocol frames only; everything human-readable goes to
    # stderr. One stray print on stdout corrupts the stream.
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stdin.reconfigure(encoding="utf-8")
    print(
        f"{SERVER_NAME} {SERVER_VERSION} ready: "
        f"{len(server.tools)} tools, allow_hardware={policy.allow_hardware}",
        file=sys.stderr,
        flush=True,
    )
    return server.serve()


if __name__ == "__main__":
    raise SystemExit(main())
