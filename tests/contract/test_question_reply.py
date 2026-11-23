"""Protocol samples only; these checks do not replace live platform acceptance."""

import unittest

from robot_agent.adapters.voice_output import VoiceOutput
from robot_agent.domain.models import CandidatePlan, PlanError, TaskView
from robot_agent.protocol.voice_schema import task_frame


def question_reply_view():
    return TaskView(
        "conversation", 3, "REPLIED", "", None, "", False,
        turn_id="question-turn", messages=(
            {"turn_id": "question-turn", "role": "user",
             "message_kind": "user_text", "content": "去桌子"},
            {"turn_id": "question-turn", "role": "assistant",
             "message_kind": "reply", "content": "去哪个桌子？"},
            {"turn_id": "later-turn", "role": "assistant",
             "message_kind": "reply", "content": "这是另一轮的回复。"},
        ),
    )


class QuestionReplyContract(unittest.IsolatedAsyncioTestCase):
    def test_legacy_clarify_is_rejected_even_with_a_valid_question(self):
        with self.assertRaisesRegex(PlanError, "invalid candidate outcome"):
            CandidatePlan.from_dict({
                "outcome": "clarify", "question": "去哪个桌子？", "required_slots": ["location"],
            })

    def test_malformed_question_reply_is_rejected(self):
        bad_fields = ({"text": " "}, {"text": 123}, {"required_slots": ["location"]},
                      {"question": "另一句"}, {"steps": [{"action": "move_to"}]})
        for fields in bad_fields:
            with self.subTest(fields=fields), self.assertRaises(PlanError):
                CandidatePlan.from_dict({"outcome": "reply", "text": "去哪个桌子？", **fields})

    def test_question_done_retains_turn_and_capture_identifiers(self):
        frame = task_frame(question_reply_view(), robot_id="robot",
                           segment_id="segment", capture_id="capture")
        self.assertEqual(frame["type"], "done")
        self.assertEqual(frame["state"], "REPLIED")
        self.assertTrue(frame["turn_finished"])
        self.assertEqual(frame["kind"], "reply")
        self.assertEqual(frame["text"], "去哪个桌子？")
        self.assertEqual(frame["reply"], frame["text"])
        self.assertEqual(frame["turn_id"], "question-turn")
        self.assertEqual(frame["segment_id"], "segment")
        self.assertEqual(frame["capture_id"], "capture")
        self.assertFalse(frame["expects_reply"])
        self.assertIsNone(frame["plan_id"])

    async def test_text_question_finishes_without_speech_playback(self):
        frames = await VoiceOutput(None).render(
            robot_id="robot", transcript="去桌子", view=question_reply_view())
        self.assertEqual([frame["type"] for frame in frames], ["state", "meta", "done"])
        self.assertEqual(frames[-1]["text"], "去哪个桌子？")
        self.assertTrue(frames[-1]["turn_finished"])
        self.assertEqual(frames[-1]["playback"], "none")
        self.assertEqual(frames[-1]["play_secs"], 0)


if __name__ == "__main__":
    unittest.main()
