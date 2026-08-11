# 长会议纪要支持 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将会议纪要改为可处理长转写的分段摘要、层级合并流程，避免一次请求超过模型上下文。

**Architecture:** `meeting_minutes.py` 提供纯函数分段和提示词/结果合并；`worker.py` 在后台线程顺序运行分段智能体请求并发送进度，最后发送一份结构化结果；`deskorb_agent.py` 只负责启动批处理、显示进度和复用现有保存器。完整录音和 WhisperX 转写仍由现有流程负责。

**Tech Stack:** Python 3.10+, `unittest`, 现有 `CodexWorker`/API/Agent 后端、Tkinter UI、JSON/Markdown 文件输出。

## Global Constraints

- 分段默认上限为 8,000 个字符，可通过 `DESKORB_AGENT_MEETING_CHUNK_CHARS` 覆盖。
- 合并默认每批最多 8 个摘要，可通过 `DESKORB_AGENT_MEETING_MERGE_BATCH` 覆盖。
- 分段和合并请求必须使用 `ephemeral=True`，不得污染普通聊天上下文。
- 录音、TXT、JSON 和现有 Markdown/JSON 纪要路径保持兼容。
- 发生分段或合并失败时，不得伪造完整最终纪要；至少保留原始转写并向 UI 报错。

---

### Task 1: Add pure long-transcript helpers

**Files:**
- Modify: `meeting_minutes.py`
- Modify: `config.py`
- Test: `tests/test_long_meeting_minutes.py`

**Interfaces:**
- Produces `split_transcript(transcript: str, max_chars: int = MEETING_CHUNK_CHARS) -> list[str]`.
- Produces `build_chunk_minutes_prompt(chunk: str, index: int, total: int) -> str`.
- Produces `build_merge_minutes_prompt(partials: list[Mapping[str, Any]], level: int = 1) -> str`.
- Produces `merge_minutes_payloads(partials: Iterable[Mapping[str, Any]]) -> dict[str, Any]` for deterministic local fallback/normalization.

- [ ] **Step 1: Write failing tests for line-aware splitting and prompts**

```python
def test_split_transcript_keeps_short_text_and_bounds_long_text():
    source = "\n".join(f"[00:{i:02d}] A：第{i}句。" for i in range(12))
    parts = split_transcript(source, max_chars=80)
    assert len(parts) > 1
    assert "第0句" in parts[0]
    assert "第11句" in parts[-1]
    assert all(len(part) <= 80 for part in parts)

def test_merge_prompt_contains_only_structured_partials():
    prompt = build_merge_minutes_prompt([{"summary": "摘要一", "key_points": []}])
    assert "摘要一" in prompt
    assert "完整会议逐字稿" not in prompt
    assert '"action_items"' in prompt
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/test_long_meeting_minutes.py -q`

Expected: FAIL because the new helper functions do not exist.

- [ ] **Step 3: Implement bounded sentence/line splitting and provider-neutral prompts**

Add environment-backed constants in `config.py` and implement splitting in `meeting_minutes.py`. Split on newline/Chinese and English sentence punctuation first; if one line is still over the limit, split at the nearest whitespace/punctuation and finally by character. Keep the existing JSON schema in every prompt.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `python -m pytest tests/test_long_meeting_minutes.py -q`

Expected: PASS.

### Task 2: Add deterministic hierarchical merge behavior

**Files:**
- Modify: `meeting_minutes.py`
- Test: `tests/test_long_meeting_minutes.py`

**Interfaces:**
- Produces `build_meeting_summary_requests(transcript: str, chunk_chars: int, merge_batch: int) -> list[tuple[str, str]]` where each tuple is `(stage, prompt)` and stage is `chunk` or `merge`.

- [ ] **Step 1: Write failing tests for multi-level request planning**

```python
def test_request_plan_merges_many_chunks_in_batches():
    transcript = "\n".join(f"[00:{i:02d}] A：第{i}句。" for i in range(30))
    requests = build_meeting_summary_requests(transcript, chunk_chars=60, merge_batch=2)
    stages = [stage for stage, _ in requests]
    assert stages.count("chunk") > 2
    assert stages[-1] == "merge"
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `python -m pytest tests/test_long_meeting_minutes.py::test_request_plan_merges_many_chunks_in_batches -q`

Expected: FAIL because request planning is not implemented.

- [ ] **Step 3: Implement bounded hierarchical merge planning**

Keep at most `merge_batch` partial payloads in a merge prompt. If the number of partials exceeds that bound, create intermediate merge prompts and repeat until one final payload remains. The runtime will execute these prompts sequentially; the planner exposes only pure, testable boundaries.

- [ ] **Step 4: Run all helper tests**

Run: `python -m pytest tests/test_long_meeting_minutes.py -q`

Expected: PASS.

### Task 3: Execute batch requests in the worker without blocking the UI

**Files:**
- Modify: `worker.py`
- Test: `tests/test_meeting_minutes_batch_worker.py`

**Interfaces:**
- Adds `CodexWorker.ask_meeting_minutes_batch(transcript: str)`.
- Adds internal `_run_meeting_minutes_batch(transcript: str)` that emits prefixed `progress`, `delta`, `error`, and `turn_done` events.

- [ ] **Step 1: Write failing tests for event order and isolation**

```python
def test_batch_worker_emits_chunk_progress_then_final_result():
    worker = make_fake_worker_with_responses([
        '{"summary":"段一"}', '{"summary":"段二"}', '{"summary":"合并"}',
    ])
    worker._run_meeting_minutes_batch("[00:00] A：一。\n[00:01] B：二。", chunk_chars=15)
    kinds = [kind for kind, _ in worker.ui.items]
    assert "meeting_minutes_progress" in kinds
    assert kinds[-2:] == ["meeting_minutes_delta", "meeting_minutes_turn_done"]
```

- [ ] **Step 2: Run the worker test and verify it fails**

Run: `python -m pytest tests/test_meeting_minutes_batch_worker.py -q`

Expected: FAIL because the batch request API and runner do not exist.

- [ ] **Step 3: Implement isolated sequential execution**

Add a request kind handled by `run()`. Temporarily route each ephemeral `_run_turn` through a capture channel, parse each response with `parse_minutes_response`, and send only progress events to the original prefixed UI channel. Execute merge prompts in the same worker thread so normal UI operations remain responsive. On success emit one JSON `delta` followed by `turn_done`; on failure emit `error` and `turn_done` without a final result.

- [ ] **Step 4: Run focused worker tests**

Run: `python -m pytest tests/test_meeting_minutes_batch_worker.py -q`

Expected: PASS.

### Task 4: Wire the UI and persistence to batch results

**Files:**
- Modify: `deskorb_agent.py`
- Modify: `meeting_minutes.py`
- Test: `tests/test_meeting_minutes_overlay.py`

- [ ] **Step 1: Write failing tests for progress and metadata**

```python
def test_minutes_progress_does_not_save_partial_payload_as_final():
    overlay = make_overlay_for_test()
    overlay._handle_meeting_minutes_event("progress", {"stage": "chunk", "current": 1, "total": 3})
    assert overlay.saved == []

def test_saved_minutes_records_summary_mode_and_chunk_count(tmp_path):
    saved = save_minutes(tmp_path, "meeting", {"summary": "合并"}, chunk_count=3, summary_mode="hierarchical")
    payload = json.loads(saved.json_path.read_text(encoding="utf-8"))
    assert payload["chunk_count"] == 3
    assert payload["summary_mode"] == "hierarchical"
```

- [ ] **Step 2: Run the UI/persistence tests and verify they fail**

Run: `python -m pytest tests/test_meeting_minutes_overlay.py -q`

Expected: FAIL because the UI does not understand progress and persistence has no metadata arguments.

- [ ] **Step 3: Implement UI wiring and metadata-compatible saving**

On transcription completion call `ask_meeting_minutes_batch` instead of building one full prompt. Render short progress messages, collect the final JSON delta, and reuse `save_minutes` with `chunk_count` and `summary_mode`. Preserve the existing single-response event behavior for compatibility.

- [ ] **Step 4: Run meeting tests**

Run: `python -m pytest tests/test_meeting_*.py -q`

Expected: PASS.

### Task 5: Document and verify the end-to-end feature

**Files:**
- Modify: `README.md`
- Test: `tests/test_long_meeting_minutes.py`

- [ ] **Step 1: Add an end-to-end simulated long-meeting test**

Use a generated transcript with at least 20 segments and fake agent responses. Assert the original transcript path remains present and the final saved Markdown contains the merged summary.

- [ ] **Step 2: Update README configuration and behavior**

Document `DESKORB_AGENT_MEETING_CHUNK_CHARS`, `DESKORB_AGENT_MEETING_MERGE_BATCH`, progress behavior, and the fact that long meeting processing continues in the background.

- [ ] **Step 3: Run the full verification suite**

Run: `python -m pytest -q`; `python -m compileall -q .`; `git diff --check`.

Expected: all tests pass, compilation exits 0, and `git diff --check` reports no whitespace errors.
