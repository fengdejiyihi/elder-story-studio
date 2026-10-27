"""Create a source-only deployment ZIP using an explicit allowlist."""
from pathlib import Path
import hashlib
import json
import zipfile


def main():
    root = Path(__file__).resolve().parent
    destination = root.parent / "elder-audio-service-cloud.zip"
    names = ["recorder.py", "polish.py", "service.py", "make_release.py", "export_preview.py", "video_script.py", "llm.py",
             "requirements.txt", "hotwords.txt", "README.md", "API.md", "CLOUD_DEPLOY.md",
             "Dockerfile", "compose.yaml", ".dockerignore", ".gitignore",
             ".env.example", ".env.cloud.example", "web/index.html", "web/submit.html",
             "start_service.ps1", "stop_service.ps1", "启动服务.bat", "停止服务.bat"]
    names += [str(p.relative_to(root)).replace("\\", "/") for p in sorted((root / "tests").glob("test_*.py"))]
    missing = [name for name in names if not (root / name).is_file()]
    if missing:
        raise FileNotFoundError(str(missing))
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(root / name, "elder-audio-service/" + name)
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == len(names)
        assert not any(Path(name).name in {".env", ".env.cloud", "runtime.json"} for name in archive.namelist())
    print(json.dumps({"archive": str(destination), "files": len(names), "bytes": destination.stat().st_size,
                      "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
