"""Visible but isolated end-to-end test for DeskOrb's desktop text input."""
from __future__ import annotations

import json
import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop_tools import DesktopTools


def main() -> int:
    root = tk.Tk()
    root.title("DeskOrb Agent Input Test")
    root.geometry("420x140+80+80")
    entry = tk.Entry(root, font=("Segoe UI", 14))
    entry.pack(fill="x", padx=24, pady=36)
    root.update_idletasks(); root.deiconify(); root.lift(); root.focus_force(); entry.focus_force(); root.update()
    tools = DesktopTools()
    tools.user32.SetForegroundWindow(root.winfo_id())
    root.after(100, root.focus_force); root.update(); time.sleep(0.2); root.update()
    state = tools.capture_state()
    expected = "DESKORB_INPUT_OK 中文"
    try:
        if not state.get("ok"):
            raise RuntimeError(state.get("error", "could not capture desktop state"))
        x = entry.winfo_rootx() + entry.winfo_width() // 2
        y = entry.winfo_rooty() + entry.winfo_height() // 2
        clicked = tools.click(state["snapshot_id"], x, y, "left")
        tools.user32.SetForegroundWindow(root.winfo_id()); root.update(); entry.focus_force(); root.update(); time.sleep(0.15); root.update()
        typed = tools.type_text(state["snapshot_id"], expected)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            root.update()
            if entry.get() == expected:
                break
            time.sleep(0.03)
        print(json.dumps({"ok": clicked.get("ok") and typed.get("ok") and entry.get() == expected,
                          "click_ok": clicked.get("ok"), "type_ok": typed.get("ok"),
                          "value_exact": entry.get() == expected}, ensure_ascii=False))
        return 0 if entry.get() == expected else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:500]}, ensure_ascii=False))
        return 3
    finally:
        root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
