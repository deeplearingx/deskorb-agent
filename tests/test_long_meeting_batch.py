import unittest

from meeting_minutes import (
    MeetingMinutesBatchError,
    summarize_transcript_in_batches,
)


class LongMeetingBatchTests(unittest.TestCase):
    def test_summarizes_chunks_then_merges_structured_results(self):
        transcript = "\n".join(f"[00:{index:02d}] speaker: sentence {index}." for index in range(12))
        prompts = []
        progress = []

        def ask(prompt):
            prompts.append(prompt)
            if "\u5404\u5206\u6bb5\u6458\u8981" in prompt:
                return '{"title":"Demo","summary":"final merge","key_points":["one"]}'
            return '{"title":"Demo","summary":"partial","key_points":["one"]}'

        result = summarize_transcript_in_batches(
            transcript,
            ask,
            chunk_chars=90,
            merge_batch=2,
            on_progress=progress.append,
        )

        self.assertGreater(result.chunk_count, 1)
        self.assertEqual(result.summary_mode, "hierarchical")
        self.assertEqual(result.payload["summary"], "final merge")
        self.assertTrue(any("\u672c\u6bb5\u4f1a\u8bae\u8f6c\u5f55\u6587\u672c" in prompt for prompt in prompts))
        self.assertTrue(any("\u5404\u5206\u6bb5\u6458\u8981" in prompt for prompt in prompts))
        self.assertEqual(progress[0]["stage"], "chunk")
        self.assertEqual(progress[-1]["stage"], "merge")

    def test_merge_failure_exposes_completed_partials_without_final_payload(self):
        transcript = "\n".join(f"[00:{index:02d}] speaker: sentence {index}." for index in range(8))
        calls = []

        def ask(prompt):
            calls.append(prompt)
            if "\u5404\u5206\u6bb5\u6458\u8981" in prompt:
                raise RuntimeError("merge unavailable")
            return '{"summary":"partial"}'

        with self.assertRaises(MeetingMinutesBatchError) as caught:
            summarize_transcript_in_batches(
                transcript,
                ask,
                chunk_chars=90,
                merge_batch=2,
            )

        self.assertEqual(caught.exception.stage, "merge")
        self.assertGreater(len(caught.exception.partials), 1)
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
