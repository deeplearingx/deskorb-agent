import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from meeting_minutes import parse_minutes_response
from meeting_recording import parse_whisperx_json


class ChinesePostprocessTests(unittest.TestCase):
    def test_whisperx_transcript_and_json_are_converted_to_simplified(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "meeting.json"
            path.write_text(json.dumps({
                "segments": [{
                    "start": 0,
                    "text": "產品評審會議",
                    "words": [{"word": "測試"}],
                }]
            }, ensure_ascii=False), encoding="utf-8")

            transcript = parse_whisperx_json(path)
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(transcript, "[00:00] Speaker: 产品评审会议")
        self.assertEqual(payload["segments"][0]["text"], "产品评审会议")
        self.assertEqual(payload["segments"][0]["words"][0]["word"], "测试")

    def test_agent_minutes_fields_are_converted_to_simplified(self):
        response = json.dumps({
            "title": "產品評審會議紀要",
            "summary": "會議確認測試報告安排",
            "key_points": ["評審結束"],
            "decisions": ["進入測試階段"],
            "action_items": [{"task": "提交測試報告", "owner": "小王"}],
            "questions": ["尚未確認事項"],
        }, ensure_ascii=False)

        payload = parse_minutes_response(response)

        self.assertEqual(payload["title"], "产品评审会议纪要")
        self.assertEqual(payload["summary"], "会议确认测试报告安排")
        self.assertEqual(payload["key_points"], ["评审结束"])
        self.assertEqual(payload["decisions"], ["进入测试阶段"])
        self.assertEqual(payload["action_items"][0]["task"], "提交测试报告")
        self.assertEqual(payload["action_items"][0]["owner"], "小王")
        self.assertEqual(payload["questions"], ["尚未确认事项"])


if __name__ == "__main__":
    unittest.main()
