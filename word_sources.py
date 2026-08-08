"""Read the explicitly selected active Microsoft Word document through COM."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from debuglog import dbg
from win32utils import root_window, window_process_name


class WordMaterialError(ValueError):
    """A user-facing problem while reading an active Word document."""


@dataclass(frozen=True)
class WordMaterial:
    """Transient text read from the current Word document; never persisted as input."""

    document_id: str
    name: str
    text: str
    character_count: int
    has_unsaved_changes: bool
    window_title: str


def is_word_window(hwnd: int) -> bool:
    """Return whether hwnd belongs to the desktop Microsoft Word process."""
    return bool(hwnd) and window_process_name(hwnd).casefold() == "winword.exe"


def read_active_word_document(expected_hwnd: int) -> WordMaterial:
    """Read the main document story only when COM still points at the expected window."""
    if not is_word_window(expected_hwnd):
        raise WordMaterialError("Focus a Microsoft Word document, then open DeskOrb Agent and try again.")
    dbg("office_com", {"product": "word", "stage": "read_start", "hwnd": expected_hwnd})

    pythoncom, client = _load_com_modules()
    initialized = False
    try:
        pythoncom.CoInitialize()
        initialized = True
        application = resolve_word_application_for_window(pythoncom, client, expected_hwnd)
        if application is None:
            dbg("office_com", {"product": "word", "stage": "rot_no_match", "hwnd": expected_hwnd})
            application = client.GetActiveObject("Word.Application")
        else:
            dbg("office_com", {"product": "word", "stage": "rot_match", "hwnd": expected_hwnd})
        return _read_document_from_application(application, expected_hwnd)
    except WordMaterialError:
        raise
    except Exception as exc:
        raise WordMaterialError(_word_error_message(exc)) from exc
    finally:
        if initialized:
            pythoncom.CoUninitialize()


def _load_com_modules() -> tuple[Any, Any]:
    try:
        import pythoncom
        from win32com import client
    except ImportError as exc:
        raise WordMaterialError("Word support is unavailable; install pywin32 and restart.") from exc
    return pythoncom, client


def resolve_word_application_for_window(pythoncom: Any, client: Any,
                                        expected_hwnd: int) -> Any | None:
    """Find the Word COM instance that owns ``expected_hwnd`` in the ROT.

    ``GetActiveObject('Word.Application')`` is ambiguous when multiple Word
    instances are running. The running object table contains the individual
    application/document objects; matching their Windows collection avoids
    reading a different, empty Word instance.
    """
    expected_root = _normalize_hwnd(root_window(expected_hwnd))
    if not expected_root:
        return None
    try:
        running_table = pythoncom.GetRunningObjectTable()
        enum = running_table.EnumRunning()
        bind_context = pythoncom.CreateBindCtx(0)
        monikers = enum.Next(128)
    except Exception:
        return None
    if not isinstance(monikers, tuple):
        return None
    dbg("office_com", {"product": "word", "stage": "rot_scan", "monikers": len(monikers),
                        "hwnd": expected_hwnd})
    for moniker in monikers:
        try:
            raw_object = moniker.BindToObject(bind_context, None, pythoncom.IID_IDispatch)
            bound_object = client.Dispatch(raw_object)
        except Exception:
            continue
        application = bound_object
        try:
            application = bound_object.Application
        except Exception:
            pass
        if _application_owns_window(application, expected_root):
            dbg("office_com", {"product": "word", "stage": "rot_window_match",
                                "hwnd": expected_hwnd})
            return application
    return None


def _application_owns_window(application: Any, expected_root: int) -> bool:
    windows = None
    try:
        windows = application.Windows
        count = int(windows.Count)
        candidates = (windows.Item(index) for index in range(1, count + 1))
    except Exception:
        try:
            candidates = iter(windows)
        except Exception:
            return False
    for window in candidates:
        try:
            hwnd = _normalize_hwnd(window.Hwnd)
            if hwnd and _normalize_hwnd(root_window(hwnd)) == expected_root:
                return True
        except Exception:
            continue
    return False


def _read_document_from_application(application: Any, expected_hwnd: int) -> WordMaterial:
    try:
        document = application.ActiveDocument
    except Exception as exc:
        raise WordMaterialError(_word_error_message(exc)) from exc

    expected_root = _normalize_hwnd(root_window(expected_hwnd))
    active_hwnd = _try_active_window_hwnd(application, document)
    active_root = _normalize_hwnd(root_window(active_hwnd)) if active_hwnd else expected_root
    if expected_root and active_hwnd and (not active_root or active_root != expected_root):
        raise WordMaterialError(
            "The active Word document does not match the Word window you opened DeskOrb Agent from. "
            "Focus that document and try again."
        )
    if not expected_root:
        raise WordMaterialError("Word has no readable active document window.")

    try:
        name = str(document.Name or "Untitled Word document").strip() or "Untitled Word document"
        text = _normalize_word_text(str(document.Content.Text or ""))
        has_unsaved_changes = not bool(document.Saved)
    except Exception as exc:
        raise WordMaterialError(_word_error_message(exc)) from exc
    if not text:
        raise WordMaterialError("No readable text was found in the current Word document.")

    document_id = f"word-window:{expected_root}"
    return WordMaterial(
        document_id=document_id,
        name=name,
        text=text,
        character_count=len(text),
        has_unsaved_changes=has_unsaved_changes,
        window_title=name,
    )


def _try_active_window_hwnd(application: Any, document: Any) -> int:
    """Read a Word window handle when COM exposes it, without requiring it."""
    for owner in (application, document):
        try:
            hwnd = _normalize_hwnd(owner.ActiveWindow.Hwnd)
        except Exception:
            continue
        if hwnd:
            return hwnd
    return 0


def _normalize_word_text(text: str) -> str:
    """Convert Word's main-story control characters into ordered readable plain text."""
    text = text.replace("\r\x07", "\n")
    text = text.replace("\x07", "")
    text = text.replace("\r", "\n").replace("\v", "\n").replace("\f", "\n")
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _normalize_hwnd(hwnd: Any) -> int:
    try:
        return int(hwnd) & 0xFFFFFFFFFFFFFFFF
    except (TypeError, ValueError):
        return 0


def _word_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    if "protected" in message.casefold():
        return "Word cannot read this document in Protected View. Enable editing, then try again."
    if message:
        return f"Word couldn't read the current document: {message}"
    return "Word couldn't read the current document. Close any Word dialog and try again."
