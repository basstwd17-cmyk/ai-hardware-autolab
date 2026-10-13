"""User-owned Codex model choices, frozen in a job's Brief (not in a Plan).

The catalogue is metadata from the daemon's CLI, never an agent turn. No auth
files are read and no model name/effort combination is guessed by the UI.
"""
from __future__ import annotations

import copy
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

ROLES = ("default", "lead-agent", "builder", "verifier")
_VALUE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}\Z")


def normalize_selection(value: Any) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - set(ROLES):
        raise ValueError("modelSelection must contain only default, lead-agent, builder, verifier")
    result = {}
    for role, choice in value.items():
        if not isinstance(choice, dict) or set(choice) - {"model", "effort"}:
            raise ValueError(f"{role}: expected model and optional effort only")
        model = choice.get("model")
        effort = choice.get("effort")
        if not isinstance(model, str) or not _VALUE.fullmatch(model):
            raise ValueError(f"{role}: a model id is required")
        if effort is not None and (not isinstance(effort, str) or not _VALUE.fullmatch(effort)):
            raise ValueError(f"{role}: invalid reasoning effort")
        result[role] = {"model": model, **({"effort": effort} if effort is not None else {})}
    return result


def selection_for(resources: Any, agent: str) -> dict[str, str]:
    selection = normalize_selection((resources or {}).get("model_selection"))
    # A role override is a complete pair: never inherit another model's effort.
    return selection.get(agent, selection.get("default", {})) if agent in ROLES[1:] else {}


def load_codex_models() -> list[dict[str, Any]]:
    # Lazy import avoids coupling control/ledger startup to the runtime adapter.
    from .codex_transport import StdioAppServer, _find_codex, PROJECT_ROOT

    with StdioAppServer(
        command=(_find_codex(), "app-server", "--stdio"), cwd=PROJECT_ROOT,
        timeout_seconds=8,
        client_info={"name": "qkd_model_catalog", "version": "0.1.0"},
    ) as client:
        return client.list_models()


@dataclass
class ModelCatalog:
    loader: Callable[[], list[dict[str, Any]]] = load_codex_models
    ttl_seconds: float = 300
    _models: list[dict[str, Any]] | None = field(default=None, init=False)
    _expires: float = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def models(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._models is None or time.monotonic() >= self._expires:
                try:
                    rows = self.loader()
                    models = []
                    for row in rows:
                        if row.get("hidden") is True:
                            continue
                        model = row["model"]
                        normalize_selection({"default": {"model": model}})
                        efforts = [item["reasoningEffort"] for item in row.get("supportedReasoningEfforts", [])]
                        if any(not isinstance(e, str) or not _VALUE.fullmatch(e) for e in efforts):
                            raise ValueError("invalid model effort metadata")
                        default = row.get("defaultReasoningEffort")
                        if default is not None and default not in efforts:
                            raise ValueError("default effort not supported")
                        models.append({"model": model, "displayName": str(row.get("displayName") or model),
                                       "defaultEffort": default, "efforts": efforts,
                                       "isDefault": row.get("isDefault") is True})
                    if not models or len({m["model"] for m in models}) != len(models):
                        raise ValueError("empty or duplicate model list")
                except Exception as exc:
                    # Do not return subprocess stderr/config/account details to a browser.
                    raise ValueError("Codex 모델 목록을 읽지 못했습니다. 데몬 PC의 CLI 로그인·연결을 확인한 뒤 다시 시도하세요.") from exc
                self._models = models
                self._expires = time.monotonic() + self.ttl_seconds
            return copy.deepcopy(self._models)

    def validate(self, value: Any) -> dict[str, dict[str, str]]:
        selection = normalize_selection(value)
        if not selection:
            return {}  # The old/default path must not depend on catalogue availability.
        models = {m["model"]: m for m in self.models()}
        for role, choice in selection.items():
            model = models.get(choice["model"])
            if model is None:
                raise ValueError(f"{role}: 모델 목록에 없는 모델입니다: {choice['model']}")
            effort = choice.get("effort", model["defaultEffort"])
            if effort is not None:
                if effort not in model["efforts"]:
                    raise ValueError(f"{role}: {choice['model']} does not support effort {effort}")
                choice["effort"] = effort
        return selection
