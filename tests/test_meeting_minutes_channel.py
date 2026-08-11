import unittest

from worker import _EventChannel


class _Queue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class MeetingMinutesEventChannelTests(unittest.TestCase):
    def test_prefixes_worker_events_without_leaking_into_chat(self):
        queue = _Queue()
        channel = _EventChannel(queue, "meeting_minutes")

        channel.put(("delta", "摘要"))
        channel.put(("turn_done", None))

        self.assertEqual(queue.items, [
            ("meeting_minutes_delta", "摘要"),
            ("meeting_minutes_turn_done", None),
        ])


if __name__ == "__main__":
    unittest.main()
