import unittest
from unittest.mock import Mock, patch

from e2e_notepad_app_probe import _activate_window


class NotepadActivationTests(unittest.TestCase):
    def test_activation_accepts_a_child_foreground_handle(self):
        state = {"foreground": 99}

        user32 = Mock()
        def hwnd_value(hwnd):
            return int(getattr(hwnd, "value", hwnd))

        user32.GetAncestor = Mock(side_effect=lambda hwnd, _flags: 101 if hwnd_value(hwnd) in {101, 202} else hwnd_value(hwnd))
        user32.GetForegroundWindow = Mock(side_effect=lambda: state["foreground"])
        user32.GetWindowThreadProcessId = Mock(side_effect=lambda _hwnd, pid: setattr(pid._obj, "value", 1) or 1)
        attached = {"value": False}
        message_queue = {"value": False}
        user32.PeekMessageW = Mock(side_effect=lambda *_args: message_queue.update(value=True) or True)
        user32.AttachThreadInput = Mock(
            side_effect=lambda _source, _target, enabled: (
                attached.update(value=bool(enabled)) or message_queue["value"]
            )
        )
        user32.SetForegroundWindow = Mock(
            side_effect=lambda _hwnd: (state.update(foreground=202) or True)
            if attached["value"] else False
        )
        user32.SetActiveWindow = Mock(return_value=True)
        user32.SetFocus = Mock(return_value=True)
        kernel32 = Mock()
        kernel32.GetCurrentThreadId = Mock(return_value=33)

        def load_library(name, **_kwargs):
            return kernel32 if name == "kernel32" else user32

        with patch("e2e_notepad_app_probe.ctypes.WinDLL", side_effect=load_library):
            self.assertTrue(_activate_window(101))
        self.assertTrue(user32.PeekMessageW.called)
        self.assertEqual(user32.AttachThreadInput.call_args_list[0].args[:2], (33, 1))

    def test_activation_uses_alt_wakeup_when_thread_attachment_is_rejected(self):
        state = {"foreground": 99, "alt_woken": False}

        user32 = Mock()
        user32.GetAncestor = Mock(side_effect=lambda hwnd, _flags: 101 if int(getattr(hwnd, "value", hwnd)) == 101 else int(getattr(hwnd, "value", hwnd)))
        user32.GetForegroundWindow = Mock(side_effect=lambda: state["foreground"])
        user32.GetWindowThreadProcessId = Mock(side_effect=lambda _hwnd, pid: setattr(pid._obj, "value", 22) or 22)
        user32.PeekMessageW = Mock(return_value=True)
        user32.AttachThreadInput = Mock(return_value=False)
        user32.keybd_event = Mock(side_effect=lambda _vk, _scan, flags, _extra: state.update(alt_woken=flags == 0x0002))
        user32.SetForegroundWindow = Mock(side_effect=lambda _hwnd: (
            state.update(foreground=101) or True
        ) if state["alt_woken"] else False)
        user32.SetActiveWindow = Mock(return_value=True)
        user32.SetFocus = Mock(return_value=True)
        kernel32 = Mock()
        kernel32.GetCurrentThreadId = Mock(return_value=33)

        with patch("e2e_notepad_app_probe.ctypes.WinDLL",
                   side_effect=lambda name, **_kwargs: kernel32 if name == "kernel32" else user32):
            self.assertTrue(_activate_window(101))

        self.assertTrue(user32.keybd_event.called)


if __name__ == "__main__":
    unittest.main()
