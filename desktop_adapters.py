"""Safe application-level policies for the Windows UIA observer.

These adapters describe which semantic backend should be preferred for a
known application.  They do not execute application-specific scripts, infer
coordinates, or read window content; actual actions remain behind DesktopUIA
and DesktopTools validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PureWindowsPath


@dataclass(frozen=True)
class DesktopApplicationProfile:
    app_id: str
    display_name: str
    process_names: tuple[str, ...]
    preferred_backend: str
    control_hints: tuple[str, ...]
    verification_hints: tuple[str, ...]

    def safe_dict(self) -> dict[str, object]:
        return {
            "id": self.app_id,
            "name": self.display_name,
            "preferred_backend": self.preferred_backend,
            "control_hints": list(self.control_hints),
            "verification_hints": list(self.verification_hints),
        }


DEFAULT_DESKTOP_APPLICATION_PROFILES: tuple[DesktopApplicationProfile, ...] = (
    DesktopApplicationProfile(
        "notepad", "Notepad", ("notepad.exe",), "uia",
        ("Prefer an Edit control with set_value for text input.",),
        ("Require UIA value readback after text input.",),
    ),
    DesktopApplicationProfile(
        "file_explorer", "File Explorer", ("explorer.exe",), "uia",
        ("Prefer address-bar Edit and named list-item controls.",),
        ("Verify the resulting path or selected item with a fresh observation.",),
    ),
    DesktopApplicationProfile(
        "chromium", "Chromium browser", ("chrome.exe", "msedge.exe"), "playwright",
        ("Use Playwright MCP for web-page content; UIA is only for browser chrome.",),
        ("Verify URL, page fields, or structured results through the browser adapter.",),
    ),
    DesktopApplicationProfile(
        "firefox", "Firefox", ("firefox.exe",), "playwright",
        ("Use Playwright MCP for web-page content; UIA is only for browser chrome.",),
        ("Verify URL, page fields, or structured results through the browser adapter.",),
    ),
    DesktopApplicationProfile(
        "qq", "QQ", ("qq.exe", "qqnt.exe"), "uia",
        ("Prefer named search, contact, and message controls; do not guess coordinates.",),
        ("Treat sending a message as high risk and verify the visible result after confirmation.",),
    ),
    DesktopApplicationProfile(
        "calculator", "Calculator", ("calculatorapp.exe", "calc.exe"), "uia",
        ("Prefer named button controls and invoke only observed controls.",),
        ("Verify the displayed result through a fresh UIA observation.",),
    ),
)


class DesktopApplicationRegistry:
    """Match a process basename to a bounded, non-secret application profile."""

    def __init__(self, profiles: tuple[DesktopApplicationProfile, ...] = DEFAULT_DESKTOP_APPLICATION_PROFILES):
        self._profiles = tuple(profiles)
        self._by_process = {
            process.lower(): profile
            for profile in self._profiles
            for process in profile.process_names
        }

    def match(self, process_name: str | None) -> DesktopApplicationProfile:
        raw = str(process_name or "").strip()
        basename = PureWindowsPath(raw.replace("/", "\\")).name.lower()
        return self._by_process.get(basename, DesktopApplicationProfile(
            "generic", "Unknown Windows application", (), "uia",
            ("Use only actions advertised by the observed controls.",),
            ("Require a fresh observation after each semantic action.",),
        ))

    def catalog(self) -> list[dict[str, object]]:
        return [profile.safe_dict() for profile in self._profiles]

    def recommended_actions(self, process_name: str | None,
                           controls: list[dict[str, object]]) -> list[dict[str, object]]:
        """Build bounded semantic action suggestions from one fresh observation.

        Suggestions contain only the short-lived control id already returned by
        UIA and never invent coordinates or application scripts.  The runtime
        still validates the id, window handle, TTL and advertised action before
        executing anything.
        """
        profile = self.match(process_name)
        if profile.preferred_backend == "playwright":
            return []
        app = profile.app_id
        recommendations: list[dict[str, object]] = []
        for control in controls if isinstance(controls, list) else []:
            if not isinstance(control, dict) or not control.get("enabled", True):
                continue
            control_id = str(control.get("control_id") or "")
            actions = {str(value) for value in control.get("actions") or ()}
            name = str(control.get("name") or "").strip()
            lowered = name.lower()
            control_type = str(control.get("control_type") or "").lower()
            if not control_id:
                continue
            if "set_value" in actions and app in {"notepad", "file_explorer", "qq", "generic"}:
                risk = "high" if app == "qq" else "normal"
                recommendations.append({
                    "control_id": control_id, "action": "set_value",
                    "risk_level": risk,
                    "reason": "Profile-selected editable control; verify value readback.",
                })
            if "invoke" in actions and control_type in {"button", "listitem", "menuitem", "checkbox", "tabitem"}:
                high_risk = app == "qq" and any(marker in lowered for marker in ("发送", "send", "提交", "publish"))
                recommendations.append({
                    "control_id": control_id, "action": "invoke",
                    "risk_level": "high" if high_risk else "normal",
                    "reason": "Profile-selected semantic control; require post-action state verification.",
                })
            if len(recommendations) >= 24:
                break
        return recommendations
