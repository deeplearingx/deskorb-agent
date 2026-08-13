"""Safe application-level policies for the Windows UIA observer."""
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
        """Build bounded semantic action suggestions from one fresh observation."""
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
                recommendations.append({
                    "control_id": control_id, "action": "set_value",
                    "risk_level": "high" if app == "qq" else "normal",
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

    def verify_action(self, process_name: str | None, action: str,
                      result: dict[str, object] | None,
                      after: dict[str, object] | None = None,
                      *, requested_value: str | None = None) -> dict[str, object]:
        """Evaluate an app-specific postcondition from bounded UIA evidence."""
        profile = self.match(process_name)
        payload = result if isinstance(result, dict) else {}
        following = after if isinstance(after, dict) else {}
        verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
        after_verification = following.get("verification") if isinstance(following.get("verification"), dict) else {}
        if profile.app_id == "notepad" and action == "set_value":
            passed = bool(payload.get("verified") and verification.get("passed")
                          and verification.get("kind") == "uia_value_readback")
            return {"passed": passed, "kind": "uia_value_readback"}
        if profile.app_id == "calculator" and action == "invoke":
            before = str(payload.get("before_observation_fingerprint") or "")
            current = str(following.get("observation_fingerprint") or "")
            has_result = any(
                isinstance(control, dict)
                and str(control.get("control_type") or "").casefold() in {"text", "edit"}
                and bool(str(control.get("name") or "").strip())
                for control in following.get("controls") or ()
            )
            return {"passed": bool(has_result and current and current != before),
                    "kind": "calculator_result_observation"}
        if profile.app_id == "qq" and action == "invoke":
            passed = bool(after_verification.get("passed")
                          and after_verification.get("kind") == "message_delivery")
            return {"passed": passed, "kind": "message_delivery"}
        if profile.app_id == "file_explorer" and action == "invoke":
            passed = bool(payload.get("verified") and verification.get("passed"))
            return {"passed": passed, "kind": "uia_control_state"}
        if action == "set_value" and requested_value is not None:
            return {"passed": bool(payload.get("verified") and verification.get("passed")),
                    "kind": "uia_value_readback"}
        return {"passed": bool(payload.get("verified") and verification.get("passed")),
                "kind": "uia_control_state"}
