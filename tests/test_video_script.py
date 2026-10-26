"""Video-maker handoff built from saved results; no model, audio or network access."""
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
SCRATCH = (PROJECT / "work" / "video-script-tests").resolve()
spec = importlib.util.spec_from_file_location("video_script_under_test", PROJECT / "video_script.py")
video_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(video_script)

TRANSCRIPT = [
    {"spk": 0, "start": 0.0, "end": 2.0, "text": "您小时候住哪儿？"},
    {"spk": 1, "start": 2.5, "end": 9.4, "text": "我小时候住在村里，"},
    {"spk": 1, "start": 9.5, "end": 14.0, "text": "家门口有棵大槐树。"},
]
STORY = {
    "title": "村口的大槐树", "summary": "讲述童年在村里的生活。",
    "narrator": {"称呼": "王奶奶", "性别": "女", "大致年龄": "", "籍贯或口音": ""},
    "time_period": "1950年代", "locations": ["村里"], "people": [{"name": "母亲", "relation": "母亲"}],
    "first_person_script": "我小时候住在村里。家门口有棵大槐树。",
    "events": [{"order": 1, "what": "童年住在村里", "original_quotes": ["我小时候住在村里，家门口有棵大槐树。", "这句没说过"]}],
    "scenes": [{"no": 1, "visual": "土路与槐树", "narration": "我小时候住在村里。", "mood": "怀念"},
               {"no": 2, "visual": "院门口", "narration": "家门口有棵大槐树。", "mood": "温暖"}],
    "uncertain": ["“槐树”可能是“柳树”"],
}


class VideoScriptTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.out = Path(tempfile.mkdtemp(prefix="case-", dir=SCRATCH)).resolve()
        self.assertEqual(self.out.parent, SCRATCH)
        self.write("story.json", STORY)
        self.write("transcript.json", TRANSCRIPT)
        (self.out / "original.m4a").write_bytes(b"unchanged audio")

    def tearDown(self):
        resolved = self.out.resolve()
        self.assertEqual(resolved.parent, SCRATCH)
        self.assertTrue(resolved.name.startswith("case-"))
        shutil.rmtree(resolved)

    def write(self, name, value):
        data = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        (self.out / name).write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def polished(self, narration="我小时候在村里长大。"):
        story = json.loads(json.dumps(STORY))
        story["first_person_script"] = "我小时候在村里长大。家门口有棵大槐树。"
        story["scenes"][0]["narration"] = narration
        return story

    def built(self):
        return json.loads((self.out / "video_script.json").read_text(encoding="utf-8"))

    def test_original_is_used_without_completed_polish_and_inputs_stay_unchanged(self):
        before = {p.name: p.read_bytes() for p in self.out.iterdir()}
        self.assertEqual(video_script.write_video_script(self.out), "original")
        for name, data in before.items():
            self.assertEqual((self.out / name).read_bytes(), data)
        script = self.built()
        self.assertEqual([s["narration"] for s in script["shots"]], ["我小时候住在村里。", "家门口有棵大槐树。"])
        markdown = (self.out / "video_script.md").read_text(encoding="utf-8")
        self.assertIn("依据：原稿", markdown)
        self.assertIn("土路与槐树", markdown)
        self.assertIn("制作前请确认", markdown)
        self.assertIn("王奶奶", markdown)

    def test_completed_polish_with_matching_hash_is_used(self):
        digest = self.write("story_polished.json", self.polished())
        self.write("manifest.json", {"polish": {"status": "completed", "output_hashes": {"story_polished.json": digest}}})
        self.assertEqual(video_script.write_video_script(self.out), "polished")
        self.assertEqual(self.built()["shots"][0]["narration"], "我小时候在村里长大。")
        self.assertIn("依据：整理稿", (self.out / "video_script.md").read_text(encoding="utf-8"))

    def test_tampered_or_failed_polish_falls_back_to_original(self):
        self.write("story_polished.json", self.polished())
        for polish in ({"status": "completed", "output_hashes": {"story_polished.json": "0" * 64}},
                       {"status": "failed"}, {"status": "running"}):
            with self.subTest(status=polish["status"]):
                self.write("manifest.json", {"polish": polish})
                self.assertEqual(video_script.write_video_script(self.out), "original")

    def test_failed_repolish_keeps_earlier_accepted_polished_copy(self):
        digest = self.write("story_polished.json", self.polished())
        previous = {"status": "completed", "output_hashes": {"story_polished.json": digest}}
        self.write("manifest.json", {"polish": {"status": "failed", "previous_completed": previous}})
        self.assertEqual(video_script.write_video_script(self.out), "polished")

    def test_quotes_are_located_in_the_recording_across_sentences(self):
        video_script.write_video_script(self.out)
        clips = self.built()["original_voice_clips"]
        self.assertEqual((clips[0]["start"], clips[0]["end"]), ("00:02", "00:14"))
        self.assertIsNone(clips[1]["start"])
        markdown = (self.out / "video_script.md").read_text(encoding="utf-8")
        self.assertIn("原录音（original.m4a）", markdown)
        self.assertIn("需人工查找", markdown)

    def test_shot_timing_is_cumulative_and_csv_opens_in_excel(self):
        video_script.write_video_script(self.out)
        shots = self.built()["shots"]
        self.assertEqual(shots[0]["start"], "00:00")
        self.assertEqual(shots[1]["start"], shots[0]["end"])
        self.assertGreaterEqual(shots[0]["seconds"], video_script.MIN_SHOT_SECONDS)
        raw = (self.out / "video_script.csv").read_bytes()
        self.assertTrue(raw.startswith("﻿".encode("utf-8")))
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertEqual(rows[0][:4], ["镜头", "开始", "结束", "时长(秒)"])
        self.assertEqual(rows[2][5], "家门口有棵大槐树。")

    def test_story_without_scenes_gets_one_shot_per_paragraph(self):
        story = dict(STORY, scenes=[], first_person_script="第一段。\n\n第二段。")
        self.write("story.json", story)
        video_script.write_video_script(self.out)
        self.assertEqual([s["narration"] for s in self.built()["shots"]], ["第一段。", "第二段。"])
        self.assertIn("按旁白自行配画面", (self.out / "video_script.md").read_text(encoding="utf-8"))

    def test_missing_story_writes_nothing(self):
        (self.out / "story.json").unlink()
        self.assertIsNone(video_script.write_video_script(self.out))
        self.assertIsNone(video_script.refresh_video_script(self.out))
        self.assertFalse((self.out / "video_script.md").exists())

    def test_refresh_rebuilds_only_when_sources_are_newer(self):
        self.assertEqual(video_script.refresh_video_script(self.out), "original")
        self.assertEqual(video_script.refresh_video_script(self.out), "current")
        digest = self.write("story_polished.json", self.polished())
        self.write("manifest.json", {"polish": {"status": "completed", "output_hashes": {"story_polished.json": digest}}})
        future = (self.out / "video_script.md").stat().st_mtime_ns + 10_000_000_000
        os.utime(self.out / "manifest.json", ns=(future, future))
        self.assertEqual(video_script.refresh_video_script(self.out), "polished")


if __name__ == "__main__":
    unittest.main()
