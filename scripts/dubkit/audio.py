"""Sample-accurate extraction, conservative editing, fitting and full-length mixing."""
import csv
import json
import math
import os
import tempfile
from pathlib import Path
import numpy as np
import soundfile as sf
from .core import (RATE, contains, digest, interval_union, job_data, lang_dir, load,
                   minus, now, run, save, say, sha, source_digest, stamp, translation_data)

def extract(job):
    root = Path(job); data = job_data(root); source = data["source"]
    path = Path(source["path"])
    stat = path.stat()
    if stat.st_size != source["size"] or stat.st_mtime_ns != source["mtime_ns"]:
        if sha(path) != source["sha256"]: raise ValueError("Source video changed")
    pcm = root/"cache/original-48k-stereo.wav"
    receipt = root/"cache/extraction.json"
    fingerprint = digest(source)
    if not (pcm.exists() and receipt.exists() and load(receipt).get("source_digest") == fingerprint):
        say("Extracting the original audio on the video timeline")
        tmp = pcm.with_suffix(".partial.wav")
        # copyts keeps non-zero audio offsets. first_pts fills leading silence or trims pre-roll.
        filters = (f"asetpts=PTS-({source['video_start']})/TB,aresample={RATE}:async=1:first_pts=0,"
                   f"apad,atrim=end_sample={source['frames']}")
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-copyts", "-i", path,
             "-map", f"0:{source['audio_stream_index']}", "-vn", "-af", filters,
             "-ac", "2", "-ar", RATE, "-c:a", "pcm_s16le", tmp])
        info = sf.info(tmp)
        if info.frames != source["frames"]: raise ValueError("Extraction did not match the video sample count")
        os.replace(tmp, pcm)
        save(receipt, {"source_digest": fingerprint, "frames": info.frames, "sha256": sha(pcm)})
    analysis = root/"cache/analysis-16k-mono.wav"
    if not analysis.exists():
        convert(pcm, analysis, 16000)
    return str(pcm)

def convert(source, dest, rate=RATE):
    dest = Path(dest); dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".partial.wav")
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", source, "-vn", "-ac", "1",
         "-ar", rate, "-c:a", "pcm_f32le", temp])
    os.replace(temp, dest)

def runs(mask):
    d = np.diff(np.r_[False, mask, False].astype(np.int8))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))

def rms_frames(audio, step=.005):
    n = max(1, round(step*RATE))
    if len(audio) < n: return np.array([np.sqrt(np.mean(audio**2))]), n
    return np.sqrt(np.mean(audio[:len(audio)//n*n].reshape(-1, n)**2, axis=1)), n

def quiet_ranges(audio, ranges, trim=True, shorten=True):
    energy, n = rms_frames(audio); floor = 10**(-52/20)
    result = []; cuts = []
    for lo, hi in ranges:
        a = max(0, int(lo*RATE/n)); b = min(len(energy), math.ceil(hi*RATE/n))
        if b <= a: continue
        active = np.flatnonzero(energy[a:b] > floor)
        if not len(active): raise ValueError("A selected range contains no audible signal")
        if trim:
            lo = max(lo, (a+int(active[0]))*n/RATE-.055)
            hi = min(hi, (a+int(active[-1])+1)*n/RATE+.09)
        local = []
        if shorten:
            for x, y in runs(energy[a:b] < floor):
                start = (a+x)*n/RATE; end = (a+y)*n/RATE
                if end-start >= .52 and start > lo+.08 and end < hi-.08:
                    local.append([start+.12, end-.12])
        result.extend(minus([[lo, hi]], local)); cuts.extend(local)
    return result, cuts

def tts_range(audio):
    """Chirp's leading padding can contain a low noise floor rather than digital silence."""
    energy,n=rms_frames(audio,.01)
    threshold=max(.00006,float(energy.max(initial=0))*.015)
    active=np.flatnonzero(energy>threshold)
    if not len(active): raise ValueError("Synthesized speech is empty")
    return [[max(0.,active[0]*n/RATE-.05),min(len(audio)/RATE,(active[-1]+1)*n/RATE+.10)]]

def atempo(audio, factor):
    if not .5 <= factor <= 2.: raise ValueError("Tempo must be between .5 and 2")
    if abs(factor-1) < 1e-6: return audio.copy()
    value = run(["ffmpeg", "-nostdin", "-v", "error", "-f", "f32le", "-ar", RATE,
                 "-ac", "1", "-i", "pipe:0", "-af", f"atempo={factor:.9f}",
                 "-f", "f32le", "pipe:1"], input=audio.astype("<f4").tobytes())
    return np.frombuffer(value.stdout, dtype="<f4").copy()

def fade(audio, left=True, right=True):
    n = min(round(.007*RATE), len(audio)//2)
    if n:
        if left: audio[:n] *= np.linspace(0, 1, n)
        if right: audio[-n:] *= np.linspace(1, 0, n)
    return audio

def pava(values):
    pools = []
    for v in values:
        pools.append([float(v), 1])
        while len(pools)>1 and pools[-2][0]>pools[-1][0]:
            b, nb = pools.pop(); a, na = pools.pop()
            pools.append([(a*na+b*nb)/(na+nb), na+nb])
    return np.array([v for v, n in pools for _ in range(n)])

def fit_group(clips, cues, lo, hi, max_tempo, min_tempo=1.):
    """Keep anchors where possible, make adjacent connected phrases stay connected."""
    from scipy.optimize import Bounds, LinearConstraint, minimize
    gaps = np.array([0. if c.get("continuous_next") else .035 for c in cues[:-1]])
    available = hi-lo-float(gaps.sum())-.02
    if available <= 0: raise ValueError("Empty time slot")
    unscaled = sum(len(c)/RATE for c in clips)
    tempo = max(min_tempo, unscaled/available)
    def fit(factor):
        fitted = []; i = 0
        while i < len(clips):
            j = i+1
            while j < len(clips) and cues[j-1].get("continuous_next"): j += 1
            source = np.concatenate(clips[i:j])
            joined = atempo(source, factor)
            edges = np.round(np.r_[0, np.cumsum([len(x) for x in clips[i:j]])]*len(joined)/len(source)).astype(int)
            fitted.extend(joined[a:b].copy() for a, b in zip(edges, edges[1:]))
            i = j
        return fitted
    if tempo > max_tempo: raise ValueError(f"Slot {lo:.2f}-{hi:.2f} needs {tempo:.3f}x; shorten text or review pauses/take")
    fitted = fit(tempo)
    if sum(len(c)/RATE for c in fitted) > available:
        tempo *= (sum(len(c)/RATE for c in fitted)+.03)/available
        if tempo > max_tempo: raise ValueError("Fitted clip needs a shorter translation or a different take")
        fitted = fit(tempo)
    durations = np.array([len(c)/RATE for c in fitted])
    cum = np.r_[0, np.cumsum(durations[:-1]+gaps)]
    last = hi-float(durations.sum())-float(gaps.sum())
    if last < lo: raise ValueError("Fitted audio exceeds its slot")
    anchors = np.array([c["anchor"] for c in cues])
    starts = np.clip(pava(anchors-cum), lo, last)+cum
    if len(cues)>1 and any(c.get("continuous_next") or c.get("linked_next") for c in cues[:-1]):
        matrix = np.zeros((len(cues)-1, len(cues)))
        upper = []
        for j, c in enumerate(cues[:-1]):
            matrix[j, j] = -1; matrix[j, j+1] = 1
            upper.append(durations[j]+(0 if c.get("continuous_next") else .45 if c.get("linked_next") else np.inf))
        opt = minimize(lambda x: float(np.sum((x-anchors)**2)), starts, jac=lambda x: 2*(x-anchors),
                       method="SLSQP", bounds=Bounds(np.full(len(cues), lo), hi-durations),
                       constraints=[LinearConstraint(matrix, durations[:-1]+gaps, upper)],
                       options={"maxiter": 200, "ftol": 1e-10})
        if not opt.success: raise ValueError(f"Cannot fit connected clauses: {opt.message}")
        starts = opt.x
    return fitted, np.round(starts*RATE)/RATE, tempo

def edits_payload(job, lang):
    t = translation_data(job, lang)
    e = load(lang_dir(job, lang)/"edits.json")
    if e["source_digest"] != source_digest(job) or e["translation_digest"] != digest(t):
        raise ValueError("Edit decisions are stale after source/translation changes")
    expected = [str(c["id"]) for c in t["cues"]]
    if [str(c["id"]) for c in e["cues"]] != expected: raise ValueError("Edit decisions must cover all cues in order")
    regions = load(Path(job)/"analysis/regions.json")["dialogue"]
    blocks = {b["id"]: b for b in e["blocks"]}
    if len(blocks) != len(e["blocks"]): raise ValueError("Duplicate block")
    previous = 0
    for b in e["blocks"]:
        if b["start"] < previous-1/RATE or not contains(regions, b["start"], b["end"]):
            raise ValueError("Overlapping blocks or block outside dialogue")
        previous = b["end"]
    files = {}
    used = {}
    for i, c in enumerate(e["cues"]):
        if c["block"] not in blocks: raise ValueError("Missing block")
        b = blocks[c["block"]]
        if not b["start"] <= c["anchor"] < b["end"]: raise ValueError("Cue anchor is outside its block")
        path = Path(c["file"])
        if not path.is_absolute(): path = Path(job)/path
        path = path.resolve(strict=True)
        key = str(path)
        if key not in files:
            info = sf.info(path)
            if info.samplerate != RATE or info.channels != 1: raise ValueError("Edit sources must be 48 kHz mono WAV")
            files[key] = {"sha256": sha(path), "duration": info.duration}
        ranges = c["source_ranges"]
        if not ranges: raise ValueError(f"No selected audio for cue {c['id']}")
        for a, z in ranges:
            if not 0 <= a < z <= files[key]["duration"]+1/RATE: raise ValueError("Source range outside recording")
            for x, y in used.get(key, []):
                if a < y-1/RATE and z > x+1/RATE: raise ValueError("Selected source audio reused in multiple cues")
            used.setdefault(key, []).append([a, z])
        if c.get("continuous_next"):
            if i+1 == len(e["cues"]): raise ValueError("Last cue cannot continue")
            following = e["cues"][i+1]
            if following["block"] != c["block"] or following["file"] != c["file"]:
                raise ValueError("Continuous clauses must share a source and a time block")
            if abs(ranges[-1][1]-following["source_ranges"][0][0]) > 1/RATE:
                raise ValueError("Continuous clauses must share the exact source cut")
    return e, files

def accept_edits(job, lang, note):
    if not note.strip(): raise ValueError("Record the take/content/edge review findings")
    e, files = edits_payload(job, lang)
    save(lang_dir(job, lang)/"edits-reviewed.json", {"digest": digest(e), "files": files, "note": note, "at": now()})

def require_edits(job, lang):
    e, files = edits_payload(job, lang)
    path = lang_dir(job, lang)/"edits-reviewed.json"
    if not path.exists(): raise ValueError("Review selected takes and run accept-edits")
    r = load(path)
    # Archived jobs may remain reachable through their original symlinked paths.
    # Compare canonical file identities while preserving the original review receipt.
    reviewed_files = {str(Path(path).resolve(strict=True)): info for path, info in r["files"].items()}
    if (r["digest"] != digest(e) or len(reviewed_files) != len(r["files"])
            or reviewed_files != files):
        raise ValueError("Selected audio/edits changed since review")
    return e

def render_fingerprint(job, lang, e):
    receipt=load(lang_dir(job, lang)/"edits-reviewed.json")
    settings=job_data(job)["settings"]
    settings={key:settings[key] for key in ["human_max_tempo","google_max_tempo","target_lufs","true_peak_db"]}
    return digest({"edits": e, "settings": settings, "engine": 1,
                   "files": receipt["files"]})

def require_render(job, lang):
    e = require_edits(job, lang)
    value = load(lang_dir(job, lang)/"render.json")
    if value.get("fingerprint") != render_fingerprint(job, lang, e):
        raise ValueError("Rendered audio is stale after edit/settings changes; render again")
    return value

def master(source, dest, settings):
    filters = "pan=stereo|c0=c0|c1=c0,highpass=f=65,acompressor=threshold=0.05:ratio=2:attack=8:release=150:knee=2.828:makeup=1"
    target = f"I={settings['target_lufs']}:TP={settings['true_peak_db']}:LRA=7"
    base = ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-i", source]
    first = run(base+["-af", filters+",loudnorm="+target+":print_format=json", "-f", "null", "-"], text=True)
    m = json.loads(first.stderr[first.stderr.rfind("{"):first.stderr.rfind("}")+1])
    if not math.isfinite(float(m["input_i"])): raise ValueError("Voice timeline is silent")
    filt = (f"{filters},loudnorm={target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
            f"offset={m['target_offset']}:linear=true:print_format=json")
    second = run(base+["-af", filt, "-ar", RATE, "-c:a", "pcm_f32le", dest], text=True)
    actual = json.loads(second.stderr[second.stderr.rfind("{"):second.stderr.rfind("}")+1])
    return {"measurement": m, "result": actual}

def mix_exact(original, voice, regions, placements, output, total):
    """Original PCM is copied bit-for-bit outside complete dialogue episodes."""
    iv = [(round(a*RATE), round(b*RATE)) for a, b in regions]
    pv = [(round(c["output_start"]*RATE), round(c["output_end"]*RATE)) for c in placements]
    preserved = 0; voice_peak = 0; total_peak = 0
    with sf.SoundFile(original) as src, sf.SoundFile(voice) as dub, sf.SoundFile(
            output, "w", samplerate=RATE, channels=2, subtype="PCM_16") as dst:
        if src.samplerate != RATE or src.channels != 2 or len(src) < total: raise ValueError("Invalid original PCM")
        if dub.samplerate != RATE or dub.channels != 2 or abs(len(dub)-total)>1: raise ValueError("Master changed the duration")
        position = 0
        while position < total:
            n = min(RATE*15, total-position)
            original_block = src.read(n, dtype="int16", always_2d=True)
            block = original_block.copy()
            v = dub.read(n, dtype="float32", always_2d=True)
            if len(v) < n: v = np.pad(v, ((0, n-len(v)), (0, 0)))
            active = np.zeros(n, dtype=bool)
            for a, b in pv:
                lo=max(a-position,0); hi=min(b-position,n)
                if hi>lo: active[lo:hi] = True
            v[~active] = 0
            if np.max(np.abs(v), initial=0) >= .999: raise ValueError("Mastered voice clips")
            new = np.round(v*32768).astype(np.int16)
            mask = np.zeros(n, dtype=bool)
            for a, b in iv:
                lo=max(a-position,0); hi=min(b-position,n)
                if hi>lo: mask[lo:hi] = True
            block[mask] = new[mask]
            if not np.array_equal(block[~mask], original_block[~mask]): raise AssertionError("Music changed")
            if np.count_nonzero(block[mask & ~active]): raise AssertionError("Original speech leaked between cues")
            preserved += int((~mask).sum())
            voice_peak = max(voice_peak, int(np.abs(block[mask].astype(np.int32)).max(initial=0)))
            total_peak = max(total_peak, int(np.abs(block.astype(np.int32)).max(initial=0)))
            dst.write(block); position += n
    if sf.info(output).frames != total: raise AssertionError("Wrong full-track duration")
    # Read the written WAV back to verify all preserved samples, not only the in-memory mix.
    with sf.SoundFile(original) as src, sf.SoundFile(output) as dst:
        position = 0
        while position < total:
            n = min(RATE*15, total-position)
            a = src.read(n, dtype="int16", always_2d=True); b = dst.read(n, dtype="int16", always_2d=True)
            mask = np.ones(n, dtype=bool)
            for x, y in iv:
                lo=max(x-position,0); hi=min(y-position,n)
                if hi>lo: mask[lo:hi] = False
            if not np.array_equal(a[mask], b[mask]): raise AssertionError("Preserved WAV PCM differs")
            position += n
    return {"exact_video_frames": total, "sample_rate": RATE, "duration_seconds": total/RATE,
            "original_pcm_preserved_seconds": preserved/RATE, "preserved_samples_equal": True,
            "original_audio_inside_dialogue_used": False, "between_cue_nonzero_samples": 0,
            "voice_peak_dbfs": 20*math.log10(max(voice_peak, 1)/32768),
            "full_track_peak_dbfs": 20*math.log10(max(total_peak, 1)/32768)}

def render(job, lang):
    root = Path(job); p = lang_dir(root, lang); data = job_data(root)
    e = require_edits(root, lang); regions = load(root/"analysis/regions.json")["dialogue"]
    settings = data["settings"]; total = data["source"]["frames"]
    fingerprint = render_fingerprint(job, lang, e)
    out = root/"deliverables"/lang/fingerprint[:12]; out.mkdir(parents=True, exist_ok=True)
    existing = p/"render.json"
    if existing.exists():
        result = load(existing)
        if (result.get("fingerprint") == fingerprint and all(Path(x).exists() for x in result["outputs"].values())
                and sha(result["outputs"]["wav"]) == result["checks"]["wav_sha256"]):
            say("Rendered files already match these inputs"); return result
    work = p/"render-work"; work.mkdir(exist_ok=True)
    timeline = np.memmap(work/"voice-timeline.f32", mode="w+", dtype="float32", shape=(total,))
    timeline[:] = 0; placed = []; details = []
    cues = e["cues"]; seen = set()
    for block in e["blocks"]:
        group = [c for c in cues if c["block"] == block["id"]]
        if not group: raise ValueError("Empty reading block")
        clips = []
        for i, c in enumerate(group):
            path = Path(c["file"])
            if not path.is_absolute(): path = root/path
            info = sf.info(path)
            # Read only the selected region plus its internal gaps.
            lo = min(a for a, b in c["source_ranges"]); hi = max(b for a, b in c["source_ranges"])
            start_frame = round(lo*RATE); end_frame = round(hi*RATE)
            audio, sr = sf.read(path, start=start_frame, stop=end_frame, dtype="float32")
            shifted = [[a-start_frame/RATE, b-start_frame/RATE] for a, b in c["source_ranges"]]
            previous_cont = i>0 and group[i-1].get("continuous_next")
            # Continuous boundaries may be inside a consonant; never trim or fade them independently.
            ranges, cuts = quiet_ranges(audio, shifted, trim=c.get("trim_edges", True) and not(previous_cont or c.get("continuous_next")),
                                        shorten=c.get("shorten_pauses", True))
            chunks = []
            for j, (a, b) in enumerate(ranges):
                piece = audio[round(a*RATE):round(b*RATE)].copy()
                fade(piece, left=j>0 or not previous_cont, right=j+1<len(ranges) or not c.get("continuous_next"))
                chunks.append(piece)
            if not chunks: raise ValueError(f"No audio after editing cue {c['id']}")
            clips.append(np.concatenate(chunks))
            details.append({"id": c["id"], "selected_ranges": [[a+start_frame/RATE,b+start_frame/RATE] for a,b in ranges],
                            "removed_quiet_ranges": [[a+start_frame/RATE,b+start_frame/RATE] for a,b in cuts]})
        max_tempo = settings["human_max_tempo" if e["mode"] == "human" else "google_max_tempo"]
        fitted, starts, tempo = fit_group(clips, group, block["start"], block["end"], max_tempo,
                                         min_tempo=float(block.get("preferred_tempo", 1.)))
        for c, clip, start in zip(group, fitted, starts):
            a = round(float(start)*RATE); b = a+len(clip)
            if a < 0 or b > total or (placed and a < round(placed[-1]["output_end"]*RATE)):
                raise ValueError("Overlapping fitted audio or audio outside video")
            if not contains(regions, a/RATE, b/RATE): raise ValueError("Fitted speech overlaps preserved music")
            timeline[a:b] = clip
            placed.append({"id": c["id"], "block": c["block"], "text": c["text"],
                           "output_start": a/RATE, "output_end": b/RATE, "tempo": tempo,
                           "continuous_next": bool(c.get("continuous_next"))})
            seen.add(str(c["id"]))
    if seen != {str(c["id"]) for c in cues}: raise ValueError("Not all cues were rendered")
    raw = work/"voice-raw.wav"; mastered = work/"voice-mastered.wav"
    sf.write(raw, timeline, RATE, subtype="FLOAT"); del timeline
    say("Mastering voice only; preserving original audio outside dialogue")
    mastering = master(raw, mastered, settings)
    stem = f"{data['name']}-{lang}-{e['mode']}"
    wav = out/f"{stem}-full.wav"; mp3 = wav.with_suffix(".mp3")
    checks = mix_exact(root/"cache/original-48k-stereo.wav", mastered, regions, placed, wav, total)
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", wav, "-c:a", "libmp3lame", "-b:a", "192k", mp3])
    # Verify decoder delay/gapless metadata by decoding the complete MP3.
    decoded = work/"mp3-check.wav"
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", mp3, "-c:a", "pcm_s16le", decoded])
    checks["mp3_decoded_frames"] = sf.info(decoded).frames
    decoded.unlink()
    if checks["mp3_decoded_frames"] != total: raise ValueError("MP3 encoder delay is not compensated")
    preview = out/f"{stem}-preview.mp3"
    preview_start = max(0, regions[0][0]-2)
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", preview_start, "-i", wav,
         "-t", min(90, total/RATE-preview_start), "-c:a", "libmp3lame", "-b:a", "192k", preview])
    srt = out/f"{stem}.srt"
    srt.write_text("\n".join(f"{i+1}\n{stamp(c['output_start'])} --> {stamp(c['output_end'])}\n{c['text']}\n" for i,c in enumerate(placed)), encoding="utf-8")
    save(out/"edit-decisions.json", e)
    with (out/"edit-list.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f); writer.writerow(["cue", "start", "end", "tempo", "text"])
        writer.writerows([[c["id"],c["output_start"],c["output_end"],c["tempo"],c["text"]] for c in placed])
    checks.update({"cue_count": len(placed), "wav_sha256": sha(wav), "mastering": mastering,
                   "tempo_min": min(c["tempo"] for c in placed), "tempo_max": max(c["tempo"] for c in placed)})
    save(out/"checks.json", checks)
    result = {"fingerprint": fingerprint, "created_at": now(), "checks": checks, "placements": placed,
              "actual_edits": details, "outputs": {"wav": str(wav), "mp3": str(mp3), "preview": str(preview), "srt": str(srt)}}
    result["output_sha256"]={kind:sha(path) for kind,path in result["outputs"].items()}
    save(p/"render.json", result)
    return result
