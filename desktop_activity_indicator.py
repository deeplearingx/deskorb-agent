"""State and event primitives for the real-desktop activity indicator.

The lifecycle in this module deliberately has no Tk or Win32 dependency.  The UI
layer supplies a main-thread scheduler and reacts to :class:`ActivityTransition`
objects, which keeps timing and stale-event behavior deterministic in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Mapping


DESKTOP_ACTIVITY_TOOLS = frozenset({
    "application_launch",
    "desktop_click",
    "desktop_type",
    "desktop_hotkey",
    "desktop_scroll",
    "window_focus",
    "window_control",
})


@dataclass(frozen=True)
class DesktopActivityEvent:
    """Privacy-bounded begin/end event exchanged between runtime and UI."""

    phase: str
    tool: str
    activity_id: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "tool": self.tool,
            "activity_id": self.activity_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "DesktopActivityEvent | None":
        if not isinstance(payload, Mapping):
            return None
        phase = payload.get("phase")
        tool = payload.get("tool")
        activity_id = payload.get("activity_id")
        if phase not in {"begin", "end"}:
            return None
        if not isinstance(tool, str) or tool not in DESKTOP_ACTIVITY_TOOLS:
            return None
        if isinstance(activity_id, bool) or not isinstance(activity_id, int) or activity_id < 1:
            return None
        return cls(phase, tool, activity_id)


@dataclass(frozen=True)
class ActivityTransition:
    """A renderer instruction emitted by the lifecycle state machine."""

    name: str
    generation: int


class DesktopActivityLifecycle:
    """Coalesce real desktop actions into one smooth visible activity cycle."""

    ENTER_MS = 200
    MIN_VISIBLE_SECONDS = 0.450
    IDLE_GRACE_SECONDS = 0.300
    EXIT_MS = 280

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        schedule: Callable[[int, Callable[[], None]], Any],
        cancel: Callable[[Any], None],
        on_transition: Callable[[ActivityTransition], None] | None = None,
    ):
        self._clock = clock
        self._schedule = schedule
        self._cancel = cancel
        self._on_transition = on_transition
        self._active: dict[int, str] = {}
        self._generation = 0
        self._state = "hidden"
        self._started_at = 0.0
        self._enter_handle = None
        self._idle_handle = None
        self._exit_handle = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def active_activity_ids(self) -> frozenset[int]:
        return frozenset(self._active)

    def handle_event(self, payload: Mapping[str, Any] | None) -> bool:
        event = DesktopActivityEvent.from_payload(payload)
        if event is None:
            return False
        if event.phase == "begin":
            return self.begin(event.activity_id, event.tool)
        return self.end(event.activity_id, event.tool)

    def begin(self, activity_id: int, tool: str) -> bool:
        if tool not in DESKTOP_ACTIVITY_TOOLS or not self._valid_id(activity_id):
            return False
        if activity_id in self._active:
            return True

        was_idle = not self._active
        self._active[activity_id] = tool
        if not was_idle:
            return True

        self._cancel_pending()
        if self._state in {"entering", "visible"}:
            # A new action arrived during the current visible grace period.  Keep
            # the same visual cycle instead of restarting the fade-in animation.
            return True
        self._generation += 1
        generation = self._generation
        self._started_at = self._clock()
        self._state = "entering"
        self._emit("enter", generation)
        self._enter_handle = self._schedule(
            self.ENTER_MS,
            lambda: self._complete_enter(generation),
        )
        return True

    def end(self, activity_id: int, tool: str | None = None) -> bool:
        if not self._valid_id(activity_id) or activity_id not in self._active:
            return False
        if tool is not None and self._active[activity_id] != tool:
            return False
        self._active.pop(activity_id, None)
        if self._active:
            return True
        self._schedule_exit(self._generation)
        return True

    def force_hide(self) -> None:
        self._active.clear()
        self._cancel_pending()
        self._generation += 1
        if self._state != "hidden":
            self._state = "hidden"
            self._emit("hidden", self._generation)

    destroy = force_hide

    @staticmethod
    def _valid_id(activity_id: Any) -> bool:
        return isinstance(activity_id, int) and not isinstance(activity_id, bool) and activity_id >= 1

    def _complete_enter(self, generation: int) -> None:
        self._enter_handle = None
        if generation != self._generation or self._state != "entering":
            return
        self._state = "visible"
        self._emit("visible", generation)

    def _schedule_exit(self, generation: int) -> None:
        self._cancel_handle("_idle_handle")
        now = self._clock()
        eligible_at = max(
            self._started_at + self.MIN_VISIBLE_SECONDS,
            now + self.IDLE_GRACE_SECONDS,
        )
        delay_ms = max(0, round((eligible_at - now) * 1000))
        self._idle_handle = self._schedule(
            delay_ms,
            lambda: self._start_exit(generation),
        )

    def _start_exit(self, generation: int) -> None:
        self._idle_handle = None
        if generation != self._generation or self._active:
            return
        if self._state == "hidden":
            return
        self._state = "exiting"
        self._emit("exit", generation)
        self._exit_handle = self._schedule(
            self.EXIT_MS,
            lambda: self._complete_exit(generation),
        )

    def _complete_exit(self, generation: int) -> None:
        self._exit_handle = None
        if generation != self._generation or self._active or self._state != "exiting":
            return
        self._state = "hidden"
        self._emit("hidden", generation)

    def _emit(self, name: str, generation: int) -> None:
        if self._on_transition is None:
            return
        try:
            self._on_transition(ActivityTransition(name, generation))
        except Exception:
            # Rendering is advisory; a broken UI callback must not strand lifecycle state.
            return

    def _cancel_pending(self) -> None:
        self._cancel_handle("_enter_handle")
        self._cancel_handle("_idle_handle")
        self._cancel_handle("_exit_handle")

    def _cancel_handle(self, attribute: str) -> None:
        handle = getattr(self, attribute)
        if handle is None:
            return
        try:
            self._cancel(handle)
        finally:
            setattr(self, attribute, None)
