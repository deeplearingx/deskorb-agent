"""Visible but isolated smoke test for DeskOrb's window inventory/control path."""
from __future__ import annotations

import json
import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop_tools import DesktopTools


TITLE = "DeskOrb Agent Window Test"


def main() -> int:
    root = tk.Tk()
    root.title(TITLE)
    root.geometry("360x120+120+120")
    tk.Label(root, text="Window-control isolation test", font=("Segoe UI", 12)).pack(padx=20, pady=35)
    root.update_idletasks(); root.deiconify(); root.lift(); root.focus_force(); root.update()
    time.sleep(0.2); root.update()
    tools = DesktopTools()
    try:
        listed = tools.list_windows()
        match = next((item for item in listed.get("windows", []) if item.get("title") == TITLE), None)
        minimized = tools.control_window(match["window_id"], "minimize") if match else {"ok": False}
        root.update(); time.sleep(0.1)
        restored = tools.control_window(match["window_id"], "restore") if match else {"ok": False}
        root.update(); time.sleep(0.1)
        focused = tools.control_window(match["window_id"], "focus") if match else {"ok": False}
        result = {"ok": bool(match and minimized.get("ok") and restored.get("ok") and focused.get("ok")),
                  "list_ok": listed.get("ok"), "found_target": bool(match),
                  "minimize_ok": minimized.get("ok"), "restore_ok": restored.get("ok"),
                  "focus_ok": focused.get("ok")}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:500]}, ensure_ascii=False))
        return 3
    finally:
        root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
