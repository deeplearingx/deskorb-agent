import unittest

from task_runtime import ExecutionDeadline


class ExecutionDeadlineTests(unittest.TestCase):
    def test_deadline_limits_each_phase_to_remaining_budget(self):
        now = [100.0]
        deadline = ExecutionDeadline(10.0, clock=lambda: now[0])

        self.assertAlmostEqual(deadline.remaining(), 10.0)
        self.assertAlmostEqual(deadline.timeout_for(30.0, phase="provider_total"), 10.0)

        now[0] = 104.25
        self.assertAlmostEqual(deadline.timeout_for(3.0, phase="provider_first_response"), 3.0)
        self.assertEqual(deadline.current_phase, "provider_first_response")

        now[0] = 110.0
        self.assertTrue(deadline.expired())
        self.assertEqual(deadline.timeout_for(3.0, phase="tool_execution"), 0.0)

    def test_snapshot_contains_only_structural_timing_data(self):
        now = [10.0]
        deadline = ExecutionDeadline(20.0, clock=lambda: now[0])
        deadline.timeout_for(5.0, phase="provider_connect")
        now[0] = 12.0
        deadline.timeout_for(5.0, phase="provider_first_response")

        snapshot = deadline.snapshot()
        self.assertEqual(snapshot["total_budget_ms"], 20_000)
        self.assertEqual(snapshot["current_phase"], "provider_first_response")
        self.assertEqual([item["phase"] for item in snapshot["phases"]],
                         ["provider_connect", "provider_first_response"])
        self.assertNotIn("prompt", str(snapshot))
        self.assertNotIn("url", str(snapshot))


if __name__ == "__main__":
    unittest.main()
