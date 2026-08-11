import json
import unittest

from worker import CodexWorker


class _Sink:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class _BatchWorker(CodexWorker):
    def __init__(self):
        self.ui = _Sink()
        self.prompts = []

    def _run_isolated_agent_text(self, prompt):
        self.prompts.append(prompt)
        if "\u5404\u5206\u6bb5\u6458\u8981" in prompt:
            return '{"title":"Demo","summary":"merged"}'
        return '{"title":"Demo","summary":"partial"}'


class MeetingMinutesBatchWorkerTests(unittest.TestCase):
    def test_batch_worker_emits_progress_and_one_final_result(self):
        worker = _BatchWorker()
        transcript = "\n".join(f"[00:{index:02d}] speaker: sentence {index}." for index in range(12))

        worker._run_meeting_minutes_batch(transcript, chunk_chars=90, merge_batch=2)

        kinds = [kind for kind, _ in worker.ui.items]
        self.assertIn("meeting_minutes_progress", kinds)
        self.assertEqual(kinds[-2:], ["meeting_minutes_delta", "meeting_minutes_turn_done"])
        final = json.loads(worker.ui.items[-2][1])
        self.assertEqual(final["summary"], "merged")
        self.assertGreater(len(worker.prompts), 2)

    def test_batch_worker_reports_error_without_final_delta(self):
        worker = _BatchWorker()

        def fail(prompt):
            worker.prompts.append(prompt)
            if "\u5404\u5206\u6bb5\u6458\u8981" in prompt:
                raise RuntimeError("provider unavailable")
            return '{"summary":"partial"}'

        worker._run_isolated_agent_text = fail
        transcript = "\n".join(f"[00:{index:02d}] speaker: sentence {index}." for index in range(12))
        worker._run_meeting_minutes_batch(transcript, chunk_chars=90, merge_batch=2)

        kinds = [kind for kind, _ in worker.ui.items]
        self.assertIn("meeting_minutes_error", kinds)
        self.assertEqual(kinds[-1], "meeting_minutes_turn_done")
        self.assertNotIn("meeting_minutes_delta", kinds)


if __name__ == "__main__":
    unittest.main()
