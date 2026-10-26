"""Structural rejection and fail-closed polishing tests; all API calls stubbed."""
from contextlib import ExitStack
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


PROJECT = Path(__file__).resolve().parents[1]
SCRATCH = (PROJECT / "work" / "polish-tests").resolve()
spec = importlib.util.spec_from_file_location("polish_under_test", PROJECT / "polish.py")
polish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(polish)


def fixtures():
    story = {"title": "例", "first_person_script": "我在上海工作。",
             "events": [{"time": "", "place": "上海", "what": "工作", "emotion": "", "original_quotes": ["我在上海工作。"]}],
             "scenes": [{"no": 1, "narration": "我在上海工作。", "visual": "城市"}], "custom_protected": {"value": 7}}
    transcript = [{"start": 0, "end": 3, "spk": 0, "text": "我在上海工作。"}]
    draft = {"patches": [{"path": "/first_person_script", "value": "我在上海工作。", "reason": "保留原意。", "source_ids": ["S000001"]}],
             "paragraphs": [{"text": "我在上海工作。", "source_ids": ["S000001"]}], "omissions": []}
    review = {"passed": True, "factual_support": True, "speaker_attribution_safe": True, "meaning_preserved": True,
              "not_over_summarized": True, "questions_safe": True,
              "paragraph_reviews": [{"paragraph_index": 0, "supported": True, "source_ids": ["S000001"], "reason": "原句直接支持。"}],
              "issues": [], "legacy_warnings": []}
    return story, transcript, draft, review


class StructuralValidationTests(unittest.TestCase):
    def setUp(self):
        self.story, self.transcript, self.draft, self.review = fixtures()

    def test_interviewer_feedback_locates_excerpt_and_sources(self):
        paragraph = {"text": "所以您是不想给女儿增加负担吗？", "source_ids": ["S000001"]}
        issues = polish._interviewer_issues(paragraph["text"], [paragraph])
        self.assertEqual(issues[0]["paragraph_index"], 0)
        self.assertEqual(issues[0]["excerpt"], paragraph["text"])
        self.assertEqual(issues[0]["source_ids"], ["S000001"])

    def test_quotation_marks_alone_do_not_launder_interviewer_question(self):
        self.assertTrue(polish._interviewer_issues("“所以您是不想给女儿增加负担吗？”"))

    def test_explicit_reported_dialogue_defers_to_semantic_review(self):
        self.assertEqual(polish._interviewer_issues("医生问我：‘您觉得哪里不舒服？’"), [])

    def test_readability_is_a_separate_fail_closed_verdict(self):
        paragraphs = [{"text": "我把那个天走成桌子后来门。", "source_ids": ["S000001"]}]
        issue = {"path": "/first_person_script", "paragraph_index": 0,
                 "excerpt": "我把那个天走成桌子后来门", "reason": "词语拼接，读者无法理解。", "source_ids": ["S000001"]}
        with self.assertRaises(polish.PolishValidationError) as caught:
            polish.validate_readability({"passed": False, "issues": [issue]}, [], paragraphs)
        self.assertEqual(caught.exception.issues[0]["excerpt"], issue["excerpt"])
        self.assertEqual(caught.exception.issues[0]["paragraph_index"], 0)
        for review in ({"passed": True, "issues": [issue]}, {"passed": False, "issues": []}, {}, {"passed": 1, "issues": []}):
            with self.assertRaises(polish.PolishValidationError):
                polish.validate_readability(review, [], paragraphs)
        # Natural dialect and explicitly unclear material are for the model to
        # judge, not a hand-written blacklist or a requirement to rewrite facts.
        acceptable = [{"text": "我呀，就喜欢在家里看看戏。[此处识别不清]", "source_ids": ["S000001"]}]
        self.assertTrue(polish.validate_readability({"passed": True, "issues": []}, [], acceptable)["passed"])

    def test_readability_issues_must_locate_real_candidate_text_and_sources(self):
        fields = [{"path": "/scenes/0/narration", "value": "这句词语拼接。", "source_ids": ["S000001"]}]
        issue = {"path": "/scenes/0/narration", "excerpt": "这句词语拼接", "reason": "无法理解", "source_ids": ["S000001"]}
        for change in ({"path": "/scenes/999/narration"}, {"excerpt": "不存在的原文"}, {"source_ids": ["S999999"]}):
            with self.assertRaises(polish.PolishValidationError):
                polish.validate_readability({"passed": False, "issues": [{**issue, **change}]}, fields, [])

    def test_semantic_context_includes_cross_sentence_subject_without_changing_citations(self):
        sources = polish._source_index([{"start": i, "end": i + 1, "text": text, "spk": i % 2,
                                         "role": "其他说话人2"}
                                        for i, text in enumerate(["之前的话", "你还", "是蛮乐观的", "对话后文", "其他内容"])])
        paragraphs = [{"text": "我还是蛮乐观的。", "source_ids": ["S000003"]}]
        contextual = polish._paragraph_context(paragraphs, sources)
        self.assertEqual(contextual[0]["source_ids"], ["S000003"])
        self.assertEqual([s["id"] for s in contextual[0]["source_context"]], ["S000001", "S000002", "S000003", "S000004", "S000005"])
        self.assertEqual(contextual[0]["source_context"][1]["text"], "你还")
        for source in contextual[0]["source_context"]:
            self.assertEqual(set(source), {"id", "start", "end", "text"})
        self.assertEqual(sources[1]["spk"], 1)
        self.assertEqual(sources[1]["role"], "其他说话人2")

    def test_pro_uses_low_thinking_for_generation_repair_and_both_reviews(self):
        client = Mock()
        client.chat.completions.create.return_value = types.SimpleNamespace(choices=[
            types.SimpleNamespace(finish_reason="stop", message=types.SimpleNamespace(content='{"passed":true}'))])
        for system in (polish.GENERATION_SYSTEM, polish.REPAIR_SYSTEM, polish.READABILITY_SYSTEM, polish.REVIEW_SYSTEM):
            with self.subTest(system=system[:20]):
                self.assertEqual(polish._call_json(client, "deepseek-v4-pro", system, {}), {"passed": True})
                kwargs = client.chat.completions.create.call_args.kwargs
                self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "enabled"}})
                self.assertEqual(kwargs["reasoning_effort"], "low")

    def test_source_units_recover_exact_original_without_model_retyping(self):
        story = {**self.story, "first_person_script": "我在上海工作。呃，那个那个。"}
        units = polish._original_units(story["first_person_script"])
        self.assertEqual("".join(u["text"] for u in units), story["first_person_script"])
        compact = {"patches": [], "paragraphs": self.draft["paragraphs"],
                   "omitted_units": [{"unit_ids": [units[-1]["id"]], "reason": "去除机械重复。", "source_ids": ["S000001"]}]}
        draft = polish._prepare_model_draft(compact, story)
        self.assertEqual(draft["omissions"][0]["original_excerpt"], "呃，那个那个。")
        self.assertEqual(draft["patches"][0]["value"], "我在上海工作。")
        polish.validate_draft(story, self.transcript, draft)
        compact["omitted_units"][0]["unit_ids"] = ["O999999"]
        with self.assertRaises(polish.PolishValidationError):
            polish._prepare_model_draft(compact, story)

    def test_repair_delta_keeps_previously_corrected_fields(self):
        previous = copy.deepcopy(self.draft)
        previous["patches"].append({"path": "/title", "value": "有据的标题", "reason": "纠正原标题", "source_ids": ["S000001"]})
        delta = {"patches": [], "paragraphs": self.draft["paragraphs"], "omitted_units": []}
        merged = polish._prepare_model_draft(delta, self.story, previous)
        checked = polish.validate_draft(self.story, self.transcript, merged)
        self.assertEqual(checked["story"]["title"], "有据的标题")
        self.assertEqual(checked["story"]["first_person_script"], "我在上海工作。")
        self.assertEqual(self.story["title"], "例")
        field_only = {"patches": [{"path": "/title", "value": "进一步纠正", "reason": "只修标题", "source_ids": ["S000001"]}]}
        merged_again = polish._prepare_model_draft(field_only, self.story, merged)
        verified = polish.validate_draft(self.story, self.transcript, merged_again)
        self.assertEqual(verified["story"]["title"], "进一步纠正")
        self.assertEqual(verified["story"]["first_person_script"], "我在上海工作。")

    def check_draft_rejected(self, draft=None, story=None, transcript=None):
        with self.assertRaises(polish.PolishValidationError):
            polish.validate_draft(story if story is not None else self.story,
                                  transcript if transcript is not None else self.transcript,
                                  draft if draft is not None else self.draft)

    def test_valid_structural_draft_preserves_protected_fields_and_input(self):
        before = copy.deepcopy(self.story)
        checked = polish.validate_draft(self.story, self.transcript, self.draft)
        self.assertEqual(self.story, before)
        for key in ("events", "scenes", "custom_protected"):
            self.assertEqual(checked["story"][key], before[key])
        self.assertEqual(checked["paragraphs"][0]["source_ids"], ["S000001"])

    def test_quote_non_narration_scene_unknown_and_new_event_patches_rejected(self):
        for path in ("/events/0/original_quotes/0", "/scenes/0/visual", "/scenes/0/mood", "/scenes/0/no", "/custom_protected/value", "/unknown", "/events/1/what"):
            draft = copy.deepcopy(self.draft)
            draft["patches"].append({"path": path, "value": "改写", "reason": "测试", "source_ids": ["S000001"]})
            with self.subTest(path=path):
                self.check_draft_rejected(draft)

    def test_source_backed_scene_narration_patch_preserves_other_scene_fields(self):
        draft = copy.deepcopy(self.draft)
        draft["patches"].append({"path": "/scenes/0/narration", "value": "我是在上海工作。", "reason": "调整表达，保留原事实。", "source_ids": ["S000001"]})
        checked = polish.validate_draft(self.story, self.transcript, draft)
        self.assertEqual(checked["story"]["scenes"][0]["narration"], "我是在上海工作。")
        self.assertEqual(self.story["scenes"][0]["narration"], "我在上海工作。")
        self.assertEqual(checked["story"]["scenes"][0]["visual"], "城市")
        self.assertEqual(checked["story"]["scenes"][0]["no"], 1)
        self.assertEqual(self.story["scenes"][0]["visual"], "城市")

    def test_missing_duplicate_or_nonexistent_sources_rejected(self):
        for ids in ([], ["S000001", "S000001"], ["S999999"], "S000001"):
            draft = copy.deepcopy(self.draft)
            draft["patches"][0]["source_ids"] = ids
            with self.subTest(ids=ids):
                self.check_draft_rejected(draft)
        draft = copy.deepcopy(self.draft)
        draft["paragraphs"][0]["source_ids"] = []
        self.check_draft_rejected(draft)

    def test_missing_body_duplicate_patch_and_extra_fields_rejected(self):
        variants = []
        draft = copy.deepcopy(self.draft)
        draft["patches"][0]["path"] = "/title"
        variants.append(draft)
        draft = copy.deepcopy(self.draft)
        draft["patches"] *= 2
        variants.append(draft)
        draft = copy.deepcopy(self.draft)
        draft["unauthorized"] = True
        variants.append(draft)
        for draft in variants:
            self.check_draft_rejected(draft)

    def test_missing_or_mismatched_paragraphs_rejected(self):
        for paragraphs in ([], [{"text": "与正文不同。", "source_ids": ["S000001"]}]):
            draft = copy.deepcopy(self.draft)
            draft["paragraphs"] = paragraphs
            self.check_draft_rejected(draft)

    def test_invalid_transcript_timing_rejected(self):
        for start, end in ((-1, 3), (4, 3), (True, 3), (0, float("inf")), (0, "3")):
            transcript = copy.deepcopy(self.transcript)
            transcript[0].update(start=start, end=end)
            with self.subTest(start=start, end=end):
                self.check_draft_rejected(transcript=transcript)

    def test_obvious_interviewer_question_rejected(self):
        draft = copy.deepcopy(self.draft)
        draft["patches"][0]["value"] = "请问您为什么去上海工作？"
        draft["paragraphs"][0]["text"] = draft["patches"][0]["value"]
        self.check_draft_rejected(draft)

    def test_unexplained_large_omission_rejected(self):
        story = copy.deepcopy(self.story)
        story["first_person_script"] = "我在上海工作，后来到乡下帮助家人，靠种地生活，过了许多年才又回到城里住。" * 6
        self.check_draft_rejected(story=story)
        draft = copy.deepcopy(self.draft)
        draft["omissions"] = [{"original_excerpt": "原文没有这段话", "reason": "删减", "source_ids": ["S000001"]}]
        self.check_draft_rejected(draft)

    def test_review_must_explicitly_pass_all_dimensions(self):
        for flag in ("passed", "factual_support", "speaker_attribution_safe", "meaning_preserved", "not_over_summarized", "questions_safe"):
            review = copy.deepcopy(self.review)
            review[flag] = False
            with self.subTest(flag=flag), self.assertRaises(polish.PolishValidationError):
                polish.validate_review(review, 1)

    def test_review_issues_uncovered_or_unsupported_paragraphs_rejected(self):
        variants = []
        review = copy.deepcopy(self.review)
        review["issues"] = [{"reason": "新增未经支持的事实"}]
        variants.append(review)
        review = copy.deepcopy(self.review)
        review["paragraph_reviews"] = []
        variants.append(review)
        review = copy.deepcopy(self.review)
        review["paragraph_reviews"][0]["supported"] = False
        variants.append(review)
        review = copy.deepcopy(self.review)
        review["paragraph_reviews"][0]["paragraph_index"] = True
        variants.append(review)
        for review in variants:
            with self.assertRaises(polish.PolishValidationError):
                polish.validate_review(review, 1)


class OfflinePolishCommitTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=SCRATCH)).resolve()
        self.story, self.transcript, self.draft, self.review = fixtures()
        self.originals = {"story.json": json.dumps(self.story, ensure_ascii=False).encode("utf-8"),
            "story.md": b"original markdown", "transcript.json": json.dumps(self.transcript, ensure_ascii=False).encode("utf-8"),
            "transcript.txt": b"original transcript", "voice/ref_1.wav": b"original audio", "original.m4a": b"original source"}
        for name, data in self.originals.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.root / "manifest.json").write_text('{"source":"original.m4a","story":"story.json"}', encoding="utf-8")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        recorder_stub = types.ModuleType("recorder")
        recorder_stub.load_env_file = Mock()
        recorder_stub.story_to_markdown = lambda story: "# " + story["title"] + "\n\n" + story["first_person_script"]
        openai_stub = types.ModuleType("openai")
        self.client = Mock()
        openai_stub.OpenAI = Mock(return_value=self.client)
        self.stack.enter_context(patch.dict(sys.modules, {"recorder": recorder_stub, "openai": openai_stub}))
        self.stack.enter_context(patch.dict(os.environ, {"LLM_API_KEY": "offline-placeholder-not-a-key", "LLM_BASE_URL": "https://never-used.invalid"}))
        self.call_json = self.stack.enter_context(patch.object(polish, "_call_json"))

    def tearDown(self):
        resolved = self.root.resolve()
        self.assertEqual(resolved.parent, SCRATCH)
        self.assertTrue(resolved.name.startswith("case-"))
        self.assertFalse(self.root.is_symlink())
        shutil.rmtree(resolved)

    def assert_originals_unchanged(self):
        for name, data in self.originals.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)

    def test_success_saves_new_files_with_independent_review_and_keeps_originals(self):
        self.call_json.side_effect = [self.draft, {"passed": True, "issues": []}, self.review]
        result = polish.polish_result(self.root, model="offline-test", max_retries=0)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.call_json.call_count, 3)
        self.assertEqual(self.call_json.call_args_list[1].args[2], polish.READABILITY_SYSTEM)
        readability_payload = self.call_json.call_args_list[1].args[3]
        self.assertEqual(set(readability_payload), {"candidate_fields", "paragraphs"})
        self.assertNotIn("source_context", readability_payload["paragraphs"][0])
        self.assert_originals_unchanged()
        for name in ("story_polished.json", "story_polished.md", "polish_changes.json"):
            self.assertTrue((self.root / name).is_file(), name)
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source"], "original.m4a")
        self.assertEqual(manifest["polish"]["status"], "completed")
        self.assertTrue(manifest["polish"]["readability_review_passed"])
        changes = json.loads((self.root / "polish_changes.json").read_text(encoding="utf-8"))
        self.assertEqual(changes["readability_review"], {"passed": True, "issues": []})
        self.assertIn(polish.DISCLAIMER, (self.root / "story_polished.md").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((self.root / "story_polished.json").read_text(encoding="utf-8"))["polish_metadata"]["disclaimer"], polish.DISCLAIMER)

    def test_semantic_failure_repairs_once_then_fails_without_publishing(self):
        review = copy.deepcopy(self.review)
        review["speaker_attribution_safe"] = False
        review["issues"] = [{"reason": "把采访者经历归给老人。"}]
        self.call_json.side_effect = [self.draft, {"passed": True, "issues": []}, review,
                                     self.draft, {"passed": True, "issues": []}, review]
        result = polish.polish_result(self.root, max_retries=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(self.call_json.call_count, 6)
        self.assert_originals_unchanged()
        self.assertFalse((self.root / "story_polished.json").exists())
        self.assertFalse((self.root / "story_polished.md").exists())

    def test_all_model_source_payloads_omit_diarization_but_audit_and_input_keep_it(self):
        self.transcript[0].update(spk=2, role="其他说话人2")
        original = json.dumps(self.transcript, ensure_ascii=False).encode("utf-8")
        self.originals["transcript.json"] = original
        (self.root / "transcript.json").write_bytes(original)
        rejected = copy.deepcopy(self.review)
        rejected["passed"] = False
        rejected["speaker_attribution_safe"] = False
        rejected["issues"] = [{"path": "/first_person_script", "source_ids": ["S000001"],
                               "reason": "模拟需要结合上下文修复的归属问题"}]
        self.call_json.side_effect = [self.draft, {"passed": True, "issues": []}, rejected,
                                     {"patches": []}, {"passed": True, "issues": []}, self.review]
        result = polish.polish_result(self.root, max_retries=1)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.call_json.call_count, 6)
        surfaces = set()

        def check_source_payloads(value):
            if isinstance(value, dict):
                for name, child in value.items():
                    if name in ("source_sentences", "source_context", "focused_sources"):
                        surfaces.add(name)
                        self.assertTrue(child)
                        for source in child:
                            self.assertEqual(set(source), {"id", "start", "end", "text"})
                            self.assertEqual(source["text"], self.transcript[0]["text"])
                    check_source_payloads(child)
            elif isinstance(value, list):
                for child in value:
                    check_source_payloads(child)

        for call in self.call_json.call_args_list:
            check_source_payloads(call.args[3])
        self.assertEqual(surfaces, {"source_sentences", "source_context", "focused_sources"})
        changes = json.loads((self.root / "polish_changes.json").read_text(encoding="utf-8"))
        self.assertEqual(changes["source_index"][0]["spk"], 2)
        self.assertEqual(changes["source_index"][0]["role"], "其他说话人2")
        self.assert_originals_unchanged()

    def test_failed_rerun_keeps_previously_published_files(self):
        old = {name: b"previous accepted version" for name in ("story_polished.json", "story_polished.md", "polish_changes.json")}
        for name, data in old.items():
            (self.root / name).write_bytes(data)
        self.call_json.side_effect = RuntimeError("simulated SDK failure with fake sk-do-not-store-this")
        result = polish.polish_result(self.root, max_retries=0)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("sk-do-not-store-this", json.dumps(result))
        self.assertNotIn("sk-do-not-store-this", (self.root / "manifest.json").read_text(encoding="utf-8"))
        for name, data in old.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assert_originals_unchanged()

    def test_http_402_reports_insufficient_balance_without_sdk_details(self):
        class APIStatusError(RuntimeError):
            status_code = 402
        self.call_json.side_effect = APIStatusError("private SDK request sk-do-not-store-this")
        progress = []
        result = polish.polish_result(self.root, max_retries=1, progress=progress.append)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "insufficient_balance")
        self.assertEqual(result["http_status"], 402)
        self.assertIn("DeepSeek 余额不足", result["message"])
        self.assertIn("原始处理已完成", result["message"])
        self.assertIn("文稿整理未完成", result["message"])
        self.assertFalse(result["semantic_review_passed"])
        self.assertEqual(self.call_json.call_count, 1)
        self.assertEqual(result["diagnostic_files"], [])
        for exposed in (json.dumps(result), "".join(progress), (self.root / "manifest.json").read_text(encoding="utf-8")):
            self.assertNotIn("sk-do-not-store-this", exposed)
            self.assertNotIn("private SDK request", exposed)
        self.assert_originals_unchanged()

    def test_api_failure_keeps_candidate_and_readability_for_resume_only(self):
        class APIStatusError(RuntimeError):
            status_code = 503
        readable = {"passed": True, "issues": []}
        self.call_json.side_effect = [self.draft, readable, APIStatusError("private SDK request sk-do-not-store-this")]
        with patch.object(polish, "_save_diagnostic", wraps=polish._save_diagnostic) as save_diagnostic:
            result = polish.polish_result(self.root, max_retries=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "processing_failed")
        self.assertEqual(result["http_status"], 503)
        self.assertEqual(self.call_json.call_count, 3)
        self.assertEqual(save_diagnostic.call_count, 1)
        self.assertEqual(len(result["diagnostic_files"]), 1)
        diagnostic_text = (self.root / result["diagnostic_files"][0]).read_text(encoding="utf-8")
        diagnostic = json.loads(diagnostic_text)
        self.assertEqual(diagnostic["status"], "rejected_not_for_delivery")
        self.assertEqual(diagnostic["candidate_draft"]["paragraphs"], self.draft["paragraphs"])
        self.assertEqual(diagnostic["readability_review"], readable)
        self.assertIsNone(diagnostic["semantic_review"])
        self.assertNotIn("private SDK request", diagnostic_text)
        self.assertNotIn("sk-do-not-store-this", diagnostic_text)
        self.assertFalse((self.root / "story_polished.json").exists())
        self.assertFalse(result["semantic_review_passed"])
        self.assertEqual(result["output_files"], [])
        self.call_json.reset_mock()
        self.call_json.side_effect = [{"patches": []}, readable, self.review]
        resumed = polish.polish_result(self.root, max_retries=0)
        self.assertEqual(resumed["status"], "completed", resumed)
        self.assertEqual(self.call_json.call_args_list[0].args[3]["previous_draft"], diagnostic["candidate_draft"])
        self.assertEqual(self.call_json.call_count, 3)
        self.assert_originals_unchanged()

    def test_review_cannot_cite_nonexistent_source(self):
        review = copy.deepcopy(self.review)
        review["paragraph_reviews"][0]["source_ids"] = ["S999999"]
        self.call_json.side_effect = [self.draft, {"passed": True, "issues": []}, review]
        result = polish.polish_result(self.root, max_retries=0)
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.root / "story_polished.json").exists())
        self.assert_originals_unchanged()

    def test_unreadable_model_verdict_blocks_semantic_review_and_is_saved_as_diagnostic(self):
        issue = {"path": "/first_person_script", "paragraph_index": 0, "excerpt": "我在上海工作",
                 "reason": "模拟独立模型检测出的可读性问题", "source_ids": ["S000001"]}
        self.call_json.side_effect = [self.draft, {"passed": False, "issues": [issue]}]
        result = polish.polish_result(self.root, max_retries=0)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["readability_review_passed"])
        self.assertEqual(self.call_json.call_count, 2)
        self.assertNotIn(polish.REVIEW_SYSTEM, [call.args[2] for call in self.call_json.call_args_list])
        self.assertFalse((self.root / "story_polished.json").exists())
        diagnostic = json.loads((self.root / result["diagnostic_files"][0]).read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["status"], "rejected_not_for_delivery")
        self.assertFalse(diagnostic["readability_review"]["passed"])
        self.assertTrue(any(item.get("excerpt") == issue["excerpt"] for item in diagnostic["validation_issues"]))
        self.assert_originals_unchanged()

    def test_readability_failure_uses_same_single_repair_budget(self):
        issue = {"path": "/first_person_script", "paragraph_index": 0, "excerpt": "我在上海工作",
                 "reason": "模拟可读性问题", "source_ids": ["S000001"]}
        bad = {"passed": False, "issues": [issue]}
        self.call_json.side_effect = [self.draft, bad, {"patches": []}, {"passed": True, "issues": []}, self.review]
        result = polish.polish_result(self.root, max_retries=1)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(self.call_json.call_count, 5)
        repair_payload = self.call_json.call_args_list[2].args[3]
        self.assertTrue(any(item.get("excerpt") == issue["excerpt"] for item in repair_payload["repair_issues"]))

    def test_completed_draft_reused_with_corrected_fields_and_fresh_reviews(self):
        prior = copy.deepcopy(self.draft)
        prior["patches"].append({"path": "/title", "value": "已经修正的标题", "reason": "原文支持", "source_ids": ["S000001"]})
        self.call_json.side_effect = [prior, {"passed": True, "issues": []}, self.review]
        initial = polish.polish_result(self.root, max_retries=0)
        self.assertEqual(initial["status"], "completed", initial)
        self.call_json.reset_mock()
        self.call_json.side_effect = [{"patches": []}, {"passed": True, "issues": []}, self.review]
        result = polish.polish_result(self.root, max_retries=0)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["reused_completed_draft"])
        self.assertEqual(self.call_json.call_count, 3)
        self.assertEqual(self.call_json.call_args_list[0].args[2], polish.REPAIR_SYSTEM)
        self.assertIn("previous_draft", self.call_json.call_args_list[0].args[3])
        output = json.loads((self.root / "story_polished.json").read_text(encoding="utf-8"))
        self.assertEqual(output["title"], "已经修正的标题")
        self.assert_originals_unchanged()

    def test_completed_draft_not_reused_when_input_or_output_hash_differs(self):
        self.call_json.side_effect = [self.draft, {"passed": True, "issues": []}, self.review]
        previous = polish.polish_result(self.root, max_retries=0)
        snapshot = polish._input_snapshot(self.root)
        self.assertIsNotNone(polish._completed_candidate(self.root, previous, snapshot, self.story, self.transcript))
        changed_snapshot = copy.deepcopy(snapshot)
        changed_snapshot["story.json"]["sha256"] = "not-matching"
        self.assertIsNone(polish._completed_candidate(self.root, previous, changed_snapshot, self.story, self.transcript))
        (self.root / "story_polished.md").write_text("changed outside workflow", encoding="utf-8")
        self.assertIsNone(polish._completed_candidate(self.root, previous, snapshot, self.story, self.transcript))


if __name__ == "__main__":
    unittest.main()
