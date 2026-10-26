"""Build the video-maker handoff (视频文案) from an existing result, without any model call.

用法:
    python video_script.py outputs/张奶奶

产出:
    video_script.md    给视频制作者的文案：配音旁白、分镜、可用原声、制作前需确认
    video_script.csv   分镜表（Excel 可直接打开）
    video_script.json  同样内容的结构化数据，供其他程序使用

整理稿通过核对时使用整理稿，否则使用原稿，并在文案开头写明。原稿、整理稿、
转写和音频均不修改。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import uuid

OUTPUT_FILES = ("video_script.md", "video_script.csv", "video_script.json")
SOURCE_FILES = ("story.json", "story_polished.json", "manifest.json", "transcript.json")
CHARS_PER_SECOND = 4.0  # 普通中文配音约每分钟 240 字
MIN_SHOT_SECONDS = 3


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        return None


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _spoken_length(text):
    return sum(1 for c in text if c.isalnum())


def _seconds(text):
    length = _spoken_length(text)
    return max(MIN_SHOT_SECONDS, math.ceil(length / CHARS_PER_SECOND)) if length else 0


def _clock(seconds):
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _normalized(text):
    return "".join(c for c in text if c.isalnum()).casefold()


def _find_in_transcript(quote, transcript):
    """Locate a quote in the recording by text; returns (start, end) seconds or None."""
    target = _normalized(quote)
    rows = [r for r in transcript if isinstance(r, dict) and isinstance(r.get("text"), str)
            and isinstance(r.get("start"), (int, float)) and isinstance(r.get("end"), (int, float))]
    if not target or not rows:
        return None
    parts = [_normalized(r["text"]) for r in rows]
    joined = "".join(parts)
    pos = joined.find(target)
    if pos < 0:
        return None
    first = last = None
    total = 0
    for i, part in enumerate(parts):
        if first is None and pos < total + len(part):
            first = i
        if pos + len(target) <= total + len(part):
            last = i
            break
        total += len(part)
    return rows[first]["start"], rows[last]["end"]


def choose_story(result_dir):
    """Return (story, version) where version is "polished" or "original"."""
    result_dir = Path(result_dir)
    manifest = _read_json(result_dir / "manifest.json")
    polish = manifest.get("polish") if isinstance(manifest, dict) else None
    polished_path = result_dir / "story_polished.json"
    # A failed re-polish keeps the earlier accepted copy; it is still usable when its bytes match.
    accepted = [polish, polish.get("previous_completed")] if isinstance(polish, dict) else []
    accepted = [a for a in accepted if isinstance(a, dict) and a.get("status") == "completed"]
    if accepted and polished_path.is_file():
        digest = hashlib.sha256(polished_path.read_bytes()).hexdigest()
        expected = [(a.get("output_hashes") or {}).get("story_polished.json") for a in accepted]
        if digest in expected or (accepted[0] is polish and not expected[0]):
            story = _read_json(polished_path)
            if isinstance(story, dict) and _text(story.get("first_person_script")):
                return story, "polished"
    story = _read_json(result_dir / "story.json")
    if isinstance(story, dict) and _text(story.get("first_person_script")):
        return story, "original"
    return None, None


def build_video_script(story, version, transcript=None, source_audio=None):
    """Pure function: story dict → handoff dict. No file or network access."""
    transcript = transcript if isinstance(transcript, list) else []
    narration = _text(story.get("first_person_script"))
    shots = []
    scenes = story.get("scenes") if isinstance(story.get("scenes"), list) else []
    for i, scene in enumerate(s for s in scenes if isinstance(s, dict)):
        shots.append({"no": scene.get("no") if isinstance(scene.get("no"), int) else i + 1,
                      "visual": _text(scene.get("visual")),
                      "narration": _text(scene.get("narration")),
                      "mood": _text(scene.get("mood"))})
    if not shots:
        # Without scenes, one shot per narration paragraph keeps the script usable.
        for i, paragraph in enumerate(p for p in narration.split("\n") if p.strip()):
            shots.append({"no": i + 1, "visual": "", "narration": paragraph.strip(), "mood": ""})
    elapsed = 0
    for shot in shots:
        shot["seconds"] = _seconds(shot["narration"])
        shot["start"] = _clock(elapsed)
        elapsed += shot["seconds"]
        shot["end"] = _clock(elapsed)

    clips = []
    for event in story.get("events") if isinstance(story.get("events"), list) else []:
        if not isinstance(event, dict):
            continue
        for quote in event.get("original_quotes") or []:
            if not _text(quote):
                continue
            found = _find_in_transcript(quote, transcript)
            clips.append({"quote": _text(quote), "event": _text(event.get("what")),
                          "start": _clock(math.floor(found[0])) if found else None,
                          "end": _clock(math.ceil(found[1])) if found else None})

    narrator = story.get("narrator") if isinstance(story.get("narrator"), dict) else {}
    people = [p for p in story.get("people") or [] if isinstance(p, dict) and _text(p.get("name"))]
    return {
        "title": _text(story.get("title")) or "老人口述回忆",
        "summary": _text(story.get("summary")),
        "version": version,
        "narrator": {k: _text(v) for k, v in narrator.items() if _text(v)},
        "time_period": _text(story.get("time_period")),
        "locations": [_text(x) for x in story.get("locations") or [] if _text(x)],
        "people": [{"name": _text(p.get("name")), "relation": _text(p.get("relation"))} for p in people],
        "narration": narration,
        "narration_characters": _spoken_length(narration),
        "narration_seconds": math.ceil(_spoken_length(narration) / CHARS_PER_SECOND),
        "shots": shots,
        "shots_seconds": elapsed,
        "source_audio": source_audio,
        "original_voice_clips": clips,
        "to_confirm": [_text(u) for u in story.get("uncertain") or [] if _text(u)],
    }


def to_markdown(script):
    version = ("整理稿（已通过可读性与来源核对）" if script["version"] == "polished"
               else "原稿（整理稿尚未生成或未通过核对，文字可能有识别错误，使用前请通读）")
    lines = [f"# {script['title']}", "", f"> 视频文案 · 依据：{version}",
             "> 内容来自老人口述的自动转写，人名、地名、年份未经外部核实。", ""]
    if script["summary"]:
        lines += [script["summary"], ""]
    lines += ["## 配音旁白（第一人称）", "",
              f"约 {script['narration_characters']} 字，按每分钟 240 字估算约 {_clock(script['narration_seconds'])}。", "",
              script["narration"], "", "## 分镜", "",
              f"共 {len(script['shots'])} 个镜头，旁白合计约 {_clock(script['shots_seconds'])}。"
              "画面描述是创作建议，不是老人原话；时长按旁白字数估算。", ""]
    for shot in script["shots"]:
        lines.append(f"### 镜头 {shot['no']}（{shot['start']}–{shot['end']}，约 {shot['seconds']} 秒）")
        lines.append("")
        lines.append("- 画面：" + (shot["visual"] or "（按旁白自行配画面）"))
        lines.append("- 旁白：" + (shot["narration"] or "（无旁白，可用空镜或音乐）"))
        if shot["mood"]:
            lines.append("- 情绪：" + shot["mood"])
        lines.append("")
    background = []
    if script["narrator"]:
        background.append("- 讲述人：" + "，".join(f"{k}：{v}" for k, v in script["narrator"].items()))
    if script["time_period"]:
        background.append("- 年代：" + script["time_period"])
    if script["locations"]:
        background.append("- 地点：" + "、".join(script["locations"]))
    if script["people"]:
        background.append("- 人物：" + "、".join(p["name"] + (f"（{p['relation']}）" if p["relation"] and p["relation"] != p["name"] else "")
                                               for p in script["people"]))
    if background:
        lines += ["## 背景资料（找素材用）", ""] + background + [""]
    clips = script["original_voice_clips"]
    if clips:
        audio = f"原录音（{script['source_audio']}）" if script["source_audio"] else "原录音"
        lines += ["## 老人原话与原声位置", "",
                  f"可在{audio}的对应时间截取老人原声，穿插在视频里。说话人由软件自动判断，截取前请先听一遍。", ""]
        for clip in clips:
            where = f"{clip['start']}–{clip['end']}" if clip["start"] else "未在转写中找到，需人工查找"
            lines.append(f"- 「{clip['quote']}」（{where}）")
        lines.append("")
    if script["to_confirm"]:
        lines += ["## 制作前请确认", ""] + [f"- {item}" for item in script["to_confirm"]] + [""]
    return "\n".join(lines)


def to_csv(script):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["镜头", "开始", "结束", "时长(秒)", "画面", "旁白", "情绪"])
    for shot in script["shots"]:
        writer.writerow([shot["no"], shot["start"], shot["end"], shot["seconds"],
                         shot["visual"], shot["narration"], shot["mood"]])
    # BOM so that Excel on Windows detects UTF-8 Chinese text.
    return "﻿" + buffer.getvalue()


def _atomic_write(path, data):
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def is_stale(result_dir):
    """True when the handoff is missing or older than any file it is built from."""
    result_dir = Path(result_dir)
    try:
        built = min((result_dir / name).stat().st_mtime_ns for name in OUTPUT_FILES)
    except OSError:
        return True
    sources = [(result_dir / name).stat().st_mtime_ns for name in SOURCE_FILES if (result_dir / name).is_file()]
    return any(mtime > built for mtime in sources)


def write_video_script(result_dir):
    """Write the handoff files; returns the version used, or None when no story exists yet."""
    result_dir = Path(result_dir).resolve()
    story, version = choose_story(result_dir)
    if story is None:
        return None
    transcript = _read_json(result_dir / "transcript.json")
    originals = sorted(p.name for p in result_dir.glob("original.*") if p.is_file())
    script = build_video_script(story, version, transcript, originals[0] if originals else None)
    _atomic_write(result_dir / "video_script.json",
                  (json.dumps(script, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    _atomic_write(result_dir / "video_script.csv", to_csv(script).encode("utf-8"))
    # Markdown last: it is the file readers look for first.
    _atomic_write(result_dir / "video_script.md", to_markdown(script).encode("utf-8"))
    return version


def refresh_video_script(result_dir):
    """Best-effort rebuild used after each pipeline step; never raises."""
    try:
        if is_stale(result_dir):
            return write_video_script(result_dir)
        return "current"
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="从已有结果生成给视频制作者的文案，不调用模型。")
    parser.add_argument("result_dir", help="包含 story.json 的结果目录")
    args = parser.parse_args()
    version = write_video_script(args.result_dir)
    if version is None:
        print("结果目录中还没有可用的原稿（story.json），无法生成视频文案。", flush=True)
        return 2
    label = "整理稿" if version == "polished" else "原稿"
    print(f"视频文案已按{label}生成 → {Path(args.result_dir).resolve() / 'video_script.md'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
