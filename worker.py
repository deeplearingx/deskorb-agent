# -*- coding: utf-8 -*-
"""Persistent Codex app-server backend for the floating overlay.

Keeping one local app-server process alive is important on networks where the
initial WebSocket connection falls back to HTTPS: the slow fallback happens
once, instead of once for every message.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from config import (API_BASE_URL, API_CONTEXT_RECENT_TURNS, API_CONTEXT_SUMMARY_TOKENS,
                    API_CONTEXT_TOKEN_BUDGET, API_MODEL, API_PROXY_URL, API_TIMEOUT,
                    CONNECTION_BACKEND, MODEL, MODEL_PROVIDER, PERMISSION_MODE, SYSTEM_APPEND, WORKING_DIR)
from agent_runtime import AgentRuntime
from conversation_context import ConversationContext
from credential_store import get_api_key
from debuglog import DEBUG_LOG, _UIQueueTap, dbg
from model_adapter import ModelAdapter, normalize_provider
from privacy_scope import requires_current_context_consent


_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_SERVICE_TIER = os.environ.get("DESKORB_AGENT_SERVICE_TIER", "fast").strip() or "fast"


class CodexWorker(threading.Thread):
    """Translate the persistent Codex app-server protocol into UI events."""

    def __init__(self, ui_queue: "queue.Queue", permission_mode: str | None = None,
                 backend: str | None = None, model: str | None = None,
                 api_base_url: str | None = None, api_proxy_url: str | None = None,
                 model_provider: str | None = None):
        super().__init__(daemon=True, name="deskorb-agent-worker")
        self.ui = _UIQueueTap(ui_queue) if DEBUG_LOG else ui_queue
        self.req: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._running = True
        self._session_id: str | None = None
        self._turn_id: str | None = None
        self._turn_thread_id: str | None = None
        self._model = self._normalize_model(model)
        self._backend = self._normalize_backend(backend or CONNECTION_BACKEND)
        self._api_base_url = self._normalize_api_base(api_base_url or API_BASE_URL)
        self._api_proxy_url = self._normalize_proxy(api_proxy_url if api_proxy_url is not None else API_PROXY_URL)
        self._model_provider = normalize_provider(model_provider or MODEL_PROVIDER)
        self._adapter = ModelAdapter(self._model_provider, self._api_base_url)
        self._api_base_url = self._adapter.profile.base_url
        self._api_context = ConversationContext(
            token_budget=API_CONTEXT_TOKEN_BUDGET,
            recent_turns=API_CONTEXT_RECENT_TURNS,
        )
        self._api_response = None
        self._api_lock = threading.Lock()
        self._permission_mode = permission_mode or PERMISSION_MODE
        self._agent = AgentRuntime(self.ui, self._model, self._api_base_url, self._api_proxy_url,
                                   full_access=self._permission_mode != "plan",
                                   model_provider=self._model_provider)
        self._proc: subprocess.Popen[str] | None = None
        self._proc_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._messages: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        self._next_request_id = 1
        self._interrupted = False
        self._tool_items_seen: set[str] = set()
        self._codex = self._find_codex()

    # -- UI-facing API -------------------------------------------------
    def ask(self, text: str, image_paths=None, current_context_consent: bool = False):
        payload = (text, list(image_paths or []))
        if current_context_consent:
            payload = (*payload, True)
        self.req.put(("ask", payload))

    def ask_ephemeral(self, text: str, image_paths=None, current_context_consent: bool = False):
        """Run one turn without adding its input to normal conversation memory."""
        payload = (text, list(image_paths or []))
        if current_context_consent:
            payload = (*payload, True)
        self.req.put(("ask_ephemeral", payload))

    def reset(self):
        self.req.put(("reset", None))

    def compact(self):
        self.req.put(("compact", None))

    def authorize_model_fallback(self, target_id: str, share_context: bool = False):
        """Queue an explicit user choice for the independent Agent runtime."""
        self.req.put(("authorize_model_fallback", {
            "target_id": str(target_id), "share_context": bool(share_context),
        }))

    def set_model(self, model: str):
        self.req.put(("set_model", str(model)))

    def configure_connection(self, backend: str, model: str, api_base_url: str,
                             api_proxy_url: str = "", model_provider: str = "auto"):
        self.req.put(("configure_connection", {
            "backend": backend, "model": model, "api_base_url": api_base_url,
            "api_proxy_url": api_proxy_url, "model_provider": model_provider,
        }))

    def set_permission_mode(self, mode: str):
        self.req.put(("set_permission_mode", str(mode)))

    def shutdown(self):
        self._running = False
        self.interrupt()
        self.req.put(("stop", None))

    def interrupt(self):
        self._interrupted = True
        with self._api_lock:
            api_response = self._api_response
        self._agent.interrupt()
        if api_response is not None:
            try:
                api_response.close()
                return
            except Exception:
                pass
        turn_id = self._turn_id
        if turn_id:
            try:
                self._send("turn/interrupt", {
                    "threadId": self._turn_thread_id or self._session_id, "turnId": turn_id,
                })
                return
            except Exception:
                pass
        with self._proc_lock:
            proc = self._proc
        if proc and proc.poll() is None:
            threading.Thread(target=self._terminate_process_tree, args=(proc,), daemon=True).start()

    # -- lifecycle -----------------------------------------------------
    def run(self):
        dbg("worker_start", {"backend": self._backend, "model": self._model})
        if not self._codex and self._resolved_backend() == "codex":
            self.ui.put(("error", "Codex CLI was not found. Install or update it with: npm install -g @openai/codex"))
        else:
            self.ui.put(("ready", None))
            self.ui.put(("model", self._model))
            self.ui.put(("backend", self._backend_status()))
            self.ui.put(("permission_mode", self._permission_mode))
            for message in self._startup_diagnostics():
                self.ui.put(("diagnostic", message))

        while self._running:
            kind, payload = self.req.get()
            if kind == "stop":
                break
            try:
                if kind == "ask":
                    self._run_turn(*payload)
                elif kind == "ask_ephemeral":
                    self._run_turn(*payload, ephemeral=True)
                elif kind == "reset":
                    self._session_id = None
                    self._api_context.clear()
                    self._agent.reset()
                    self._tool_items_seen.clear()
                    self.ui.put(("reset_done", None))
                elif kind == "compact":
                    self._compact()
                elif kind == "authorize_model_fallback":
                    payload = payload if isinstance(payload, dict) else {}
                    result = self._agent.authorize_model_fallback(
                        payload.get("target_id", ""), user_confirmed=True,
                        share_context=bool(payload.get("share_context")),
                    )
                    self.ui.put(("model_fallback_result", result))
                    if result.get("ok"):
                        self._model = self._agent.model
                        self._model_provider = self._agent.model_provider
                        self._api_base_url = self._agent.api_base_url
                        self._api_context.clear()
                        self.ui.put(("model", self._model))
                        self.ui.put(("status", "fallback model selected; current task authorization was cleared"))
                elif kind == "set_model":
                    self._model = self._normalize_model(payload)
                    self._session_id = None
                    self._api_context.clear()
                    self._agent.configure(self._model, self._api_base_url, self._api_proxy_url, self._model_provider)
                    self.ui.put(("model", self._model))
                    self.ui.put(("status", "model changed; a new chat will start on the next turn"))
                elif kind == "configure_connection":
                    self._backend = self._normalize_backend(payload.get("backend"))
                    self._model = self._normalize_model(payload.get("model"))
                    self._api_base_url = self._normalize_api_base(payload.get("api_base_url"))
                    self._api_proxy_url = self._normalize_proxy(payload.get("api_proxy_url"))
                    self._model_provider = normalize_provider(payload.get("model_provider"))
                    self._adapter = ModelAdapter(self._model_provider, self._api_base_url)
                    self._api_base_url = self._adapter.profile.base_url
                    self._session_id = None
                    self._api_context.clear()
                    self._agent.configure(self._model, self._api_base_url, self._api_proxy_url, self._model_provider)
                    self.ui.put(("model", self._model))
                    self.ui.put(("backend", self._backend_status()))
                    self.ui.put(("connection_done", {
                        "backend": self._backend, "active": self._resolved_backend(),
                        "model": self._model, "api_base_url": self._api_base_url,
                        "api_proxy_url": self._api_proxy_url, "model_provider": self._model_provider,
                    }))
                elif kind == "set_permission_mode":
                    self._permission_mode = str(payload)
                    self._agent.set_permission_mode(self._permission_mode)
                    self._session_id = None
                    self.ui.put(("permission_mode", self._permission_mode))
                    self.ui.put(("status", "permission mode changed; a new chat will start on the next turn"))
            except BaseException as exc:
                self.ui.put(("error", f"{type(exc).__name__}: {exc}"))
                self.ui.put(("turn_done", None))
        self._stop_server()

    # -- app-server transport ----------------------------------------
    @staticmethod
    def _normalize_backend(value: Any) -> str:
        value = str(value or "auto").strip().lower()
        return value if value in ("auto", "codex", "api", "agent") else "auto"

    @staticmethod
    def _normalize_model(value: Any) -> str:
        value = value.strip() if isinstance(value, str) else ""
        return API_MODEL if not value or value.lower() in ("none", "null") else value

    @staticmethod
    def _normalize_api_base(value: Any) -> str:
        value = value.strip().rstrip("/") if isinstance(value, str) else ""
        if not value or value.lower() in ("none", "null"):
            return API_BASE_URL or "https://api.openai.com/v1"
        if not value.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            return API_BASE_URL or "https://api.openai.com/v1"
        return value

    @staticmethod
    def _normalize_proxy(value: Any) -> str:
        value = value.strip().rstrip("/") if isinstance(value, str) else ""
        if not value or value.lower() in ("none", "null"):
            return ""
        return value if value.startswith(("http://", "https://")) else ""

    def _resolved_backend(self) -> str:
        if self._backend == "auto":
            return "agent" if get_api_key() else "codex"
        return self._backend

    def _backend_status(self) -> str:
        active = self._resolved_backend()
        return f"auto→{active}" if self._backend == "auto" else active

    def _startup_diagnostics(self) -> list[str]:
        """Return actionable local configuration checks without probing the network."""
        active = self._resolved_backend()
        messages: list[str] = []
        if active in {"api", "agent"}:
            if get_api_key():
                messages.append("✓ API key configured. Connection will be checked on the first message.")
            else:
                messages.append("⚠ API key is not configured. Open Gear → Connection settings to add one.")
            messages.append(f"API endpoint: {self._api_base_url}")
        if active == "agent":
            pwsh = self._agent.tools._powershell_7_executable()
            if pwsh:
                messages.append("✓ PowerShell 7 available for confirmed shell tasks.")
            else:
                messages.append("⚠ PowerShell 7 (pwsh) was not found. Shell tasks will be unavailable; set DESKORB_AGENT_PWSH or install PowerShell 7.")
            messages.append(f"Agent working folder: {self._agent.tools.root}")
        return messages

    @staticmethod
    def _find_codex() -> str | None:
        return shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")

    def _launch_prefix(self) -> list[str]:
        if not self._codex:
            return []
        if os.name == "nt" and self._codex.lower().endswith((".cmd", ".bat")):
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", self._codex]
        return [self._codex]

    def _sandbox(self) -> str:
        return "read-only" if self._permission_mode == "plan" else "workspace-write"

    # Kept for compatibility with the lightweight unit tests and diagnostics.
    def _build_command(self, image_paths: list[str]) -> list[str]:
        command = self._launch_prefix() + ["-c", f'service_tier="{_SERVICE_TIER}"']
        if self._session_id:
            command += ["exec", "resume", "--json", "-m", self._model, "--skip-git-repo-check", self._session_id, "-"]
        else:
            command += ["exec", "--json", "-m", self._model, "-s", self._sandbox(), "-C", str(Path(WORKING_DIR).expanduser()), "--skip-git-repo-check", "-"]
        return command

    def _start_server(self):
        if self._proc and self._proc.poll() is None:
            return
        command = self._launch_prefix() + ["-c", f'service_tier="{_SERVICE_TIER}"', "app-server", "--stdio"]
        cwd = str(Path(WORKING_DIR).expanduser())
        if not os.path.isdir(cwd):
            cwd = str(Path.home())
        self._messages = queue.Queue()
        self._next_request_id = 1
        proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=_CREATE_NO_WINDOW)
        with self._proc_lock:
            self._proc = proc
        assert proc.stdout is not None
        def reader():
            for raw in proc.stdout:
                try:
                    message = json.loads(raw)
                    if isinstance(message, dict):
                        self._messages.put(message)
                except json.JSONDecodeError:
                    continue
            self._messages.put(None)
        threading.Thread(target=reader, daemon=True, name="codex-app-server-reader").start()
        request_id = self._send("initialize", {"clientInfo": {"name": "deskorb-agent", "version": "0.2.0"}, "capabilities": {"experimentalApi": True}})
        self._wait_for(lambda m: m.get("id") == request_id, 25)

    def _send(self, method: str, params: dict[str, Any]) -> int:
        with self._write_lock:
            with self._proc_lock:
                proc = self._proc
            if not proc or proc.poll() is not None or not proc.stdin:
                raise RuntimeError("Codex background connection is not running")
            request_id = self._next_request_id
            self._next_request_id += 1
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            return request_id

    def _wait_for(self, predicate, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self._messages.get(timeout=min(1.0, max(.05, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if message is None:
                raise RuntimeError("Codex background connection closed unexpectedly")
            if "method" in message:
                self._handle_notification(message)
            if predicate(message):
                if "error" in message:
                    raise RuntimeError(self._event_message(message["error"]))
                return message
        raise TimeoutError("Codex background connection timed out")

    def _ensure_thread(self):
        if self._session_id:
            return
        self._start_server()
        cwd = str(Path(WORKING_DIR).expanduser())
        if not os.path.isdir(cwd):
            cwd = str(Path.home())
        request_id = self._send("thread/start", {"cwd": cwd, "model": self._model, "sandbox": self._sandbox(), "approvalPolicy": "never", "serviceTier": _SERVICE_TIER})
        response = self._wait_for(lambda m: m.get("id") == request_id, 30)
        self._session_id = str(response["result"]["thread"]["id"])

    # -- turn execution ------------------------------------------------
    def _run_turn(self, text: str, image_paths: list[str], current_context_consent: bool = False,
                  ephemeral: bool = False):
        self._interrupted = False
        self._tool_items_seen.clear()
        if requires_current_context_consent(text) and not current_context_consent:
            # Keep the privacy boundary backend-independent. AgentRuntime has the same
            # check for direct callers, while this guard prevents API/Codex transports
            # from receiving an image before a user-clicked grant reaches them.
            self.ui.put(("privacy_consent_required", {
                "scope": "active_window_and_local_diagnostics",
                "message": "Current desktop diagnostics require explicit one-turn user consent.",
                "text": str(text),
            }))
            return
        active = self._resolved_backend()
        self.ui.put(("backend", self._backend_status()))
        try:
            if active == "api":
                try:
                    self._run_api_turn(text, image_paths, ephemeral=ephemeral)
                except BaseException as exc:
                    if self._backend == "auto" and self._codex:
                        self.ui.put(("system", f"↪ API 不可用（{self._short_status(str(exc))}），已自动切回 Codex。"))
                        self.ui.put(("backend", "auto→codex"))
                        self._run_codex_turn(text, image_paths, ephemeral=ephemeral)
                    else:
                        raise
            elif active == "agent":
                try:
                    self._run_agent_turn(text, image_paths, ephemeral=ephemeral,
                                         current_context_consent=current_context_consent)
                except BaseException as exc:
                    if self._backend == "auto" and self._codex:
                        self.ui.put(("system", f"↪ Agent 不可用（{self._short_status(str(exc))}），已自动切回 Codex。"))
                        self.ui.put(("backend", "auto→codex"))
                        self._run_codex_turn(text, image_paths, ephemeral=ephemeral)
                    else:
                        raise
            else:
                self._run_codex_turn(text, image_paths, ephemeral=ephemeral)
        except BaseException as exc:
            if self._interrupted:
                self.ui.put(("system", "⏹ stopped."))
            else:
                self.ui.put(("error", f"Could not run {active.upper()}: {type(exc).__name__}: {exc}"))
        finally:
            self._turn_id = None
            self._turn_thread_id = None
            with self._api_lock:
                self._api_response = None
            self.ui.put(("status", ""))
            self.ui.put(("turn_done", None))

    def _run_codex_turn(self, text: str, image_paths: list[str], ephemeral: bool = False):
        if not self._codex:
            raise RuntimeError("Codex CLI is not installed or is not on PATH")
        if ephemeral:
            self._start_server()
            cwd = str(Path(WORKING_DIR).expanduser())
            if not os.path.isdir(cwd):
                cwd = str(Path.home())
            request_id = self._send("thread/start", {
                "cwd": cwd, "model": self._model, "sandbox": self._sandbox(),
                "approvalPolicy": "never", "serviceTier": _SERVICE_TIER,
            })
            response = self._wait_for(lambda m: m.get("id") == request_id, 30)
            thread_id = str(response["result"]["thread"]["id"])
            first_turn = True
        else:
            first_turn = self._session_id is None
            self._ensure_thread()
            thread_id = self._session_id
        prompt = text
        if first_turn and SYSTEM_APPEND:
            prompt = "[OVERLAY CONTEXT]\n" + SYSTEM_APPEND.strip() + "\n[/OVERLAY CONTEXT]\n\n" + text
        self.ui.put(("status", "thinking…"))
        if first_turn:
            self.ui.put((
                "system",
                "⏳ 正在建立 Codex 连接。当前网络会先回退到 HTTPS，首次回复可能需要约 2 分钟；后续提问会复用连接并明显加快。",
            ))
        try:
            self._turn_thread_id = thread_id
            input_items: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            input_items += [{"type": "localImage", "path": str(Path(p).expanduser()), "detail": "auto"} for p in image_paths if os.path.isfile(str(Path(p).expanduser()))]
            request_id = self._send("turn/start", {"threadId": thread_id, "input": input_items, "approvalPolicy": "never", "serviceTier": _SERVICE_TIER})
            response = self._wait_for(lambda m: m.get("id") == request_id, 30)
            self._turn_id = str(response.get("result", {}).get("turn", {}).get("id") or "") or None
            self._wait_for(lambda m: m.get("method") == "turn/completed" and m.get("params", {}).get("threadId") == thread_id, 360)
            if self._interrupted:
                self.ui.put(("system", "⏹ stopped."))
        except BaseException as exc:
            if self._interrupted:
                raise
            else:
                self._stop_server()
                if not ephemeral:
                    self._session_id = None
                raise

    def _run_api_turn(self, text: str, image_paths: list[str], ephemeral: bool = False):
        api_key = get_api_key()
        if not api_key:
            raise RuntimeError("API Key 未配置；点击底部状态栏打开 Connection settings")
        self.ui.put(("status", "API connecting…"))
        if not ephemeral:
            self._maybe_compact_api_context(api_key)
        content: list[dict[str, Any]] = [{
            "type": "input_text", "text": text if ephemeral else self._api_context.build_input(text),
        }]
        for raw_path in image_paths:
            path = Path(raw_path).expanduser()
            if not path.is_file():
                continue
            mime = mimetypes.guess_type(str(path))[0] or "image/png"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append({"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"})
        payload: dict[str, Any] = {
            "model": self._model,
            "instructions": SYSTEM_APPEND,
            "input": [{"role": "user", "content": content}],
            "stream": True,
        }
        response = self._open_api_response(payload, api_key, "text/event-stream")
        with self._api_lock:
            self._api_response = response
        completed = False
        saw_delta = False
        answer_parts: list[str] = []
        try:
            for raw_line in response:
                if self._interrupted:
                    break
                line = raw_line.decode("utf-8", "replace").strip()
                if not line or line.startswith(":") or line.startswith("event:"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = str(event.get("type", ""))
                if self._adapter.protocol == "chat_completions" and isinstance(event.get("choices"), list):
                    choice = event["choices"][0] if event["choices"] else {}
                    message = choice.get("message") if isinstance(choice, dict) else None
                    if isinstance(message, dict):
                        # Some compatible gateways ignore stream=true and return
                        # one ordinary chat-completion object.
                        completed = True
                        if not saw_delta:
                            text_out = self._extract_api_text(self._adapter.normalize_response(event))
                            if text_out:
                                answer_parts.append(text_out)
                                self.ui.put(("delta", text_out))
                    delta = choice.get("delta") if isinstance(choice, dict) else {}
                    text_delta = delta.get("content") if isinstance(delta, dict) else None
                    if text_delta:
                        saw_delta = True
                        answer_parts.append(str(text_delta))
                        self.ui.put(("delta", str(text_delta)))
                    if isinstance(choice, dict) and choice.get("finish_reason") is not None:
                        completed = True
                elif event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if delta:
                        saw_delta = True
                        text_delta = str(delta)
                        answer_parts.append(text_delta)
                        self.ui.put(("delta", text_delta))
                elif event_type == "response.completed":
                    completed = True
                    result = event.get("response") or {}
                    if not saw_delta:
                        text_out = self._extract_api_text(result)
                        if text_out:
                            answer_parts.append(text_out)
                            self.ui.put(("delta", text_out))
                elif event_type in ("response.failed", "response.incomplete", "error"):
                    raise RuntimeError(self._api_error_detail(event))
                elif not event_type and isinstance(event, dict) and event.get("id"):
                    # A compatible endpoint may ignore stream=true and return one JSON object.
                    completed = True
                    text_out = self._extract_api_text(self._adapter.normalize_response(event))
                    if text_out:
                        answer_parts.append(text_out)
                        self.ui.put(("delta", text_out))
        finally:
            try:
                response.close()
            except Exception:
                pass
        if self._interrupted:
            self.ui.put(("system", "⏹ stopped."))
        elif not completed:
            raise RuntimeError("API stream ended before response.completed")
        else:
            if not ephemeral:
                self._api_context.add_turn(text, "".join(answer_parts))
                self.ui.put(("ctx", self._api_context.usage_percent()))

    def _run_agent_turn(self, text: str, image_paths: list[str], ephemeral: bool = False,
                        current_context_consent: bool = False):
        if ephemeral:
            if current_context_consent:
                self._agent.run_ephemeral_turn(text, image_paths,
                                               current_context_consent=True)
            else:
                self._agent.run_ephemeral_turn(text, image_paths)
        else:
            if current_context_consent:
                self._agent.run_turn(text, image_paths, current_context_consent=True)
            else:
                self._agent.run_turn(text, image_paths)

    def _open_api_response(self, payload: dict[str, Any], api_key: str, accept: str):
        endpoint = self._adapter.endpoint
        request_body = self._adapter.prepare_request(payload)
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": accept,
                "User-Agent": "deskorb-agent/0.1.0",
            },
            method="POST",
        )
        try:
            if self._api_proxy_url:
                proxy = urllib.request.ProxyHandler({"http": self._api_proxy_url,
                                                      "https": self._api_proxy_url})
                return urllib.request.build_opener(proxy).open(request, timeout=API_TIMEOUT)
            return urllib.request.urlopen(request, timeout=API_TIMEOUT)
        except urllib.error.HTTPError as exc:
            detail = exc.read(32 * 1024).decode("utf-8", "replace")
            raise RuntimeError(f"API HTTP {exc.code}: {self._api_error_detail(detail)}") from exc

    def _maybe_compact_api_context(self, api_key: str):
        if not self._api_context.compaction_candidate():
            return
        self.ui.put(("status", "optimizing API memory..."))
        try:
            self._compact_api_context(api_key, force=False)
        except Exception as exc:
            # Optimization must never prevent the user's actual request. The context
            # builder still enforces the hard budget by selecting recent complete turns.
            dbg("api_context_compaction_failed", {"error": self._short_status(str(exc))})

    def _compact_api_context(self, api_key: str, force: bool) -> dict[str, int]:
        candidate = self._api_context.compaction_candidate(force=force)
        pre_tokens = self._api_context.estimated_tokens()
        if candidate is None:
            return {"pre_tokens": pre_tokens, "post_tokens": pre_tokens}
        transcript = {
            "previous_summary": candidate.previous_summary or None,
            "dialogue_to_compact": [
                {"role": message.role, "content": message.text}
                for message in candidate.messages
            ],
        }
        summary_prompt = (
            "Update the rolling conversation memory from the JSON below. Preserve user goals "
            "and preferences, exact identifiers and constraints, decisions and reasons, completed "
            "work, unresolved tasks, errors and attempted fixes. Remove greetings, repetition, "
            "transient wording and obsolete details. Never invent facts. Return only a compact "
            "structured Markdown summary with short headings; do not answer the conversation.\n\n"
            + json.dumps(transcript, ensure_ascii=False, separators=(",", ":"))
        )
        payload: dict[str, Any] = {
            "model": self._model,
            "instructions": "You are a precise conversation-memory compactor.",
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": summary_prompt},
            ]}],
            "stream": False,
            "max_output_tokens": API_CONTEXT_SUMMARY_TOKENS,
        }
        response = self._open_api_response(payload, api_key, "application/json")
        with self._api_lock:
            self._api_response = response
        try:
            raw = response.read(2 * 1024 * 1024).decode("utf-8", "replace")
            result = self._adapter.normalize_response(json.loads(raw))
            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError(self._api_error_detail(result))
            summary = self._extract_api_text(result)
            if not summary:
                raise RuntimeError("API context compaction returned no summary")
            if not self._api_context.apply_compaction(candidate, summary):
                raise RuntimeError("API context changed while compaction was running")
        finally:
            try:
                response.close()
            except Exception:
                pass
            with self._api_lock:
                self._api_response = None
        post_tokens = self._api_context.estimated_tokens()
        self.ui.put(("ctx", self._api_context.usage_percent()))
        return {"pre_tokens": candidate.pre_tokens, "post_tokens": post_tokens}

    @staticmethod
    def _extract_api_text(response: Any) -> str:
        if not isinstance(response, dict):
            return ""
        direct = response.get("output_text")
        if isinstance(direct, str):
            return direct
        parts: list[str] = []
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") in ("output_text", "text"):
                    value = content.get("text")
                    if isinstance(value, str):
                        parts.append(value)
        return "".join(parts)

    @staticmethod
    def _api_error_detail(value: Any) -> str:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                return value[:500]
        if isinstance(value, dict):
            error = value.get("error") or value.get("response", {}).get("error") or value
            if isinstance(error, dict):
                return str(error.get("message") or error.get("code") or error)[:500]
            return str(error)[:500]
        return str(value)[:500]

    def _handle_notification(self, message: dict[str, Any]):
        method, params = str(message.get("method", "")), message.get("params", {})
        if not isinstance(params, dict):
            return
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if delta:
                self.ui.put(("delta", str(delta)))
        elif method in ("item/started", "item/updated", "item/completed"):
            self._handle_item(params.get("item"), method.replace("/", "."))
        elif method == "error":
            detail = self._event_message(params)
            if detail:
                self.ui.put(("status", self._short_status(detail)))

    def _compact(self):
        self._interrupted = False
        self.ui.put(("compacting", None))
        if self._resolved_backend() == "agent":
            try:
                meta = self._agent.compact(force=True)
                self.ui.put(("ctx", self._agent.context.usage_percent()))
                detail = "No older turns need compaction." if meta is None else "Context compacted."
                self.ui.put(("compact_done", {"status": "ok", "meta": meta, "detail": detail}))
            except Exception as exc:
                status = "cancelled" if self._interrupted else "error"
                self.ui.put(("compact_done", {
                    "status": status, "meta": None, "detail": self._short_status(str(exc)),
                }))
            return
        if self._resolved_backend() == "api":
            try:
                api_key = get_api_key()
                if not api_key:
                    raise RuntimeError("API Key is not configured")
                meta = self._compact_api_context(api_key, force=True)
                self.ui.put(("compact_done", {"status": "ok", "meta": meta}))
            except Exception as exc:
                status = "cancelled" if self._interrupted else "error"
                self.ui.put(("compact_done", {
                    "status": status, "meta": None, "detail": self._short_status(str(exc)),
                }))
            return
        try:
            self._ensure_thread()
            request_id = self._send("thread/compact/start", {"threadId": self._session_id})
            self._wait_for(lambda m: m.get("id") == request_id, 30)
            self.ui.put(("compact_done", {"status": "completed", "meta": None, "detail": "Context compacted."}))
        except Exception as exc:
            self.ui.put(("compact_done", {"status": "unconfirmed", "meta": None, "detail": str(exc)}))

    # -- rendering helpers --------------------------------------------
    def _handle_item(self, item: Any, event_type: str):
        if not isinstance(item, dict):
            return
        item_type, item_id = str(item.get("type", "")), str(item.get("id", ""))
        if item_type == "agent_message":
            text = item.get("text") or item.get("message")
            if event_type == "item.completed" and text:
                self.ui.put(("delta", str(text)))
            return
        if item_type in ("reasoning", "reasoning_summary"):
            text = item.get("text") or item.get("summary")
            if event_type == "item.completed" and text:
                self.ui.put(("think", str(text)))
            return
        if item_id and item_id in self._tool_items_seen or event_type == "item.updated":
            return
        name, data = self._tool_description(item_type, item)
        if name:
            if item_id:
                self._tool_items_seen.add(item_id)
            self.ui.put(("tool", (name, data)))

    @staticmethod
    def _tool_description(item_type: str, item: dict[str, Any]):
        if item_type == "command_execution": return "Shell", {"command": item.get("command", "")}
        if item_type == "file_change": return "File changes", {"description": item.get("changes") or item.get("files") or ""}
        if item_type == "mcp_tool_call": return str(item.get("tool") or item.get("name") or "MCP tool"), {"description": item.get("arguments") or item.get("input") or ""}
        if item_type == "web_search": return "Web search", {"query": item.get("query", "")}
        if item_type == "image_view": return "View image", {"path": item.get("path", "")}
        return None, None

    @staticmethod
    def _event_message(event: Any) -> str:
        if isinstance(event, str): return event
        if not isinstance(event, dict): return str(event or "")
        value = event.get("message") or event.get("detail") or event.get("error")
        if isinstance(value, dict): value = value.get("message") or value.get("detail") or json.dumps(value)
        text = str(value or "")
        try:
            decoded = json.loads(text)
            if isinstance(decoded, dict): text = str(decoded.get("detail") or decoded.get("message") or text)
        except Exception: pass
        return text

    @staticmethod
    def _short_status(message: str) -> str:
        one_line = " ".join(str(message).split())
        return one_line[:110] + ("…" if len(one_line) > 110 else "")

    def _stop_server(self):
        with self._proc_lock:
            proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            self._terminate_process_tree(proc)

    @staticmethod
    def _terminate_process_tree(proc: subprocess.Popen[str]):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW, timeout=8, check=False)
            else:
                proc.terminate()
        except Exception:
            try: proc.kill()
            except Exception: pass
