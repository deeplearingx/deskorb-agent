import json
import unittest

from meeting_minutes import (
    build_merge_minutes_prompt,
    build_minutes_chunk_prompt,
    merge_minutes_payloads,
    split_transcript,
)


class LongMeetingMinutesTests(unittest.TestCase):
    def test_split_transcript_keeps_sentence_boundaries_and_bounded_parts(self):
        source = "\n".join(f"[00:{i:02d}] A：第{i}句。" for i in range(12))

        parts = split_transcript(source, max_chars=80)

        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 80 for part in parts))
        self.assertIn("第0句", parts[0])
        self.assertIn("第11句", parts[-1])
        self.assertTrue(all(not part.endswith("第") for part in parts))

    def test_split_transcript_does_not_split_short_transcript(self):
        source = "[00:00] 张三：周五发布。"

        self.assertEqual(split_transcript(source, max_chars=80), [source])

    def test_chunk_prompt_has_fixed_markdown_contract(self):
        prompt = build_minutes_chunk_prompt(
            "[00:00] SPEAKER_00: 周四提测", index=2, total=4
        )

        self.assertIn("第 2/4 段", prompt)
        self.assertIn("周四提测", prompt)
        self.assertIn("## 1. 基础信息", prompt)
        self.assertIn("只输出会议纪要正文", prompt)
        self.assertNotIn('"action_items"', prompt)
    def test_merge_prompt_contains_only_structured_partials(self):
        prompt = build_merge_minutes_prompt([
            {"title": "产品评审", "summary": "摘要一", "key_points": []},
            {"title": "产品评审", "summary": "摘要二", "key_points": []},
        ])

        self.assertIn("摘要一", prompt)
        self.assertIn("摘要二", prompt)
        self.assertNotIn("完整会议逐字稿", prompt)
        self.assertIn('"summary"', prompt)

    def test_merge_minutes_payloads_deduplicates_items(self):
        result = merge_minutes_payloads([
            {
                "title": "产品评审",
                "summary": "确定方向",
                "key_points": ["登录流程", "登录流程"],
                "decisions": ["周五发布"],
                "action_items": [{"task": "补充测试", "owner": "小王"}],
                "questions": ["是否灰度"],
            },
            {
                "title": "产品评审",
                "summary": "确认排期",
                "key_points": ["接口联调"],
                "decisions": ["周五发布"],
                "action_items": [{"task": "补充测试", "owner": "小王"}],
                "questions": ["是否灰度"],
            },
        ])

        self.assertEqual(result["title"], "产品评审")
        self.assertEqual(result["key_points"], ["登录流程", "接口联调"])
        self.assertEqual(result["decisions"], ["周五发布"])
        self.assertEqual(len(result["action_items"]), 1)
        self.assertEqual(result["questions"], ["是否灰度"])



    def test_merge_minutes_payloads_preserves_markdown_fields(self):
        result = merge_minutes_payloads([
            {
                "title": "Demo",
                "summary": "Summary",
                "speakers": ["SPEAKER_00"],
                "topics": ["topic"],
                "open_items": ["pending"],
                "risks": ["risk"],
            },
        ])

        self.assertEqual(result["speakers"], ["SPEAKER_00"])
        self.assertEqual(result["topics"], ["topic"])
        self.assertEqual(result["open_items"], ["pending"])
        self.assertEqual(result["risks"], ["risk"])

if __name__ == "__main__":
    unittest.main()
