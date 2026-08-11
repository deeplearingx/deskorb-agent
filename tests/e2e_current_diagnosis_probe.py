"""Consent-gated, read-only diagnosis of the currently running DeskOrb environment."""
from __future__ import annotations

import json
import argparse
import os
import re
import sys
import threading
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from agent_runtime import AgentRuntime
from config import API_BASE_URL, API_MODEL, API_PROXY_URL, API_TIMEOUT, MODEL_PROVIDER, WORKING_DIR
from task_runtime import InMemoryTaskJournal


CONSENT_TOKEN = "I_AUTHORIZE_CURRENT_DESKTOP_DIAGNOSTIC"


class _EphemeralHealthStore:
    """Keep provider health metadata in memory for the read-only probe."""

    def __init__(self):
        self.records: list[dict[str, object]] = []

    def record(self, **values: object) -> None:
        self.records.append({key: values[key] for key in (
            "provider", "model", "ok", "latency_ms", "failure_kind", "operation",
        ) if key in values})

    def summary(self, provider: str, model: str, *, limit: int = 100,
                operation: str | None = None) -> dict[str, object]:
        rows = [item for item in self.records
                if item.get("provider") == provider and item.get("model") == model
                and (operation is None or item.get("operation") == operation)][-max(1, int(limit)):]
        return {"provider": provider, "model": model,
                "operation": operation or "all", "samples": len(rows)}


def _safe_log_hint(root: Path, log_path: Path | None = None) -> str:
    """Describe only a configured log path that stays inside the read scope."""
    configured = (str(log_path) if log_path is not None else
                  os.environ.get("DESKORB_AGENT_DEBUG_LOG", "").strip())
    if not configured:
        return (
            "没有配置 DESKORB_AGENT_DEBUG_LOG。不要假设固定日志文件名；先用 filesystem_list 列出工作目录，"
            "如果没有安全的文本日志，明确报告日志未配置或缺失。"
        )
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        relative = candidate.resolve().relative_to(root.expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return "configured log path outside working directory; do not read outside the configured scope."
    return (
        f"已配置日志候选相对路径 {relative.as_posix()}；只有当 filesystem_list 确认它是普通文件时才读取，"
        "不要读取工作目录之外的路径。"
    )


def _build_diagnostic_task(root: Path, log_path: Path | None = None) -> str:
    return (
        "The user explicitly authorized this read-only diagnostic. Use only the configured working directory. "
        "First call desktop_get_active_window and filesystem_list with path '.' and max_entries 200. "
        "Use the listing to discover a regular UTF-8-compatible DeskOrb log rather than assuming a filename; "
        "read at most one relevant log with filesystem_read_text and do not expose tokens, passwords, cookies, "
        "or unrelated user content in the answer. "
        + _safe_log_hint(root, log_path)
        + " Clearly separate evidence from hypotheses and list safe next actions. "
        "Do not capture a screenshot, read the clipboard, launch applications, run shell commands, or modify files."
    )


def _diagnostic_environment_ready(answer: str, tools: list[str]) -> tuple[bool, str]:
    """Make the CLI truthful when observations prove the desktop is not ready."""
    if not answer or not {"Active window", "List files"}.issubset(set(tools)):
        return False, "diagnostic_incomplete"
    if "Read file" not in tools:
        return False, "environment_not_ready"
    lowered = answer.casefold()
    not_ready = (
        re.search(r"capturable\s*[:：]\s*false", lowered)
        or "no usable foreground" in lowered
        or "no capturable foreground window" in lowered
        or "path is not a file" in lowered
        or "log is missing" in lowered
        or "log is unavailable" in lowered
        or "log not configured" in lowered
        or ("not configured" in lowered and "log" in lowered)
        or "未获得可捕获" in answer
        or "未检测到可捕获" in answer
        or "拒绝访问" in answer
        or "访问被拒绝" in answer
        or "无法读取工作目录" in answer
        or "不可发现/不可读取" in answer
        or "未发现或读取" in answer
        or "权限不足" in answer
        or "日志未配置" in answer
        or "日志缺失" in answer
    )
    return not bool(not_ready), ("environment_not_ready" if not_ready else "ready")


def _run_bounded_diagnostic(runtime: AgentRuntime, task: str, timeout_seconds: int) -> tuple[bool, str | None]:
    """Bound the provider turn and keep a stuck gateway from hanging the CLI."""
    errors: list[BaseException] = []

    def target() -> None:
        try:
            runtime.run_turn(task, [])
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(max(1, int(timeout_seconds)))
    if thread.is_alive():
        try:
            runtime.interrupt()
        except Exception:
            pass
        thread.join(5)
        return False, "provider_timeout"
    return (not errors), (None if not errors else "diagnostic_runtime_error")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consent-gated read-only current desktop diagnosis")
    parser.add_argument("--confirm-current-desktop", action="store_true",
                        help="explicitly authorize sending active-window metadata and the local DeskOrb log")
    parser.add_argument("--working-dir", type=Path, default=Path(WORKING_DIR).expanduser(),
                        help="read-only DeskOrb working directory (defaults to DESKORB_AGENT_WORKING_DIR)")
    parser.add_argument("--log-path", type=Path,
                        help="optional log path, relative to --working-dir; paths outside it are rejected")
    parser.add_argument("--timeout-seconds", type=int, default=max(1, int(API_TIMEOUT) + 5),
                        help="maximum provider turn duration (defaults to API timeout plus a small join margin)")
    args = parser.parse_args(argv)
    env_consent = os.environ.get("DESKORB_AGENT_LIVE_DIAGNOSTIC_CONFIRM", "").strip()
    if not args.confirm_current_desktop and env_consent != CONSENT_TOKEN:
        print(json.dumps({"ok": False, "status": "consent_required",
                          "requires_user_confirmation": True,
                          "message": "Run with --confirm-current-desktop only after the user explicitly authorizes this diagnostic."},
                         ensure_ascii=False))
        return 4
    root = Path(args.working_dir).expanduser()
    events: Queue = Queue()
    # Read-only prevents this live check from modifying desktop state or files.
    runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL, working_dir=root,
                           full_access=False, model_provider=MODEL_PROVIDER,
                           task_journal=InMemoryTaskJournal())
    # The diagnostic must not create or update SQLite health/task files under
    # a user's configured read-only working directory.
    runtime._model_health_store = _EphemeralHealthStore()
    task = _build_diagnostic_task(root, args.log_path)
    try:
        turn_ok, turn_failure = _run_bounded_diagnostic(runtime, task, args.timeout_seconds)
        items = []
        while True:
            try:
                items.append(events.get_nowait())
            except Empty:
                break
        answer = "\n".join(str(value) for kind, value in items if kind == "delta")
        tools = [value[0] for kind, value in items if kind == "tool" and isinstance(value, tuple)]
        if not turn_ok:
            observed_ready, observed_status = _diagnostic_environment_ready(answer, tools)
            status = (observed_status if observed_status == "environment_not_ready"
                      else turn_failure or "diagnostic_runtime_error")
            result = {"ok": False, "status": status,
                      "working_dir": str(root), "tools": tools, "answer": answer[-3000:]}
            if status != turn_failure and turn_failure:
                result["provider_failure"] = turn_failure
            print(json.dumps(result, ensure_ascii=False))
            return 2
        ready, status = _diagnostic_environment_ready(answer, tools)
        result = {"ok": ready, "status": status, "working_dir": str(root),
                  "tools": tools, "answer": answer[-3000:]}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:1000]}, ensure_ascii=False))
        return 3
    finally:
        if runtime.mcp:
            runtime.mcp.close()


if __name__ == "__main__":
    raise SystemExit(main())
