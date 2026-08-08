"""End-to-end acceptance run for the configured Agent + OfficeCLI path.

Run from the repository root with the local virtual environment after placing
the real provider settings in ``volcengine.env``.  The script intentionally
does not print or persist the API key.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from queue import Queue

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from agent_runtime import AgentRuntime
from provider_env import api_key


DOCX = ROOT / "warhammer40k_acceptance.docx"
PPTX = ROOT / "warhammer40k_primarchs_acceptance.pptx"


def drain(events: Queue) -> list[tuple[str, object]]:
    items: list[tuple[str, object]] = []
    while not events.empty():
        items.append(events.get_nowait())
    return items


def run_task(runtime: AgentRuntime, prompt: str) -> None:
    events = runtime.ui
    print("TASK:", prompt.splitlines()[0][:160], flush=True)
    runtime.run_turn(prompt, [])
    output = drain(events)
    approvals = [payload for kind, payload in output if kind == "approval"]
    if approvals:
        raise RuntimeError("Acceptance task unexpectedly requested confirmation")
    for kind, payload in output:
        if kind == "tool":
            name, arguments = payload
            command = arguments.get("command") if isinstance(arguments, dict) else None
            print("TOOL:", name, str(command or arguments)[:180], flush=True)
        elif kind == "delta":
            print("FINAL:", str(payload)[:500], flush=True)


def main() -> int:
    if not api_key(config.MODEL_PROVIDER):
        print("API key is not configured for the selected provider.", file=sys.stderr)
        return 2
    print("CONFIG:", json.dumps({
        "provider": config.MODEL_PROVIDER,
        "model": config.API_MODEL,
        "base_url": config.API_BASE_URL,
        "docx": str(DOCX),
        "pptx": str(PPTX),
    }, ensure_ascii=False), flush=True)
    runtime = AgentRuntime(
        Queue(), config.API_MODEL, config.API_BASE_URL,
        working_dir=ROOT, full_access=True,
        model_provider=config.MODEL_PROVIDER,
    )
    try:
        run_task(runtime, f"""
必须完成这个任务，不要只给计划或解释。必须使用 OfficeCLI MCP，不要使用 python-docx、python-pptx、openpyxl 或 COM。
创建并保存 Word 文档到：{DOCX}
主题：介绍战锤 40K。
内容至少包括：标题、战锤 40K 背景、主要势力、帝国与混沌、18 位基因原体的中文简介，以及总结。使用清晰的标题和段落，必要时使用表格。
完成后必须执行 OfficeCLI validate，并用 OfficeCLI view 读取正文确认内容非空。遇到工具错误时请修正命令并继续，直到文件存在、validate 成功、读取结果非空。
""")
        run_task(runtime, f"""
必须完成这个任务，不要只给计划或解释。必须使用 OfficeCLI MCP，不要使用 python-pptx、python-docx、openpyxl 或 COM。
创建并保存 PowerPoint 到：{PPTX}
主题：介绍战锤 40K 的 18 位基因原体。
请制作 20 页左右：封面、背景/时间线/设定概览、然后为 18 位基因原体各制作一页。每位基因原体页面必须包含姓名、忠诚/叛变阵营、军团和一段简介，并放置一张对应人物图片。
图片可以从公开网络来源下载；请为 18 位基因原体准备 18 个本地图片文件并插入到对应页面。如果某个网络来源失败，请更换来源或使用可用的生成图片，不要因此停止整个任务。
优先使用 OfficeCLI batch 批量执行连续编辑，避免重复 help。完成后必须执行 validate、view outline，并至少执行一次 view screenshot --page 1 验证布局。
遇到任何工具错误都要根据错误修正并继续，直到文件存在、validate 成功、outline 非空、截图成功返回图片结果。
""")
    finally:
        if runtime.mcp:
            runtime.mcp.close()
    for path in (DOCX, PPTX):
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Acceptance output missing or empty: {path}")
    print("ACCEPTANCE_OUTPUTS_OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
