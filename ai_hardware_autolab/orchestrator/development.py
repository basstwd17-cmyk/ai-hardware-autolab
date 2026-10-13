"""Workspace identity and paths excluded from source edits."""
from typing import Any
MODE = "autonomous-v1"
PROTECTED = (".git", ".codex", ".agents", "ai_hardware_autolab/runs", "ai_hardware_autolab/workspaces", "ai_hardware_autolab/webui/state")

def autonomous(resources: Any) -> bool:
    return isinstance(resources, dict) and resources.get("development_mode") == MODE

def protected_path(path: str) -> bool:
    path = path.replace("\\", "/").casefold().strip("/")
    return any(path == prefix.casefold() or path.startswith(prefix.casefold() + "/")
               for prefix in PROTECTED) or ".git" in path.split("/")
