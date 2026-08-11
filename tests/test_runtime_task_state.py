import json
import unittest

from runtime_task_state import RuntimeTaskState
from task_runtime import InMemoryTaskJournal, TASK_STATUS_COMPLETED, TASK_STATUS_WAITING_HUMAN, TASK_STATUS_WAITING_VERIFICATION


class RuntimeTaskStateTests(unittest.TestCase):
    def setUp(self):
        self.journal = InMemoryTaskJournal()

    def test_successful_action_without_evidence_cannot_finish(self):
        state = RuntimeTaskState.start(self.journal, "打开记事本", requires_action=True)
        state.record_tool_result("application_launch", {"ok": True})

        progress = state.finish("completed")

        self.assertEqual(progress["terminal"], "waiting_verification")
        self.assertFalse(progress["verified"])
        self.assertEqual(self.journal.task(state.task_id)["status"], TASK_STATUS_WAITING_VERIFICATION)

    def test_action_request_without_any_action_cannot_finish(self):
        state = RuntimeTaskState.start(self.journal, "打开记事本", requires_action=True)

        progress = state.finish("completed")

        self.assertEqual(progress["terminal"], "waiting_verification")
        self.assertFalse(progress["verified"])

    def test_verified_effect_finishes_and_journal_is_structural(self):
        state = RuntimeTaskState.start(self.journal, "写入临时文件", requires_action=True)
        state.record_tool_result("filesystem_write", {
            "ok": True, "verified": True, "path": "secret-file.txt", "text": "private payload",
        })

        progress = state.finish("completed")
        snapshot = self.journal.evidence_snapshot(state.task_id)

        self.assertEqual(progress["terminal"], "completed")
        self.assertTrue(progress["verified"])
        self.assertEqual(self.journal.task(state.task_id)["status"], TASK_STATUS_COMPLETED)
        self.assertTrue(any(item["kind"] == "workflow_finished" for item in snapshot["events"]))
        self.assertNotIn("private payload", json.dumps(snapshot, ensure_ascii=False))
        self.assertNotIn("secret-file.txt", json.dumps(snapshot, ensure_ascii=False))

    def test_human_handoff_can_resume_without_claiming_completion(self):
        state = RuntimeTaskState.start(self.journal, "完成公开网页验证", requires_action=True)
        waiting = state.waiting_for_human("captcha")
        resumed = state.resumed_by_human()

        self.assertTrue(waiting["waiting_human"])
        self.assertEqual(self.journal.task(state.task_id)["status"], "active")
        self.assertFalse(resumed["waiting_human"])
        self.assertEqual(self.journal.task(state.task_id)["status"], "active")


if __name__ == "__main__":
    unittest.main()
