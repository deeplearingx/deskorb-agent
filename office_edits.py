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


class OfficePlanError(ValueError):
    """A plan is unsafe, stale, or could not be applied to the live Office source."""


@dataclass(frozen=True)
class WordTextEdit:
    locator: str
    expected_value: str
    value: str


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

    targets = {target.locator: target for target in snapshot.targets}
    edits: list[WordTextEdit | ExcelCellEdit] = []
    seen: set[str] = set()
    for raw_edit in raw_plan["edits"]:
        edit = _parse_edit(snapshot.kind, raw_edit, targets)
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
            current_value = _word_target_value_or_empty(targets, edit.locator)
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
        _verify_targets(snapshot.kind, snapshot.expected_root, plan)
    except OfficePlanError:
        raise
    except Exception as exc:
        raise OfficePlanError(
            "Office wrote changes but could not verify them. Inspect the document and use Office Undo if needed."
        ) from exc
    count = len(plan.edits)
    return OfficeApplyResult(count, count, "Changes were written to the open document and were not saved.")


def _parse_edit(kind: str, raw_edit: Any, targets: dict[str, OfficeTarget]):
    if not isinstance(raw_edit, dict):
        raise OfficePlanError("Every Office edit must be a JSON object.")
    if kind == "word":
        _require_exact_keys(raw_edit, {"type", "locator", "expected_value", "value"}, "Word edit")
        if raw_edit["type"] != "word_replace_text":
            raise OfficePlanError("Word edits must use word_replace_text.")
        locator = raw_edit["locator"]
        if not isinstance(locator, str) or not isinstance(raw_edit["expected_value"], str) or not isinstance(raw_edit["value"], str):
            raise OfficePlanError("Word edits require string locator, expected_value, and value fields.")
        target = _target_for(locator, targets)
        if target.value != raw_edit["expected_value"]:
            raise OfficePlanError(f"The expected content for {locator} does not match the snapshot.")
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
            matches = _word_target_value_or_empty(targets, edit.locator) == edit.expected_value
        else:
            target = _target_for(edit.locator, targets)
            matches = target.value == edit.expected_value and target.formula == edit.expected_formula
        if not matches:
            raise OfficePlanError(
                f"The Office target {edit.locator} changed since the preview. Read it again before applying edits."
            )


def _word_target_value_or_empty(targets: dict[str, OfficeTarget], locator: str) -> str:
    """Word snapshots omit empty paragraphs, but an omitted paragraph is still editable."""
    target = targets.get(locator)
    if target is not None:
        return str(target.value)
    if re.fullmatch(r"paragraph:\d+", locator):
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


def _verify_targets(kind: str, expected_root: int, plan: OfficeEditPlan) -> None:
    def verify(document: Any) -> None:
        for edit in plan.edits:
            if kind == "word":
                actual = _read_word_text(document, edit.locator)
                expected = _normalize_word_text(edit.value)
            elif edit.formula is not None:
                actual = str(_resolve_excel_cell(document, edit.locator).Formula or "")
                expected = edit.formula
            else:
                actual = _resolve_excel_cell(document, edit.locator).Value2
                expected = edit.value
            if actual != expected:
                raise OfficePlanError(f"Office did not verify the requested change at {edit.locator}.")

    _with_active_document(kind, expected_root, None, verify)


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
        application = client.GetActiveObject("Word.Application" if kind == "word" else "Excel.Application")
        active_root = int(root_window(application.ActiveWindow.Hwnd) or 0)
        if active_root != expected_root:
            raise OfficePlanError("The active Office document no longer matches the previewed window.")
        callback(application.ActiveDocument if kind == "word" else application.ActiveWorkbook)
    finally:
        if initialized:
            pythoncom.CoUninitialize()


def _write_word_text(document: Any, edit: WordTextEdit) -> None:
    source_range = _resolve_word_range(document, edit.locator)
    writable = source_range.Duplicate
    writable.End = writable.End - 1
    writable.Text = edit.value


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
