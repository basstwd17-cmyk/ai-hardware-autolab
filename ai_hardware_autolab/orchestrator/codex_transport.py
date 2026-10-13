"""Codex App Server transport for model discovery and scenario turns."""
from __future__ import annotations
import json, os, queue, shutil, subprocess, threading, time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
PROJECT_ROOT = Path(__file__).resolve().parents[2]
EventSink = Callable[[dict[str, Any]], None]

class CodexRunnerError(RuntimeError):
    """The runtime did not produce a trustworthy handoff."""

class CodexProtocolError(CodexRunnerError):
    """The App Server stream violated the JSON-RPC/JSONL contract."""

class CodexTurnFailed(CodexRunnerError):
    """Codex completed a turn without a usable final answer."""

    def __init__(
        self,
        message: str,
        *,
        events: Sequence[dict[str, Any]] = (),
        final_text: str = "",
    ) -> None:
        super().__init__(message)
        self.events = tuple(dict(event) for event in events)
        self.final_text = final_text

class CodexApprovalRequired(CodexRunnerError):
    """The runtime requested authority this runner was not given."""

def _approval_command(params: dict[str, Any]) -> str:
    """Extract the proposed command from version-tolerant approval payloads.

    App Server has changed the nesting of command approval details across
    versions.  Unknown shapes fail closed instead of approving a command we
    could not classify.
    """

    keys = {"command", "cmd", "argv", "commandLine", "command_line"}
    pending: list[Any] = [params]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if key in keys:
                    if isinstance(child, str) and child.strip():
                        return child
                    if isinstance(child, list) and child:
                        return " ".join(str(part) for part in child)
                if isinstance(child, (dict, list)):
                    pending.append(child)
        elif isinstance(value, list):
            pending.extend(value)
    raise CodexApprovalRequired(
        "command approval payload did not expose the proposed command; refusing it"
    )

def _find_codex() -> str:
    names = ("codex.cmd", "codex.exe", "codex") if os.name == "nt" else ("codex",)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise CodexRunnerError("codex CLI is not on PATH")

class StdioAppServer:
    """Sequential JSON-RPC client over App Server's stdio JSONL transport."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: float,
        client_info: Mapping[str, str],
        event_sink: EventSink | None = None,
        experimental_api: bool = False,
    ) -> None:
        if not command:
            raise ValueError("App Server command may not be empty")
        self.command = tuple(command)
        self.cwd = Path(cwd)
        self.timeout_seconds = float(timeout_seconds)
        self.event_sink = event_sink
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._messages: queue.Queue[dict[str, Any] | BaseException | None] = queue.Queue()
        self._notifications: deque[dict[str, Any]] = deque()
        self._stderr: deque[str] = deque(maxlen=40)
        self._write_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active_turns: dict[str, str] = {}
        self._interrupt_ids: set[int] = set()
        self._closed = False
        self._process = subprocess.Popen(  # noqa: S603 - argv is a local daemon configuration
            list(self.command),
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            bufsize=1,
            shell=False,
        )
        assert self._process.stdout is not None and self._process.stderr is not None
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        try:
            initialize: dict[str, Any] = {"clientInfo": dict(client_info)}
            if experimental_api:
                initialize["capabilities"] = {"experimentalApi": True}
            self._request("initialize", initialize)
            self._notify("initialized", {})
        except Exception:
            self.close()
            raise

    def list_models(self) -> list[dict[str, Any]]:
        """Read available model metadata without creating a thread or spending a turn."""
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        deadline = time.monotonic() + self.timeout_seconds
        for _ in range(20):
            params: dict[str, Any] = {"limit": 100, "includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexProtocolError("model/list timed out")
            original_timeout = self.timeout_seconds
            try:
                self.timeout_seconds = remaining
                result = self._request("model/list", params)
            finally:
                self.timeout_seconds = original_timeout
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise CodexProtocolError("model/list returned no data list")
            rows.extend(result["data"])
            cursor = result.get("nextCursor")
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise CodexProtocolError("model/list returned an invalid pagination cursor")
            seen.add(cursor)
        raise CodexProtocolError("model/list exceeded the pagination limit")

    def start_thread(self, params: dict[str, Any]) -> str:
        result = self._request("thread/start", params)
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexProtocolError("thread/start returned no thread id")
        return thread_id

    def __enter__(self) -> "StdioAppServer":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def run_turn(
        self,
        thread_id: str,
        params: dict[str, Any],
        *,
        server_request: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> str:
        start = dict(params)
        start["threadId"] = thread_id
        result = self._request("turn/start", start, server_request=server_request)
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexProtocolError("turn/start returned no turn id")

        final_text: str | None = None
        deadline = time.monotonic() + self.timeout_seconds
        hard_deadline = time.monotonic() + max(self.timeout_seconds, 3600.0)
        with self._active_lock:
            self._active_turns[thread_id] = turn_id
        try:
            while True:
                message = self._next_message(min(deadline, hard_deadline))
                method = message.get("method")
                if isinstance(method, str) and "id" in message:
                    self._answer_server_request(message, server_request)
                    continue
                if not isinstance(method, str):
                    if self._consume_interrupt_response(message):
                        continue
                    raise CodexProtocolError(f"unexpected response while waiting for turn: {message}")
                self._emit(message)
                message_thread, message_turn = _message_context(message)
                if message_thread not in (None, thread_id) or message_turn not in (None, turn_id):
                    continue
                if method.startswith("item/") and message_thread == thread_id:
                    deadline = time.monotonic() + self.timeout_seconds
                if method == "item/completed":
                    item = (message.get("params") or {}).get("item")
                    text = _final_agent_text(item)
                    if text is not None:
                        final_text = text
                if method != "turn/completed":
                    continue
                completed = (message.get("params") or {}).get("turn")
                if not isinstance(completed, dict) or completed.get("id") != turn_id:
                    continue
                status = completed.get("status")
                if status != "completed":
                    error = completed.get("error")
                    detail = error.get("message") if isinstance(error, dict) else status
                    raise CodexTurnFailed(f"turn {turn_id} ended {status}: {detail}")
                for item in completed.get("items") or []:
                    text = _final_agent_text(item)
                    if text is not None:
                        final_text = text
                if final_text is None:
                    raise CodexTurnFailed(f"turn {turn_id} completed without a final agent message")
                return final_text
        finally:
            with self._active_lock:
                if self._active_turns.get(thread_id) == turn_id:
                    self._active_turns.pop(thread_id, None)

    def interrupt_turn(self, thread_id: str) -> bool:
        """Send the installed App Server's ``turn/interrupt`` request."""
        with self._active_lock:
            turn_id = self._active_turns.get(thread_id)
        if turn_id is None:
            return False
        request_id = self._request_id()
        with self._active_lock:
            self._interrupt_ids.add(request_id)
        try:
            self._send(
                {
                    "method": "turn/interrupt",
                    "id": request_id,
                    "params": {"threadId": thread_id, "turnId": turn_id},
                }
            )
        except Exception:
            with self._active_lock:
                self._interrupt_ids.discard(request_id)
            raise
        return True

    def unsubscribe(self, thread_id: str) -> None:
        self._request("thread/unsubscribe", {"threadId": thread_id})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except OSError:
                pass
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        server_request: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        request_id = self._request_id()
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            message = self._next_wire_message(deadline)
            message_method = message.get("method")
            if isinstance(message_method, str):
                if "id" in message:
                    if server_request is None:
                        raise CodexProtocolError(
                            f"server requested {message_method} while handling {method}"
                        )
                    self._answer_server_request(message, server_request)
                else:
                    self._notifications.append(message)
                continue
            if message.get("id") != request_id:
                if self._consume_interrupt_response(message):
                    continue
                raise CodexProtocolError(
                    f"response id {message.get('id')!r} arrived while waiting for {request_id}"
                )
            error = message.get("error")
            if isinstance(error, dict):
                raise CodexProtocolError(
                    f"{method} failed ({error.get('code')}): {error.get('message')}"
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexProtocolError(f"{method} returned a non-object result")
            return result

    def _request_id(self) -> int:
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id

    def _consume_interrupt_response(self, message: dict[str, Any]) -> bool:
        response_id = message.get("id")
        if not isinstance(response_id, int):
            return False
        with self._active_lock:
            if response_id not in self._interrupt_ids:
                return False
            self._interrupt_ids.remove(response_id)
            return True

    def _answer_server_request(
        self,
        message: dict[str, Any],
        handler: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            raise CodexProtocolError("malformed App Server request")
        try:
            result = handler(method, params)
        except Exception as exc:
            self._send(
                {
                    "id": message.get("id"),
                    "error": {"code": -32000, "message": str(exc)},
                }
            )
            raise
        self._send({"id": message.get("id"), "result": result})

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def _send(self, message: dict[str, Any]) -> None:
        if self._closed or self._process.stdin is None:
            raise CodexProtocolError("App Server is closed")
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                self._process.stdin.write(line + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise CodexProtocolError(self._exited_message("App Server stdin closed")) from exc

    def _next_message(self, deadline: float) -> dict[str, Any]:
        if self._notifications:
            return self._notifications.popleft()
        return self._next_wire_message(deadline)

    def _next_wire_message(self, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexProtocolError("timed out waiting for App Server")
        try:
            item = self._messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise CodexProtocolError("timed out waiting for App Server") from exc
        if item is None:
            raise CodexProtocolError(self._exited_message("App Server stdout closed"))
        if isinstance(item, BaseException):
            raise CodexProtocolError(f"invalid App Server output: {item}") from item
        return item

    def _emit(self, message: dict[str, Any]) -> None:
        if self.event_sink is not None:
            self.event_sink(message)

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        try:
            for raw in self._process.stdout:
                line = raw.strip()
                if not line:
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("JSONL frame is not an object")
                self._messages.put(value)
        except BaseException as exc:  # a reader failure must wake the caller
            self._messages.put(exc)
        finally:
            self._messages.put(None)

    def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr.append(line.rstrip())

    def _exited_message(self, prefix: str) -> str:
        code = self._process.poll()
        detail = " | ".join(self._stderr)
        suffix = f"; stderr: {detail}" if detail else ""
        return f"{prefix} (exit={code}){suffix}"

def _message_context(message: dict[str, Any]) -> tuple[str | None, str | None]:
    params = message.get("params")
    if not isinstance(params, dict):
        return None, None
    thread_id = params.get("threadId")
    turn_id = params.get("turnId")
    return (
        thread_id if isinstance(thread_id, str) else None,
        turn_id if isinstance(turn_id, str) else None,
    )

def _final_agent_text(item: Any) -> str | None:
    if not isinstance(item, dict) or item.get("type") != "agentMessage":
        return None
    phase = item.get("phase")
    if phase not in (None, "final_answer"):
        return None
    text = item.get("text")
    return text if isinstance(text, str) and text else None
