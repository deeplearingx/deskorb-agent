"""End-to-end diagnosis/fix checks using disposable, synthetic workspaces."""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from agent_runtime import AgentRuntime
from config import API_BASE_URL, API_MODEL, API_PROXY_URL, MODEL_PROVIDER


def drain(events: Queue) -> list[tuple[str, object]]:
    items = []
    while True:
        try:
            items.append(events.get_nowait())
        except Empty:
            return items


def confirmation_token(events: list[tuple[str, object]]) -> str | None:
    for kind, value in events:
        if kind == "approval":
            match = re.search(r"确认\s+([A-F0-9]{6,})", str(value))
            if match:
                return match.group(1)
    return None


def run_agent(task: str, directory: Path) -> tuple[str, list[tuple[str, object]]]:
    events: Queue = Queue()
    runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL, working_dir=directory,
                           model_provider=MODEL_PROVIDER)
    all_events: list[tuple[str, object]] = []
    try:
        runtime.run_turn(task, [])
        for _ in range(4):
            batch = drain(events)
            all_events.extend(batch)
            token = confirmation_token(batch)
            if not token:
                break
            runtime.run_turn("确认 " + token, [])
        all_events.extend(drain(events))
        answer = "\n".join(str(value) for kind, value in all_events if kind == "delta")
        return answer, all_events
    finally:
        if runtime.mcp:
            runtime.mcp.close()


def dependency_case(root: Path) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("from dotenv import load_dotenv\nload_dotenv()\nprint('ready')\n", encoding="utf-8")
    (root / "requirements.txt").write_text("requests>=2\n", encoding="utf-8")
    (root / "startup.log").write_text("ModuleNotFoundError: No module named 'dotenv'\n", encoding="utf-8")
    answer, events = run_agent(
        "这个项目启动失败了。检查 app.py、requirements.txt 和 startup.log，找出根因。修复 requirements.txt 中缺失的依赖，"
        "不要修改无关文件；如需运行命令请走确认。最后报告根因、修改和验证证据。", root)
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    return {"id": "diagnose-001", "ok": "python-dotenv" in requirements.lower()
            and ("dotenv" in answer.lower() or "依赖" in answer), "requirements": requirements,
            "approvals": sum(1 for kind, _ in events if kind == "approval"), "answer": answer[-2000:]}


def network_case(root: Path) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "connection.json"
    config_path.write_text(json.dumps({"api_proxy_url": "http://127.0.0.1:badport",
                                       "fallback_proxy": "http://127.0.0.1:12000", "api_key": "<redacted>"},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "deskorb.log").write_text("API network connection failed: invalid proxy port 'badport'\n", encoding="utf-8")
    answer, events = run_agent(
        "DeskOrb 连接失败。检查 connection.json 和 deskorb.log，说明根因；将 api_proxy_url 改为配置中已有的 fallback_proxy，"
        "不要显示或修改 api_key，也不要发起网络请求。最后验证 JSON 和报告修改。", root)
    content = json.loads(config_path.read_text(encoding="utf-8"))
    return {"id": "diagnose-002", "ok": content.get("api_proxy_url") == "http://127.0.0.1:12000"
            and "<redacted>" in config_path.read_text(encoding="utf-8"), "config": content,
            "approvals": sum(1 for kind, _ in events if kind == "approval"), "answer": answer[-2000:]}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="deskorb-e2e-diagnosis-") as temp:
        root = Path(temp)
        report = [dependency_case(root / "dependency"), network_case(root / "network")]
    print(json.dumps({"ok": all(case["ok"] for case in report), "cases": report}, ensure_ascii=False))
    return 0 if all(case["ok"] for case in report) else 2


if __name__ == "__main__":
    raise SystemExit(main())
