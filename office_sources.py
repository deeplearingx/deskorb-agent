"""Read the active Microsoft Office document through COM without persisting it.

The overlay calls this module from a worker thread.  Every returned snapshot is
an in-memory, immutable view of the Word document or Excel workbook that was
focused before the overlay opened.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from config import OFFICE_MAX_NONEMPTY_CELLS, OFFICE_MAX_RENDERED_CHARS
from win32utils import root_window, window_process_name


class OfficeSourceError(ValueError):
    """A user-facing problem while reading an active Office document."""


@dataclass(frozen=True)
class OfficeTarget:
    """One editable plain-text target in an Office snapshot."""

    locator: str
    label: str
    value: Any
    formula: str = ""


@dataclass(frozen=True)
class OfficeSnapshot:
    """Immutable Office content and identity used for preview and stale checks."""

    kind: str
    expected_root: int
    identity: str
    name: str
    rendered_text: str
    fingerprint: str
    targets: tuple[OfficeTarget, ...]
    has_unsaved_changes: bool


def is_word_window(hwnd: int) -> bool:
    return _is_process_window(hwnd, "winword.exe")


def is_excel_window(hwnd: int) -> bool:
    return _is_process_window(hwnd, "excel.exe")


def read_active_office_snapshot(kind: str, expected_hwnd: int) -> OfficeSnapshot:
    """Read Word or Excel only when its active root window is still expected_hwnd."""
    kind = str(kind).strip().casefold()
    if kind not in ("word", "excel"):
        raise OfficeSourceError("Office source kind must be Word or Excel.")
    if not _is_expected_window(kind, expected_hwnd):
        product = "Word" if kind == "word" else "Excel"
        raise OfficeSourceError(
            f"Focus a Microsoft {product} document, then open DeskOrb Agent and try again."
        )

    pythoncom, client = _load_com_modules()
    initialized = False
    try:
        pythoncom.CoInitialize()
        initialized = True
        prog_id = "Word.Application" if kind == "word" else "Excel.Application"
        application = client.GetActiveObject(prog_id)
        if kind == "word":
            return _read_word_snapshot(application, expected_hwnd)
        return _read_excel_snapshot(application, expected_hwnd)
    except OfficeSourceError:
        raise
    except Exception as exc:
        product = "Word" if kind == "word" else "Excel"
        raise OfficeSourceError(_office_error_message(product, exc)) from exc
    finally:
        if initialized:
            pythoncom.CoUninitialize()


def _is_expected_window(kind: str, hwnd: int) -> bool:
    return is_word_window(hwnd) if kind == "word" else is_excel_window(hwnd)


def _is_process_window(hwnd: int, process_name: str) -> bool:
    return bool(hwnd) and window_process_name(hwnd).casefold() == process_name


def _load_com_modules() -> tuple[Any, Any]:
    try:
        import pythoncom
        from win32com import client
    except ImportError as exc:
        raise OfficeSourceError("Office support is unavailable; install pywin32 and restart.") from exc
    return pythoncom, client


def _read_word_snapshot(application: Any, expected_hwnd: int) -> OfficeSnapshot:
    expected_root = _verify_active_root(application, expected_hwnd, "Word")
    try:
        document = application.ActiveDocument
        _ensure_word_editable(document)
        targets = _word_targets(document)
        name = _office_name(document.Name, "Untitled Word document")
        identity = _office_identity("word", expected_root, document)
        changed = not bool(document.Saved)
    except OfficeSourceError:
        raise
    except Exception as exc:
        raise OfficeSourceError(_office_error_message("Word", exc)) from exc
    return _build_snapshot("word", expected_root, identity, name, targets, changed)


def _read_excel_snapshot(application: Any, expected_hwnd: int) -> OfficeSnapshot:
    expected_root = _verify_active_root(application, expected_hwnd, "Excel")
    try:
        workbook = application.ActiveWorkbook
        _ensure_excel_editable(workbook)
        targets = _excel_targets(workbook)
        name = _office_name(workbook.Name, "Untitled Excel workbook")
        identity = _office_identity("excel", expected_root, workbook)
        changed = not bool(workbook.Saved)
    except OfficeSourceError:
        raise
    except Exception as exc:
        raise OfficeSourceError(_office_error_message("Excel", exc)) from exc
    return _build_snapshot("excel", expected_root, identity, name, targets, changed)


def _verify_active_root(application: Any, expected_hwnd: int, product: str) -> int:
    try:
        active_hwnd = _normalize_hwnd(application.ActiveWindow.Hwnd)
    except Exception as exc:
        raise OfficeSourceError(f"{product} has no readable active document window.") from exc
    expected_root = _normalize_hwnd(root_window(expected_hwnd))
    active_root = _normalize_hwnd(root_window(active_hwnd))
    if not expected_root or active_root != expected_root:
        raise OfficeSourceError(
            f"The active {product} document does not match the {product} window you opened "
            "DeskOrb Agent from. Focus that document and try again."
        )
    return expected_root


def _ensure_word_editable(document: Any) -> None:
    if bool(document.ReadOnly) or int(document.ProtectionType or 0) != 0:
        raise OfficeSourceError("Word document is read-only or protected; enable editing before continuing.")


def _ensure_excel_editable(workbook: Any) -> None:
    if bool(workbook.ReadOnly):
        raise OfficeSourceError("Excel workbook is read-only; enable editing before continuing.")
    for sheet in _iter_collection(workbook.Worksheets):
        if bool(sheet.ProtectContents):
            raise OfficeSourceError(
                f"Excel worksheet '{sheet.Name}' is protected; unprotect it before continuing."
            )


def _word_targets(document: Any) -> tuple[OfficeTarget, ...]:
    targets: list[OfficeTarget] = []
    for paragraph_index, paragraph in enumerate(_iter_collection(document.Paragraphs), start=1):
        if int(paragraph.Range.Tables.Count or 0):
            continue
        value = _normalize_word_text(str(paragraph.Range.Text or ""))
        if value:
            locator = f"paragraph:{paragraph_index}"
            targets.append(OfficeTarget(locator, locator, value))
    for table_index, table in enumerate(_iter_collection(document.Tables), start=1):
        for row_index, row in enumerate(_iter_collection(table.Rows), start=1):
            for column_index, cell in enumerate(_iter_collection(row.Cells), start=1):
                value = _normalize_word_text(str(cell.Range.Text or ""))
                if value:
                    locator = f"table:{table_index}/{row_index}/{column_index}"
                    targets.append(OfficeTarget(locator, locator, value))
    if not targets:
        raise OfficeSourceError("No readable body or table text was found in the current Word document.")
    return tuple(targets)


def _excel_targets(workbook: Any) -> tuple[OfficeTarget, ...]:
    targets: list[OfficeTarget] = []
    for sheet in _iter_collection(workbook.Worksheets):
        used_range = sheet.UsedRange
        rows = int(used_range.Rows.Count or 0)
        columns = int(used_range.Columns.Count or 0)
        for row in range(1, rows + 1):
            for column in range(1, columns + 1):
                cell = used_range.Cells(row, column)
                value = cell.Value2
                formula = str(cell.Formula or "")
                if _excel_is_empty(value, formula):
                    continue
                if len(targets) >= OFFICE_MAX_NONEMPTY_CELLS:
                    raise OfficeSourceError(
                        "The active Excel workbook is too large to attach safely. "
                        f"It exceeds the {OFFICE_MAX_NONEMPTY_CELLS:,}-cell limit."
                    )
                address = str(cell.Address(False, False) or "").replace("$", "")
                locator = f"{sheet.Name}!{address}"
                targets.append(OfficeTarget(locator, locator, value, formula))
    if not targets:
        raise OfficeSourceError("No readable cell values or formulas were found in the active Excel workbook.")
    return tuple(targets)


def _build_snapshot(
    kind: str,
    expected_root: int,
    identity: str,
    name: str,
    targets: tuple[OfficeTarget, ...],
    has_unsaved_changes: bool,
) -> OfficeSnapshot:
    rendered_text = _render_targets(targets)
    if len(rendered_text) > OFFICE_MAX_RENDERED_CHARS:
        raise OfficeSourceError(
            "The active Office document is too large to attach safely. "
            f"It exceeds the {OFFICE_MAX_RENDERED_CHARS:,}-character limit."
        )
    fingerprint_payload = [
        {"locator": target.locator, "value": target.value, "formula": target.formula}
        for target in targets
    ]
    encoded = json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, default=str)
    fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return OfficeSnapshot(
        kind=kind,
        expected_root=expected_root,
        identity=identity,
        name=name,
        rendered_text=rendered_text,
        fingerprint=fingerprint,
        targets=targets,
        has_unsaved_changes=has_unsaved_changes,
    )


def _render_targets(targets: tuple[OfficeTarget, ...]) -> str:
    lines = []
    for target in targets:
        details = f"[{target.locator}] value={target.value!r}"
        if target.formula:
            details += f" formula={target.formula!r}"
        lines.append(details)
    return "\n".join(lines)


def _office_identity(kind: str, expected_root: int, document: Any) -> str:
    location = str(getattr(document, "FullName", "") or getattr(document, "Name", "") or "Untitled")
    return f"{kind}:{expected_root}:{location}"


def _office_name(value: Any, fallback: str) -> str:
    return str(value or fallback).strip() or fallback


def _iter_collection(collection: Any) -> list[Any]:
    try:
        return list(collection)
    except TypeError:
        count = int(collection.Count or 0)
        return [collection.Item(index) for index in range(1, count + 1)]


def _normalize_word_text(text: str) -> str:
    text = text.replace("\r\x07", "\n")
    text = text.replace("\x07", "")
    text = text.replace("\r", "\n").replace("\v", "\n").replace("\f", "\n")
    return re.sub(r"\n{2,}", "\n", text).strip()


def _excel_is_empty(value: Any, formula: str) -> bool:
    return value is None and not formula


def _normalize_hwnd(hwnd: Any) -> int:
    try:
        return int(hwnd) & 0xFFFFFFFFFFFFFFFF
    except (TypeError, ValueError):
        return 0


def _office_error_message(product: str, exc: Exception) -> str:
    message = str(exc).strip()
    if "protected" in message.casefold():
        return f"{product} cannot access this protected document. Enable editing, then try again."
    if message:
        return f"{product} couldn't read the current document: {message}"
    return f"{product} couldn't read the current document. Close any {product} dialog and try again."
