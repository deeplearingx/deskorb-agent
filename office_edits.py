"""Validate previewed Office edit plans and apply them only after local consent."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable

from office_sources import (
    OfficeSnapshot,
    OfficeTarget,
    _load_com_modules,
    _normalize_word_text,
    read_active_office_snapshot,
    root_window,
)
from word_sources import resolve_word_application_for_window


class OfficePlanError(ValueError):
    """A plan is unsafe, stale, or could not be applied to the live Office source."""


@dataclass(frozen=True)
class WordTextEdit:
    locator: str
    expected_value: str
    value: str
    mode: str = "replace"


@dataclass(frozen=True)
class ExcelCellEdit:
    locator: str
    expected_value: Any
    expected_formula: str
    value: Any | None = None
    formula: str | None = None


@dataclass(frozen=True)
class OfficeEditPlan:
    kind: str
    snapshot_fingerprint: str
    edits: tuple[WordTextEdit | ExcelCellEdit, ...]


@dataclass(frozen=True)
class OfficeApplyResult:
    applied: int
    verified: int
    message: str


@dataclass(frozen=True)
class OfficeEditRecord:
    operation_id: str
    kind: str
    identity: str
    edits: tuple[WordTextEdit | ExcelCellEdit, ...]


def parse_office_plan(snapshot: OfficeSnapshot, raw_json: str) -> tuple[str, OfficeEditPlan | None]:
    """Parse the model's one-object response into a locally validated edit plan."""
    try:
        envelope = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise OfficePlanError("The Office response was not valid JSON, so no changes were prepared.") from exc
    _require_exact_keys(envelope, {"answer", "plan"}, "Office response")
    answer = envelope["answer"]
    if not isinstance(answer, str) or not answer.strip():
        raise OfficePlanError("The Office response must contain a non-empty answer.")
    raw_plan = envelope["plan"]
    if raw_plan is None:
        return answer.strip(), None
    _require_exact_keys(raw_plan, {"kind", "snapshot_fingerprint", "edits"}, "Office plan")
    if raw_plan["kind"] != snapshot.kind:
        raise OfficePlanError("The proposed Office plan has the wrong document type.")
    if raw_plan["snapshot_fingerprint"] != snapshot.fingerprint:
        raise OfficePlanError("The proposed Office plan does not match the current snapshot.")
    if not isinstance(raw_plan["edits"], list) or not raw_plan["edits"]:
        raise OfficePlanError("The Office plan must contain at least one edit.")

    edits: list[WordTextEdit | ExcelCellEdit] = []
    seen: set[str] = set()
    for raw_edit in raw_plan["edits"]:
        edit = _parse_edit(snapshot.kind, raw_edit, snapshot)
        locator = edit.locator
        if locator in seen:
            raise OfficePlanError(f"The Office plan contains duplicate edits for {locator}.")
        seen.add(locator)
        edits.append(edit)
    return answer.strip(), OfficeEditPlan(snapshot.kind, snapshot.fingerprint, tuple(edits))


def record_from_plan(plan: OfficeEditPlan, snapshot: OfficeSnapshot, operation_id: str) -> OfficeEditRecord:
    if plan.kind != snapshot.kind or plan.snapshot_fingerprint != snapshot.fingerprint:
        raise OfficePlanError("The Office edit plan does not match the snapshot for history recording.")
    return OfficeEditRecord(operation_id, snapshot.kind, snapshot.identity, tuple(plan.edits))


def inverse_plan(record: OfficeEditRecord, snapshot: OfficeSnapshot) -> OfficeEditPlan:
    if record.kind != snapshot.kind or record.identity != snapshot.identity:
        raise OfficePlanError("The Office history does not match the current document.")
    targets = {target.locator: target for target in snapshot.targets}
    inverse_edits: list[WordTextEdit | ExcelCellEdit] = []
    for edit in record.edits:
        if isinstance(edit, WordTextEdit):
            if edit.mode == "insert_paragraph_after":
                anchor = _target_for(edit.locator, targets)
                created_locator = _next_paragraph_locator(edit.locator)
                created = _target_for(created_locator, targets)
                if anchor.value != edit.expected_value or created.value != edit.value:
                    raise OfficePlanError(f"The Office target {edit.locator} changed since the recorded edit.")
                inverse_edits.append(WordTextEdit(created_locator, edit.value, "", "delete_paragraph"))
                continue
            if edit.mode == "delete_paragraph":
                current_value = _word_target_value_or_empty(targets, edit.locator, snapshot.paragraph_count)
                if current_value != edit.value:
                    raise OfficePlanError(f"The Office target {edit.locator} changed since the recorded edit.")
                anchor_locator = _previous_paragraph_locator(edit.locator)
                anchor = _target_for(anchor_locator, targets)
                inverse_edits.append(
                    WordTextEdit(anchor_locator, str(anchor.value), edit.expected_value, "insert_paragraph_after")
                )
                continue
            current_value = _word_target_value_or_empty(targets, edit.locator, snapshot.paragraph_count)
            if current_value != edit.value:
                raise OfficePlanError(f"The Office target {edit.locator} changed since the recorded edit.")
            inverse_edits.append(WordTextEdit(edit.locator, edit.value, edit.expected_value))
            continue
        target = _target_for(edit.locator, targets)
        if edit.formula is not None:
            if target.formula != edit.formula:
                raise OfficePlanError(f"The Office target {edit.locator} changed since the recorded edit.")
        elif target.formula or target.value != edit.value:
            raise OfficePlanError(f"The Office target {edit.locator} changed since the recorded edit.")
        if edit.expected_formula:
            inverse_edits.append(ExcelCellEdit(edit.locator, edit.value, edit.formula or "",
                                               value=None, formula=edit.expected_formula))
        else:
            inverse_edits.append(ExcelCellEdit(edit.locator, edit.value, edit.formula or "",
                                               value=edit.expected_value, formula=None))
    return OfficeEditPlan(record.kind, snapshot.fingerprint, tuple(inverse_edits))


def apply_office_plan(snapshot: OfficeSnapshot, plan: OfficeEditPlan) -> OfficeApplyResult:
    """Revalidate, write all confirmed edits, then read each edited target back."""
    if plan.kind != snapshot.kind or plan.snapshot_fingerprint != snapshot.fingerprint:
        raise OfficePlanError("The Office edit plan does not match the previewed document.")
    current = read_active_office_snapshot(snapshot.kind, snapshot.expected_root)
    if current.identity != snapshot.identity or current.fingerprint != snapshot.fingerprint:
        raise OfficePlanError("The Office document changed since the preview. Read it again before applying edits.")
    _validate_live_targets(current, plan)
    try:
        _apply_targets(snapshot.kind, snapshot.expected_root, plan)
    except OfficePlanError:
        raise
    except Exception as exc:
        raise OfficePlanError(
            "Office could not write all requested changes. Inspect the document and use Office Undo if needed."
        ) from exc
    try:
        selected = _verify_targets(snapshot.kind, snapshot.expected_root, plan)
    except OfficePlanError:
        raise
    except Exception as exc:
        raise OfficePlanError(
            "Office wrote changes but could not verify them. Inspect the document and use Office Undo if needed."
        ) from exc
    count = len(plan.edits)
    selection_note = (
        " Changed Office content was selected."
        if selected else
        " Office selection was unavailable; keep the DeskOrb preview as the change marker."
    )
    return OfficeApplyResult(
        count, count,
        "Changes were written to the open document and were not saved." + selection_note,
    )


def _parse_edit(kind: str, raw_edit: Any, snapshot: OfficeSnapshot):
    if not isinstance(raw_edit, dict):
        raise OfficePlanError("Every Office edit must be a JSON object.")
    targets = {target.locator: target for target in snapshot.targets}
    if kind == "word":
        _require_exact_keys(raw_edit, {"type", "locator", "expected_value", "value"}, "Word edit")
        edit_type = raw_edit["type"]
        if edit_type not in {"word_replace_text", "word_insert_paragraph_after"}:
            raise OfficePlanError(
                "Word edits must use word_replace_text or word_insert_paragraph_after."
            )
        locator = raw_edit["locator"]
        if not isinstance(locator, str) or not isinstance(raw_edit["expected_value"], str) or not isinstance(raw_edit["value"], str):
            raise OfficePlanError("Word edits require string locator, expected_value, and value fields.")
        target = targets.get(locator)
        if target is None:
            if (
                edit_type == "word_replace_text"
                and raw_edit["expected_value"] == ""
                and _is_known_word_paragraph(snapshot, locator)
            ):
                return WordTextEdit(locator, "", raw_edit["value"])
            if edit_type == "word_replace_text" and raw_edit["expected_value"] == "":
                repaired = _repair_legacy_end_insert(snapshot, locator, raw_edit["value"])
                if repaired is not None:
                    return repaired
            _target_for(locator, targets)
        if target.value != raw_edit["expected_value"]:
            raise OfficePlanError(f"The expected content for {locator} does not match the snapshot.")
        if edit_type == "word_insert_paragraph_after":
            if not _is_last_word_paragraph(snapshot, locator):
                raise OfficePlanError(
                    "word_insert_paragraph_after is only supported after the last non-empty body paragraph."
                )
            if not raw_edit["value"]:
                raise OfficePlanError("word_insert_paragraph_after requires non-empty value.")
            return WordTextEdit(locator, raw_edit["expected_value"], raw_edit["value"], "insert_paragraph_after")
        return WordTextEdit(locator, raw_edit["expected_value"], raw_edit["value"])

    _require_exact_keys(
        raw_edit,
        {"type", "sheet", "address", "expected_value", "expected_formula", "value", "formula"},
        "Excel edit",
    )
    if raw_edit["type"] != "excel_set_cell":
        raise OfficePlanError("Excel edits must use excel_set_cell.")
    sheet, address = raw_edit["sheet"], raw_edit["address"]
    if not isinstance(sheet, str) or not isinstance(address, str) or not sheet or not _EXCEL_ADDRESS.fullmatch(address):
        raise OfficePlanError("Excel edits require a sheet and a simple A1 cell address.")
    if raw_edit["formula"] is not None and not isinstance(raw_edit["formula"], str):
        raise OfficePlanError("Excel formula must be a string or null.")
    if (raw_edit["value"] is None) == (raw_edit["formula"] is None):
        raise OfficePlanError("Excel edits must set exactly one of value or formula.")
    locator = f"{sheet}!{address.replace('$', '')}"
    target = _target_for(locator, targets)
    expected_formula = "" if raw_edit["expected_formula"] is None else raw_edit["expected_formula"]
    if not isinstance(expected_formula, str):
        raise OfficePlanError("Excel expected_formula must be a string or null.")
    if target.value != raw_edit["expected_value"] or target.formula != expected_formula:
        raise OfficePlanError(f"The expected content for {locator} does not match the snapshot.")
    return ExcelCellEdit(locator, raw_edit["expected_value"], expected_formula, raw_edit["value"], raw_edit["formula"])


def _target_for(locator: str, targets: dict[str, OfficeTarget]) -> OfficeTarget:
    try:
        return targets[locator]
    except KeyError as exc:
        raise OfficePlanError(f"The target {locator} is not present in the snapshot.") from exc


def _validate_live_targets(current: OfficeSnapshot, plan: OfficeEditPlan) -> None:
    targets = {target.locator: target for target in current.targets}
    for edit in plan.edits:
        if isinstance(edit, WordTextEdit):
            if edit.mode == "insert_paragraph_after":
                target = _target_for(edit.locator, targets)
                matches = target.value == edit.expected_value
            else:
                matches = _word_target_value_or_empty(
                    targets, edit.locator, current.paragraph_count
                ) == edit.expected_value
        else:
            target = _target_for(edit.locator, targets)
            matches = target.value == edit.expected_value and target.formula == edit.expected_formula
        if not matches:
            raise OfficePlanError(
                f"The Office target {edit.locator} changed since the preview. Read it again before applying edits."
            )


def _word_target_value_or_empty(
    targets: dict[str, OfficeTarget], locator: str, paragraph_count: int | None = None
) -> str:
    """Word snapshots omit empty paragraphs, but an omitted paragraph is still editable."""
    target = targets.get(locator)
    if target is not None:
        return str(target.value)
    match = re.fullmatch(r"paragraph:(\d+)", locator)
    if match and (paragraph_count is None or int(match.group(1)) <= paragraph_count):
        return ""
    raise OfficePlanError(f"The target {locator} is not present in the snapshot.")


def _apply_targets(kind: str, expected_root: int, plan: OfficeEditPlan) -> None:
    def write(document: Any) -> None:
        for edit in plan.edits:
            if kind == "word":
                _write_word_text(document, edit)
            else:
                _write_excel_cell(document, edit)

    _with_active_document(kind, expected_root, plan.snapshot_fingerprint, write)


def _verify_targets(kind: str, expected_root: int, plan: OfficeEditPlan) -> bool:
    selected = True

    def verify(document: Any) -> None:
        nonlocal selected
        for edit in plan.edits:
            if kind == "word":
                if edit.mode == "insert_paragraph_after":
                    target_locator = _next_paragraph_locator(edit.locator)
                    actual = _read_word_text(document, target_locator)
                    expected = _normalize_word_text(edit.value)
                elif edit.mode == "delete_paragraph":
                    index = _paragraph_index(edit.locator)
                    if _word_paragraph_count(document) >= index:
                        raise OfficePlanError(f"Office did not verify the requested change at {edit.locator}.")
                    selected = False
                    continue
                else:
                    target_locator = edit.locator
                    actual = _read_word_text(document, target_locator)
                    expected = _normalize_word_text(edit.value)
            elif edit.formula is not None:
                actual = str(_resolve_excel_cell(document, edit.locator).Formula or "")
                expected = edit.formula
            else:
                actual = _resolve_excel_cell(document, edit.locator).Value2
                expected = edit.value
            if actual != expected:
                raise OfficePlanError(f"Office did not verify the requested change at {edit.locator}.")
            if kind == "word":
                selected = _focus_word_target(document, target_locator) and selected
            else:
                selected = _focus_excel_cell(document, edit.locator) and selected

    _with_active_document(kind, expected_root, None, verify)
    return selected


def _with_active_document(
    kind: str, expected_root: int, expected_fingerprint: str | None, callback: Callable[[Any], None]
) -> None:
    if expected_fingerprint is not None:
        current = read_active_office_snapshot(kind, expected_root)
        if current.fingerprint != expected_fingerprint:
            raise OfficePlanError("The Office document changed since the preview. Read it again before applying edits.")
    pythoncom, client = _load_com_modules()
    initialized = False
    try:
        pythoncom.CoInitialize()
        initialized = True
        if kind == "word":
            # GetActiveObject may return a different Word process when several
            # Word instances are running.  Use the same ROT/window resolver as
            # the read path so writes and verification target the previewed
            # document rather than an unrelated instance.
            application = resolve_word_application_for_window(pythoncom, client, expected_root)
            if application is None:
                application = client.GetActiveObject("Word.Application")
        else:
            application = client.GetActiveObject("Excel.Application")
        active_root = _application_root_for_edit(application, kind, expected_root)
        if active_root != expected_root:
            raise OfficePlanError("The active Office document no longer matches the previewed window.")
        callback(application.ActiveDocument if kind == "word" else application.ActiveWorkbook)
    finally:
        if initialized:
            pythoncom.CoUninitialize()


def _application_root_for_edit(application: Any, kind: str, expected_root: int) -> int:
    """Return the active window root, tolerating transient Word HWND failures."""
    for owner in (application, application.ActiveDocument if kind == "word" else None):
        if owner is None:
            continue
        try:
            hwnd = int(owner.ActiveWindow.Hwnd or 0)
            if hwnd:
                return int(root_window(hwnd) or 0)
        except Exception:
            continue
    if kind == "word" and expected_root:
        # The resolver already matched this Word application to expected_root;
        # Word can temporarily reject ActiveWindow.Hwnd during SDI focus changes.
        return int(expected_root)
    return 0


def _write_word_text(document: Any, edit: WordTextEdit) -> None:
    source_range = _resolve_word_range(document, edit.locator)
    if edit.mode == "insert_paragraph_after":
        if not re.fullmatch(r"paragraph:\d+", edit.locator):
            raise OfficePlanError("word_insert_paragraph_after requires a body paragraph locator.")
        inserted = source_range.Duplicate
        inserted.End = inserted.End - 1
        inserted.Collapse(0)
        format_source = _nearest_word_format_source(source_range, at_end=True)
        inserted.Text = "\r" + _word_text_for_write(edit.value)
        _copy_word_format(format_source, inserted)
        return
    if edit.mode == "delete_paragraph":
        source_range.Delete()
        return
    if edit.mode != "replace":
        raise OfficePlanError(f"Unsupported Word edit mode {edit.mode}.")
    writable = source_range.Duplicate
    writable.End = writable.End - 1
    old_value = _normalize_word_text(edit.expected_value)
    new_value = _normalize_word_text(edit.value)
    if new_value.startswith(old_value) and len(new_value) > len(old_value):
        inserted = source_range.Duplicate
        inserted.End = inserted.End - 1
        inserted.Collapse(0)
        format_source = _nearest_word_format_source(source_range, at_end=True)
        inserted.Text = _word_text_for_write(edit.value[len(edit.expected_value):])
        _copy_word_format(format_source, inserted)
        return
    if old_value and new_value.endswith(old_value) and len(new_value) > len(old_value):
        inserted = source_range.Duplicate
        inserted.End = inserted.Start
        format_source = _nearest_word_format_source(source_range, at_end=False)
        inserted.Text = _word_text_for_write(edit.value[:-len(edit.expected_value)])
        _copy_word_format(format_source, inserted)
        return
    format_source = _nearest_word_format_source(source_range, at_end=False)
    writable.Text = _word_text_for_write(edit.value)
    _copy_word_format(format_source, writable)


def _word_text_for_write(value: str) -> str:
    """Use Word's in-range manual line break for normalized newline characters."""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\v")


def _nearest_word_format_source(source_range: Any, at_end: bool) -> Any:
    try:
        source = source_range.Duplicate
        start = int(source_range.Start)
        end = int(source_range.End) - 1
        if end <= start:
            return source_range
        if at_end:
            source.Start = max(start, end - 1)
            source.End = end
        else:
            source.Start = start
            source.End = min(end, start + 1)
        return source
    except Exception:
        return source_range


def _copy_word_format(source_range: Any, target_range: Any) -> None:
    properties = (
        "Name", "Size", "Color", "ColorIndex", "Bold", "Italic", "Underline",
        "HighlightColorIndex", "StrikeThrough", "Subscript", "Superscript",
    )
    try:
        source_font = source_range.Font
        target_font = target_range.Font
    except Exception:
        return
    for name in properties:
        try:
            setattr(target_font, name, getattr(source_font, name))
        except Exception:
            continue


def _focus_word_target(document: Any, locator: str) -> bool:
    try:
        target = _resolve_word_range(document, locator).Duplicate
        target.End = target.End - 1
        target.Select()
        return True
    except Exception:
        return False


def _paragraph_index(locator: str) -> int:
    match = re.fullmatch(r"paragraph:(\d+)", locator)
    if not match:
        raise OfficePlanError(f"Word paragraph operation requires a paragraph locator, got {locator}.")
    return int(match.group(1))


def _next_paragraph_locator(locator: str) -> str:
    return f"paragraph:{_paragraph_index(locator) + 1}"


def _previous_paragraph_locator(locator: str) -> str:
    index = _paragraph_index(locator)
    if index <= 1:
        raise OfficePlanError("A Word inserted paragraph must have a previous paragraph anchor.")
    return f"paragraph:{index - 1}"


def _word_paragraph_count(document: Any) -> int:
    try:
        return int(document.Paragraphs.Count or 0)
    except Exception:
        try:
            return len(list(document.Paragraphs))
        except Exception:
            return 0


def _is_known_word_paragraph(snapshot: OfficeSnapshot, locator: str) -> bool:
    try:
        index = _paragraph_index(locator)
    except OfficePlanError:
        return False
    return snapshot.paragraph_count is not None and 1 <= index <= snapshot.paragraph_count


def _is_last_word_paragraph(snapshot: OfficeSnapshot, locator: str) -> bool:
    if snapshot.paragraph_count is None:
        return False
    index = _paragraph_index(locator)
    if not 1 <= index <= snapshot.paragraph_count:
        return False
    body_indices = _word_body_paragraph_indices(snapshot)
    return bool(body_indices) and index == max(body_indices)


def _repair_legacy_end_insert(snapshot: OfficeSnapshot, locator: str, value: str) -> WordTextEdit | None:
    if snapshot.paragraph_count is None or not value:
        return None
    try:
        index = _paragraph_index(locator)
    except OfficePlanError:
        return None
    body_indices = _word_body_paragraph_indices(snapshot)
    if not body_indices or index != max(body_indices) + 1:
        return None
    targets = {target.locator: target for target in snapshot.targets}
    anchor = targets.get(f"paragraph:{max(body_indices)}")
    if anchor is None:
        return None
    return WordTextEdit(anchor.locator, str(anchor.value), value, "insert_paragraph_after")


def _word_body_paragraph_indices(snapshot: OfficeSnapshot) -> list[int]:
    return [
        _paragraph_index(target.locator)
        for target in snapshot.targets
        if re.fullmatch(r"paragraph:\d+", target.locator)
    ]


def _focus_excel_cell(workbook: Any, locator: str) -> bool:
    try:
        _resolve_excel_cell(workbook, locator).Select()
        return True
    except Exception:
        return False


def _read_word_text(document: Any, locator: str) -> str:
    text = str(_resolve_word_range(document, locator).Text or "")
    return _normalize_word_text(text)


def _resolve_word_range(document: Any, locator: str) -> Any:
    paragraph = re.fullmatch(r"paragraph:(\d+)", locator)
    if paragraph:
        return _collection_item(document.Paragraphs, int(paragraph.group(1))).Range
    table = re.fullmatch(r"table:(\d+)/(\d+)/(\d+)", locator)
    if not table:
        raise OfficePlanError(f"Unsupported Word target {locator}.")
    table_object = _collection_item(document.Tables, int(table.group(1)))
    row = _collection_item(table_object.Rows, int(table.group(2)))
    return _collection_item(row.Cells, int(table.group(3))).Range


def _write_excel_cell(workbook: Any, edit: ExcelCellEdit) -> None:
    cell = _resolve_excel_cell(workbook, edit.locator)
    if edit.formula is not None:
        cell.Formula = edit.formula
    else:
        cell.Value2 = edit.value


def _resolve_excel_cell(workbook: Any, locator: str) -> Any:
    sheet_name, address = locator.rsplit("!", 1)
    for sheet in _iter_collection(workbook.Worksheets):
        if str(sheet.Name) == sheet_name:
            return sheet.Range(address)
    raise OfficePlanError(f"Excel worksheet '{sheet_name}' is no longer available.")


def _collection_item(collection: Any, index: int) -> Any:
    try:
        return collection.Item(index)
    except Exception:
        return list(collection)[index - 1]


def _iter_collection(collection: Any) -> list[Any]:
    try:
        return list(collection)
    except TypeError:
        return [_collection_item(collection, index) for index in range(1, int(collection.Count or 0) + 1)]


def _require_exact_keys(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise OfficePlanError(f"{label} has an unsupported JSON shape.")


_EXCEL_ADDRESS = re.compile(r"\$?[A-Za-z]{1,3}\$?[1-9]\d*")
