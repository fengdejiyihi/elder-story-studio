"""Choose the language model that writes and polishes the story.

LLM_BACKEND=deepseek (default) calls an OpenAI-compatible API (DeepSeek unless
LLM_BASE_URL says otherwise) with LLM_API_KEY. LLM_BACKEND=codex runs the Codex
CLI already signed in on this computer (`codex exec`), so no API key is needed.

Both backends answer through the same `client.chat.completions.create(...)`
shape, so recorder.py and polish.py keep one code path.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

BACKENDS = ("deepseek", "codex")
DEFAULT_CODEX_TIMEOUT = 900


def load_env_file(directory):
    """Read KEY=VALUE lines (optional `export`) from directory/.env without overriding the environment."""
    env = Path(directory) / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class LLMBackendError(RuntimeError):
    """A backend failure whose message is safe to show and store (no prompts, keys or responses)."""


def backend():
    name = os.environ.get("LLM_BACKEND", "").strip().lower() or "deepseek"
    if name not in BACKENDS:
        raise LLMBackendError(f"LLM_BACKEND 只能是 {' 或 '.join(BACKENDS)}，当前为 {name[:30]}。")
    return name


def model_name(model=None):
    """The model recorded in manifests; Codex uses its own default unless CODEX_MODEL is set."""
    if backend() == "codex":
        return model or os.environ.get("CODEX_MODEL", "").strip() or "codex"
    return model or os.environ.get("LLM_MODEL", "deepseek-v4-pro")


def create_client(api_key=None, timeout=180.0):
    if backend() == "codex":
        return CodexClient()
    from openai import OpenAI
    return OpenAI(api_key=api_key if api_key is not None else os.environ["LLM_API_KEY"],
                  base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
                  timeout=timeout, max_retries=0)


def _codex_command():
    configured = os.environ.get("CODEX_PATH", "").strip().strip('"')
    executable = configured or shutil.which("codex")
    if not executable or not Path(executable).is_file():
        raise LLMBackendError("未找到 Codex 命令行。请先安装并登录 Codex（运行 codex login），或在 .env 中用 CODEX_PATH 指定 codex 的完整路径。")
    return executable


def _extract_json(text):
    """Codex answers in prose mode; keep only the outermost JSON object it wrote."""
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else text


class CodexClient:
    """Minimal stand-in for the OpenAI client, backed by `codex exec`."""

    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def close(self):
        pass

    def _create(self, model, messages, **_options):
        # temperature, response_format and extra_body are OpenAI API options; Codex has none of them.
        executable = _codex_command()
        try:
            timeout = int(os.environ.get("CODEX_TIMEOUT", DEFAULT_CODEX_TIMEOUT))
        except ValueError:
            timeout = DEFAULT_CODEX_TIMEOUT
        prompt = "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages)
        prompt += "\n\n只输出一个完整的 JSON 对象，不要代码围栏、说明或其他文字。不要读取或修改任何文件，不要运行命令。"
        with tempfile.TemporaryDirectory(prefix="codex-") as workdir:
            answer = Path(workdir) / "answer.txt"
            command = [executable, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                       "--output-last-message", str(answer)]
            if model and model != "codex":
                command += ["-m", model]
            command.append("-")  # read the prompt from stdin; transcripts are too long for a command line
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            try:
                finished = subprocess.run(command, input=prompt, cwd=workdir, capture_output=True,
                                          text=True, encoding="utf-8", errors="replace",
                                          timeout=timeout, shell=False, **options)
            except subprocess.TimeoutExpired:
                raise LLMBackendError(f"Codex 超过 {timeout} 秒未完成，可在 .env 中调大 CODEX_TIMEOUT。") from None
            except OSError:
                raise LLMBackendError("无法启动 Codex 命令行，请检查 CODEX_PATH 或 Codex 安装。") from None
            if finished.returncode != 0:
                # stderr can echo the prompt (the elder's words), so it is never passed on.
                raise LLMBackendError(f"Codex 运行失败（退出码 {finished.returncode}）。请确认这台电脑已运行 codex login 且账号额度可用；"
                                      "可在命令行运行 codex exec 查看详情。")
            content = answer.read_text(encoding="utf-8", errors="replace") if answer.is_file() else ""
        content = _extract_json(content)
        finish = "stop" if content else "error"
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
                                                        message=SimpleNamespace(content=content))])
