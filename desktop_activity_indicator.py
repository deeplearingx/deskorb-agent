"""State and event primitives for the real-desktop activity indicator.

The lifecycle in this module deliberately has no Tk or Win32 dependency.  The UI
layer supplies a main-thread scheduler and reacts to :class:`ActivityTransition`
objects, which keeps timing and stale-event behavior deterministic in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Mapping


def indicator_window_geometry(
    monitors: list[Mapping[str, Any]],
    *,
    edge_band: int = 60,
    pill_width: int = 360,
    pill_height: int = 44,
    top_margin: int = 20,
) -> list[dict[str, Any]]:
    """Return safe rectangles for each monitor's four edge bands and pill."""
    layout: list[dict[str, Any]] = []
    for monitor_index, monitor in enumerate(monitors):
        rect = monitor.get("rect") if isinstance(monitor, Mapping) else None
        if not isinstance(rect, (tuple, list)) or len(rect) != 4:
            continue
        left, top, right, bottom = (int(value) for value in rect)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            continue
        band_x = max(1, min(int(edge_band), max(1, width // 3)))
        band_y = max(1, min(int(edge_band), max(1, height // 3)))
        inner_left, inner_top = left + band_x, top + band_y
        inner_right, inner_bottom = right - band_x, bottom - band_y
        if inner_right <= inner_left:
            inner_left, inner_right = left, right
        if inner_bottom <= inner_top:
            inner_top, inner_bottom = top, bottom
        item_base = {"monitor_index": monitor_index}
        for kind, item_rect in (
            ("top", (left, top, right, inner_top)),
            ("bottom", (left, inner_bottom, right, bottom)),
            ("left", (left, inner_top, inner_left, inner_bottom)),
            ("right", (inner_right, inner_top, right, inner_bottom)),
        ):
            layout.append({**item_base, "kind": kind, "rect": item_rect})

        actual_pill_width = min(max(1, int(pill_width)), width)
        actual_pill_height = min(max(1, int(pill_height)), height)
        pill_left = left + max(0, (width - actual_pill_width) // 2)
        pill_top = top + max(0, min(int(top_margin), height - actual_pill_height))
        layout.append({
            **item_base,
            "kind": "pill",
            "rect": (pill_left, pill_top,
                     pill_left + actual_pill_width, pill_top + actual_pill_height),
        })
    return layout


DESKTOP_ACTIVITY_TOOLS = frozenset({
    "application_launch",
    "desktop_click",
    "desktop_type",
    "desktop_hotkey",
    "desktop_scroll",
    "window_focus",
    "window_control",
    "desktop_uia_invoke",
    "desktop_uia_set_value",
})

# Browser actions use the same privacy-bounded visual affordance, while page
# observation/extraction remain invisible because they do not manipulate the
# user's computer.
BROWSER_ACTIVITY_TOOLS = frozenset({"browser_navigate", "browser_click", "browser_input", "browser_select"})
ACTIVITY_TOOLS = DESKTOP_ACTIVITY_TOOLS | BROWSER_ACTIVITY_TOOLS


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
        if not isinstance(tool, str) or tool not in ACTIVITY_TOOLS:
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
        if tool not in ACTIVITY_TOOLS or not self._valid_id(activity_id):
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


class DesktopActivityIndicator:
    """Tk renderer for the lifecycle's passive desktop-operation indicator.

    All methods are intended for the Tk thread.  The constructor accepts small
    adapters so the window lifecycle can be tested without a display server or
    real Win32 HWNDs.
    """

    TEXT = "DeskOrb 正在操作你的电脑"
    MAX_ALPHA = 0.24
    FRAME_MS = 20
    EDGE_BAND = 60
    PILL_WIDTH = 360
    PILL_HEIGHT = 44
    TOP_MARGIN = 20

    def __init__(
        self,
        root: Any,
        *,
        monitor_provider: Callable[[], list[Mapping[str, Any]]] | None = None,
        window_factory: Callable[[Any], Any] | None = None,
        configure_window: Callable[..., bool] | None = None,
        uninstall_window: Callable[[Any], Any] | None = None,
        content_builder: Callable[[Any, str, int, int], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        schedule: Callable[[int, Callable[[], None]], Any] | None = None,
        cancel: Callable[[Any], None] | None = None,
    ):
        self.root = root
        self._monitor_provider = monitor_provider or self._default_monitor_provider
        self._window_factory = window_factory or self._default_window_factory
        self._configure_window = configure_window or self._default_configure_window
        self._uninstall_window = uninstall_window or self._default_uninstall_window
        self._content_builder = content_builder or self._default_content_builder
        self._clock = clock
        self._schedule = schedule or root.after
        self._cancel = cancel or root.after_cancel
        self.windows: list[Any] = []
        self._window_records: list[dict[str, Any]] = []
        self._animation_handle = None
        self._animation_generation = 0
        self._animation_started = 0.0
        self._animation_duration = 0.0
        self._animation_from = 0.0
        self._animation_to = 0.0
        self._render_alpha = 0.0
        self._layout_signature: tuple[tuple[int, int, int, int], ...] | None = None
        self._layout_pending = False
        self._animation_window_visible = False
        self.disabled = False
        self.destroyed = False
        self._lifecycle = DesktopActivityLifecycle(
            clock=clock,
            schedule=self._schedule,
            cancel=self._cancel,
            on_transition=self._on_transition,
        )

    @property
    def lifecycle(self) -> DesktopActivityLifecycle:
        return self._lifecycle

    @property
    def window_count(self) -> int:
        return len(self.windows)

    def handle_event(self, payload: Mapping[str, Any] | None) -> bool:
        if self.disabled or self.destroyed:
            return False
        event = DesktopActivityEvent.from_payload(payload)
        if event is None:
            return False
        if event.phase == "begin" and not self._window_records:
            if not self._ensure_windows():
                self.disabled = True
                return False
        elif event.phase == "begin":
            self.refresh_layout()
        accepted = self._lifecycle.handle_event(payload)
        if not accepted and event.phase == "begin" and not self._lifecycle.active_activity_ids:
            self._hide_windows()
        return accepted

    def refresh_layout(self) -> bool:
        """Refresh monitor geometry when idle; defer rebuilding during an action."""
        if self.disabled or self.destroyed:
            return False
        try:
            monitors = list(self._monitor_provider() or [])
            layout = indicator_window_geometry(
                monitors,
                edge_band=self.EDGE_BAND,
                pill_width=self.PILL_WIDTH,
                pill_height=self.PILL_HEIGHT,
                top_margin=self.TOP_MARGIN,
            )
            signature = tuple(tuple(item["rect"]) for item in layout)
            if signature == self._layout_signature:
                return True
            if self._lifecycle.active_activity_ids or self._lifecycle.state != "hidden":
                self._layout_pending = True
                return False
            if self._window_records:
                self._destroy_windows_after_failure()
            if not layout:
                return False
            return self._ensure_windows(monitors=monitors, layout=layout)
        except Exception:
            self._destroy_windows_after_failure()
            self.disabled = True
            return False

    def force_hide(self) -> None:
        if self.destroyed:
            return
        self._lifecycle.force_hide()
        self._cancel_animation()
        self._hide_windows()

    def destroy(self) -> None:
        if self.destroyed:
            return
        self.destroyed = True
        self._lifecycle.force_hide()
        self._cancel_animation()
        for record in list(self._window_records):
            window = record["window"]
            hwnd = record.get("hwnd")
            try:
                if hwnd:
                    self._uninstall_window(hwnd)
            except Exception:
                pass
            try:
                window.destroy()
            except Exception:
                pass
        self._window_records.clear()
        self.windows.clear()

    def _ensure_windows(self, *, monitors=None, layout=None) -> bool:
        try:
            monitors = list(self._monitor_provider() or []) if monitors is None else list(monitors)
            layout = indicator_window_geometry(
                monitors,
                edge_band=self.EDGE_BAND,
                pill_width=self.PILL_WIDTH,
                pill_height=self.PILL_HEIGHT,
                top_margin=self.TOP_MARGIN,
            ) if layout is None else list(layout)
            if not layout:
                return False
            for item in layout:
                left, top, right, bottom = item["rect"]
                width, height = right - left, bottom - top
                window = self._window_factory(self.root)
                if hasattr(window, "overrideredirect"):
                    window.overrideredirect(True)
                if hasattr(window, "attributes"):
                    try:
                        window.attributes("-topmost", True)
                        window.attributes("-alpha", 0.0)
                    except Exception:
                        pass
                if hasattr(window, "geometry"):
                    window.geometry(f"{width}x{height}+{left}+{top}")
                self._content_builder(window, item["kind"], width, height)
                if hasattr(window, "update_idletasks"):
                    window.update_idletasks()
                hwnd = window.winfo_id() if hasattr(window, "winfo_id") else None
                self._window_records.append({"window": window, "hwnd": hwnd, "kind": item["kind"]})
                self.windows.append(window)
                if not self._configure_window(hwnd, capture_excluded=True):
                    raise RuntimeError("indicator window safety configuration failed")
                if hasattr(window, "withdraw"):
                    window.withdraw()
            self._layout_signature = tuple(tuple(item["rect"]) for item in layout)
            self._layout_pending = False
            return True
        except Exception:
            self._destroy_windows_after_failure()
            return False

    def _destroy_windows_after_failure(self) -> None:
        for record in list(self._window_records):
            try:
                hwnd = record.get("hwnd")
                if hwnd:
                    self._uninstall_window(hwnd)
            except Exception:
                pass
            try:
                record["window"].destroy()
            except Exception:
                pass
        self._window_records.clear()
        self.windows.clear()

    def _on_transition(self, transition: ActivityTransition) -> None:
        if self.destroyed or self.disabled:
            return
        self._animation_generation = transition.generation
        if transition.name == "enter":
            # When an action arrives during fade-out, reverse from the current
            # opacity so the edge never flashes off and on again.
            self._start_animation(transition.generation, self._current_alpha(), self.MAX_ALPHA, 0.200)
        elif transition.name == "visible":
            self._cancel_animation()
            self._set_alpha(self.MAX_ALPHA)
            self._show_windows()
        elif transition.name == "exit":
            self._start_animation(transition.generation, self._current_alpha(), 0.0, 0.280)
        elif transition.name == "hidden":
            self._cancel_animation()
            self._set_alpha(0.0)
            self._hide_windows()
            if self._layout_pending:
                self.refresh_layout()

    def _start_animation(self, generation: int, start: float, target: float, duration: float) -> None:
        self._cancel_animation()
        self._animation_generation = generation
        self._animation_started = self._clock()
        self._animation_duration = duration
        self._animation_from = max(0.0, min(self.MAX_ALPHA, start))
        self._animation_to = max(0.0, min(self.MAX_ALPHA, target))
        self._show_windows()
        self._set_alpha(self._animation_from)
        self._animation_handle = self._schedule(self.FRAME_MS, self._animate_frame)

    def _animate_frame(self) -> None:
        self._animation_handle = None
        if self.destroyed or self.disabled:
            return
        elapsed = max(0.0, self._clock() - self._animation_started)
        progress = min(1.0, elapsed / max(0.001, self._animation_duration))
        eased = self._ease(progress)
        alpha = self._animation_from + (self._animation_to - self._animation_from) * eased
        self._set_alpha(alpha)
        if progress >= 1.0:
            if self._animation_to <= 0.0:
                self._hide_windows()
            return
        self._animation_handle = self._schedule(self.FRAME_MS, self._animate_frame)

    @staticmethod
    def _ease(progress: float) -> float:
        # Smoothstep gives a gentle start/end without importing an animation toolkit.
        return progress * progress * (3.0 - 2.0 * progress)

    def _current_alpha(self) -> float:
        return self._render_alpha

    def _set_alpha(self, alpha: float) -> None:
        alpha = max(0.0, min(self.MAX_ALPHA, float(alpha)))
        self._render_alpha = alpha
        for window in self.windows:
            try:
                window.attributes("-alpha", alpha)
            except Exception:
                try:
                    window.wm_attributes("-alpha", alpha)
                except Exception:
                    pass

    def _show_windows(self) -> None:
        if self._animation_window_visible:
            return
        for window in self.windows:
            try:
                window.deiconify()
            except Exception:
                pass
        self._animation_window_visible = True

    def _hide_windows(self) -> None:
        for window in self.windows:
            try:
                window.withdraw()
            except Exception:
                pass
        self._animation_window_visible = False

    def _cancel_animation(self) -> None:
        if self._animation_handle is None:
            return
        try:
            self._cancel(self._animation_handle)
        except Exception:
            pass
        finally:
            self._animation_handle = None

    @staticmethod
    def _default_monitor_provider() -> list[Mapping[str, Any]]:
        try:
            from win32utils import enumerate_monitors
            return enumerate_monitors()
        except Exception:
            return []

    @staticmethod
    def _default_window_factory(root: Any) -> Any:
        import tkinter as tk
        window = tk.Toplevel(root)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.withdraw()
        return window

    @staticmethod
    def _default_configure_window(hwnd: Any, **kwargs: Any) -> bool:
        try:
            from win32utils import configure_indicator_window
            return bool(configure_indicator_window(hwnd, **kwargs))
        except Exception:
            return False

    @staticmethod
    def _default_uninstall_window(hwnd: Any) -> bool:
        try:
            from win32utils import uninstall_indicator_hit_test
            return bool(uninstall_indicator_hit_test(hwnd))
        except Exception:
            return False

    @staticmethod
    def _default_content_builder(window: Any, kind: str, width: int, height: int) -> Any:
        """Paint feathered edge bands or the fixed status pill without screen content."""
        from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk
        import tkinter as tk

        image = Image.new("RGBA", (max(1, width), max(1, height)), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        if kind == "pill":
            draw.rounded_rectangle(
                (1, 1, max(1, width - 2), max(1, height - 2)),
                radius=min(18, max(1, height // 2)),
                fill=(18, 27, 43, 232),
                outline=(125, 211, 252, 210),
                width=1,
            )
            font = None
            for font_path in (
                r"C:\Windows\Fonts\msyh.ttc",
                r"C:\Windows\Fonts\segoeui.ttf",
            ):
                try:
                    font = ImageFont.truetype(font_path, 15)
                    break
                except Exception:
                    pass
            if font is None:
                font = ImageFont.load_default()
            dot_y = height // 2
            draw.ellipse((14, dot_y - 5, 24, dot_y + 5), fill=(125, 211, 252, 240))
            draw.text((34, max(2, dot_y - 10)), DesktopActivityIndicator.TEXT,
                      font=font, fill=(235, 245, 255, 245))
        else:
            # A feathered low-alpha band reads as peripheral blur/dimming while the
            # center of the monitor remains completely uncovered.
            steps = max(1, height if kind in {"top", "bottom"} else width)
            for index in range(steps):
                distance = index / max(1, steps - 1)
                strength = int(72 * (1.0 - distance) ** 1.7)
                if kind == "top":
                    box = (0, index, width, index + 1)
                elif kind == "bottom":
                    box = (0, height - index - 1, width, height - index)
                elif kind == "left":
                    box = (index, 0, index + 1, height)
                else:
                    box = (width - index - 1, 0, width - index, height)
                draw.rectangle(box, fill=(9, 18, 31, strength))
            image = image.filter(ImageFilter.GaussianBlur(radius=1.2))

        if kind == "pill":
            try:
                window.attributes("-transparentcolor", "#000000")
            except Exception:
                pass
        canvas = tk.Canvas(window, width=width, height=height, bd=0,
                           highlightthickness=0, bg="#000000")
        canvas.pack(fill="both", expand=True)
        photo = ImageTk.PhotoImage(image)
        canvas.create_image(0, 0, anchor="nw", image=photo)
        window._deskorb_indicator_photo = photo
        window._deskorb_indicator_canvas = canvas
        return canvas
