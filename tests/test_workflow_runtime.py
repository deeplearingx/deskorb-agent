import unittest

from workflow_runtime import TaskWorkflow, verify_price_candidates


class TaskWorkflowTests(unittest.TestCase):
    def test_successful_action_is_not_treated_as_verified_without_evidence(self):
        workflow = TaskWorkflow("T1", "Open an application")
        workflow.record_tool_result("application_launch", {"ok": True})
        progress = workflow.finish("completed")
        self.assertFalse(progress["verified"])
        self.assertEqual(workflow.nodes[0].kind, "action")

    def test_verified_write_and_state_change_supply_evidence(self):
        workflow = TaskWorkflow("T2", "Write a file")
        workflow.record_tool_result("filesystem_write", {"ok": True, "verified": True})
        workflow.record_tool_result("desktop_verify_state", {"ok": True, "screen_changed": True})
        progress = workflow.finish("completed")
        self.assertTrue(progress["verified"])
        self.assertEqual(progress["evidence_steps"], 2)
        self.assertEqual(workflow.nodes[1].parent_id, 1)

    def test_human_handoff_is_visible_in_progress(self):
        workflow = TaskWorkflow("T3", "Search")
        workflow.waiting_for_human()
        self.assertTrue(workflow.progress()["waiting_human"])
        workflow.resumed_by_human()
        self.assertFalse(workflow.progress()["waiting_human"])

    def test_nodes_have_conditions_evidence_schema_and_retry_policy(self):
        workflow = TaskWorkflow("T4", "Write a file")
        node = workflow.record_tool_result("filesystem_write", {"ok": True, "verified": True})
        self.assertEqual(node.precondition["authorized"], True)
        self.assertTrue(node.postcondition["evidence"])
        self.assertEqual(node.evidence_schema, "path_and_content_hash")
        self.assertIn("max_attempts", node.retry_policy)

    def test_product_verifier_rejects_unstructured_or_out_of_range_items(self):
        result = verify_price_candidates([
            {"title": "Cotton tee", "price_cny": 129, "url": "https://shop.test/p/1", "rating": 4.8},
            {"title": "Too cheap", "price_cny": 80, "url": "https://shop.test/p/2"},
            {"title": "Missing URL", "price_cny": 130},
        ], 100, 150)
        self.assertTrue(result["verified"])
        self.assertEqual(len(result["candidates"]), 1)


if __name__ == "__main__":
    unittest.main()
