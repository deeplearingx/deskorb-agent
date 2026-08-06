"""Deterministic process-restart recovery probe.

The probe uses a temporary SQLite journal and scripted model responses.  It
does not contact a provider, open a real application, or persist a transcript.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from queue import Queue
from unittest.mock import patch

os.environ.setdefault("DESKORB_AGENT_PLAYWRIGHT_MCP", "0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentRuntime
from task_runtime import TASK_STATUS_PAUSED
from workflow_runtime import TaskContract


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="deskorb-restart-probe-") as directory:
        root = Path(directory)
        first = AgentRuntime(Queue(), "fixture-model", "https://example.test/v1", working_dir=root)
        task_id = first.task_journal.start("打开记事本并输入重启恢复测试")
        contract = TaskContract.from_goal("打开记事本并输入重启恢复测试", requires_action=True)
        first.task_journal.set_contract(task_id, contract.safe_dict())
        capabilities = frozenset({"desktop_control"})
        first.task_journal.set_status(task_id, TASK_STATUS_PAUSED, capabilities=capabilities)
        first.task_journal.checkpoint(task_id, {"last_tool": "desktop_type", "last_node_id": 1})
        effect = {"snapshot_id": "S-before", "text": "RESTART_ONCE",
                  "risk_level": "normal", "risk_reason": "fixture"}
        first.task_journal.record_effect(task_id, "desktop_type", effect)
        del first

        second = AgentRuntime(Queue(), "fixture-model", "https://example.test/v1", working_dir=root)
        recoverable = second.recoverable_tasks()
        confirmation = second.resume_task(task_id)
        second._task_id = task_id
        second._resuming_task = True
        second._resume_observed = True
        duplicate = second._run_tool_with_recovery("desktop_type", effect)
        second._task_id = None
        second._resuming_task = False

        responses = iter([
            {"output": [{"type": "function_call", "call_id": "observe",
                         "name": "desktop_capture_state", "arguments": "{}"}]},
            {"output": [{"type": "function_call", "call_id": "verify",
                         "name": "desktop_verify_state",
                         "arguments": '{"snapshot_id":"S-before"}'}]},
            {"output_text": "恢复后已重新观察并验证桌面状态。", "output": []},
        ])
        second._request = lambda _payload, _key: next(responses)
        second._append_desktop_observation = lambda transcript, _name: transcript

        def fake_local_tool(name, _arguments):
            if name == "desktop_capture_state":
                return {"ok": True, "snapshot_id": "S-after", "active_window": "fixture"}
            if name == "desktop_verify_state":
                return {"ok": True, "screen_changed": True, "active_window_changed": False}
            return {"ok": False, "error": "unexpected fixture tool"}

        second._run_local_tool = fake_local_tool
        with patch("agent_runtime.get_api_key", return_value="fixture-key"):
            second.resume_task(task_id, user_confirmed=True)

        events = []
        while not second.ui.empty():
            events.append(second.ui.get_nowait())
        progress = [value for kind, value in events
                    if kind == "task_progress" and isinstance(value, dict) and value.get("terminal")]
        final = progress[-1] if progress else {}
        result = {
            "ok": bool(recoverable and confirmation.get("requires_confirmation")
                      and duplicate.get("duplicate_prevented")
                      and final.get("terminal") == "completed"
                      and final.get("verified")),
            "recoverable_count": len(recoverable),
            "requires_confirmation": bool(confirmation.get("requires_confirmation")),
            "duplicate_prevented": bool(duplicate.get("duplicate_prevented")),
            "terminal": final.get("terminal"),
            "verified": bool(final.get("verified")),
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
