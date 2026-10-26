"""Export a clearly labelled, unapproved model candidate without any API call."""
import argparse
import json
from pathlib import Path

import polish


def export_preview(directory):
    directory = Path(directory).resolve()
    with polish._ResultLock(directory):
        manifest = polish._read_json(directory / "manifest.json")
        state = manifest.get("polish", {})
        if state.get("status") != "failed":
            raise ValueError("Only failed runs have an unapproved preview; use the formal output after success.")
        original = polish._read_json(directory / "story.json")
        transcript = polish._read_json(directory / "transcript.json")
        candidate = None
        for relative in reversed(state.get("diagnostic_files", [])):
            path = (directory / relative).resolve()
            if not path.is_relative_to((directory / "polish_diagnostics").resolve()):
                continue
            diagnostic = polish._read_json(path)
            if not isinstance(diagnostic.get("candidate_draft"), dict):
                continue
            polish._verify_snapshot(directory, diagnostic["input_hashes"])
            try:
                checked = polish.validate_draft(original, transcript, diagnostic["candidate_draft"])
            except polish.PolishValidationError:
                continue
            candidate = (relative, diagnostic, checked)
            break
        if candidate is None:
            raise ValueError("No structurally valid saved candidate is available.")
        relative, diagnostic, checked = candidate
        revised = checked["story"]
        note = "整理预览：尚未通过最终核对，不是正式完成稿。以下文字来自软件保存的模型候选，未经聊天助手手工改写。"
        lines = ["# " + note.split("：")[0], "", "> " + note, "", "> " + polish.DISCLAIMER,
                 "", "当前任务状态：" + state.get("status", "unknown"),
                 "", "候选来源：" + relative, "", "## 候选标题", "", revised.get("title", ""),
                 "", "## 候选摘要", "", revised.get("summary", ""),
                 "", "## 候选正文", "", revised["first_person_script"],
                 "", "## 本轮尚未解决的问题", ""]
        issues = diagnostic.get("validation_issues", [])
        for issue in issues:
            if isinstance(issue, dict):
                lines.append("- " + issue.get("reason", "待核对"))
        lines += ["", "模型接口中断时，较新的候选可能尚未保存；此处仅展示已有诊断中的候选。",
                  "正式原稿、转写和声音素材仍在原目录；补足模型余额后，可对该目录单独重新整理，无需重新识别音频。"]
        record = {"status": "unapproved_preview", "disclaimer": note,
                  "diagnostic_file": relative, "input_hashes": diagnostic["input_hashes"],
                  "story": revised, "paragraphs": checked["paragraphs"],
                  "validation_issues": issues}
        (directory / "review_preview.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (directory / "review_preview.json").write_bytes(polish._json_bytes(record))
        return {"status": "unapproved_preview", "preview": str(directory / "review_preview.md")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_dir")
    print(json.dumps(export_preview(parser.parse_args().result_dir), ensure_ascii=False))
