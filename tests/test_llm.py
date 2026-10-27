"""Offline checks for choosing between DeepSeek and the local Codex CLI. Nothing is called for real."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
import llm  # noqa: E402

spec = importlib.util.spec_from_file_location("recorder_for_llm_tests", PROJECT / "recorder.py")
recorder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recorder)

STORY = {"title": "童年回忆", "summary": "讲述童年生活。", "first_person_script": "我小时候住在村里。",
         "events": [], "scenes": [], "uncertain": []}


class FakeCodex:
    """Stands in for `codex exec`: records the call and writes the final message file."""

    def __init__(self, answer, returncode=0, stderr=""):
        self.answer, self.returncode, self.stderr = answer, returncode, stderr
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.answer is not None:
            target = Path(command[command.index("--output-last-message") + 1])
            target.write_text(self.answer, encoding="utf-8")
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


class BackendChoiceTests(unittest.TestCase):
    def test_deepseek_is_the_default_and_unknown_backends_are_rejected(self):
        with patch.dict(os.environ, {"LLM_BACKEND": ""}):
            self.assertEqual(llm.backend(), "deepseek")
            self.assertEqual(llm.model_name(), os.environ.get("LLM_MODEL", "deepseek-v4-pro"))
        with patch.dict(os.environ, {"LLM_BACKEND": " Codex "}):
            self.assertEqual(llm.backend(), "codex")
        with patch.dict(os.environ, {"LLM_BACKEND": "gpt"}), self.assertRaises(llm.LLMBackendError):
            llm.backend()

    def test_codex_model_name_defaults_to_codex_and_honours_codex_model(self):
        with patch.dict(os.environ, {"LLM_BACKEND": "codex", "CODEX_MODEL": ""}):
            self.assertEqual(llm.model_name(), "codex")
        with patch.dict(os.environ, {"LLM_BACKEND": "codex", "CODEX_MODEL": "gpt-5-codex"}):
            self.assertEqual(llm.model_name(), "gpt-5-codex")

    def test_deepseek_client_keeps_bounded_sdk_settings(self):
        module = types.ModuleType("openai")
        module.OpenAI = Mock()
        with patch.dict(sys.modules, {"openai": module}), patch.dict(os.environ, {"LLM_BACKEND": "deepseek", "LLM_API_KEY": "placeholder", "LLM_BASE_URL": ""}):
            os.environ.pop("LLM_BASE_URL")
            llm.create_client()
        kwargs = module.OpenAI.call_args.kwargs
        self.assertEqual((kwargs["base_url"], kwargs["timeout"], kwargs["max_retries"]), ("https://api.deepseek.com", 180.0, 0))


class CodexClientTests(unittest.TestCase):
    def run_codex(self, fake, model="codex"):
        with patch.dict(os.environ, {"LLM_BACKEND": "codex"}), \
                patch.object(llm, "_codex_command", return_value="codex"), \
                patch.object(llm.subprocess, "run", side_effect=fake):
            client = llm.create_client()
            return client.chat.completions.create(
                model=model, messages=[{"role": "system", "content": "整理成 JSON"},
                                       {"role": "user", "content": "转写：我小时候住在村里。"}],
                response_format={"type": "json_object"}, temperature=0, extra_body={"thinking": {}})

    def test_prompt_goes_to_stdin_of_a_read_only_non_interactive_run(self):
        fake = FakeCodex(json.dumps(STORY, ensure_ascii=False))
        response = self.run_codex(fake)
        self.assertEqual(response.choices[0].finish_reason, "stop")
        self.assertEqual(json.loads(response.choices[0].message.content), STORY)
        command, kwargs = fake.calls[0]
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", command)
        self.assertEqual(command[-1], "-")
        self.assertNotIn("-m", command)
        self.assertIn("整理成 JSON", kwargs["input"])
        self.assertIn("我小时候住在村里", kwargs["input"])
        self.assertIs(kwargs["shell"], False)
        self.assertFalse(Path(kwargs["cwd"]).exists(), "the scratch directory is removed afterwards")

    def test_fenced_or_chatty_answers_are_reduced_to_the_json_object(self):
        for answer in ("```json\n" + json.dumps(STORY) + "\n```", "好的，结果如下：\n" + json.dumps(STORY) + "\n完成。"):
            with self.subTest(answer=answer[:12]):
                response = self.run_codex(FakeCodex(answer))
                self.assertEqual(json.loads(response.choices[0].message.content), STORY)

    def test_explicit_codex_model_is_passed_through(self):
        fake = FakeCodex("{}")
        self.run_codex(fake, model="gpt-5-codex")
        command = fake.calls[0][0]
        self.assertEqual(command[command.index("-m") + 1], "gpt-5-codex")

    def test_failures_become_safe_backend_errors(self):
        fake = FakeCodex(None, returncode=1, stderr="secret prompt text\nError: not logged in")
        with self.assertRaises(llm.LLMBackendError) as caught:
            self.run_codex(fake)
        self.assertIn("codex login", str(caught.exception))
        self.assertNotIn("secret prompt", str(caught.exception))
        timeout = Mock(side_effect=subprocess.TimeoutExpired("codex", 1))
        with self.assertRaises(llm.LLMBackendError):
            self.run_codex(timeout)
        response = self.run_codex(FakeCodex(""))
        self.assertEqual(response.choices[0].finish_reason, "error")

    def test_missing_codex_cli_is_reported(self):
        with patch.dict(os.environ, {"CODEX_PATH": ""}), patch.object(llm.shutil, "which", return_value=None), \
                self.assertRaises(llm.LLMBackendError) as caught:
            llm._codex_command()
        self.assertIn("CODEX_PATH", str(caught.exception))

    def test_story_extraction_runs_through_codex_without_an_api_key(self):
        fake = FakeCodex(json.dumps(STORY, ensure_ascii=False))
        environment = {k: v for k, v in os.environ.items() if k != "LLM_API_KEY"}
        environment["LLM_BACKEND"] = "codex"
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(llm, "_codex_command", return_value="codex"), \
                patch.object(llm.subprocess, "run", side_effect=fake):
            self.assertEqual(recorder.extract_story("我小时候住在村里。"), STORY)


if __name__ == "__main__":
    unittest.main()
