"""Bounded semantic browser execution on top of the local Playwright MCP.

The model-facing contract is intentionally smaller than Playwright's raw tool
set.  This module owns observation generations, one-action batches, progress
checks, and bounded recovery.  Browser content is returned to the model when
needed, but state fingerprints, refs, and extraction history stay in memory.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Protocol

from browser_actions import STATE_CHANGING_ACTIONS, BrowserAction, validate_browser_action_batch
from browser_ref_extractor import TrustedBrowserRefExtractor, parse_ref_snapshot


class BrowserBackend(Protocol):
    def call(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class PlaywrightMCPBackend:
    """Translate semantic browser actions to the pinned Playwright MCP tools."""

    _TOOLS = {
        "navigate": "mcp_playwright_browser_navigate",
        "snapshot": "mcp_playwright_browser_snapshot",
        "click_ref": "mcp_playwright_browser_click",
        "fill_ref": "mcp_playwright_browser_type",
        "select_ref": "mcp_playwright_browser_select_option",
        "wait": "mcp_playwright_browser_wait_for",
        "switch_tab": "mcp_playwright_browser_tabs",
        "extract": "mcp_playwright_browser_snapshot",
    }

    def __init__(self, bridge: Any):
        self.bridge = bridge

    def call(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if action == "verify":
            return {"ok": False, "error": "verify is evaluated by the semantic browser runtime."}
        tool_name = self._TOOLS.get(action)
        if not tool_name:
            return {"ok": False, "error": "Unsupported semantic browser action.",
                    "failure_kind": "unsupported_browser_action"}
        payload = self._arguments(action, arguments)
        # The bridge strips these fields before the MCP process sees them. They
        # keep the raw tool contract valid while the semantic runtime remains
        # the only model-facing policy boundary.
        if action in STATE_CHANGING_ACTIONS:
            payload.setdefault("_deskorb_risk_level", "normal")
            payload.setdefault("_deskorb_risk_reason", "bounded browser task action")
        return self.bridge.call(tool_name, payload)

    @staticmethod
    def _arguments(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = arguments if isinstance(arguments, dict) else {}
        if action == "navigate":
            return {"url": str(args.get("url") or "")}
        if action == "snapshot":
            payload: dict[str, Any] = {}
            if args.get("target"):
                payload["target"] = str(args["target"])
            if args.get("depth") is not None:
                payload["depth"] = int(args["depth"])
            return payload
        if action == "extract":
            return {"target": str(args.get("ref") or "")}
        if action == "click_ref":
            payload = {"target": str(args.get("ref") or "")}
            for key in ("button", "doubleClick", "modifiers", "element"):
                if key in args:
                    payload[key] = args[key]
            return payload
        if action == "fill_ref":
            value = args.get("value", args.get("text", ""))
            payload = {"target": str(args.get("ref") or ""),
                       "text": str(value)}
            for key in ("slowly", "submit", "element"):
                if key in args:
                    payload[key] = args[key]
            return payload
        if action == "select_ref":
            return {"target": str(args.get("ref") or ""),
                    "values": [str(value) for value in args.get("values") or []]}
        if action == "wait":
            payload = {}
            if args.get("text") is not None:
                payload["text"] = str(args["text"])
            if args.get("text_gone") is not None:
                payload["textGone"] = str(args["text_gone"])
            if args.get("seconds") is not None:
                payload["time"] = float(args["seconds"])
            elif args.get("time") is not None:
                payload["time"] = float(args["time"])
            elif args.get("ms") is not None:
                payload["time"] = max(0.0, float(args["ms"]) / 1000.0)
            return payload
        if action == "switch_tab":
            return {"action": "select", "index": int(args.get("index", 0))}
        return {}


class BrowserExecutionSession:
    """Execute bounded semantic browser batches with fail-closed recovery."""

    def __init__(self, backend: BrowserBackend, *, max_action_steps: int = 20,
                 handoff_timeout_seconds: int = 120, clock=time.monotonic,
                 on_state_action: Callable[[str, str], None] | None = None):
        self.backend = backend
        self.max_action_steps = max(1, int(max_action_steps))
        self.handoff_timeout_seconds = max(1, int(handoff_timeout_seconds))
        self.clock = clock
        self.on_state_action = on_state_action
        self.reset()

    def reset(self) -> None:
        self._observation_counter = 0
        self._observation_id = ""
        self._state_fingerprint = ""
        self._last_snapshot: Any = None
        self._last_no_progress_signature: str | None = None
        self._no_progress_count = 0
        self._last_state_action_signature: str | None = None
        self._repeated_state_action_count = 0
        self._reobservation_required = False
        self._handoff_required = False
        self._handoff_reason = ""
        self._action_steps = 0
        self._extractions: list[dict[str, Any]] = []
        self._last_extract_signature: str | None = None
        self._last_extract_observation_id = ""
        self._last_verify_signature: str | None = None
        self._last_verify_observation_id = ""
        self._evidence_failure_count = 0
        self._trusted_extractor = TrustedBrowserRefExtractor()

    @property
    def observation_id(self) -> str:
        return self._observation_id

    @property
    def action_steps(self) -> int:
        return self._action_steps

    def execute(self, value: Any) -> dict[str, Any]:
        if self._handoff_required:
            return self._failure("browser_handoff_required",
                                 "Manual browser handoff must be completed before continuing.",
                                 handoff_required=True,
                                 handoff_timeout_seconds=self.handoff_timeout_seconds)
        actions, error = validate_browser_action_batch(value)
        if error:
            return self._failure("invalid_browser_action_batch", error,
                                 next_allowed_action=self._recovery_hint(error))
        if self._action_steps + len(actions) > self.max_action_steps:
            return self._failure("browser_action_budget_exceeded",
                                 "The browser action budget has been exhausted.")

        initial_observation = self._observation_id
        observations: list[dict[str, Any]] = []
        final: dict[str, Any] = {"ok": True}
        for action in actions:
            if self._action_steps >= self.max_action_steps:
                return self._failure("browser_action_budget_exceeded",
                                     "The browser action budget has been exhausted.",
                                     observations=observations)
            if action.action == "snapshot":
                final = self._observe(action.arguments)
            elif action.action == "extract":
                final = self._extract(action.arguments)
            elif action.action == "verify":
                final = self._verify(action.arguments)
            else:
                final = self._state_action(action, initial_observation)
            self._action_steps += 1
            observations.append({
                "action": action.action,
                "ok": bool(final.get("ok")),
                "state_changed": bool(final.get("state_changed")),
                "failure_kind": final.get("failure_kind"),
                "verification": final.get("verification"),
                "observation_id": final.get("observation_id", self._observation_id),
                **({"ref": action.arguments.get("ref")} if action.action == "extract" else {}),
                **({"extraction": final["extraction"]} if "extraction" in final else {}),
                **({"content": final["content"]} if "content" in final else {}),
            })
            if not final.get("ok"):
                break
            # A snapshot followed by a ref action in one batch is valid only
            # when the supplied ref was bound to the observation that existed
            # when this batch started. The action method handles the generation
            # update without exposing generated IDs to the model mid-batch.
        result = dict(final)
        result["observations"] = observations
        result["batch_action_steps"] = len(actions)
        result["action_steps"] = self._action_steps
        result["observation_id"] = self._observation_id
        return result

    def _observe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self.backend.call("snapshot", arguments)
        except Exception:
            return self._failure(
                "browser_backend_failure",
                "The local browser backend failed while obtaining a fresh observation.",
                requires_reobservation=True,
            )
        if not isinstance(result, dict):
            return self._failure(
                "browser_backend_failure",
                "The local browser backend returned an invalid observation.",
                requires_reobservation=True,
            )
        if not result.get("ok"):
            return self._backend_failure(result)
        content = result.get("content", result)
        self._observation_counter += 1
        self._observation_id = f"obs-{self._observation_counter}"
        self._state_fingerprint = self._fingerprint(content)
        self._last_snapshot = content
        self._reobservation_required = False
        # A new observation invalidates all ref-bound evidence and any
        # observation-only retry signature.  State actions already do this
        # through _observe(); explicit snapshots must obey the same rule.
        self._extractions.clear()
        self._last_extract_signature = None
        self._last_extract_observation_id = ""
        self._last_verify_signature = None
        self._last_verify_observation_id = ""
        self._trusted_extractor.observe({"observation_id": self._observation_id})
        return {**result, "ok": True, "observation_id": self._observation_id,
                "state_changed": True}

    def _state_action(self, action: BrowserAction, initial_observation: str) -> dict[str, Any]:
        if self._handoff_required:
            return self._handoff_failure()
        supplied = str(action.arguments.get("observation_id") or "")
        # Navigation is the only state action that can begin a browser task
        # without an existing page observation. Ref actions must always be
        # bound to the current generation; this is what prevents a model from
        # reusing a ref after a re-render or tab switch.
        observation_required = action.action != "navigate"
        ref_bound = action.action in {"click_ref", "fill_ref", "select_ref"}
        if observation_required and (not self._observation_id or supplied != self._observation_id):
            # When a batch starts with snapshot, allow the action to use the
            # pre-batch generation; the snapshot is the fresh observation it
            # was meant to bind to. No stale generation from an older batch is
            # accepted.
            if not ref_bound and not supplied and self._observation_id:
                action = BrowserAction(action.action, {
                    **action.arguments, "observation_id": self._observation_id,
                })
            elif not (initial_observation and supplied == initial_observation
                    and initial_observation != self._observation_id):
                return self._failure("stale_browser_observation",
                                     "Fresh browser observation is required before this action.")
            else:
                action = BrowserAction(action.action, {
                    **action.arguments, "observation_id": self._observation_id,
                })
        signature = self._action_signature(action)
        if self._last_state_action_signature == signature:
            self._repeated_state_action_count += 1
            if self._repeated_state_action_count >= 2:
                return self._handoff_failure()
            return self._failure(
                "browser_repeated_state_action",
                "This browser state action was already completed. Use the latest observation "
                "to choose the next semantic target instead of repeating it.",
                requires_reobservation=True,
            )
        if self._last_no_progress_signature == signature and not self._reobservation_required:
            return self._handoff_failure()
        if self._reobservation_required:
            return self._failure("browser_reobservation_required",
                                 "Fresh browser observation is required before retrying this action.",
                                 requires_reobservation=True)

        before = self._state_fingerprint
        if action.action in {"click_ref", "fill_ref", "select_ref"} and self._is_high_risk_action(action):
            return self._failure("browser_high_risk_confirmation_required",
                                 "This browser action is outside the bounded read-only contract.")
        self._emit_state_activity("begin", action.action)
        result: dict[str, Any] = {"ok": False, "failure_kind": "browser_backend_failure"}
        try:
            result = self.backend.call(action.action, action.arguments)
        except Exception:
            result = {"ok": False, "failure_kind": "browser_backend_failure",
                      "error": "The local browser backend failed while executing the action."}
        finally:
            self._emit_state_activity("end", action.action)
        if not isinstance(result, dict):
            return self._backend_failure({
                "ok": False,
                "failure_kind": "browser_backend_failure",
                "error": "The local browser backend returned an invalid action result.",
            })
        if not result.get("ok"):
            return self._backend_failure(result)
        after = self._observe({})
        if not after.get("ok"):
            return {**after, "requires_reobservation": True}
        changed = bool(after.get("observation_id") and self._state_fingerprint != before)
        if changed:
            self._last_no_progress_signature = None
            self._no_progress_count = 0
            self._last_state_action_signature = signature
            self._repeated_state_action_count = 0
            self._reobservation_required = False
            self._extractions.clear()
            self._evidence_failure_count = 0
            return {**result, "ok": True, "state_changed": True,
                    "observation_id": self._observation_id,
                    "content": after.get("content")}

        self._no_progress_count += 1
        if self._no_progress_count >= 2:
            return self._handoff_failure()
        self._last_no_progress_signature = signature
        self._last_state_action_signature = None
        self._repeated_state_action_count = 0
        self._reobservation_required = False
        return {"ok": False, "state_changed": False,
                "failure_kind": "browser_no_progress",
                "error": "The browser action completed without an observable page change.",
                "requires_reobservation": True,
                "observation_id": self._observation_id,
                "content": after.get("content")}

    def _emit_state_activity(self, phase: str, action: str) -> None:
        if self.on_state_action is None:
            return
        try:
            self.on_state_action(phase, action)
        except Exception:
            # The activity indicator is advisory and cannot change browser
            # execution semantics.
            return

    def _extract(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ref = str(arguments.get("ref") or "")
        supplied = str(arguments.get("observation_id") or "")
        if not self._observation_id or supplied != self._observation_id:
            return self._failure("stale_browser_observation",
                                 "Fresh browser observation is required before extraction.")
        if self._reobservation_required:
            return self._failure(
                "browser_reobservation_required",
                "Fresh browser observation is required before retrying this evidence action.",
                requires_reobservation=True,
            )
        signature = self._evidence_signature("extract", arguments)
        if (self._last_extract_signature == signature
                and self._last_extract_observation_id == self._observation_id):
            return self._failure(
                "browser_evidence_loop",
                "This ref-bound extraction was already attempted for the current observation. "
                "Choose a different ref or obtain a fresh observation before retrying.",
                requires_reobservation=True,
            )
        self._last_extract_signature = signature
        self._last_extract_observation_id = self._observation_id
        try:
            result = self.backend.call("extract", arguments)
        except Exception:
            result = {"ok": False, "failure_kind": "browser_backend_failure",
                      "error": "The local browser backend failed while extracting evidence."}
        if not isinstance(result, dict):
            result = {"ok": False, "failure_kind": "browser_backend_failure",
                      "error": "The local browser backend returned an invalid extraction result."}
        if not result.get("ok"):
            return self._evidence_failure(
                str(result.get("failure_kind") or "browser_tool_failure"),
                "The requested page ref could not be extracted. Obtain a fresh observation and relocate once.",
            )
        fields = arguments.get("fields") or []
        parsed = parse_ref_snapshot(result.get("content", result), ref, fields)
        native = {
            "source": "deskorb_ref_subtree",
            "ref": ref,
            "fields": parsed.get("fields") if isinstance(parsed, dict) else {},
            "matched_fields": parsed.get("matched_fields") if isinstance(parsed, dict) else 0,
            "observed_chars": parsed.get("observed_chars") if isinstance(parsed, dict) else 0,
            "trusted_ref": bool(isinstance(parsed, dict) and parsed.get("trusted_ref")),
            "observation_id": self._observation_id,
        }
        attested = self._trusted_extractor.extract(
            {"extraction": native},
            {"ref": ref, "fields": fields},
            source_tool="browser_extract_ref",
        ).safe_dict()
        extraction = {**parsed, **attested, "observation_id": self._observation_id}
        verification = {
            "passed": bool(attested.get("trusted_ref") and
                            extraction.get("matched_fields") == len(fields)),
            "kind": "browser_structured_verification",
            "matched_fields": int(extraction.get("matched_fields") or 0),
                            "required_fields": len(fields),
        }
        if not verification["passed"]:
            return self._evidence_failure(
                "browser_evidence_insufficient",
                "The selected page ref did not provide all requested structured fields. "
                "Obtain a fresh observation and relocate once; do not repeat the same ref.",
                extraction=extraction,
                verification=verification,
                observation_id=self._observation_id,
                state_changed=False,
            )
        extraction["observation_id"] = self._observation_id
        self._evidence_failure_count = 0
        self._extractions.append(extraction)
        return {**result, "ok": True, "extraction": extraction,
                "verification": verification, "observation_id": self._observation_id,
                "state_changed": False}

    def _verify(self, arguments: dict[str, Any]) -> dict[str, Any]:
        latest = self._extractions[-1] if self._extractions else {}
        signature = self._evidence_signature("verify", {
            **arguments, "extraction_index": len(self._extractions),
        })
        if (self._last_verify_signature == signature
                and self._last_verify_observation_id == self._observation_id):
            return {
                "ok": False,
                "failure_kind": "browser_evidence_loop",
                "error": "This verification was already attempted for the current evidence.",
                "requires_reobservation": True,
                "observation_id": self._observation_id,
                "state_changed": False,
            }
        self._last_verify_signature = signature
        self._last_verify_observation_id = self._observation_id
        fields = latest.get("fields") if isinstance(latest, dict) else {}
        fields = fields if isinstance(fields, dict) else {}
        if (not latest or str(latest.get("observation_id") or "") != self._observation_id):
            return {
                "ok": True,
                "verified": False,
                "verification": {
                    "passed": False,
                    "kind": "browser_structured_verification",
                    "matched_fields": 0,
                    "required_fields": len(arguments.get("required_fields") or []),
                    "failure_kind": "browser_stale_evidence",
                },
                "failure_kind": "browser_stale_evidence",
                "observation_id": self._observation_id,
                "state_changed": False,
            }
        required = arguments.get("required_fields") or []
        passed = bool(latest.get("trusted_ref")) and all(str(fields.get(name) or "").strip()
                                                         for name in required)
        contains = arguments.get("contains")
        values = [contains] if isinstance(contains, str) else list(contains or [])
        rendered = " ".join(str(value) for value in fields.values())
        passed = passed and all(value in rendered for value in values)
        expected = arguments.get("expected")
        if isinstance(expected, dict):
            passed = passed and all(
                str(fields.get(str(key)) or "").strip() == str(value).strip()
                for key, value in expected.items()
            )
        price = self._number(fields.get("price"))
        if arguments.get("price_min") is not None:
            passed = passed and price is not None and price >= float(arguments["price_min"])
        if arguments.get("price_max") is not None:
            passed = passed and price is not None and price <= float(arguments["price_max"])
        verification = {"passed": bool(passed), "kind": "browser_structured_verification",
                        "matched_fields": sum(bool(value) for value in fields.values()),
                        "required_fields": len(required)}
        return {"ok": True, "verified": bool(passed), "verification": verification,
                "observation_id": self._observation_id, "state_changed": False}

    def _evidence_failure(self, failure_kind: str, error: str, **extra: Any) -> dict[str, Any]:
        """Allow one fresh-observation relocation, then hand off the task."""
        self._evidence_failure_count += 1
        if self._evidence_failure_count >= 2:
            return self._handoff_failure()
        self._reobservation_required = True
        return {
            "ok": False,
            "failure_kind": failure_kind,
            "error": error,
            "requires_reobservation": True,
            "observation_id": self._observation_id,
            **extra,
        }

    def _handoff_failure(self) -> dict[str, Any]:
        self._handoff_required = True
        self._handoff_reason = "browser_no_progress"
        self._observation_id = ""
        self._state_fingerprint = ""
        self._last_snapshot = None
        self._extractions.clear()
        self._last_extract_signature = None
        self._last_extract_observation_id = ""
        self._last_verify_signature = None
        self._last_verify_observation_id = ""
        self._evidence_failure_count = 0
        self._trusted_extractor.invalidate()
        return {"ok": False, "failure_kind": self._handoff_reason,
                "error": "Browser interaction made no progress; manual handoff is required.",
                "handoff_required": True,
                "handoff_timeout_seconds": self.handoff_timeout_seconds,
                "observation_id": ""}

    def resume_after_handoff(self) -> None:
        self._handoff_required = False
        self._handoff_reason = ""
        self._last_no_progress_signature = None
        self._no_progress_count = 0
        self._last_state_action_signature = None
        self._repeated_state_action_count = 0
        self._reobservation_required = True
        self._observation_id = ""
        self._state_fingerprint = ""
        self._last_snapshot = None
        self._extractions.clear()
        self._last_extract_signature = None
        self._last_extract_observation_id = ""
        self._last_verify_signature = None
        self._last_verify_observation_id = ""
        self._evidence_failure_count = 0
        self._trusted_extractor.invalidate()

    def _failure(self, failure_kind: str, error: str, **extra: Any) -> dict[str, Any]:
        return {"ok": False, "failure_kind": failure_kind, "error": error,
                "observation_id": self._observation_id, **extra}

    @staticmethod
    def _recovery_hint(error: str) -> str:
        text = str(error or "").lower()
        if "state-changing" in text or "final action" in text:
            return "send_one_state_action_in_a_separate_final_batch"
        if "observation_id" in text:
            return "use_the_latest_observation_id_from_a_fresh_snapshot"
        if "extract" in text:
            return "extract_with_ref_observation_id_and_nonempty_fields"
        if "verify" in text:
            return "verify_with_required_fields_or_contains_after_successful_extract"
        if "wait" in text:
            return "wait_with_seconds_or_ms"
        return "read_the_bounded_error_and_retry_once_with_the_semantic_schema"

    @staticmethod
    def _backend_failure(result: dict[str, Any]) -> dict[str, Any]:
        return {**result, "ok": False,
                "failure_kind": str(result.get("failure_kind") or "browser_tool_failure")}

    @staticmethod
    def _fingerprint(content: Any) -> str:
        try:
            encoded = json.dumps(content, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            encoded = repr(content).encode("utf-8", errors="replace")
        return hashlib.sha256(encoded).hexdigest()[:24]

    @staticmethod
    def _action_signature(action: BrowserAction) -> str:
        args = {key: value for key, value in action.arguments.items()
                if key != "observation_id"}
        try:
            encoded = json.dumps({"action": action.action, "arguments": args},
                                 ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"))
        except (TypeError, ValueError):
            encoded = repr((action.action, args))
        return encoded

    @staticmethod
    def _evidence_signature(kind: str, arguments: dict[str, Any]) -> str:
        """Create an in-memory signature for an observation-only evidence call."""
        try:
            return json.dumps({"kind": kind, "arguments": arguments},
                              ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))
        except (TypeError, ValueError):
            return repr((kind, arguments))

    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None:
            return None
        text = str(value).replace(",", "")
        import re
        match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
        return float(match.group(0)) if match else None

    @staticmethod
    def _is_high_risk_action(action: BrowserAction) -> bool:
        args = action.arguments
        if bool(args.get("submit")) or bool(args.get("doubleClick")):
            return True
        text = " ".join(str(args.get(key) or "").lower() for key in ("ref", "value", "element"))
        return any(marker in text for marker in (
            "send", "submit", "purchase", "buy", "checkout", "delete", "upload", "login", "password",
            "发送", "购买", "结算", "删除", "上传", "登录", "密码",
        ))


__all__ = ["BrowserExecutionSession", "PlaywrightMCPBackend"]
