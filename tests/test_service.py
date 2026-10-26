"""Offline HTTP boundary and worker tests with tiny local stub subprocesses."""
from contextlib import ExitStack
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlencode


PROJECT = Path(__file__).resolve().parents[1]
SCRATCH = (PROJECT / "work" / "service-tests").resolve()
spec = importlib.util.spec_from_file_location("service_under_test", PROJECT / "service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

FAKE_RECORDER = '''import json,sys
from pathlib import Path
if sys.argv[1]=='--resume-result':
    assert len(sys.argv)==3, sys.argv
    source=out=Path(sys.argv[2])
    assert not (out/'story.json').exists() and not (out/'story.md').exists()
    assert (out/'transcript.json').is_file() and (out/'transcript.txt').is_file() and (out/'voice').is_dir()
    print('[4/5] resume from transcript',flush=True)
else:
    source=Path(sys.argv[1]); out=Path(sys.argv[sys.argv.index('-o')+1])
out.mkdir(exist_ok=True)
(out/'worker_arguments.json').write_text(json.dumps(sys.argv),encoding='utf-8')
(out/'story.json').write_text('{"title":"original"}',encoding='utf-8')
(out/'story.md').write_text('original story',encoding='utf-8')
print('[5/5] POLISH_STARTED',flush=True)
if source.name.startswith('fail'):
    print('synthetic credential sk-test-only-secret Authorization: Bearer fake-token',flush=True)
    (out/'manifest.json').write_text('{"polish":{"status":"failed"}}',encoding='utf-8')
    sys.exit(2)
(out/'story_polished.md').write_text('polished',encoding='utf-8')
(out/'manifest.json').write_text(json.dumps({'polish':{'status':'pending' if source.name.startswith('stale') else 'completed'}}),encoding='utf-8')
'''


def partial_result(directory, missing=()):
    directory.mkdir(parents=True, exist_ok=True)
    fixtures = {"transcript.json": b'[{"start":0,"end":1,"spk":0,"text":"source text"}]',
                "transcript.txt": b"[00:00-00:01] source text", "original.m4a": b"unchanged original"}
    for name, data in fixtures.items():
        if name not in missing:
            (directory / name).write_bytes(data)
    if "voice" not in missing:
        (directory / "voice").mkdir(exist_ok=True)
        (directory / "voice/ref_1.wav").write_bytes(b"unchanged audio reference")
    return file_hashes(directory)


def file_hashes(directory):
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob("*") if p.is_file()}


class LocalHttpTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"SERVICE_API_TOKEN": "", "SERVICE_PUBLIC_ORIGIN": "",
            "SERVICE_DATA_DIR": "", "SERVICE_ALLOWED_HOSTS": "", "MAX_UPLOAD_MB": "1"}))
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=SCRATCH)).resolve()
        self.assertEqual(self.root.parent, SCRATCH)
        (self.root / "web").mkdir()
        (self.root / "web/index.html").write_text('<html nonce="__NONCE__">__CSRF_TOKEN__</html>', encoding="utf-8")
        (self.root / "recorder.py").write_text(FAKE_RECORDER, encoding="utf-8")
        self.server = service.create_server(host="127.0.0.1", port=0, root=self.root, python_executable=sys.executable)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive())
        resolved = self.root.resolve()
        self.assertEqual(resolved.parent, SCRATCH)
        self.assertTrue(resolved.name.startswith("case-"))
        self.assertFalse(self.root.is_symlink())
        shutil.rmtree(resolved)

    def request(self, method, path, body=None, headers=None, raw=False):
        hdrs = {"Host": f"127.0.0.1:{self.server.server_port}"}
        if body is not None and not raw:
            body = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=hdrs)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def post(self, body, **headers):
        return self.request("POST", "/api/jobs", body, {"X-CSRF-Token": self.server.state.token, **headers})

    def wait_job(self, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.server.state.get_job(job_id)
            if state["status"] not in service.ACTIVE and "exit_code" in state:
                return state
            time.sleep(.02)
        self.fail("stub child process did not finish within 10 seconds")

    def test_health_identifies_service_without_leaking_token(self):
        status, _, raw = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual(body["service"], "elder-audio-service")
        self.assertIn("version", body)
        self.assertIn("resume", body["supported_modes"])
        self.assertNotIn(self.server.state.token.encode(), raw)
        self.assertNotIn("token", body)

    def test_external_host_origin_and_cross_site_fetch_rejected(self):
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                status, _, _ = self.request("GET", "/api/results", headers=headers)
                self.assertEqual(status, 403)

    def test_post_requires_json_and_valid_csrf(self):
        status, _, _ = self.request("POST", "/api/jobs", b"{}", {"Content-Type": "text/plain", "X-CSRF-Token": self.server.state.token}, raw=True)
        self.assertEqual(status, 415)
        for token in ("", "wrong"):
            status, _, _ = self.request("POST", "/api/jobs", {"mode": "audio", "path": "missing"}, {"X-CSRF-Token": token})
            self.assertEqual(status, 403)

    def test_bad_job_inputs_and_oversized_json_rejected(self):
        for payload in ({"mode": "bad", "path": str(self.root)}, {"mode": "audio", "path": str(self.root / "missing.wav")},
                        {"mode": "audio", "path": "x" * 4097}, {"mode": "audio", "path": "../x.wav"},
                        {"mode": "audio", "path": str(self.root), "unexpected": True}):
            with self.subTest(mode=payload.get("mode")):
                status, _, _ = self.post(payload)
                self.assertEqual(status, 400)
        status, _, _ = self.post({"mode": "audio", "path": "x" * 17000})
        self.assertEqual(status, 400)
        self.assertEqual(self.server.state.list_jobs(), [])

    def test_file_whitelist_and_traversal(self):
        output = self.root / "outputs" / "sample"
        output.mkdir()
        (output / "story.md").write_text("original document", encoding="utf-8")
        (output / ".env").write_text("not an actual secret", encoding="utf-8")
        result_id = self.server.state.register_result(output)
        for name in ("../story.md", "../../.env", ".env", "original.m4a", "voice/ref_1.wav"):
            query = urlencode({"result_id": result_id, "name": name})
            status, _, _ = self.request("GET", "/api/files?" + query)
            self.assertEqual(status, 403, name)
        query = urlencode({"result_id": result_id, "name": "story.md", "download": "1"})
        status, headers, content = self.request("GET", "/api/files?" + query)
        self.assertEqual(status, 200)
        self.assertEqual(content, b"original document")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_failed_worker_job_does_not_block_following_job(self):
        for name in ("fail.wav", "good.wav"):
            (self.root / name).write_bytes(b"stub audio; never decoded")
        first = self.server.state.enqueue("audio", str(self.root / "fail.wav"))
        second = self.server.state.enqueue("audio", str(self.root / "good.wav"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            first_state, second_state = self.server.state.get_job(first["id"]), self.server.state.get_job(second["id"])
            if first_state["status"] not in service.ACTIVE and second_state["status"] not in service.ACTIVE:
                break
            time.sleep(.02)
        self.assertEqual(first_state["status"], "failed")
        self.assertEqual(first_state["exit_code"], 2)
        self.assertEqual(second_state["status"], "completed")
        self.assertEqual(second_state["exit_code"], 0)
        self.assertTrue(Path(first["output_path"], "story.json").is_file())
        log = (self.root / "logs" / (first["id"] + ".log")).read_text(encoding="utf-8")
        self.assertNotIn("sk-test-only-secret", log)
        self.assertNotIn("fake-token", log)

    def test_non_loopback_binding_requires_configured_authentication(self):
        with self.assertRaises(ValueError):
            service.create_server(host="0.0.0.0", port=0, root=self.root, python_executable=sys.executable)

    def test_zero_exit_with_polished_file_but_uncompleted_manifest_is_failure(self):
        source = self.root / "stale.wav"
        source.write_bytes(b"stub audio; never decoded")
        job = self.server.state.enqueue("audio", str(source))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.server.state.get_job(job["id"])
            if state["status"] not in service.ACTIVE:
                break
            time.sleep(.02)
        self.assertEqual(state["exit_code"], 0)
        self.assertTrue(Path(state["output_path"], "story_polished.md").is_file())
        self.assertEqual(state["status"], "failed")
        self.assertTrue(Path(state["output_path"], "story.json").is_file())

    def test_local_resume_runs_only_resume_cli_and_preserves_transcript_and_voice(self):
        output = self.server.state.output_dir / "pending interview & spaces"
        before = partial_result(output)
        with patch.object(service.subprocess, "Popen", wraps=service.subprocess.Popen) as launch:
            status, _, raw = self.post({"mode": "resume", "path": str(output)})
            self.assertEqual(status, 202, raw)
            job = self.wait_job(json.loads(raw)["job"]["id"])
        self.assertEqual(job["status"], "completed", job)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[0], [sys.executable, "-u", str(self.root / "recorder.py"),
                                                  "--resume-result", str(output)])
        self.assertIs(launch.call_args.kwargs["shell"], False)
        self.assertEqual(json.loads((output / "worker_arguments.json").read_text()),
                         [str(self.root / "recorder.py"), "--resume-result", str(output)])
        after = file_hashes(output)
        for name, digest in before.items():
            self.assertEqual(after[name], digest, name)

    def test_partial_results_list_resume_action_without_prior_registration(self):
        output = self.server.state.output_dir / "partial"
        partial_result(output)
        status, _, raw = self.request("GET", "/api/results")
        self.assertEqual(status, 200)
        item = next(item for item in json.loads(raw)["results"] if item["name"] == output.name)
        self.assertEqual(item["document_status"], "partial_transcript")
        self.assertEqual(item["available_action"], "resume")
        self.assertIs(item["resume_ready"], True)
        self.assertEqual(item["resume_missing"], [])

    def test_local_inspect_registers_partial_external_result(self):
        output = self.root / "external result"
        partial_result(output)
        status, _, raw = self.request("POST", "/api/results/inspect", {"path": str(output)},
                                      {"X-CSRF-Token": self.server.state.token})
        self.assertEqual(status, 200, raw)
        item = json.loads(raw)["result"]
        self.assertEqual(item["document_status"], "partial_transcript")
        self.assertEqual(item["available_action"], "resume")
        self.assertEqual(self.server.state.results[item["id"]], output.resolve())

    def test_resume_rejects_either_existing_original_story_without_modifying_files(self):
        for name in ("story.json", "story.md"):
            with self.subTest(name=name):
                output = self.server.state.output_dir / ("existing-" + name)
                partial_result(output)
                (output / name).write_bytes(b"existing immutable original")
                before = file_hashes(output)
                status, _, raw = self.post({"mode": "resume", "path": str(output)})
                self.assertEqual(status, 400, raw)
                self.assertEqual(file_hashes(output), before)
        self.assertEqual(self.server.state.list_jobs(), [])

    def test_resume_rejects_each_missing_input_and_reports_missing(self):
        for name in ("transcript.json", "transcript.txt", "voice"):
            with self.subTest(name=name):
                output = self.server.state.output_dir / ("missing-" + name)
                partial_result(output, missing=(name,))
                status, _, raw = self.post({"mode": "resume", "path": str(output)})
                self.assertEqual(status, 400, raw)
                status, _, raw = self.request("POST", "/api/results/inspect", {"path": str(output)},
                                              {"X-CSRF-Token": self.server.state.token})
                self.assertEqual(status, 200, raw)
                item = json.loads(raw)["result"]
                self.assertIs(item["resume_ready"], False)
                self.assertIsNone(item["available_action"])
                self.assertIn(name, " ".join(item["resume_missing"]))
        self.assertEqual(self.server.state.list_jobs(), [])


class CloudHttpTests(unittest.TestCase):
    request = LocalHttpTests.request
    tearDown = LocalHttpTests.tearDown
    TOKEN = "offline-service-access-token-not-a-real-secret"

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"SERVICE_API_TOKEN": self.TOKEN,
            "SERVICE_PUBLIC_ORIGIN": "https://voice.example.test", "SERVICE_DATA_DIR": "",
            "SERVICE_ALLOWED_HOSTS": "", "MAX_UPLOAD_MB": "1"}))
        # These cases validate submission and persistence; never start an audio model.
        self.stack.enter_context(patch.object(service.AppState, "_work", return_value=None))
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=SCRATCH)).resolve()
        (self.root / "web").mkdir()
        (self.root / "web/index.html").write_text('<html nonce="__NONCE__">__CSRF_TOKEN__</html>', encoding="utf-8")
        (self.root / "recorder.py").write_text(FAKE_RECORDER, encoding="utf-8")
        self.server = service.create_server(host="0.0.0.0", port=0, root=self.root, python_executable=sys.executable)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()

    def authorized(self, **extra):
        return {"Authorization": "Bearer " + self.TOKEN, **extra}

    def upload(self, filename="录音测试.wav", content=b"offline fake audio", **extra):
        status, _, raw = self.request("POST", "/api/uploads", content,
            self.authorized(**{"Content-Type": "application/octet-stream", "X-Filename": quote(filename), **extra}), raw=True)
        return status, json.loads(raw)

    def test_cloud_health_and_public_page_do_not_expose_access_token_or_server_paths(self):
        status, _, raw = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        health = json.loads(raw)
        self.assertIs(health["auth_required"], True)
        self.assertIs(health["local_paths"], False)
        for key in ("pid", "port", "app_id", "instance_id", "token"):
            self.assertNotIn(key, health)
        self.assertNotIn(self.TOKEN.encode(), raw)
        status, headers, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn(self.TOKEN.encode(), html)
        self.assertIn(self.server.state.token.encode(), html)
        self.assertIn("Content-Security-Policy", headers)

    def test_cloud_api_requires_correct_bearer(self):
        for auth in (None, "Bearer wrong", "Basic " + self.TOKEN):
            headers = {} if auth is None else {"Authorization": auth}
            for endpoint in ("/api/jobs", "/api/results", "/api/files?name=story.md&result_id=x"):
                with self.subTest(endpoint=endpoint, auth=auth is not None):
                    status, _, _ = self.request("GET", endpoint, headers=headers)
                    self.assertEqual(status, 401)
            status, _, _ = self.request("POST", "/api/uploads", b"fake", {**headers, "X-Filename": "test.wav"}, raw=True)
            self.assertEqual(status, 401)
        status, _, _ = self.request("GET", "/api/results", headers=self.authorized())
        self.assertEqual(status, 200)

    def test_cloud_rejects_server_paths_even_with_authentication(self):
        source = self.root / "server-file.wav"
        source.write_bytes(b"private source")
        status, _, _ = self.request("POST", "/api/jobs", {"mode": "audio", "path": str(source)}, self.authorized())
        self.assertEqual(status, 400)
        self.assertEqual(self.server.state.list_jobs(), [])

    def test_upload_and_job_submission_use_ids_without_exposing_server_paths(self):
        status, upload = self.upload()
        self.assertEqual(status, 201, upload)
        self.assertEqual(upload["name"], "录音测试.wav")
        entry = self.server.state.uploads[upload["upload_id"]]
        saved = self.server.state.upload_dir / entry["file"]
        self.assertEqual(saved.read_bytes(), b"offline fake audio")
        status, _, raw = self.request("POST", "/api/jobs", {"mode": "audio", "upload_id": upload["upload_id"]}, self.authorized())
        self.assertEqual(status, 202, raw)
        public_job = json.loads(raw)["job"]
        self.assertEqual(public_job["status"], "queued")
        self.assertNotIn("source", public_job)
        self.assertNotIn("output_path", public_job)
        status, _, raw = self.request("GET", "/api/jobs", headers=self.authorized())
        self.assertEqual(status, 200)
        self.assertNotIn(str(self.root).encode(), raw)

    def test_cloud_browser_requires_csrf_and_allows_only_configured_origin(self):
        base = {"Content-Type": "application/octet-stream", "X-Filename": "test.wav", "Origin": "https://voice.example.test"}
        status, _, _ = self.request("POST", "/api/uploads", b"fake", self.authorized(**base), raw=True)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/uploads", b"fake",
            self.authorized(**{**base, "X-CSRF-Token": self.server.state.token}), raw=True)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/uploads", b"fake",
            self.authorized(**{**base, "X-CSRF-Token": self.server.state.token, "Origin": "https://evil.example"}), raw=True)
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/health", headers={"Host": "voice.example.test"})
        self.assertEqual(status, 200)

    def test_upload_filename_is_only_a_label_and_cannot_escape_uploads(self):
        status, upload = self.upload("../../outside\\nested/录音.wav")
        self.assertEqual(status, 201)
        self.assertNotIn("/", upload["name"])
        self.assertNotIn("\\", upload["name"])
        entry = self.server.state.uploads[upload["upload_id"]]
        saved = (self.server.state.upload_dir / entry["file"]).resolve()
        self.assertEqual(saved.parent, self.server.state.upload_dir.resolve())
        self.assertTrue(saved.is_file())
        self.assertFalse((self.root.parent / "outside").exists())

    def test_invalid_format_oversized_and_chunked_uploads_rejected(self):
        status, _ = self.upload("payload.exe")
        self.assertEqual(status, 400)
        status, _ = self.upload("too-large.wav", b"x", **{"Content-Length": str(1024 * 1024 + 1)})
        self.assertEqual(status, 413)
        status, _ = self.upload("chunked.wav", b"x", **{"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 400)
        self.assertEqual(list(self.server.state.upload_dir.iterdir()), [])

    def test_restart_marks_unfinished_job_failed_and_preserves_upload(self):
        status, upload = self.upload()
        self.assertEqual(status, 201)
        job = self.server.state.enqueue("audio", upload_id=upload["upload_id"])
        self.assertEqual(job["status"], "queued")
        reloaded = service.AppState(self.root, python_executable=sys.executable, allow_local_paths=False, api_token=self.TOKEN)
        recovered = reloaded.get_job(job["id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertIn("停止", recovered["message"])
        self.assertIn(upload["upload_id"], reloaded.uploads)
        saved = reloaded.upload_dir / reloaded.uploads[upload["upload_id"]]["file"]
        self.assertEqual(saved.read_bytes(), b"offline fake audio")
        persisted = json.loads((reloaded.data_dir / "jobs.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["jobs"][0]["status"], "failed")

    def test_cloud_polish_by_result_id_and_file_reads_remain_scoped(self):
        output = self.server.state.output_dir / "existing"
        output.mkdir()
        (output / "story.json").write_text('{"title":"original"}', encoding="utf-8")
        (output / "story.md").write_text("original", encoding="utf-8")
        result_id = self.server.state.register_result(output)
        status, _, raw = self.request("POST", "/api/jobs", {"mode": "polish", "result_id": result_id}, self.authorized())
        self.assertEqual(status, 202, raw)
        self.assertNotIn("output_path", json.loads(raw)["job"])
        query = urlencode({"result_id": result_id, "name": "story.md"})
        status, _, raw = self.request("GET", "/api/files?" + query, headers=self.authorized())
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"original")
        query = urlencode({"result_id": result_id, "name": "../../.env"})
        status, _, _ = self.request("GET", "/api/files?" + query, headers=self.authorized())
        self.assertEqual(status, 403)

    def test_cloud_resume_by_id_runs_stub_but_direct_path_is_rejected(self):
        output = self.server.state.output_dir / "pending"
        before = partial_result(output)
        result_id = self.server.state.register_result(output)
        status, _, raw = self.request("POST", "/api/jobs", {"mode": "resume", "path": str(output)}, self.authorized())
        self.assertEqual(status, 400, raw)
        status, _, raw = self.request("POST", "/api/jobs", {"mode": "resume", "result_id": result_id}, self.authorized())
        self.assertEqual(status, 202, raw)
        public_job = json.loads(raw)["job"]
        self.assertNotIn("source", public_job)
        self.assertNotIn("output_path", public_job)
        # The queue worker is disabled in cloud fixtures; run this one tiny stub explicitly.
        self.server.state._run_job(public_job["id"])
        self.assertEqual(self.server.state.get_job(public_job["id"])["status"], "completed")
        after = file_hashes(output)
        for name, digest in before.items():
            self.assertEqual(after[name], digest, name)

    def test_cloud_inspect_rejects_local_path_without_registering_it(self):
        output = self.root / "external"
        partial_result(output)
        status, _, raw = self.request("POST", "/api/results/inspect", {"path": str(output)}, self.authorized())
        self.assertEqual(status, 400, raw)
        self.assertNotIn(output.resolve(), self.server.state.results.values())
        self.assertNotIn(str(output).encode(), raw)

    def test_resume_worker_rechecks_original_created_while_queued(self):
        output = self.server.state.output_dir / "pending-race"
        partial_result(output)
        result_id = self.server.state.register_result(output)
        status, _, raw = self.request("POST", "/api/jobs", {"mode": "resume", "result_id": result_id}, self.authorized())
        self.assertEqual(status, 202, raw)
        job_id = json.loads(raw)["job"]["id"]
        (output / "story.json").write_text('{"title":"created elsewhere"}', encoding="utf-8")
        before = file_hashes(output)
        with patch.object(service.subprocess, "Popen") as launch:
            self.server.state._run_job(job_id)
        launch.assert_not_called()
        self.assertEqual(self.server.state.get_job(job_id)["status"], "failed")
        self.assertEqual(file_hashes(output), before)

    def test_active_audio_output_cannot_be_resumed_or_polished_by_another_job(self):
        status, upload = self.upload()
        self.assertEqual(status, 201)
        job = self.server.state.enqueue("audio", upload_id=upload["upload_id"])
        output = Path(job["output_path"])
        partial_result(output)
        self.server.state._update(job["id"], "generating_story", "offline stub active")
        self.assertIn("generating_story", service.ACTIVE)
        status, _, raw = self.request("POST", "/api/jobs", {"mode": "resume", "result_id": job["result_id"]}, self.authorized())
        self.assertEqual(status, 400, raw)
        self.assertIn("排队或处理中", json.loads(raw)["error"])
        (output / "story.json").write_text('{"title":"now original ready"}', encoding="utf-8")
        before = file_hashes(output)
        status, _, raw = self.request("POST", "/api/jobs", {"mode": "polish", "result_id": job["result_id"]}, self.authorized())
        self.assertEqual(status, 400, raw)
        self.assertIn("排队或处理中", json.loads(raw)["error"])
        self.assertEqual(file_hashes(output), before)
        self.assertEqual(len(self.server.state.list_jobs()), 1)


if __name__ == "__main__":
    unittest.main()
