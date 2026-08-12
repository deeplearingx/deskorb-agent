import unittest

from workflow_runtime import TaskContract, TaskWorkflow, verify_price_candidates


class TaskWorkflowTests(unittest.TestCase):
    def test_contract_requires_matching_evidence_schema_for_browser_task(self):
        contract = TaskContract.from_goal("搜索淘宝商品并返回价格", requires_action=True)
        self.assertIn("browser_structured_verification", contract.required_evidence_schemas)
        workflow = TaskWorkflow("TC", "搜索淘宝商品并返回价格", contract=contract)
        workflow.record_tool_result("desktop_verify_state", {"ok": True, "screen_changed": True})
        progress = workflow.finish("completed")
        self.assertFalse(progress["verified"])
        self.assertEqual(progress["terminal"], "waiting_verification")

    def test_composite_contract_requires_every_evidence_schema(self):
        contract = TaskContract.from_goal("搜索淘宝商品并保存结果到文件", requires_action=True)
        self.assertEqual(set(contract.required_evidence_schemas), {
            "browser_structured_verification", "path_and_content_hash",
        })
        workflow = TaskWorkflow("TC-composite", contract.goal, contract=contract)
        workflow.record_tool_result("browser_action_batch", {
            "ok": True, "verification": {"passed": True},
        })
        partial = workflow.finish("completed")
        self.assertFalse(partial["verified"])
        self.assertEqual(partial["terminal"], "waiting_verification")
        workflow.record_tool_result("filesystem_write", {"ok": True, "verified": True})
        complete = workflow.finish("completed")
        self.assertTrue(complete["verified"])
        self.assertEqual(complete["terminal"], "completed")

    def test_read_only_file_diagnosis_does_not_require_write_hash(self):
        contract = TaskContract.from_goal("检查项目文件并报告错误", requires_action=True)
        self.assertNotIn("path_and_content_hash", contract.required_evidence_schemas)

    def test_negative_desktop_scope_does_not_create_file_change_contract(self):
        contract = TaskContract.from_goal(
            "A disposable Notepad window is open. Do not open or modify any other application.",
            requires_action=True,
        )
        self.assertNotIn("path_and_content_hash", contract.required_evidence_schemas)

    def test_repair_task_requires_verified_file_change_evidence(self):
        contract = TaskContract.from_goal("诊断问题并修复安全修复的问题", requires_action=True)
        self.assertIn("path_and_content_hash", contract.required_evidence_schemas)
        workflow = TaskWorkflow("T-repair", contract.goal, contract=contract)
        workflow.record_tool_result("filesystem_search_text", {"ok": True, "results": []})
        progress = workflow.finish("completed")
        self.assertFalse(progress["verified"])
        self.assertEqual(progress["terminal"], "waiting_verification")

    def test_successful_read_only_shell_check_is_exit_status_evidence(self):
        workflow = TaskWorkflow("T-shell", "运行语法检查并报告结果",
                                contract=TaskContract.from_goal("运行语法检查并报告结果", requires_action=True))
        workflow.record_tool_result("shell_run", {"ok": True, "exit_code": 0})
        progress = workflow.finish("completed")
        self.assertTrue(progress["verified"])

    def test_read_only_file_observation_is_evidence_for_diagnosis(self):
        workflow = TaskWorkflow("T-file-read", "检查项目文件并报告错误")
        workflow.record_tool_result("filesystem_search_text", {
            "ok": True, "results": [{"path": "app.py", "line": 1}],
        })

        progress = workflow.finish("completed")

        self.assertTrue(progress["verified"])
        self.assertEqual(progress["terminal"], "completed")

    def test_successful_action_is_not_treated_as_verified_without_evidence(self):
        workflow = TaskWorkflow("T1", "Open an application")
        workflow.record_tool_result("application_launch", {"ok": True})
        progress = workflow.finish("completed")
        self.assertFalse(progress["verified"])
        self.assertEqual(progress["terminal"], "waiting_verification")
        self.assertEqual(workflow.nodes[0].kind, "action")

    def test_failed_action_cannot_be_hidden_by_later_evidence(self):
        workflow = TaskWorkflow("T-failed", "打开浏览器并返回结果")
        workflow.record_tool_result("mcp_playwright_browser_click", {"ok": False, "error": "target closed"})
        workflow.record_tool_result("mcp_playwright_browser_snapshot", {
            "ok": True, "verification": {"passed": True},
        })

        progress = workflow.finish("completed")

        self.assertFalse(progress["verified"])
        self.assertEqual(progress["terminal"], "failed")

    def test_unexecuted_focus_failure_can_be_retried_before_verification(self):
        contract = TaskContract.from_goal("在记事本中输入文本", requires_action=True)
        workflow = TaskWorkflow("T-focus-retry", contract.goal, contract=contract)
        workflow.record_tool_result("desktop_type", {
            "ok": False, "error": "Active window changed since the snapshot; capture fresh state first.",
        }, failure_kind="desktop_focus_failure")
        workflow.record_tool_result("desktop_type", {"ok": True})
        workflow.record_tool_result("desktop_verify_state", {
            "ok": True, "screen_changed": True,
        })

        progress = workflow.finish("completed")

        self.assertTrue(progress["verified"])
        self.assertEqual(progress["terminal"], "completed")

    def test_browser_protocol_rejection_can_be_retried_without_false_failure(self):
        contract = TaskContract.from_goal("浏览器搜索结果", requires_action=True)
        workflow = TaskWorkflow("T-browser-retry", contract.goal, contract=contract)
        workflow.record_tool_result("browser_action_batch", {
            "ok": False, "failure_kind": "invalid_browser_action_batch",
        })
        workflow.record_tool_result("browser_action_batch", {
            "ok": True,
            "verification": {"passed": True, "kind": "browser_structured_verification"},
        })
        progress = workflow.finish("completed")
        self.assertTrue(progress["verified"])
        self.assertEqual(progress["terminal"], "completed")

    def test_verified_write_and_state_change_supply_evidence(self):
        workflow = TaskWorkflow("T2", "Write a file")
        workflow.record_tool_result("filesystem_write", {"ok": True, "verified": True})
        workflow.record_tool_result("desktop_verify_state", {"ok": True, "screen_changed": True})
        progress = workflow.finish("completed")
        self.assertTrue(progress["verified"])
        self.assertEqual(progress["evidence_steps"], 2)
        self.assertEqual(workflow.nodes[1].parent_id, 1)

    def test_verified_uia_value_readback_supplies_desktop_evidence(self):
        workflow = TaskWorkflow("T2b", "打开记事本并输入内容")
        node = workflow.record_tool_result("desktop_uia_set_value", {
            "ok": True, "verified": True, "verification": {"passed": True},
        })
        progress = workflow.finish("completed")
        self.assertTrue(progress["verified"])
        self.assertEqual(node.evidence_schema, "desktop_state_delta")

    def test_verified_uia_invoke_state_change_supplies_desktop_evidence(self):
        workflow = TaskWorkflow("T2c", "勾选记住设置")
        node = workflow.record_tool_result("desktop_uia_invoke", {
            "ok": True, "verified": True,
            "verification": {"passed": True, "kind": "uia_state_change"},
        })
        progress = workflow.finish("completed")
        self.assertTrue(progress["verified"])
        self.assertEqual(node.evidence_schema, "desktop_state_delta")

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

    def test_contract_compiles_product_price_terms_and_fields(self):
        contract = TaskContract.from_goal("搜索淘宝，找到 100-150 元的 T 恤", requires_action=True)
        checks = contract.browser_postconditions()
        self.assertEqual(checks["price_min"], 100.0)
        self.assertEqual(checks["price_max"], 150.0)
        self.assertIn("T 恤", checks["contains"])
        self.assertEqual(checks["required_fields"], ["title", "url"])
        self.assertEqual(checks["origin"], "taobao.com")
        restored = TaskContract.from_dict(contract.safe_dict(), contract.goal)
        self.assertEqual(restored.browser_postconditions(), checks)

    def test_windows_in_learning_task_does_not_trigger_window_contract(self):
        contract = TaskContract.from_goal(
            "打开浏览器并查找 Windows PowerShell 7 学习资料",
            requires_action=True,
        )
        self.assertIn("browser_structured_verification", contract.required_evidence_schemas)
        self.assertNotIn("desktop_state_delta", contract.required_evidence_schemas)

    def test_browser_input_restriction_does_not_trigger_desktop_contract(self):
        contract = TaskContract.from_goal(
            "使用浏览器搜索公开网页，禁止输入搜索框，只读取页面结果",
            requires_action=True,
        )
        self.assertIn("browser_structured_verification", contract.required_evidence_schemas)
        self.assertNotIn("desktop_state_delta", contract.required_evidence_schemas)

    def test_browser_contract_uses_requested_source_field_without_forcing_url(self):
        contract = TaskContract.from_goal(
            "浏览器搜索结果，返回标题和来源",
            requires_action=True,
        )
        self.assertEqual(contract.browser_postconditions()["required_fields"], ["title", "source"])

    def test_negative_browser_desktop_fallback_language_does_not_add_desktop_contract(self):
        contract = TaskContract.from_goal(
            "浏览器搜索结果并返回来源，不要使用地址栏或桌面坐标",
            requires_action=True,
        )
        self.assertNotIn("desktop_state_delta", contract.required_evidence_schemas)

    def test_message_task_requires_delivery_evidence_not_just_draft(self):
        contract = TaskContract.from_goal("在 QQ 给李狗日的发送消息", requires_action=True)
        self.assertIn("message_delivery", contract.required_evidence_schemas)
        self.assertNotIn("desktop_state_delta", contract.required_evidence_schemas)
        workflow = TaskWorkflow("T-message", contract.goal, contract=contract)
        workflow.record_tool_result("desktop_uia_set_value", {"ok": True, "verified": True})
        partial = workflow.finish("completed")
        self.assertFalse(partial["verified"])
        self.assertEqual(partial["terminal"], "waiting_verification")
        workflow.record_tool_result("desktop_uia_observe", {
            "ok": True,
            "verification": {"passed": True, "kind": "message_delivery"},
        })
        complete = workflow.finish("completed")
        self.assertTrue(complete["verified"])
        self.assertEqual(workflow.nodes[-1].evidence_schema, "message_delivery")


if __name__ == "__main__":
    unittest.main()
