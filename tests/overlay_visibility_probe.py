r"""Manual Windows/Tk visibility probe; run from the repository root.

Examples:
    .\.venv\Scripts\python.exe .\tests\overlay_visibility_probe.py
    .\.venv\Scripts\python.exe .\tests\overlay_visibility_probe.py --no-taskbar

The probe deliberately does not force, move, focus, hide, or otherwise alter the
window. It reports the state that Windows and Tk expose once per second. Stop it
with Ctrl+C in the console.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import deskorb_agent as app


def _window_report(overlay):
    root = overlay.root
    hwnd = overlay._hwnd()
    rect = wt.RECT()
    app._user32.GetWindowRect(hwnd, ctypes.byref(rect))
    print(
        "state=%s mapped=%s viewable=%s geometry=%s hwnd=%s visible=%s "
        "iconic=%s foreground=%s rect=(%s,%s,%s,%s) topmost=%s capture_excluded=%s"
        % (
            root.state(),
            root.winfo_ismapped(),
            root.winfo_viewable(),
            root.geometry(),
            hwnd,
            bool(app._user32.IsWindowVisible(hwnd)),
            bool(app._user32.IsIconic(hwnd)),
            app._user32.GetForegroundWindow(),
            rect.left,
            rect.top,
            rect.right,
            rect.bottom,
            root.attributes("-topmost"),
            overlay._capture_excluded,
        ),
        flush=True,
    )
    root.after(1000, _window_report, overlay)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-taskbar",
        action="store_true",
        help="disable only the DeskOrb taskbar-button setup for this diagnostic run",
    )
    parser.add_argument(
        "--origin",
        action="store_true",
        help="place the window at +100+100 without changing any other startup behavior",
    )
    parser.add_argument(
        "--share-visible",
        action="store_true",
        help="do not apply Windows screen-capture exclusion for this diagnostic run",
    )
    args = parser.parse_args()
    if args.no_taskbar:
        app.TASKBAR_BUTTON = False
        print("probe: TASKBAR_BUTTON=False", flush=True)
    else:
        print("probe: TASKBAR_BUTTON=%s" % app.TASKBAR_BUTTON, flush=True)
    if args.share_visible:
        app.SHOW_IN_SCREEN_SHARE_DEFAULT = True
        print("probe: SHOW_IN_SCREEN_SHARE_DEFAULT=True", flush=True)

    print("probe: monitors=%s" % app.enumerate_monitors(), flush=True)
    print("probe: virtual_screen=%s" % (app.virtual_screen_metrics(),), flush=True)

    app.set_dpi_awareness()
    app.set_app_user_model_id()
    overlay = app.Overlay()
    if args.origin:
        overlay.root.geometry("+100+100")
        print("probe: geometry forced to +100+100", flush=True)
    overlay.root.after(500, _window_report, overlay)
    overlay.run()


if __name__ == "__main__":
    main()
