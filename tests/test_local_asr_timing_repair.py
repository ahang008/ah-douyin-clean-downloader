"""Machine word timing may be repaired locally without editing recognized words."""

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/transcribe_local.py"
SPEC = importlib.util.spec_from_file_location("asr_timing_fixture", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LocalTimingRepairTests(unittest.TestCase):
    def original(self):
        segments = [
            {"start": 2.0, "end": 3.0, "text": "前文"},
            {"start": 3.0, "end": 4.50, "text": "这一种方法"},
            {"start": 4.50, "end": 4.48, "text": "是的"},
            {"start": 4.48, "end": 6.68, "text": "只是他用到了AI上"},
            {"start": 6.68, "end": 8.0, "text": "后文"},
        ]
        return {"text": "".join(item["text"] for item in segments), "segments": segments}

    def window(self):
        return {"text": "这一种方法是的只是他用到了AI上",
                "segments": [{"start": 3.98, "end": 4.40, "text": "这一种方法"},
                             {"start": 4.40, "end": 4.54, "text": "是的"},
                             {"start": 4.54, "end": 6.62, "text": "只是他用到了AI上"}]}

    def test_exact_local_alignment_repairs_only_three_timestamps(self):
        original = self.original()
        repaired, details = MODULE.repair_timestamp_from_window(original, self.window(), 2, 0.0, 10.0)
        self.assertEqual(repaired["text"], original["text"])
        self.assertEqual([row["text"] for row in repaired["segments"]],
                         [row["text"] for row in original["segments"]])
        self.assertEqual(repaired["segments"][2]["start"], 4.40)
        self.assertEqual(repaired["segments"][2]["end"], 4.54)
        self.assertEqual(repaired["segments"][3]["start"], 4.54)
        self.assertEqual(original["segments"][2]["end"], 4.48)
        self.assertFalse(details["machine_words_edited"])

    def test_mismatched_local_words_cannot_be_used_to_force_valid_timecodes(self):
        window = self.window()
        window["segments"][1]["text"] = "其他词"
        with self.assertRaisesRegex(ValueError, "did not reproduce"):
            MODULE.repair_timestamp_from_window(self.original(), window, 2, 0.0, 10.0)

    def test_short_repeated_machine_output_requires_audio_review(self):
        result = {"text": "我看你了" * 20,
                  "segments": [{"start": float(i), "end": float(i + 1), "text": "我看你了"}
                               for i in range(20)]}
        validation = MODULE.validate_result(result, 20.0)
        self.assertTrue(validation["machine_quality_review_required"])
        self.assertIn("short_phrase_repeated_across_clip", validation["quality_flags"])
        normal = {"text": "今天讲第一步接着讲第二步",
                  "segments": [{"start": 0.0, "end": 2.0, "text": "今天讲第一步"},
                               {"start": 2.0, "end": 4.0, "text": "接着讲第二步"}]}
        self.assertFalse(MODULE.validate_result(normal, 5.0)["machine_quality_review_required"])


if __name__ == "__main__":
    unittest.main()
