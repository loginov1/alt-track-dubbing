"""Durable jobs, schemas, provenance and review receipts. No service credentials here."""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

RATE = 48000
RUNTIME = Path(os.environ.get("ALT_TRACK_DUBBING_HOME", Path.home()/".local/share/alt-track-dubbing"))
LOCALES = {
    "es": "es-ES", "spanish": "es-ES",
    "fr": "fr-FR", "french": "fr-FR",
    "de": "de-DE", "german": "de-DE",
    "ca": "ca-ES", "catalan": "ca-ES",
    "ko": "ko-KR", "korean": "ko-KR",
    "zh": "cmn-CN", "zh-cn": "cmn-CN", "cmn": "cmn-CN", "chinese": "cmn-CN",
    "ar": "ar-XA", "arabic": "ar-XA",
    "en": "en-US", "pt": "pt-BR", "ja": "ja-JP", "it": "it-IT", "nl": "nl-NL",
}

def locale(value):
    value = value.strip()
    value = LOCALES.get(value.lower(), value)
    if not re.fullmatch(r"[a-z]{2,3}-[A-Z]{2}", value):
        raise ValueError(f"Use an explicit Google locale, e.g. es-ES: {value!r}")
    return value

def asr_language(value):
    return "zh" if value.startswith("cmn") else value.split("-")[0]

def now():
    return datetime.now(timezone.utc).isoformat()

def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()

def sha(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()

def run(args, **kwargs):
    # Never invoke a shell; paths and text are always arguments, never executable code.
    return subprocess.run([str(x) for x in args], check=True, capture_output=True, **kwargs)

def say(message):
    print(message, flush=True)

@contextlib.contextmanager
def lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/".dubbing.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Another dubbing command is writing {directory}") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def probe(path):
    data = json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                           "-of", "json", path]).stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    if audio is None:
        raise ValueError("The input has no audio stream")
    if not video:
        raise ValueError("A video is required to establish the delivery timeline")
    start = float(video.get("start_time", data["format"].get("start_time", 0)))
    duration = video.get("duration")
    if duration is None and "duration_ts" in video:
        from fractions import Fraction
        duration = float(int(video["duration_ts"])*Fraction(video["time_base"]))
    if duration is None:
        duration = float(data["format"]["duration"])-start
    duration = float(duration)
    if duration <= 0:
        raise ValueError("Invalid video duration")
    return {"duration": duration, "frames": round(duration*RATE), "sample_rate": RATE,
            "video_start": start, "audio_start": float(audio.get("start_time", 0)),
            "audio_stream_index": audio["index"], "video_stream_index": video["index"]}

def init_job(video, languages, out=None, source_language="auto", max_usd=5.):
    video = Path(video).expanduser().resolve(strict=True)
    say("Hashing source video (once)")
    fingerprint = sha(video)
    name = re.sub(r"[^\w.-]+", "-", video.stem, flags=re.UNICODE).strip("-")[:80]
    out = Path(out).expanduser().resolve() if out else video.parent/"Dubbing"/f"{name}-{fingerprint[:8]}"
    with lock(out):
        path = out/"job.json"
        if path.exists():
            job = load(path)
            if job["source"]["sha256"] != fingerprint:
                raise ValueError("Job directory belongs to another video; choose a new directory")
            job["languages"] = list(dict.fromkeys(job["languages"]+[locale(x) for x in languages]))
        else:
            stat = video.stat()
            job = {"schema": 1, "created_at": now(), "name": name,
                   "source": {"path": str(video), "sha256": fingerprint,
                              "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, **probe(video)},
                   "source_language": asr_language(source_language),
                   "languages": list(dict.fromkeys(locale(x) for x in languages)),
                   "settings": {"max_usd": max_usd, "chirp_usd_per_million": 30.,
                                "human_max_tempo": 1.16, "google_max_tempo": 1.25,
                                "target_lufs": -17.5, "true_peak_db": -1.5}}
        for child in ["analysis", "cache", "recordings", "languages", "deliverables"]:
            (out/child).mkdir(exist_ok=True)
        save(path, job)
    with lock(RUNTIME):
        index_path = RUNTIME/"jobs.json"
        index = load(index_path) if index_path.exists() else {}
        index[str(out)] = {"name": name, "source": str(video), "updated_at": now()}
        save(index_path, index)
    return str(out)

def job_data(job):
    return load(Path(job)/"job.json")

def lang_dir(job, lang):
    path = Path(job)/"languages"/locale(lang)
    path.mkdir(parents=True, exist_ok=True)
    return path

def interval_union(intervals, gap=0.):
    result = []
    for a, b in sorted((float(a), float(b)) for a, b in intervals if b > a):
        if result and a <= result[-1][1]+gap:
            result[-1][1] = max(result[-1][1], b)
        else:
            result.append([a, b])
    return result

def minus(intervals, cuts):
    result = [list(x) for x in intervals]
    for lo, hi in cuts:
        updated = []
        for a, b in result:
            if hi <= a or lo >= b:
                updated.append([a, b])
            else:
                if a < lo: updated.append([a, lo])
                if b > hi: updated.append([hi, b])
        result = updated
    return result

def contains(intervals, a, b, tolerance=1/RATE):
    return any(a >= lo-tolerance and b <= hi+tolerance for lo, hi in intervals)

def source_payload(job):
    root = Path(job)/"analysis"
    return {"source_sha256": job_data(job)["source"]["sha256"],
            "regions": load(root/"regions.json"), "cues": load(root/"cues.json")}

def source_digest(job, require_review=True):
    value = digest(source_payload(job))
    receipt = Path(job)/"analysis/source-reviewed.json"
    if require_review and (not receipt.exists() or load(receipt).get("digest") != value):
        raise ValueError("Source changed or is unreviewed. Review regions/cues and run accept-source.")
    return value

def validate_source(job):
    data = source_payload(job)
    duration = job_data(job)["source"]["frames"]/RATE
    regions = data["regions"]["dialogue"]
    if regions != interval_union(regions):
        raise ValueError("Dialogue intervals must be sorted and disjoint")
    for a, b in regions:
        if not 0 <= a < b <= duration:
            raise ValueError("Dialogue interval outside video")
    music = data["regions"].get("preserve", [])
    for a, b in music:
        if not 0 <= a < b <= duration or any(a < d and b > c for c, d in regions):
            raise ValueError("Preserved region overlaps dialogue or exceeds the video")
    cues = data["cues"]["cues"]
    if not cues:
        raise ValueError("No dialogue cues; confirm a music-only video separately")
    ids = set(); previous = 0
    for c in cues:
        if str(c["id"]) in ids or not str(c["text"]).strip():
            raise ValueError("Duplicate cue id or empty source text")
        ids.add(str(c["id"]))
        if c["start"] < previous-1/RATE or c["end"] <= c["start"]:
            raise ValueError(f"Overlapping or invalid source cue {c['id']}")
        if not contains(regions, c["start"], c["end"]):
            raise ValueError(f"Cue {c['id']} is not inside a dialogue episode")
        previous = c["end"]
    return data

def accept_source(job, note):
    if not note.strip(): raise ValueError("Record the source review findings")
    data = validate_source(job)
    save(Path(job)/"analysis/source-reviewed.json", {"digest": digest(data), "note": note, "at": now()})

def make_blocks(cues, regions, max_seconds=25.):
    blocks = []
    for c in cues:
        episode = next(i for i, (a, b) in enumerate(regions) if contains([[a, b]], c["start"], c["end"]))
        if (blocks and blocks[-1]["speaker"] == c.get("speaker", "A")
                and blocks[-1]["episode"] == episode
                and c["start"]-blocks[-1]["end"] <= 1.5
                and c["end"]-blocks[-1]["start"] <= max_seconds):
            blocks[-1]["cue_ids"].append(str(c["id"]))
            blocks[-1]["end"] = c["end"]
        else:
            blocks.append({"id": f"B{len(blocks)+1:03d}", "speaker": c.get("speaker", "A"),
                           "episode": episode, "start": c["start"], "end": c["end"],
                           "cue_ids": [str(c["id"])]})
    for i, b in enumerate(blocks):
        hi = regions[b["episode"]][1]
        if i+1 < len(blocks): hi = min(hi, blocks[i+1]["start"]-.025)
        b["end"] = min(hi, b["end"]+.25)
    return blocks

def translation_template(job, lang, mode="auto"):
    source = source_digest(job)
    lang = locale(lang)
    p = lang_dir(job, lang)/"translation.json"
    if p.exists():
        old = load(p)
        if old.get("source_digest") == source: return str(p)
        backup = p.with_name(f"translation-stale-{digest(old)[:10]}.json")
        if not backup.exists(): shutil.copy2(p, backup)
        raise ValueError(f"Source changed: reconcile existing translation explicitly: {p}")
    cues = load(Path(job)/"analysis/cues.json")["cues"]
    save(p, {"locale": lang, "source_digest": source, "mode": mode,
             "pronunciation_notes": [], "alignment_aliases": {},
             "cues": [{"id": str(c["id"]), "source_text": c["text"], "speaker": c.get("speaker", "A"),
                       "available_seconds": round(c["end"]-c["start"], 3),
                       "text": "", "tts_text": "", "human_text": ""} for c in cues]})
    return str(p)

def translation_data(job, lang):
    t = load(lang_dir(job, lang)/"translation.json")
    if t["locale"] != locale(lang) or t["source_digest"] != source_digest(job):
        raise ValueError("Translation locale/source is stale")
    if t.get("mode") not in ("auto", "google", "human"):
        raise ValueError("Translation mode must be auto, google or human")
    expected = [str(c["id"]) for c in load(Path(job)/"analysis/cues.json")["cues"]]
    if [str(c["id"]) for c in t["cues"]] != expected:
        raise ValueError("Translation must cover every cue exactly once, in source order")
    for c in t["cues"]:
        if not c.get("text", "").strip(): raise ValueError(f"Empty translation: {c['id']}")
        for field in ("text", "tts_text", "human_text"):
            if not isinstance(c.get(field, ""), str): raise ValueError("Translation fields must be strings")
    return t

def stamp(seconds):
    ms = round(seconds*1000)
    return f"{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}"

def status(job):
    root = Path(job); data = job_data(root)
    result = {"job": str(root), "source": data["source"]["path"], "languages": {}}
    try: source_digest(root); result["source_reviewed"] = True
    except (ValueError, FileNotFoundError): result["source_reviewed"] = False
    for lang in data["languages"]:
        p = root/"languages"/lang
        result["languages"][lang] = {x: (p/x).exists() for x in ["translation.json", "reading-script.txt",
            "edits.json", "edits-reviewed.json", "render.json", "qa-asr.json", "release.json"]}
        release = p/"release.json"
        if release.exists():
            try:
                from .audio import require_render
                from .qa import require_qa
                rendered = require_render(root,lang)
                quality = require_qa(root,lang,rendered)
                receipt = load(release)
                result["languages"][lang]["release_current"] = (receipt["render_digest"] == digest(rendered)
                    and receipt["translation_digest"] == digest(translation_data(root, lang))
                    and receipt.get("qa_digest") == digest(quality))
            except (ValueError, FileNotFoundError): result["languages"][lang]["release_current"] = False
    ledger = root/"cloud-ledger.json"
    if ledger.exists(): result["reserved_usd_upper_bound"] = sum(r["reserved_usd"] for r in load(ledger)["attempts"])
    return result
