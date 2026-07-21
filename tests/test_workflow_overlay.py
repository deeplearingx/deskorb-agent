import unittest
from unittest.mock import Mock, patch

from claude_overlay import Overlay
from workflow_sources import FileMaterial, MAX_TOTAL_TEXT_CHARS
from workflows import WorkflowSource
from word_sources import WordMaterial


class WorkflowOverlayDispatchTests(unittest.TestCase):
    def _overlay(self):
        overlay = Overlay.__new__(Overlay)
        overlay.busy = False
        overlay.workflow_sources = []
        overlay.workflow_shots = []
        overlay.workflow_selected_id = "meeting_minutes"
        overlay._workflow_run = None
        overlay._workflow_stream_buffer = ""
        overlay._workflow_tail_started = False
        overlay.add_err = Mock()
        overlay.add_sys = Mock()
        overlay.add_user = Mock()
        overlay._refresh_workflow_sources = Mock()
        overlay._workflow_parameters = Mock(return_value={"language": "Chinese", "detail": "concise"})
        overlay._workflow_text_source = Mock(return_value=None)
        overlay._dispatch_turn = Mock()
        overlay._word_read_request = None
        return overlay

    def test_add_window_material_uses_only_the_active_window_capture(self):
        overlay = self._overlay()
        overlay._grab_window_shot = Mock(return_value=([
            {"path": "C:/temp/window.png", "primary": True, "index": 1, "window": "Mail"}
        ], None))
        overlay._grab_shots = Mock()

        overlay._add_workflow_window()

        overlay._grab_window_shot.assert_called_once_with()
        overlay._grab_shots.assert_not_called()
        self.assertEqual(overlay.workflow_sources[0], WorkflowSource("active_window", "Mail", ""))
        self.assertEqual(overlay.workflow_shots[0]["path"], "C:/temp/window.png")

    def test_removing_second_window_material_keeps_first_window_image(self):
        overlay = self._overlay()
        overlay._grab_window_shot = Mock(side_effect=[
            ([{"path": "C:/temp/window-a.png", "window": "Window A"}], None),
            ([{"path": "C:/temp/window-b.png", "window": "Window B"}], None),
        ])
        overlay._add_workflow_window()
        overlay.workflow_sources.append(WorkflowSource("file", "notes.md", "Meeting notes"))
        overlay._add_workflow_window()
        overlay.workflow_source_list = Mock()
        overlay.workflow_source_list.curselection.return_value = (2,)

        overlay._remove_workflow_source()

        self.assertEqual(
            [shot["path"] for shot in overlay.workflow_shots],
            ["C:/temp/window-a.png"],
        )

    def test_rejects_file_addition_when_selected_file_sources_exceed_total_text_limit(self):
        overlay = self._overlay()
        overlay.root = Mock()
        first = FileMaterial("first.txt", "a" * (MAX_TOTAL_TEXT_CHARS - 1))
        second = FileMaterial("second.txt", "bb")

        with patch("claude_overlay.filedialog.askopenfilenames", side_effect=[("first.txt",), ("second.txt",)]), \
                patch("claude_overlay.extract_file_materials", side_effect=[[first], [second]]):
            overlay._add_workflow_files()
            overlay._add_workflow_files()

        self.assertEqual([source.summary for source in overlay.workflow_sources], ["first.txt"])
        overlay.add_err.assert_called_once_with(
            f"Selected materials exceed the {MAX_TOTAL_TEXT_CHARS:,}-character text limit."
        )

    def test_busy_overlay_does_not_capture_or_start_a_workflow(self):
        overlay = self._overlay()
        overlay.busy = True
        overlay._grab_window_shot = Mock()

        overlay._add_workflow_window()
        overlay._run_selected_workflow()

        overlay._grab_window_shot.assert_not_called()
        overlay._dispatch_turn.assert_not_called()

    def test_word_button_requires_a_word_window_and_never_captures_a_screenshot(self):
        overlay = self._overlay()
        overlay._capture_target_hwnd = Mock(return_value=77)
        overlay._grab_window_shot = Mock()

        with patch("claude_overlay.is_word_window", return_value=False):
            overlay._add_workflow_word()

        overlay._grab_window_shot.assert_not_called()
        overlay.add_err.assert_called_once_with(
            "Focus a Microsoft Word document before opening the Overlay, then try again."
        )

    def test_selected_workflow_combines_sources_and_uses_existing_dispatch(self):
        overlay = self._overlay()
        overlay.workflow_sources = [WorkflowSource("text", "Pasted text", "Discuss launch timing.")]

        overlay._run_selected_workflow()

        prompt, shots = overlay._dispatch_turn.call_args.args
        self.assertIn("Discuss launch timing.", prompt)
        self.assertIn("CODEX_OVERLAY_RECORD", prompt)
        self.assertEqual(shots, [])
        self.assertEqual(overlay._workflow_run["workflow_id"], "meeting_minutes")
        overlay.add_user.assert_called_once()

    def test_word_read_replaces_the_same_document_without_persisting_its_text(self):
        overlay = self._overlay()
        overlay._word_read_request = 9
        overlay.workflow_sources = [
            WorkflowSource("file", "agenda.txt", "Agenda"),
            WorkflowSource("word_document", "Project brief.docx", "Old draft", "word-window:101"),
        ]
        material = WordMaterial(
            document_id="word-window:101", name="Project brief.docx", text="Latest unsaved draft.",
            character_count=22, has_unsaved_changes=True, window_title="Project brief.docx",
        )

        overlay._finish_workflow_word_read(9, material=material)

        self.assertEqual([source.source_type for source in overlay.workflow_sources], ["file", "word_document"])
        word_source = overlay.workflow_sources[1]
        self.assertEqual(word_source.content, "Latest unsaved draft.")
        self.assertEqual(word_source.status, "22 chars · unsaved changes")
        self.assertIsNone(overlay._word_read_request)

        overlay._run_selected_workflow()
        self.assertEqual(
            overlay._workflow_run["source_summaries"],
            [
                {"kind": "file", "name": "agenda.txt"},
                {"kind": "word_document", "name": "Project brief.docx", "character_count": "21"},
            ],
        )
        prompt = overlay._dispatch_turn.call_args.args[0]
        self.assertIn("Latest unsaved draft.", prompt)

    def test_word_read_rejects_text_that_exceeds_the_shared_material_limit(self):
        overlay = self._overlay()
        overlay._word_read_request = 10
        overlay.workflow_sources = [WorkflowSource("file", "notes.txt", "a" * MAX_TOTAL_TEXT_CHARS)]
        material = WordMaterial(
            document_id="word-window:101", name="Project brief.docx", text="new", character_count=3,
            has_unsaved_changes=False, window_title="Project brief.docx",
        )

        overlay._finish_workflow_word_read(10, material=material)

        self.assertEqual(len(overlay.workflow_sources), 1)
        overlay.add_err.assert_called_once_with(
            f"Selected materials exceed the {MAX_TOTAL_TEXT_CHARS:,}-character text limit."
        )

    def test_word_read_ignores_a_late_result_after_timeout(self):
        overlay = self._overlay()
        overlay._word_read_request = None
        material = WordMaterial(
            document_id="word-window:101", name="Project brief.docx", text="Late result", character_count=11,
            has_unsaved_changes=False, window_title="Project brief.docx",
        )

        overlay._finish_workflow_word_read(22, material=material)

        self.assertEqual(overlay.workflow_sources, [])

    def test_word_read_disables_pasted_notes_until_it_finishes(self):
        overlay = self._overlay()
        overlay._word_read_request = 23
        overlay.workflow_text = Mock()

        overlay._refresh_workflow_controls()

        overlay.workflow_text.configure.assert_called_once_with(state="disabled")

    def test_tail_is_not_sent_to_chat_stream_and_is_saved_as_a_record(self):
        overlay = self._overlay()
        overlay._workflow_run = {
            "workflow_id": "meeting_minutes",
            "source_summaries": [{"kind": "text", "title": "Pasted text"}],
        }
        overlay._turn_raw = "# Minutes\n\nDone.\n\n"
        overlay._workflow_stream_buffer = ""
        overlay._workflow_tail_started = False
        overlay._md_feed = Mock()
        overlay.workflow_repository = Mock()

        visible = overlay._workflow_visible_delta(
            "<!-- CODEX_OVERLAY_RECORD\n"
            '{"workflow_id":"meeting_minutes","summary":"Done","decisions":[],"action_items":[],"open_questions":[]}\n-->'
        )
        overlay._complete_workflow_run()

        self.assertEqual(visible, "")
        self.assertEqual(overlay._turn_raw, "# Minutes\n\nDone.")
        overlay.workflow_repository.create_record.assert_called_once()
        self.assertIsNone(overlay._workflow_run)


if __name__ == "__main__":
    unittest.main()
