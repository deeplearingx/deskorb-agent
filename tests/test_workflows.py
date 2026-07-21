import unittest

from workflows import (
    WORKFLOWS,
    WorkflowSource,
    build_workflow_prompt,
    get_workflow,
    parse_workflow_result,
)


class WorkflowRegistryTests(unittest.TestCase):
    def test_registers_the_four_stable_communication_workflows(self):
        self.assertEqual(
            [workflow.id for workflow in WORKFLOWS],
            [
                "meeting_minutes",
                "action_tracker",
                "draft_reply",
                "post_meeting_followup",
            ],
        )
        for workflow in WORKFLOWS:
            self.assertTrue(workflow.label)
            self.assertTrue(workflow.parameters)
            self.assertTrue(workflow.allowed_sources)
            self.assertTrue(workflow.output_fields)

    def test_unknown_workflow_is_rejected_with_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, "Unknown workflow"):
            get_workflow("not-a-workflow")

    def test_prompt_contains_selected_sources_parameters_and_record_tail_contract(self):
        prompt = build_workflow_prompt(
            "meeting_minutes",
            [
                WorkflowSource("text", "pasted agenda", "Discuss launch timing."),
                WorkflowSource("file", "notes.md", "Decision: ship Friday."),
            ],
            {"language": "Chinese", "detail": "concise"},
        )

        self.assertIn("Discuss launch timing.", prompt)
        self.assertIn("notes.md", prompt)
        self.assertIn("Chinese", prompt)
        self.assertIn("readable Markdown", prompt)
        self.assertIn("<!-- CODEX_OVERLAY_RECORD", prompt)
        self.assertIn('"workflow_id": "meeting_minutes"', prompt)
        self.assertIn("action_items", prompt)

    def test_prompt_rejects_source_types_not_allowed_by_the_workflow(self):
        with self.assertRaisesRegex(ValueError, "does not accept source type"):
            build_workflow_prompt(
                "draft_reply",
                [WorkflowSource("unknown", "mystery", "text")],
            )

    def test_word_document_is_a_supported_transient_source(self):
        source = WorkflowSource(
            "word_document", "Project brief.docx", "Latest unsaved draft.",
            source_id="word-window:101", status="23 chars · unsaved changes",
        )

        prompt = build_workflow_prompt("meeting_minutes", [source])

        self.assertIn("word_document", prompt)
        self.assertIn("Project brief.docx", prompt)
        self.assertIn("Latest unsaved draft.", prompt)


class WorkflowResultParsingTests(unittest.TestCase):
    def test_parser_returns_visible_markdown_and_validated_record_data(self):
        result = parse_workflow_result(
            "# Meeting notes\n\nReady to share.\n\n"
            "<!-- CODEX_OVERLAY_RECORD\n"
            '{"workflow_id":"meeting_minutes","summary":"Ready","decisions":[],"action_items":[],"open_questions":[]}\n'
            "-->",
            "meeting_minutes",
        )

        self.assertEqual(result.markdown, "# Meeting notes\n\nReady to share.")
        self.assertEqual(result.data["summary"], "Ready")

    def test_parser_hides_invalid_record_tail_without_losing_visible_markdown(self):
        result = parse_workflow_result(
            "# Draft\n\nHello.\n\n<!-- CODEX_OVERLAY_RECORD\nnot json\n-->",
            "draft_reply",
        )

        self.assertEqual(result.markdown, "# Draft\n\nHello.")
        self.assertIsNone(result.data)

    def test_parser_rejects_record_for_a_different_workflow(self):
        result = parse_workflow_result(
            "Visible reply\n<!-- CODEX_OVERLAY_RECORD\n"
            '{"workflow_id":"meeting_minutes","summary":"wrong","decisions":[],"action_items":[],"open_questions":[]}\n'
            "-->",
            "draft_reply",
        )

        self.assertEqual(result.markdown, "Visible reply")
        self.assertIsNone(result.data)

    def test_parser_rejects_malformed_action_items_and_keeps_visible_markdown(self):
        result = parse_workflow_result(
            "# Minutes\n\nVisible result.\n\n<!-- CODEX_OVERLAY_RECORD\n"
            '{"workflow_id":"meeting_minutes","summary":"Done","decisions":[],"action_items":[{"task":"","due_date":"tomorrow"}],"open_questions":[]}\n'
            "-->",
            "meeting_minutes",
        )

        self.assertEqual(result.markdown, "# Minutes\n\nVisible result.")
        self.assertIsNone(result.data)

    def test_parser_rejects_every_declared_field_with_the_wrong_type(self):
        list_fields = {
            "decisions", "open_questions", "risks", "commitments_to_confirm", "action_items",
        }
        for workflow in WORKFLOWS:
            valid = {"workflow_id": workflow.id}
            valid.update({
                field.id: ([] if field.id in list_fields else "Valid text")
                for field in workflow.output_fields
            })
            for field in workflow.output_fields:
                with self.subTest(workflow=workflow.id, field=field.id):
                    invalid = dict(valid)
                    invalid[field.id] = "not a list" if field.id in list_fields else []
                    result = parse_workflow_result(
                        "Visible result\n<!-- CODEX_OVERLAY_RECORD\n"
                        + __import__("json").dumps(invalid)
                        + "\n-->",
                        workflow.id,
                    )

                    self.assertEqual(result.markdown, "Visible result")
                    self.assertIsNone(result.data)


if __name__ == "__main__":
    unittest.main()
