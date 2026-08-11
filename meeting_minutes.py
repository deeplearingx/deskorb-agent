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
from config import MEETING_CHUNK_CHARS, MEETING_MERGE_BATCH


MAX_TRANSCRIPT_CHARS = 80_000
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_SENTENCE_BOUNDARY = re.compile(r"[.\u3002\uff01\uff1f!?;\uff1b\n]")


@dataclass(frozen=True)
class SavedMinutes:
    markdown_path: Path
    json_path: Path
    payload: dict[str, Any]

def _cut_transcript_piece(text: str, max_chars: int) -> tuple[str, str]:
    """Cut one oversized line at the last sentence boundary within the limit."""
    if len(text) <= max_chars:
        return text, ""
    window = text[:max_chars]
    boundaries = [match.end() for match in _SENTENCE_BOUNDARY.finditer(window)]
    cut = max(boundaries, default=0)
    if cut < max_chars // 2:
        whitespace = [index for index, char in enumerate(window) if char.isspace()]
        cut = max(whitespace, default=0)
    if cut <= 0:
        cut = max_chars
    return text[:cut].rstrip(), text[cut:].lstrip()


def split_transcript(
    transcript: str,
    *,
    max_chars: int = MEETING_CHUNK_CHARS,
) -> list[str]:
    """Split a WhisperX transcript into bounded, line/sentence-aware chunks."""
    limit = max(1, int(max_chars))
    text = to_simplified_chinese(str(transcript or "").strip())
    if not text:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        lines = [text]
    chunks: list[str] = []
    current = ""
    for original_line in lines:
        remainder = original_line
        while remainder:
            piece, remainder = _cut_transcript_piece(remainder, limit)
            if not piece:
                break
            candidate = f"{current}\n{piece}" if current else piece
            if current and len(candidate) > limit:
                chunks.append(current)
                current = piece
            else:
                current = candidate
            if not remainder:
                break
            if current:
                chunks.append(current)
                current = ""
    if current:
        chunks.append(current)
    return chunks

def _minutes_json_contract() -> str:
    return (
        '{"title":"\u4f1a\u8bae\u4e3b\u9898","summary":"\u4e0d\u8d85\u8fc7200\u5b57\u7684\u6458\u8981",'
        '"key_points":["\u5173\u952e\u8ba8\u8bba\u70b9"],"decisions":["\u5df2\u786e\u8ba4\u51b3\u7b56"],'
        '"action_items":[{"task":"\u5f85\u529e\u4e8b\u9879","owner":"\u8d1f\u8d23\u4eba\u6216\u672a\u6307\u5b9a",'
        '"deadline":"\u622a\u6b62\u65f6\u95f4\u6216\u672a\u6307\u5b9a","status":"\u5f85\u5904\u7406"}],'
        '"questions":["\u5c1a\u672a\u89e3\u51b3\u7684\u95ee\u9898"]}'
    )


_MEETING_MINUTES_INSTRUCTIONS = """# 系统指令
## 角色定位
你是专业结构化会议纪要智能处理引擎，专职完成语音转录文稿的信息萃取、内容规整、结构化输出。严格基于原始转录文本工作，禁止臆造、推演不存在的信息。

## 输入规范
输入文本为语音识别原始结果，包含说话人标记格式：SPEAKER_00、SPEAKER_01……；文本存在口语助词、重复语句、识别错别字、无效停顿、无关闲聊、语句断裂等噪声。

## 前置处理规则（必须依次执行）
1. 噪声清洗：移除寒暄、语气词、重复赘述、无效插话、识别乱码；修正明显同音识别错误。
2. 语义归并：将同一发言人连续碎片化发言合并，不保留流水式原始对话。
3. 发言人关联：关键决议、工作指派、核心观点绑定发言人；普通交流讨论无需逐句挂载发言标识。
4. 信息校验：原始文本未提及的时间、人员、方案、结论一律不得自行补充，缺失信息统一标注【信息未明确】。

## 强制输出结构，不可调整模块顺序、不可删减模块，统一使用Markdown格式
# 会议纪要
## 1. 基础信息
- 会议主题：根据对话内容提炼精准主题
- 参会发言人：罗列全部出现的SPEAKER编号
- 会议简述：80–150字，说明本次会议目标与整体讨论范围

## 2. 议题研讨与核心共识
按议题分类梳理各方观点、方案讨论、分歧内容、最终达成的统一结论。分层分点撰写，语言书面简洁。

## 3. 行动待办清单（核心模块）
统一格式：【发言人】｜工作任务｜完成时限｜前置条件/备注
无明确时限填写：待定；无前置条件填写：无

## 4. 悬置待确认事项
记录本次会议未能敲定、需要后续调研、下次会议继续研讨的问题与分歧点。

## 5. 风险、卡点与补充说明
记录项目风险、技术阻碍、资源需求、约束条件等其他重要信息；无内容则填写：无

## 输出硬性约束
1. 只输出会议纪要正文，禁止前置开场白、后置多余解释；
2. 行文正式商务风格，禁用网络用语、情绪化表述；
3. 客观中立记录内容，不对发言观点做评价；
4. 严禁简单复制粘贴原始对话，必须提炼浓缩；
5. 若输入有效内容过少，如实说明，不强行填充篇幅。"""


def _build_markdown_prompt(context: str, transcript: str) -> str:
    text = to_simplified_chinese(str(transcript or "").strip())
    return _MEETING_MINUTES_INSTRUCTIONS + "\n\n" + context + text


def build_minutes_chunk_prompt(chunk: str, *, index: int, total: int) -> str:
    """Build the approved Markdown prompt for one bounded transcript chunk."""
    return _build_markdown_prompt(
        f"当前为完整会议的第 {index}/{total} 段。仅根据本段转录提取事实，未出现的信息标注【信息未明确】。\n"
        "本段会议转录文本：\n",
        chunk,
    )


def build_merge_minutes_prompt(
    partials: Iterable[Mapping[str, Any]],
    *,
    level: int = 1,
) -> str:
    """Build the approved Markdown prompt that merges structured partial summaries."""
    items: list[dict[str, Any]] = []
    for item in partials:
        if not isinstance(item, Mapping):
            continue
        value = dict(item)
        value.pop("raw_markdown", None)
        items.append(value)
    encoded = json.dumps(items, ensure_ascii=False, indent=2)
    return _build_markdown_prompt(
        f"当前为第 {level} 层合并。以下是各分段摘要的结构化内容，请去除重复、保留有事实依据的决策和待办。\n"
        "各分段摘要：\n",
        encoded,
    )


def build_minutes_prompt(transcript: str, *, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """Build the approved Markdown prompt for the complete transcript."""
    text = to_simplified_chinese(str(transcript or "").strip())
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[转写过长，后续内容已截断]"
    return _build_markdown_prompt("以下为完整会议转录文本：\n", text)

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
            speaker = _text(
                item.get("speaker") or item.get("owner") or item.get("负责人"),
                "【信息未明确】",
            )
            result.append({
                "speaker": speaker,
                "task": task,
                "owner": speaker,
                "deadline": _text(
                    item.get("deadline") or item.get("due") or item.get("截止时间"),
                    "待定",
                ),
                "notes": _text(
                    item.get("notes") or item.get("prerequisite") or item.get("备注"),
                    "无",
                ),
                "status": _text(item.get("status") or item.get("状态"), "待处理"),
            })
        elif str(item).strip():
            result.append({
                "speaker": "【信息未明确】",
                "task": _text(item),
                "owner": "【信息未明确】",
                "deadline": "待定",
                "notes": "无",
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


_MARKDOWN_FENCE_RE = re.compile(
    r"^\s*\x60{3}(?:markdown|md)?\s*(.*?)\s*\x60{3}\s*$",
    re.IGNORECASE | re.DOTALL,
)
_MARKDOWN_SECTION_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")
_ACTION_LINE_RE = re.compile(r"^\s*\u3010(?P<speaker>[^\u3011]+)\u3011\s*[\uFF5C|]\s*(?P<task>.*?)\s*[\uFF5C|]\s*(?P<deadline>.*?)\s*[\uFF5C|]\s*(?P<notes>.*?)\s*$")


def _clean_markdown_response(response: str) -> str:
    text = str(response or "").strip()
    fenced = _MARKDOWN_FENCE_RE.match(text)
    return fenced.group(1).strip() if fenced else text


def _markdown_sections(text: str) -> dict[str, str]:
    matches = list(_MARKDOWN_SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        number = re.match(r"^(\d+)\.", heading)
        if not number:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[number.group(1)] = text[start:end].strip()
    return sections


def _markdown_label(body: str, label: str) -> str:
    pattern = rf"(?m)^\s*(?:[-*+]\s*)?{re.escape(label)}\s*[：:]\s*(.*?)\s*$"
    match = re.search(pattern, body)
    return _text(match.group(1)) if match else ""


def _markdown_items(body: str) -> list[str]:
    values: list[str] = []
    for raw_line in str(body or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("|---") or line.startswith("| ---"):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        if line.startswith("|") and line.endswith("|"):
            continue
        if line in {"\u65e0", "\u6682\u65e0", "\u65e0\u5185\u5bb9"}:
            continue
        if line:
            values.append(_text(line))
    return values


def _markdown_speakers(value: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"SPEAKER_\d+", value or "", flags=re.IGNORECASE)))


def _markdown_actions(body: str) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    for raw_line in str(body or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("|---") or line.startswith("| ---"):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        match = _ACTION_LINE_RE.match(line)
        if not match:
            continue
        speaker = _text(match.group("speaker"), "\u3010\u4fe1\u606f\u672a\u660e\u3011")
        task = _text(match.group("task"))
        if not task:
            continue
        deadline = _text(match.group("deadline"), "\u5f85\u5b9a")
        notes = _text(match.group("notes"), "\u65e0")
        actions.append({
            "speaker": speaker,
            "task": task,
            "owner": speaker,
            "deadline": deadline,
            "notes": notes,
            "status": "\u5f85\u5904\u7406",
        })
    return actions


def _parse_markdown_response(response: str) -> Optional[dict[str, Any]]:
    text = _clean_markdown_response(response)
    if not text or not re.search(r"(?m)^#\s*\u4f1a\u8bae\u7eaa\u8981\s*$", text):
        return None
    sections = _markdown_sections(text)
    if not sections:
        return None

    basic = sections.get("1", "")
    topics = _markdown_items(sections.get("2", ""))
    actions = _markdown_actions(sections.get("3", ""))
    pending = _markdown_items(sections.get("4", ""))
    risks = _markdown_items(sections.get("5", ""))
    subject = _markdown_label(basic, "\u4f1a\u8bae\u4e3b\u9898") or "\u4f1a\u8bae\u7eaa\u8981"
    speakers = _markdown_speakers(_markdown_label(basic, "\u53c2\u4f1a\u53d1\u8a00\u4eba"))
    brief = _markdown_label(basic, "\u4f1a\u8bae\u7b80\u8ff0")
    decisions = [
        item for item in topics
        if re.search(r"\u5171\u8bc6|\u51b3\u5b9a|\u7ed3\u8bba|\u786e\u8ba4", item)
    ]
    return {
        "title": _text(subject, "\u4f1a\u8bae\u7eaa\u8981"),
        "meeting_subject": _text(subject, "\u4f1a\u8bae\u7eaa\u8981"),
        "speakers": speakers,
        "meeting_brief": brief,
        "summary": brief or "\u672a\u751f\u6210\u4f1a\u8bae\u6458\u8981\u3002",
        "topics": topics,
        "key_points": topics,
        "decisions": decisions,
        "action_items": actions,
        "open_items": pending,
        "questions": pending,
        "risks": risks,
        "raw_markdown": text,
    }

def parse_minutes_response(response: str) -> dict[str, Any]:
    """Normalize legacy JSON or the approved Markdown response into one stable schema."""
    markdown = _parse_markdown_response(response)
    if markdown is not None:
        return markdown

    raw = _decode_json_response(response)
    if raw is None:
        return {
            "title": "会议纪要",
            "meeting_subject": "会议纪要",
            "summary": _text(response, "未生成会议摘要。"),
            "meeting_brief": _text(response, "未生成会议摘要。"),
            "speakers": [],
            "topics": [],
            "key_points": [],
            "decisions": [],
            "action_items": [],
            "open_items": [],
            "questions": [],
            "risks": [],
        }
    normalized = {
        "title": _text(raw.get("title") or raw.get("主题"), "会议纪要"),
        "meeting_subject": _text(raw.get("meeting_subject") or raw.get("主题") or raw.get("title"), "会议纪要"),
        "summary": _text(raw.get("summary") or raw.get("摘要"), "未生成会议摘要。"),
        "meeting_brief": _text(raw.get("meeting_brief") or raw.get("会议简述") or raw.get("summary"), "未生成会议摘要。"),
        "speakers": _markdown_speakers(_text(raw.get("speakers") or raw.get("参会发言人"))),
        "topics": _strings(raw.get("topics") or raw.get("议题") or raw.get("key_points") or raw.get("highlights") or raw.get("关键点")),
        "key_points": _strings(raw.get("key_points") or raw.get("highlights") or raw.get("关键点") or raw.get("topics") or raw.get("议题")),
        "decisions": _strings(raw.get("decisions") or raw.get("决策") or raw.get("结论")),
        "action_items": _action_items(raw.get("action_items") or raw.get("todos") or raw.get("待办")),
        "open_items": _strings(raw.get("open_items") or raw.get("悬置事项") or raw.get("questions") or raw.get("open_questions") or raw.get("待确认")),
        "questions": _strings(raw.get("questions") or raw.get("open_questions") or raw.get("待确认") or raw.get("open_items") or raw.get("悬置事项")),
        "risks": _strings(raw.get("risks") or raw.get("风险") or raw.get("卡点")),
    }

    if raw.get("chunk_count") is not None:
        normalized["chunk_count"] = int(raw["chunk_count"])
    if raw.get("summary_mode"):
        normalized["summary_mode"] = _text(raw["summary_mode"])
    if isinstance(raw.get("partial_summaries"), list):
        normalized["partial_summaries"] = raw["partial_summaries"]
    if raw.get("raw_markdown"):
        normalized["raw_markdown"] = _clean_markdown_response(str(raw["raw_markdown"]))
    return normalized

def _dedupe_text(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _text(value)
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def merge_minutes_payloads(
    partials: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Merge normalized partial payloads deterministically while preserving order."""
    normalized = [
        parse_minutes_response(json.dumps(dict(item), ensure_ascii=False))
        for item in partials
        if isinstance(item, Mapping)
    ]
    if not normalized:
        return parse_minutes_response("")
    title = next(
        (item["title"] for item in normalized if item.get("title") not in ("", "会议纪要")),
        "会议纪要",
    )
    briefs = _dedupe_text(item.get("meeting_brief") or item.get("summary") for item in normalized)
    speakers = _dedupe_text(
        speaker
        for item in normalized
        for speaker in item.get("speakers", [])
    )
    topics = _dedupe_text(
        topic
        for item in normalized
        for topic in (item.get("topics") or item.get("key_points") or [])
    )
    decisions = _dedupe_text(item for payload in normalized for item in payload.get("decisions", []))
    pending = _dedupe_text(
        item
        for payload in normalized
        for item in (payload.get("open_items") or payload.get("questions") or [])
    )
    risks = _dedupe_text(item for payload in normalized for item in payload.get("risks", []))
    actions: list[dict[str, str]] = []
    action_keys: set[tuple[str, str, str, str, str]] = set()
    for item in normalized:
        for action in _action_items(item.get("action_items")):
            key = (
                action["task"],
                action["owner"],
                action["deadline"],
                action.get("notes", "无"),
                action["status"],
            )
            if key not in action_keys:
                action_keys.add(key)
                actions.append(action)
    return {
        "title": title,
        "meeting_subject": title,
        "summary": "；".join(briefs),
        "meeting_brief": "；".join(briefs),
        "speakers": speakers,
        "topics": topics,
        "key_points": topics,
        "decisions": decisions,
        "action_items": actions,
        "open_items": pending,
        "questions": pending,
        "risks": risks,
    }

class MeetingMinutesBatchError(RuntimeError):
    """Raised when a chunk or merge request fails after partial work."""

    def __init__(self, stage: str, partials: list[dict[str, Any]], chunk_count: int, cause: BaseException):
        self.stage = stage
        self.partials = list(partials)
        self.chunk_count = int(chunk_count)
        self.cause = cause
        super().__init__(f"meeting minutes {stage} stage failed: {cause}")


@dataclass(frozen=True)
class BatchMinutesResult:
    payload: dict[str, Any]
    chunk_count: int
    summary_mode: str
    partials: tuple[dict[str, Any], ...]


def summarize_transcript_in_batches(
    transcript: str,
    ask,
    *,
    chunk_chars: int = MEETING_CHUNK_CHARS,
    merge_batch: int = MEETING_MERGE_BATCH,
    on_progress=None,
) -> BatchMinutesResult:
    """Run chunk summaries and hierarchical merges through a synchronous callback."""
    chunks = split_transcript(transcript, max_chars=chunk_chars)
    if not chunks:
        raise ValueError("meeting transcript is empty")
    batch_size = max(2, int(merge_batch))
    partials: list[dict[str, Any]] = []

    def notify(value: dict[str, Any]) -> None:
        if on_progress is not None:
            on_progress(dict(value))

    for index, chunk in enumerate(chunks, start=1):
        notify({"stage": "chunk", "current": index, "total": len(chunks)})
        try:
            response = str(ask(build_minutes_chunk_prompt(chunk, index=index, total=len(chunks))) or "").strip()
            if not response:
                raise RuntimeError("agent returned an empty chunk summary")
            partials.append(parse_minutes_response(response))
        except BaseException as exc:
            raise MeetingMinutesBatchError("chunk", partials, len(chunks), exc) from exc

    current = partials
    level = 1
    while len(current) > 1:
        groups = [current[offset:offset + batch_size] for offset in range(0, len(current), batch_size)]
        merged: list[dict[str, Any]] = []
        for group_index, group in enumerate(groups, start=1):
            notify({"stage": "merge", "current": group_index, "total": len(groups), "level": level})
            if len(group) == 1:
                merged.append(group[0])
                continue
            try:
                response = str(ask(build_merge_minutes_prompt(group, level=level)) or "").strip()
                if not response:
                    raise RuntimeError("agent returned an empty merge summary")
                merged.append(parse_minutes_response(response))
            except BaseException as exc:
                raise MeetingMinutesBatchError("merge", partials, len(chunks), exc) from exc
        current = merged
        level += 1

    return BatchMinutesResult(
        payload=current[0],
        chunk_count=len(chunks),
        summary_mode="single" if len(chunks) == 1 else "hierarchical",
        partials=tuple(partials),
    )


def _md_cell(value: Any) -> str:
    return _text(value).replace("|", "\\|")

def _markdown(payload: Mapping[str, Any], transcript_path: Optional[Path]) -> str:
    """Render the approved five-section meeting-minutes Markdown document."""
    title = _text(
        payload.get("meeting_subject") or payload.get("title"),
        "【信息未明确】",
    )
    speakers_value = payload.get("speakers")
    if isinstance(speakers_value, str):
        speakers = _markdown_speakers(speakers_value)
    else:
        speakers = _strings(speakers_value)
    speakers_text = "、".join(speakers) if speakers else "【信息未明确】"
    brief = _text(
        payload.get("meeting_brief") or payload.get("summary"),
        "【信息未明确】",
    )
    topics = _dedupe_text(
        list(payload.get("topics") or payload.get("key_points") or [])
        + list(payload.get("decisions") or [])
    )
    actions = _action_items(payload.get("action_items"))
    pending = _strings(payload.get("open_items") or payload.get("questions"))
    risks = _strings(payload.get("risks"))

    lines = [
        "# 会议纪要",
        "",
        "## 1. 基础信息",
        f"- 会议主题：{title}",
        f"- 参会发言人：{speakers_text}",
        f"- 会议简述：{brief}",
        "",
        "## 2. 议题研讨与核心共识",
    ]
    lines.extend([f"- {item}" for item in topics] or ["无"])
    lines.extend(["", "## 3. 行动待办清单（核心模块）"])
    if actions:
        lines.extend(
            f"【{_text(item.get('speaker') or item.get('owner'), '【信息未明确】')}】｜"
            f"{_text(item.get('task'), '【信息未明确】')}｜"
            f"{_text(item.get('deadline'), '待定')}｜"
            f"{_text(item.get('notes') or item.get('prerequisite'), '无')}"
            for item in actions
        )
    else:
        lines.append("无")
    lines.extend(["", "## 4. 悬置待确认事项"])
    lines.extend([f"- {item}" for item in pending] or ["无"])
    lines.extend(["", "## 5. 风险、卡点与补充说明"])
    lines.extend([f"- {item}" for item in risks] or ["无"])
    return "\n".join(lines).rstrip() + "\n"

def save_minutes(
    output_dir: Path,
    stem: str,
    payload: Mapping[str, Any],
    *,
    transcript_path: Optional[Path] = None,
    raw_response: str = "",
    chunk_count: Optional[int] = None,
    summary_mode: str = "",
    partial_summaries: Optional[Iterable[Mapping[str, Any]]] = None,
) -> SavedMinutes:
    """Write a human-readable Markdown file and machine-readable JSON file."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    normalized = parse_minutes_response(json.dumps(dict(payload), ensure_ascii=False))
    normalized["created_at"] = datetime.now(timezone.utc).isoformat()
    if transcript_path:
        normalized["transcript_path"] = str(Path(transcript_path))
    if chunk_count is not None:
        normalized["chunk_count"] = int(chunk_count)
    if summary_mode:
        normalized["summary_mode"] = _text(summary_mode)
    if partial_summaries is not None:
        normalized["partial_summaries"] = [dict(item) for item in partial_summaries if isinstance(item, Mapping)]
    if raw_response:
        normalized["agent_response"] = to_simplified_chinese(raw_response)
    markdown_path = target / f"{stem}.md"
    json_path = target / f"{stem}.json"
    markdown_path.write_text(_markdown(normalized, transcript_path), encoding="utf-8")
    json_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return SavedMinutes(markdown_path=markdown_path, json_path=json_path, payload=normalized)
def save_partial_minutes(
    output_dir: Path,
    stem: str,
    partial_summaries: Iterable[Mapping[str, Any]],
    *,
    stage: str,
    chunk_count: int,
    transcript_path: Optional[Path] = None,
) -> Path:
    """Persist completed partial summaries when a long-meeting request fails."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    normalized = [
        parse_minutes_response(json.dumps(dict(item), ensure_ascii=False))
        for item in partial_summaries
        if isinstance(item, Mapping)
    ]
    payload: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stage": _text(stage),
        "chunk_count": int(chunk_count),
        "partial_summaries": normalized,
    }
    if transcript_path:
        payload["transcript_path"] = str(Path(transcript_path))
    path = target / f"{stem}.partial.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
