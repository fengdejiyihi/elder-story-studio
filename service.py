"""Local-only browser service for the elder recording workflow (standard library)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from video_script import refresh_video_script

SERVICE = "elder-audio-service"
VERSION = "1.1.0"
ROOT = Path(__file__).resolve().parent
FILES = {
    "video_script.md": "视频文案", "video_script.csv": "分镜表（Excel）", "video_script.json": "视频文案 JSON",
    "story.md": "原稿", "story.json": "原稿 JSON",
    "story_polished.md": "整理稿", "story_polished.json": "整理稿 JSON",
    "polish_changes.json": "整理记录", "transcript.txt": "转写文本",
    "transcript.json": "转写 JSON", "manifest.json": "处理清单",
}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".wma", ".mp4"}
ACTIVE = {"queued", "asr", "generating_story", "original_ready", "polishing"}
MAX_HOTWORD_CHARS = 500
# The submission entry only ever hands out the video-maker copy, never transcripts or drafts.
SUBMIT_FILES = ("video_script.md", "video_script.csv", "video_script.json")
MAX_SUBMIT_ACTIVE = 10  # leaves queue room for the team's own jobs


def clean_hotword(value):
    """Hotwords are names and places separated by spaces; never command-line options."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_HOTWORD_CHARS:
        raise ValueError(f"热词须为不超过 {MAX_HOTWORD_CHARS} 个字符的文本。")
    words = [w.lstrip("-") for w in re.split(r"[\s,，、;；]+", value) if w.strip("-")]
    if any(not w.isprintable() for w in words):
        raise ValueError("热词包含无效字符。")
    return " ".join(words) or None


def ticket_digest(ticket):
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def app_identity(root):
    return hashlib.sha256(str(Path(root).resolve()).casefold().encode("utf-8")).hexdigest()[:24]


def safe_file(directory, name):
    if name not in FILES:
        return None
    try:
        directory = Path(directory)
        if directory.is_symlink():
            return None
        directory = directory.resolve()
        candidate = directory / name
        if candidate.is_symlink() or candidate.resolve().parent != directory or not candidate.is_file():
            return None
        return candidate
    except (OSError, RuntimeError):
        return None


def redact_log(line):
    line = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[密钥已隐藏]", line)
    return re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,]+", r"\1[已隐藏]", line)


def resolve_input_path(path):
    if not isinstance(path, str) or not path.strip() or len(path) > 4096 or any(c in path for c in "\x00\r\n"):
        raise ValueError("请输入有效的本机完整路径（不超过 4096 个字符）。")
    raw = path.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() or raw.startswith(("\\\\", "//")):
        raise ValueError("请填写本机完整路径。")
    try:
        return candidate.resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        raise ValueError("路径不存在或当前用户无法访问。") from None


def resume_missing_files(directory):
    missing = [name for name in ("transcript.json", "transcript.txt") if not safe_file(directory, name)]
    voice = Path(directory) / "voice"
    if not voice.is_dir() or voice.is_symlink() or voice.resolve().parent != Path(directory).resolve():
        missing.append("voice/")
    return missing


def original_file_exists(directory):
    # Even a broken symlink is an existing original path; never replace it through resume.
    return any(os.path.lexists(Path(directory) / name) for name in ("story.json", "story.md"))


class AppState:
    def __init__(self, root=ROOT, python_executable=None, allow_local_paths=True, api_token=None, submit_code=None):
        self.root = Path(root).resolve()
        storage = os.environ.get("SERVICE_DATA_DIR")
        self.storage = Path(storage).expanduser().resolve() if storage else self.root
        self.data_dir = self.storage if storage else self.root / "data"
        self.output_dir = self.storage / "outputs"
        self.upload_dir = self.storage / "uploads"
        self.log_dir = self.storage / "logs"
        self.python_executable = python_executable or sys.executable
        self.allow_local_paths = allow_local_paths
        self.api_token = api_token
        # Loopback is open without a code; a public deployment opens the entry only with one.
        self.submit_code = submit_code
        self.submit_enabled = bool(submit_code) or allow_local_paths
        self.submit_reserved = 0
        self.app_id = app_identity(self.root)
        self.token = secrets.token_urlsafe(32)
        self.instance_id = secrets.token_urlsafe(16)
        self.lock = threading.RLock()
        self.jobs = {}
        self.results = {}
        self.uploads = {}
        self.queue = queue.Queue()
        self.stopping = False
        for directory in (self.data_dir, self.output_dir, self.upload_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._load_history()
        self.worker = threading.Thread(target=self._work, name="recording-worker", daemon=True)
        self.worker.start()

    def _save_json(self, name, value):
        target = self.data_dir / name
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def _save_jobs(self):
        self._save_json("jobs.json", {"version": 1, "jobs": list(self.jobs.values())})

    def _load_history(self):
        history = self.data_dir / "jobs.json"
        if history.exists():
            try:
                data = json.loads(history.read_text(encoding="utf-8"))
                for job in data.get("jobs", []):
                    if not isinstance(job, dict) or not isinstance(job.get("id"), str):
                        continue
                    if job.get("status") in ACTIVE:
                        job.update(status="failed", stage="failed", updated_at=time.time(),
                                   message="服务在任务完成前停止，原有文件已保留；请选择此结果继续处理，或重新提交。")
                    self.jobs[job["id"]] = job
                    output = Path(job.get("output_path", "")).resolve()
                    if output.is_dir() and (self.allow_local_paths or output.parent == self.output_dir.resolve()):
                        self.register_result(output)
                self._save_jobs()
            except (OSError, ValueError, TypeError):
                raise RuntimeError("任务记录无法读取，请保留 data/jobs.json 并检查内容。") from None
        uploaded = self.data_dir / "uploads.json"
        if uploaded.exists():
            try:
                entries = json.loads(uploaded.read_text(encoding="utf-8"))
                for upload_id, entry in entries.items():
                    path = (self.upload_dir / entry["file"]).resolve()
                    if path.parent == self.upload_dir.resolve() and path.is_file():
                        self.uploads[upload_id] = entry
            except (OSError, ValueError, TypeError, KeyError):
                raise RuntimeError("上传记录无法读取，请保留 data/uploads.json 并检查内容。") from None

    def register_upload(self, upload_id, file_name, original_name, size):
        with self.lock:
            self.uploads[upload_id] = {"file": file_name, "name": original_name, "size": size}
            self._save_json("uploads.json", self.uploads)

    def public_job(self, job):
        job = dict(job)
        job.pop("ticket_hash", None)
        if not self.allow_local_paths:
            job.pop("source", None)
            job.pop("output_path", None)
        return job

    def register_result(self, directory):
        directory = Path(directory).resolve()
        result_id = hashlib.sha256(str(directory).casefold().encode("utf-8")).hexdigest()[:24]
        with self.lock:
            self.results[result_id] = directory
        return result_id

    def result_info(self, result_id, directory):
        if safe_file(directory, "story.json") and not self._busy(directory):
            # Results made before the handoff existed, or re-polished since, get a current copy.
            refresh_video_script(directory)
        polish_status = None
        manifest_file = safe_file(directory, "manifest.json")
        if manifest_file:
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
                polish = manifest.get("polish", {})
                if isinstance(polish, dict) and polish.get("status") in {"completed", "failed", "pending", "running", "skipped"}:
                    polish_status = polish["status"]
            except (OSError, ValueError, AttributeError):
                pass
        files = [{"name": name, "label": label} for name, label in FILES.items() if safe_file(directory, name)]
        names = {file["name"] for file in files}
        missing = resume_missing_files(directory)
        resume_ready = not missing and not original_file_exists(directory)
        if "story.json" in names:
            document_status = "completed" if polish_status == "completed" and "story_polished.md" in names else "original_ready"
            available_action = "polish"
        elif original_file_exists(directory):
            document_status, available_action = "incomplete_original", None
        elif "transcript.json" in names:
            document_status, available_action = "partial_transcript", "resume" if resume_ready else None
        else:
            document_status, available_action = "empty", None
        return {"id": result_id, "name": directory.name,
                "path": str(directory) if self.allow_local_paths else None,
                "polish_status": polish_status,
                "document_status": document_status, "available_action": available_action,
                "resume_ready": resume_ready, "resume_missing": missing,
                "updated_at": max((p.stat().st_mtime_ns for name in FILES
                                   if (p := safe_file(directory, name))), default=0),
                "files": files}

    def inspect_result(self, path):
        if not self.allow_local_paths:
            raise ValueError("云端不允许检查服务器文件路径，请选择已登记的结果。")
        directory = resolve_input_path(path)
        if not directory.is_dir():
            raise ValueError("请选择已有结果目录。")
        result_id = self.register_result(directory)
        return self.result_info(result_id, directory)

    def list_results(self):
        for directory in self.output_dir.iterdir():
            if directory.is_dir() and not directory.is_symlink():
                if any(safe_file(directory, name) for name in FILES):
                    self.register_result(directory)
        with self.lock:
            entries = list(self.results.items())
        entries.sort(key=lambda item: item[1].stat().st_mtime if item[1].exists() else 0, reverse=True)
        return [self.result_info(i, p) for i, p in entries if p.is_dir()]

    def _busy(self, directory):
        with self.lock:
            return any(j["status"] in ACTIVE and j.get("output_path") == str(directory) for j in self.jobs.values())

    def list_jobs(self):
        with self.lock:
            return [dict(j) for j in reversed(list(self.jobs.values()))]

    def get_job(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def _update(self, job_id, status, message):
        with self.lock:
            self.jobs[job_id].update(status=status, stage=status, message=message, updated_at=time.time())
            self._save_jobs()

    def enqueue(self, mode, path=None, upload_id=None, result_id=None, hotword=None, ticket_hash=None):
        if not isinstance(mode, str) or mode not in {"audio", "polish", "resume"}:
            raise ValueError("请选择处理录音或整理已有结果。")
        hotword = clean_hotword(hotword)
        if hotword and mode != "audio":
            raise ValueError("热词只用于识别新录音。")
        if sum(value is not None for value in (path, upload_id, result_id)) != 1:
            raise ValueError("请选择一份音频或已有结果。")
        if path is not None and not self.allow_local_paths:
            raise ValueError("云端仅接收已上传音频或已登记结果，不接受服务器文件路径。")
        if upload_id is not None:
            if mode != "audio" or not isinstance(upload_id, str):
                raise ValueError("上传音频编号无效。")
            with self.lock:
                upload = self.uploads.get(upload_id)
            if not upload:
                raise ValueError("上传音频不存在，请重新上传。")
            path = str(self.upload_dir / upload["file"])
        if result_id is not None:
            if mode not in {"polish", "resume"} or not isinstance(result_id, str):
                raise ValueError("结果编号无效。")
            self.list_results()
            with self.lock:
                result = self.results.get(result_id)
            if result is None:
                raise ValueError("结果不存在，请刷新后选择。")
            if not self.allow_local_paths and result.parent != self.output_dir.resolve():
                raise ValueError("仅允许整理当前服务的结果。")
            path = str(result)
        candidate = resolve_input_path(path)
        if mode == "audio":
            if not candidate.is_file() or candidate.suffix.lower() not in AUDIO_EXTENSIONS:
                raise ValueError("请选择 mp3、m4a、wav、aac、flac 等音频文件。")
            if upload_id is not None and candidate.parent != self.upload_dir.resolve():
                raise ValueError("上传音频路径无效。")
        elif mode == "resume":
            if not candidate.is_dir():
                raise ValueError("继续生成文稿需要已有结果目录。")
            if original_file_exists(candidate):
                raise ValueError("已有原始经历稿，已拒绝覆盖；请改为单独整理文稿。")
            missing = resume_missing_files(candidate)
            if missing:
                raise ValueError("已有结果缺少 " + "、".join(missing) + "，无法继续生成文稿。")
        elif not candidate.is_dir() or not safe_file(candidate, "story.json"):
            raise ValueError("缺少 story.json；已有完整转写时请选择继续生成文稿。")
        with self.lock:
            if self.stopping:
                raise ValueError("服务正在停止，请重新启动后再提交。")
            if sum(j["status"] in ACTIVE for j in self.jobs.values()) >= 20:
                raise ValueError("队列已有 20 个任务，请等待部分完成后再提交。")
            if any(j["status"] in ACTIVE and (j["source"] == str(candidate)
                   or (mode in {"polish", "resume"} and j["output_path"] == str(candidate)))
                   for j in self.jobs.values()):
                raise ValueError("此路径已有任务在排队或处理中。")
            job_id = secrets.token_hex(12)
            display_name = upload["name"] if upload_id is not None else candidate.name
            if mode == "audio":
                stem = Path(display_name).stem[:100].rstrip(" .") or "recording"
                output = self.output_dir / (stem + "_" + time.strftime("%Y%m%d_%H%M%S") + "_" + job_id[:6])
                output.mkdir()
            else:
                output = candidate
            result_id = self.register_result(output)
            job = {"id": job_id, "mode": mode, "name": display_name, "source": str(candidate),
                   "result_id": result_id, "output_path": str(output), "status": "queued", "stage": "queued",
                   "message": "已加入队列", "created_at": time.time(), "updated_at": time.time()}
            if hotword:
                job["hotword"] = hotword
            if ticket_hash:
                job.update(channel="submit", ticket_hash=ticket_hash)
            self.jobs[job_id] = job
            self._save_jobs()
            self.queue.put(job_id)
            return dict(job)

    def reserve_submission(self):
        """Hold a slot before the body is stored so concurrent uploads cannot overshoot the cap."""
        with self.lock:
            active = sum(j["status"] in ACTIVE and "ticket_hash" in j for j in self.jobs.values())
            if active + self.submit_reserved >= MAX_SUBMIT_ACTIVE:
                return False
            self.submit_reserved += 1
            return True

    def release_submission(self):
        with self.lock:
            self.submit_reserved -= 1

    def discard_upload(self, upload_id):
        with self.lock:
            entry = self.uploads.pop(upload_id, None)
            if entry:
                self._save_json("uploads.json", self.uploads)
        if entry:
            (self.upload_dir / entry["file"]).unlink(missing_ok=True)

    def submit(self, upload_id, hotword=None):
        """Queue a recording from the submission entry; the ticket is the submitter's only key."""
        ticket = secrets.token_urlsafe(24)
        self.enqueue("audio", upload_id=upload_id, hotword=hotword, ticket_hash=ticket_digest(ticket))
        return ticket

    def submission(self, ticket):
        if not isinstance(ticket, str) or not 20 <= len(ticket) <= 64:
            return None
        digest = ticket_digest(ticket)
        with self.lock:
            for job in self.jobs.values():
                if secrets.compare_digest(job.get("ticket_hash", ""), digest):
                    return dict(job)
        return None

    def submission_info(self, job):
        """Status in the submitter's terms: no paths, ids, drafts or internal stage names."""
        output = Path(job["output_path"])
        if job["status"] in ACTIVE:
            with self.lock:
                ahead = sum(j["status"] in ACTIVE and j["created_at"] < job["created_at"] for j in self.jobs.values())
            if job["status"] == "queued":
                message = f"排队中，前面还有 {ahead} 份录音" if ahead else "排队中，马上开始"
            elif job["status"] == "asr":
                message = "正在识别录音"
            else:
                message = "录音已识别，正在撰写文案"
            return {"status": "processing", "message": message, "name": job["name"], "files": []}
        if safe_file(output, "story.json") and not self._busy(output):
            refresh_video_script(output)
        files = [{"name": name, "label": FILES[name]} for name in SUBMIT_FILES if safe_file(output, name)]
        if safe_file(output, "video_script.md"):
            return {"status": "ready", "message": "文案已生成，可以预览和下载", "name": job["name"], "files": files}
        return {"status": "failed", "name": job["name"], "files": [],
                "message": "这份录音没能整理出文案。请确认录音清晰、以老人讲述为主后重新提交；多次失败请联系我们。"}

    def _work(self):
        while True:
            job_id = self.queue.get()
            try:
                self._run_job(job_id)
            except Exception:
                self._update(job_id, "failed", "任务未完成，请检查本机日志后重试。原有文件会保留。")
            finally:
                self.queue.task_done()

    def _run_job(self, job_id):
        job = self.get_job(job_id)
        output = Path(job["output_path"])
        if job["mode"] == "audio":
            command = [self.python_executable, "-u", str(self.root / "recorder.py"), job["source"], "-o", str(output)]
            if job.get("hotword"):
                command += ["--hotword", job["hotword"]]
            self._update(job_id, "asr", "正在预处理与识别录音")
        elif job["mode"] == "resume":
            if original_file_exists(output):
                self._update(job_id, "failed", "排队期间已有原稿生成，未覆盖。请改为单独整理文稿。")
                return
            command = [self.python_executable, "-u", str(self.root / "recorder.py"), "--resume-result", str(output)]
            self._update(job_id, "generating_story", "使用已有转写继续生成文稿，无需重新识别")
        else:
            command = [self.python_executable, "-u", str(self.root / "polish.py"), str(output)]
            self._update(job_id, "polishing", "正在整理已有原稿")
        env = os.environ.copy()
        env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with self.log_dir.joinpath(job_id + ".log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=self.root, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                       shell=False, **options)
            try:
                for line in process.stdout:
                    log.write(redact_log(line))
                    log.flush()
                    current = self.get_job(job_id)["status"]
                    if job["mode"] in {"audio", "resume"} and current == "asr" and "[4/5]" in line:
                        self._update(job_id, "generating_story", "转写已完成，正在生成原稿")
                    if job["mode"] in {"audio", "resume"} and current in {"asr", "generating_story"} and safe_file(output, "story.json"):
                        self._update(job_id, "original_ready", "原稿已生成，可以查看；即将整理")
                    if ("POLISH_STARTED" in line or "[5/5]" in line or "开始整理" in line
                            or "文稿整理" in line or "整理原稿" in line):
                        self._update(job_id, "polishing", "原稿已生成，正在整理文稿")
            finally:
                process.stdout.close()
                return_code = process.wait()
        manifest = {}
        manifest_file = safe_file(output, "manifest.json")
        if manifest_file:
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                pass
        polish = manifest.get("polish", {}) if isinstance(manifest, dict) else {}
        polish_status = polish.get("status") if isinstance(polish, dict) else None
        video = refresh_video_script(output) if safe_file(output, "story.json") else None
        if return_code == 0 and polish_status == "completed" and safe_file(output, "story_polished.md"):
            self._update(job_id, "completed", "处理完成，视频文案、原稿和整理稿均可查看与下载")
        elif safe_file(output, "story.json") and video:
            self._update(job_id, "failed", "整理稿未通过核对，已按原稿生成视频文案，可以先用；也可选择此结果单独重试整理。")
        elif safe_file(output, "story.json"):
            self._update(job_id, "failed", "原稿已保留，整理未完成。可选择此结果单独重试整理。")
        elif safe_file(output, "transcript.json"):
            self._update(job_id, "failed", "转写已保留，文稿生成未完成。可在已有结果中选择继续生成文稿。")
        elif job["mode"] == "resume":
            self._update(job_id, "failed", "继续生成文稿失败，请检查已有结果目录及本机日志。")
        else:
            self._update(job_id, "failed", "录音处理失败，请检查本机日志后重试。")
        with self.lock:
            self.jobs[job_id]["exit_code"] = return_code
            self._save_jobs()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    server_version = "ElderLocal/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):
        # No query strings, source paths, tokens, or API responses in access logs.
        pass

    @property
    def state(self):
        return self.server.state

    def _send(self, code, data, content_type="application/json; charset=utf-8", extra=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code, message):
        self._send(code, {"error": message})

    def _local_request(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        hosts.update(self.server.allowed_hosts)
        if self.headers.get("Host", "").lower() not in hosts:
            self._error(403, "仅允许本机访问。")
            return False
        origin = self.headers.get("Origin")
        origins = {"http://" + h for h in {f"127.0.0.1:{port}", f"localhost:{port}"}}
        origins.update(self.server.allowed_origins)
        if origin is not None and origin.lower().rstrip("/") not in origins:
            self._error(403, "不允许外部网站访问本服务。")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._error(403, "不允许跨站请求。")
            return False
        return True

    def _authenticated(self):
        if not self.state.api_token:
            return True
        authorization = self.headers.get("Authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not secrets.compare_digest(supplied.encode("utf-8"), self.state.api_token.encode("utf-8")):
            self._error(401, "请输入有效的服务访问令牌。")
            return False
        return True

    def do_GET(self):
        if not self._local_request():
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/submit" or parsed.path.startswith("/submit/"):
            return self._submit_get(parsed)
        if parsed.path.startswith("/api/") and parsed.path != "/api/health" and not self._authenticated():
            return
        if parsed.path == "/api/health":
            health = {"service": SERVICE, "version": VERSION,
                      "auth_required": bool(self.state.api_token), "local_paths": self.state.allow_local_paths,
                      "supported_modes": ["audio", "polish", "resume"], "submit_enabled": self.state.submit_enabled,
                      "max_upload_mb": self.server.max_upload_bytes // (1024 * 1024)}
            if self.state.allow_local_paths:
                health.update(pid=os.getpid(), port=self.server.server_port,
                              app_id=self.state.app_id, instance_id=self.state.instance_id)
            self._send(200, health)
        elif parsed.path == "/":
            self._page("index.html")
        elif parsed.path == "/api/jobs":
            self._send(200, {"jobs": [self.state.public_job(j) for j in self.state.list_jobs()]})
        elif parsed.path.startswith("/api/jobs/"):
            job = self.state.get_job(parsed.path[len("/api/jobs/"):])
            self._send(200, {"job": self.state.public_job(job)}) if job else self._error(404, "任务不存在。")
        elif parsed.path == "/api/results":
            self._send(200, {"results": self.state.list_results()})
        elif parsed.path == "/api/files":
            self._file(parse_qs(parsed.query, keep_blank_values=True))
        else:
            self._error(404, "页面不存在。")

    def _page(self, name, replacements=None):
        nonce = secrets.token_urlsafe(18)
        html = self.state.root.joinpath("web", name).read_text(encoding="utf-8")
        html = html.replace("__CSRF_TOKEN__", self.state.token).replace("__NONCE__", nonce)
        for key, value in (replacements or {}).items():
            html = html.replace(key, value)
        policy = (f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                  "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self._send(200, html, "text/html; charset=utf-8", {"Content-Security-Policy": policy})

    def _submit_get(self, parsed):
        if parsed.path == "/submit":
            return self._page("submit.html", {
                "__SUBMIT_ENABLED__": "true" if self.state.submit_enabled else "false",
                "__CODE_REQUIRED__": "true" if self.state.submit_code else "false",
                "__MAX_UPLOAD_MB__": str(self.server.max_upload_bytes // (1024 * 1024))})
        parts = parsed.path.split("/")
        # /submit/api/submissions/<ticket>  or  /submit/api/submissions/<ticket>/files/<name>
        if len(parts) not in {5, 7} or parts[1:4] != ["submit", "api", "submissions"] or (len(parts) == 7 and parts[5] != "files"):
            return self._error(404, "页面不存在。")
        job = self.state.submission(parts[4])
        if job is None:
            return self._error(404, "没有找到这份提交，请检查链接是否完整。")
        if len(parts) == 5:
            return self._send(200, self.state.submission_info(job))
        name = parts[6]
        if name not in SUBMIT_FILES or job["status"] in ACTIVE:
            return self._error(404, "文件尚未生成或不存在。")
        path = safe_file(job["output_path"], name)
        if path is None:
            return self._error(404, "文件尚未生成或不存在。")
        kind = ("application/json; charset=utf-8" if name.endswith(".json")
                else "text/csv; charset=utf-8" if name.endswith(".csv") else "text/plain; charset=utf-8")
        download = parse_qs(parsed.query).get("download") == ["1"]
        self._send(200, path.read_bytes(), kind, {"Content-Disposition": f'attachment; filename="{name}"'} if download else {})

    def _submit_post(self):
        if not self.state.submit_enabled:
            return self._error(404, "提交入口未开放。")
        if self.headers.get("Origin") is not None:
            token = self.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(token.encode("utf-8"), self.state.token.encode("utf-8")):
                return self._error(403, "页面已过期，请刷新后重试。")
        if self.state.submit_code:
            supplied = unquote(self.headers.get("X-Access-Code", "")).strip()
            if not secrets.compare_digest(supplied.encode("utf-8"), self.state.submit_code.encode("utf-8")):
                return self._error(401, "提交码不正确。")
        try:
            hotword = clean_hotword(unquote(self.headers.get("X-Hotword", ""), errors="strict") or None)
        except (ValueError, UnicodeError) as exc:
            return self._error(400, str(exc) if isinstance(exc, ValueError) else "热词编码无效。")
        if self.headers.get("Transfer-Encoding"):
            return self._error(400, "请求格式无效。")
        if not self.state.reserve_submission():
            return self._error(429, "当前提交较多，请过一段时间再提交。")
        try:
            upload = self._receive_upload()
            if upload is None:
                return
            try:
                ticket = self.state.submit(upload["upload_id"], hotword)
            except (ValueError, OSError) as exc:
                # No ticket was handed out, so nobody could ever reach this recording.
                self.state.discard_upload(upload["upload_id"])
                return self._error(400 if isinstance(exc, ValueError) else 500,
                                   str(exc) if isinstance(exc, ValueError) else "暂时无法接收录音，请稍后再试。")
        finally:
            self.state.release_submission()
        self._send(202, {"ticket": ticket, "status_url": "/submit/api/submissions/" + ticket,
                         "page_url": "/submit#" + ticket})

    def _file(self, query):
        if any(len(v) != 1 for v in query.values()) or set(query) - {"job_id", "result_id", "name", "download"}:
            return self._error(400, "文件请求无效。")
        if ("job_id" in query) == ("result_id" in query):
            return self._error(400, "请选择一个任务或结果。")
        name = query.get("name", [""])[0]
        if name not in FILES:
            return self._error(403, "不允许访问此文件。")
        result_id = query.get("result_id", [None])[0]
        if "job_id" in query:
            job = self.state.get_job(query["job_id"][0])
            if not job:
                return self._error(404, "任务不存在。")
            result_id = job["result_id"]
        with self.state.lock:
            directory = self.state.results.get(result_id)
        if directory and not self.state.allow_local_paths and directory.resolve().parent != self.state.output_dir.resolve():
            return self._error(403, "不允许访问此结果目录。")
        path = safe_file(directory, name) if directory else None
        if path is None:
            return self._error(404, "文件尚未生成或不存在。")
        if path.stat().st_size > 32 * 1024 * 1024:
            return self._error(413, "文件过大，请从本机结果目录打开。")
        kind = ("application/json; charset=utf-8" if name.endswith(".json")
                else "text/csv; charset=utf-8" if name.endswith(".csv") else "text/plain; charset=utf-8")
        headers = {"Content-Disposition": f'attachment; filename="{name}"'} if query.get("download") == ["1"] else {}
        self._send(200, path.read_bytes(), kind, headers)

    def do_POST(self):
        if not self._local_request():
            return
        if self.path == "/submit/api/submissions":
            return self._submit_post()
        if not self._authenticated():
            return
        token = self.headers.get("X-CSRF-Token", "")
        bearer_only_client = bool(self.state.api_token) and self.headers.get("Origin") is None
        if not bearer_only_client and not secrets.compare_digest(token.encode("utf-8"), self.state.token.encode("utf-8")):
            return self._error(403, "页面已过期，请刷新后重试。")
        if self.headers.get("Transfer-Encoding"):
            return self._error(400, "请求格式无效。")
        if self.path == "/api/uploads":
            return self._upload()
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            return self._error(415, "请使用 JSON 请求。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError()
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            return self._error(400, "请求内容无效或过长。")
        if self.path == "/api/results/inspect":
            if set(body) != {"path"}:
                return self._error(400, "请求必须包含 path。")
            try:
                self._send(200, {"result": self.state.inspect_result(body["path"])})
            except (ValueError, OSError) as exc:
                self._error(400, str(exc) if isinstance(exc, ValueError) else "无法读取结果目录。")
        elif self.path == "/api/jobs":
            sources = set(body) & {"path", "upload_id", "result_id"}
            if "mode" not in body or len(sources) != 1 or set(body) - {"mode", "path", "upload_id", "result_id", "hotword"}:
                return self._error(400, "请求须包含 mode，以及 path、upload_id、result_id 中的一项。")
            try:
                job = self.state.enqueue(**body)
                self._send(202, {"job": self.state.public_job(job)})
            except (ValueError, OSError) as exc:
                message = str(exc) if isinstance(exc, ValueError) else "无法读取输入或创建输出目录。"
                self._error(400, message)
        elif self.path == "/api/shutdown":
            with self.state.lock:
                if any(j["status"] in ACTIVE for j in self.state.jobs.values()):
                    return self._error(409, "仍有任务在处理或排队，请完成后再停止服务。")
                self.state.stopping = True
            self._send(200, {"status": "stopping"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._error(404, "接口不存在。")

    def _upload(self):
        upload = self._receive_upload()
        if upload is not None:
            self._send(201, upload)

    def _receive_upload(self):
        """Store a raw audio body; on failure the error is already sent and None is returned."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "上传长度无效。")
            return None
        if length <= 0 or length > self.server.max_upload_bytes:
            self._error(413, "音频为空或超过本服务的上传大小限制。")
            return None
        encoded_name = self.headers.get("X-Filename", "")
        if not encoded_name or len(encoded_name) > 2048:
            self._error(400, "缺少有效的音频文件名。")
            return None
        try:
            original_name = unquote(encoded_name, errors="strict")
        except UnicodeError:
            self._error(400, "音频文件名编码无效。")
            return None
        # Treat the header as a label, never as a path. Both Windows and POSIX separators are removed.
        cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", original_name).strip(" .")
        suffix = Path(cleaned).suffix.lower()
        if suffix not in AUDIO_EXTENSIONS:
            self._error(400, "不支持此音频格式。")
            return None
        cleaned = (Path(cleaned).stem[:100].rstrip(" .") or "recording") + suffix
        upload_id = secrets.token_hex(16)
        file_name = upload_id + "_" + cleaned
        destination = self.state.upload_dir / file_name
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            self.connection.settimeout(60)
            remaining = length
            with temporary.open("xb") as handle:
                while remaining:
                    block = self.rfile.read(min(65536, remaining))
                    if not block:
                        raise OSError("incomplete upload")
                    handle.write(block)
                    remaining -= len(block)
            temporary.replace(destination)
            self.state.register_upload(upload_id, file_name, cleaned, length)
        except OSError:
            temporary.unlink(missing_ok=True)
            self._error(400, "上传未完成，请重试。")
            return None
        return {"upload_id": upload_id, "name": cleaned, "size": length}


def create_server(host="127.0.0.1", port=8765, root=ROOT, python_executable=None):
    if host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        raise ValueError("host 必须为 127.0.0.1、localhost 或 0.0.0.0。")
    local = host in {"127.0.0.1", "localhost"}
    api_token = os.environ.get("SERVICE_API_TOKEN", "").strip()
    if not local and not api_token:
        raise ValueError("非回环绑定必须设置 SERVICE_API_TOKEN。")
    public_origin = os.environ.get("SERVICE_PUBLIC_ORIGIN", "").strip().rstrip("/").lower()
    origin = urlsplit(public_origin) if public_origin else None
    if origin and (origin.scheme not in {"http", "https"} or not origin.netloc or origin.path or origin.query or origin.fragment or origin.username):
        raise ValueError("SERVICE_PUBLIC_ORIGIN 必须是完整的 http/https 来源，不含路径。")
    submit_code = os.environ.get("SUBMIT_ACCESS_CODE", "").strip() or None
    if submit_code and api_token and secrets.compare_digest(submit_code.encode("utf-8"), api_token.encode("utf-8")):
        raise ValueError("SUBMIT_ACCESS_CODE 不能与 SERVICE_API_TOKEN 相同。")
    upload_limit = int(os.environ.get("MAX_UPLOAD_MB", "256"))
    if not 1 <= upload_limit <= 2048:
        raise ValueError("MAX_UPLOAD_MB 必须为 1–2048。")
    server = LocalServer((host, port), Handler)
    server.allowed_hosts = {h.strip().lower() for h in os.environ.get("SERVICE_ALLOWED_HOSTS", "").split(",") if h.strip()}
    if origin:
        server.allowed_hosts.add(origin.netloc)
    server.allowed_origins = {public_origin} if public_origin else set()
    server.max_upload_bytes = upload_limit * 1024 * 1024
    try:
        server.state = AppState(root, python_executable, allow_local_paths=local,
                                api_token=api_token or None, submit_code=submit_code)
    except Exception:
        server.server_close()
        raise
    return server


def main():
    parser = argparse.ArgumentParser(description="老人录音本地服务")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须为 1–65535。")
    server = create_server(host=args.host, port=args.port)
    state_file = server.state.storage / ".service-state.json"
    record = {"service": SERVICE, "app_id": server.state.app_id, "pid": os.getpid(),
              "port": server.server_port, "instance_id": server.state.instance_id, "token": server.state.token}
    state_file.write_text(json.dumps(record), encoding="utf-8")
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        try:
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            if saved.get("instance_id") == server.state.instance_id:
                state_file.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
