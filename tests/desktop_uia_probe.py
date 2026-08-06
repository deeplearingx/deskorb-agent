"""Read-only UI Automation readiness probe for the current Windows session.

The output intentionally contains only counts and capability metadata.  It
does not print control names, window titles, values, screenshots, or other
user content.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Running the file directly puts ``tests`` rather than the project root on
# ``sys.path``; keep the probe executable as a standalone acceptance command.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop_tools import foreground_capture_window
from desktop_uia import DesktopUIA


def main() -> int:
    observer = DesktopUIA()
    hwnd = int(foreground_capture_window() or 0)
    result = observer.observe_active_window(hwnd, max_elements=120)
    controls = result.get("controls") if isinstance(result, dict) else None
    controls = controls if isinstance(controls, list) else []
    actions = {"invoke": 0, "set_value": 0}
    for control in controls:
        if not isinstance(control, dict):
            continue
        for action in control.get("actions") or ():
            if action in actions:
                actions[action] += 1
    payload = {
        "ok": bool(result.get("ok")),
        "uia_available": observer.available,
        "foreground_window_available": bool(hwnd),
        "control_count": len(controls),
        "semantic_action_counts": actions,
        "requires_user_attention": bool(result.get("requires_user_attention")),
        "error_kind": "unavailable" if not observer.available else None,
    }
    recommendations = result.get("recommended_actions") if isinstance(result, dict) else None
    recommendations = recommendations if isinstance(recommendations, list) else []
    payload["recommended_action_count"] = len(recommendations)
    payload["recommended_high_risk_count"] = sum(
        str(item.get("risk_level") or "").lower() == "high"
        for item in recommendations if isinstance(item, dict)
    )
    application = result.get("application") if isinstance(result, dict) else None
    if isinstance(application, dict):
        payload["application_id"] = application.get("id")
        payload["preferred_backend"] = application.get("preferred_backend")
    if not result.get("ok"):
        error = str(result.get("error") or "UI Automation observation failed")
        payload["error"] = error[:240]
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
