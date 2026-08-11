"""Agent-generated meeting minutes parsing, rendering, and persistence.

WhisperX produces a timestamped transcript.  This module keeps the next step
independent from any provider: the existing DeskOrb worker supplies the agent
response, while these helpers turn that response into stable Markdown and JSON
artifacts that can be opened without the app.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Optional
from text_normalization import to_simplified_chinese


MAX_TRANSCRIPT_CHARS = 80_000
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class SavedMinutes:
    markdown_path: Path
    json_path: Path
    payload: dict[str, Any]


def _text(value: Any, default: str = "") -> str:
    result = to_simplified_chinese(str(value or "").strip())
    fallback = to_simplified_chinese(default)
    return result or fallback


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, Iterable) or isinstance(value, (bytes, dict)):
        return []
    return [_text(item) for item in value if _text(item)]


def _action_items(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, Mapping):
            task = _text(item.get("task") or item.get("action") or item.get("内容"))
            if not task:
                continue
            result.append({
                "task": task,
                "owner": _text(item.get("owner") or item.get("负责人"), "未指定"),
                "deadline": _text(item.get("deadline") or item.get("due") or item.get("截止时间"), "未指定"),
                "status": _text(item.get("status") or item.get("状态"), "待处理"),
            })
        elif str(item).strip():
            result.append({
                "task": _text(item),
                "owner": "未指定",
                "deadline": "未指定",
                "status": "待处理",
            })
    return result


def _decode_json_response(response: str) -> Optional[dict[str, Any]]:
    text = str(response or "").strip()
    if not text:
        return None
    fenced = _JSON_FENCE_RE.search(text)
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def parse_minutes_response(response: str) -> dict[str, Any]:
    """Normalize a model response into the stable minutes schema.

    Models sometimes wrap JSON in Markdown fences or add a short preface.  If
    no JSON can be recovered, the plain response is retained as the summary so
    the user still receives a useful artifact instead of losing the result.
    """
    raw = _decode_json_response(response)
    if raw is None:
        return {
            "title": "会议纪要",
            "summary": _text(response, "未生成会议摘要。"),
            "key_points": [],
            "decisions": [],
            "action_items": [],
            "questions": [],
        }
    return {
        "title": _text(raw.get("title") or raw.get("主题"), "会议纪要"),
        "summary": _text(raw.get("summary") or raw.get("摘要"), "未生成会议摘要。"),
        "key_points": _strings(raw.get("key_points") or raw.get("highlights") or raw.get("关键点")),
        "decisions": _strings(raw.get("decisions") or raw.get("决策") or raw.get("结论")),
        "action_items": _action_items(raw.get("action_items") or raw.get("todos") or raw.get("待办")),
        "questions": _strings(raw.get("questions") or raw.get("open_questions") or raw.get("待确认")),
    }


def build_minutes_prompt(transcript: str, *, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """Build a strict, provider-neutral prompt for the configured DeskOrb agent."""
    text = to_simplified_chinese(str(transcript or "").strip())
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[转写过长，后续内容已截断]"
    return (
        "你是会议纪要整理智能体。请根据下面的会议逐字稿生成结构化纪要。\n"
        "只返回 JSON，不要 Markdown 代码块，不要添加解释。\n"
        "JSON 必须包含这些字段：\n"
        '{"title":"会议主题","summary":"不超过200字的摘要",'
        '"key_points":["关键讨论点"],"decisions":["已确认决策"],'
        '"action_items":[{"task":"待办事项","owner":"负责人或未指定",'
        '"deadline":"截止时间或未指定","status":"待处理"}],'
        '"questions":["尚未解决的问题"]}\n'
        "不要臆造逐字稿中没有的事实；没有内容的数组返回 []。\n\n"
        "会议逐字稿：\n"
        + text
    )


def _md_cell(value: Any) -> str:
    return _text(value).replace("|", "\\|")

def _markdown(payload: Mapping[str, Any], transcript_path: Optional[Path]) -> str:
    lines = [f"# {_text(payload.get('title'), '会议纪要')}", ""]
    lines.extend(["## 摘要", _text(payload.get("summary"), "未生成会议摘要。"), ""])

    key_points = _strings(payload.get("key_points"))
    lines.append("## 关键讨论")
    lines.extend([f"- {item}" for item in key_points] or ["- 无"])
    lines.append("")

    decisions = _strings(payload.get("decisions"))
    lines.append("## 决策与结论")
    lines.extend([f"- {item}" for item in decisions] or ["- 无"])
    lines.append("")

    lines.extend(["## 待办事项", "| 事项 | 负责人 | 截止时间 | 状态 |", "|---|---|---|---|"])
    actions = _action_items(payload.get("action_items"))
    lines.extend(
        f"| {_md_cell(item['task'])} | {_md_cell(item['owner'])} | "
        f"{_md_cell(item['deadline'])} | {_md_cell(item['status'])} |"
        for item in actions
    )
    if not actions:
        lines.append("| 无 | 未指定 | 未指定 | - |")
    lines.append("")

    questions = _strings(payload.get("questions"))
    lines.append("## 待确认问题")
    lines.extend([f"- {item}" for item in questions] or ["- 无"])
    if transcript_path:
        lines.extend(["", f"> 原始转写：`{transcript_path}`"])
    return "\n".join(lines).rstrip() + "\n"


def save_minutes(
    output_dir: Path,
    stem: str,
    payload: Mapping[str, Any],
    *,
    transcript_path: Optional[Path] = None,
    raw_response: str = "",
) -> SavedMinutes:
    """Write a human-readable Markdown file and machine-readable JSON file."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    normalized = parse_minutes_response(json.dumps(dict(payload), ensure_ascii=False))
    normalized["created_at"] = datetime.now(timezone.utc).isoformat()
    if transcript_path:
        normalized["transcript_path"] = str(Path(transcript_path))
    if raw_response:
        normalized["agent_response"] = to_simplified_chinese(raw_response)
    markdown_path = target / f"{stem}.md"
    json_path = target / f"{stem}.json"
    markdown_path.write_text(_markdown(normalized, transcript_path), encoding="utf-8")
    json_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return SavedMinutes(markdown_path=markdown_path, json_path=json_path, payload=normalized)
