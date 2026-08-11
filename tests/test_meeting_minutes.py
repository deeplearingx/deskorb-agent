import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from meeting_minutes import (
    build_minutes_prompt,
    parse_minutes_response,
    save_minutes,
)


class MeetingMinutesTests(unittest.TestCase):
    def test_parses_fenced_json_from_agent_response(self):
        response = """```json
{"title":"产品评审","summary":"确定周五发布","key_points":["完成登录流程"],"decisions":["周五发布"],"action_items":[{"task":"补充测试","owner":"小王","deadline":"周四"}],"questions":["是否需要灰度"]}
```"""

        result = parse_minutes_response(response)

        self.assertEqual(result["title"], "产品评审")
        self.assertEqual(result["decisions"], ["周五发布"])
        self.assertEqual(result["action_items"][0]["owner"], "小王")

    def test_falls_back_to_plain_text_when_agent_does_not_return_json(self):
        result = parse_minutes_response("先完成接口联调，再安排上线。")

        self.assertEqual(result["title"], "会议纪要")
        self.assertEqual(result["summary"], "先完成接口联调，再安排上线。")
        self.assertEqual(result["key_points"], [])

    def test_prompt_contains_fixed_markdown_contract(self):
        prompt = build_minutes_prompt("[00:00] SPEAKER_00: 周四提测")
        self.assertIn("# 会议纪要", prompt)
        for heading in (
            "## 1. 基础信息",
            "## 2. 议题研讨与核心共识",
            "## 3. 行动待办清单",
            "## 4. 悬置待确认事项",
            "## 5. 风险、卡点与补充说明",
        ):
            self.assertIn(heading, prompt)
        self.assertIn("只输出会议纪要正文", prompt)
        self.assertNotIn('"action_items"', prompt)

    def test_parses_fixed_markdown_response(self):
        response = """# 会议纪要
## 1. 基础信息
- 会议主题：后端接口进度同步
- 参会发言人：SPEAKER_00、SPEAKER_01
- 会议简述：同步接口进度并确认提测时间。
## 2. 议题研讨与核心共识
- 后端接口基本完成，需优化并发查询。
## 3. 行动待办清单（核心模块）
【SPEAKER_01】｜完成单元测试｜周四下午｜接口稳定
## 4. 悬置待确认事项
- 并发查询优化方案待确认。
## 5. 风险、卡点与补充说明
- 服务显存使用需要控制。"""
        result = parse_minutes_response(response)

        self.assertEqual(result["title"], "后端接口进度同步")
        self.assertEqual(result["speakers"], ["SPEAKER_00", "SPEAKER_01"])
        self.assertEqual(result["meeting_brief"], "同步接口进度并确认提测时间。")
        self.assertIn("后端接口基本完成", result["topics"][0])
        self.assertEqual(result["action_items"][0]["owner"], "SPEAKER_01")
        self.assertEqual(result["action_items"][0]["notes"], "接口稳定")
        self.assertIn("并发查询优化方案", result["questions"][0])
        self.assertIn("显存", result["risks"][0])
        self.assertTrue(result["raw_markdown"].startswith("# 会议纪要"))
    def test_save_minutes_writes_markdown_and_json(self):
        with TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "meeting-001.txt"
            transcript.write_text("[00:00] 会议开始", encoding="utf-8")
            result = save_minutes(
                Path(tmp),
                "meeting-001",
                parse_minutes_response('{"title":"例会","summary":"同步进度"}'),
                transcript_path=transcript,
            )

            self.assertTrue(result.markdown_path.is_file())
            self.assertTrue(result.json_path.is_file())
            markdown = result.markdown_path.read_text(encoding="utf-8")
            self.assertIn("例会", markdown)
            headings = [
                "# 会议纪要",
                "## 1. 基础信息",
                "## 2. 议题研讨与核心共识",
                "## 3. 行动待办清单（核心模块）",
                "## 4. 悬置待确认事项",
                "## 5. 风险、卡点与补充说明",
            ]
            positions = [markdown.index(item) for item in headings]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("- 会议主题：例会", markdown)
            self.assertIn("- 会议简述：同步进度", markdown)
            self.assertIn("无", markdown)
            payload = json.loads(result.json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"], "同步进度")


if __name__ == "__main__":
    unittest.main()
