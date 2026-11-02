"""Local speech/music analysis, resumable ASR and optional speaker clustering."""
import gc
import hashlib
import os
import platform
from pathlib import Path
import numpy as np
import soundfile as sf
from .core import (RUNTIME, RATE, asr_language, contains, digest, interval_union,
                   job_data, load, minus, save, say, sha)

AST_MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"
MLX_MODEL = "mlx-community/whisper-large-v3-turbo"

def setup_models():
    os.environ.setdefault("HF_HOME", str(RUNTIME/"models"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

def vad(audio, sr=16000):
    import torch
    from silero_vad import load_silero_vad, get_speech_timestamps
    if sr != 16000: raise ValueError("VAD expects 16 kHz mono")
    torch.set_num_threads(2)
    model = load_silero_vad(onnx=True)
    result = get_speech_timestamps(torch.from_numpy(audio), model, sampling_rate=sr,
                                  threshold=.35, min_speech_duration_ms=150,
                                  min_silence_duration_ms=160, speech_pad_ms=60,
                                  return_seconds=True)
    return [[float(x["start"]), float(x["end"])] for x in result]

def classify(audio, output, step=2., window=4.):
    setup_models()
    import torch
    from transformers import AutoFeatureExtractor, AutoModelForAudioClassification
    torch.set_num_threads(2)
    fingerprint = digest({"audio_sha": hashlib.sha256(audio.tobytes()).hexdigest(),
                          "model": AST_MODEL, "window": window, "step": step})
    out = Path(output)
    old = load(out) if out.exists() else {}
    rows = old.get("windows", []) if old.get("fingerprint") == fingerprint else []
    times = list(np.arange(0, len(audio)/16000, step))
    if len(rows) == len(times): return rows
    say("Loading local speech/music classifier")
    processor = AutoFeatureExtractor.from_pretrained(AST_MODEL)
    model = AutoModelForAudioClassification.from_pretrained(AST_MODEL).eval()
    labels = model.config.id2label
    def ids(names): return [int(k) for k,v in labels.items() if v in names]
    speech_ids = ids({"Speech", "Male speech, man speaking", "Female speech, woman speaking", "Conversation", "Narration, monologue"})
    music_ids = ids({"Music", "Background music", "Instrumental music"})
    singing_ids = ids({"Singing", "Male singing", "Female singing", "Choir", "Vocal music"})
    if not speech_ids or not music_ids: raise ValueError("Classifier label schema changed")
    for i in range(len(rows), len(times)):
        start = float(times[i]); end = min(len(audio)/16000, start+window)
        wave = audio[round(start*16000):round(end*16000)]
        if len(wave) < 1600: wave = np.pad(wave, (0, 1600-len(wave)))
        with torch.inference_mode():
            features = processor(wave, sampling_rate=16000, return_tensors="pt")
            scores = torch.sigmoid(model(**features).logits[0]).cpu().numpy()
        def maximum(index): return float(scores[index].max()) if index else 0.
        top = np.argsort(scores)[-4:][::-1]
        rows.append({"start": start, "end": end, "speech": maximum(speech_ids),
                     "music": maximum(music_ids), "singing": maximum(singing_ids),
                     "top": [[labels[int(k)], round(float(scores[k]), 4)] for k in top]})
        save(out, {"fingerprint": fingerprint, "model": AST_MODEL, "windows": rows})
        if i % 15 == 0: say(f"Classified {end:.0f}/{len(audio)/16000:.0f} seconds")
    del model, processor
    gc.collect()
    return rows

def propose_regions(spans, windows, duration):
    """Conservative episode proposal. Ambiguous/mixed material remains visible for review."""
    music = []
    for w in windows:
        if (w["music"] >= .30 and w["speech"] < .18 and
                w["speech"] < w["music"]*.25 and w.get("singing", 0) < .12):
            # The overlapping windows' interior is more reliable than the edge.
            music.append([w["start"]+.6, w["end"]-.6])
    music = interval_union(music)
    seeds = []; rejected = []
    for a,b in spans:
        nearby = [w for w in windows if w["start"] <= (a+b)/2 <= w["end"]]
        speech = max([w["speech"] for w in nearby], default=0.)
        musical = max([w["music"] for w in nearby], default=0.)
        if speech < .18 and musical > .45 and speech < musical*.25 and contains(music, a, b):
            rejected.append({"start": a, "end": b, "reason": "VAD on strong music; check transcript if uncertain"})
        else: seeds.append([max(0,a-.30), min(duration,b+.30)])
    dialogue = []
    for a,b in interval_union(seeds):
        if dialogue:
            previous = dialogue[-1][1]
            protected_gap = sum(max(0,min(a,y)-max(previous,x)) for x,y in music)
            if a-previous <= 12 and protected_gap < .6:
                dialogue[-1][1] = b; continue
        dialogue.append([a,b])
    # Trim music at an episode edge, but never punch small holes inside an interview.
    # A VAD hangover on a drum beat otherwise eats the beginning of a music insert.
    for episode in dialogue:
        for a,b in music:
            if a<=episode[0]<b<episode[1]: episode[0]=b
            if episode[0]<a<episode[1]<=b: episode[1]=a
    preserve = minus([[0,duration]], dialogue)
    mixed = [[w["start"],w["end"]] for w in windows if w["speech"]>.15 and w["music"]>.15]
    uncertain = [[w["start"],w["end"]] for w in windows if w["speech"]>.05 and
                 not contains(dialogue,w["start"],w["end"])]
    return {"dialogue": dialogue, "preserve": preserve, "music_evidence": music,
            "mixed_audio": interval_union(mixed), "speech_outside_proposed_dialogue": interval_union(uncertain),
            "rejected_vad": rejected,
            "review_note": "Review all transitions and speech-outside candidates. Dialogue is replaced in full, including pauses. Mixed background requires a clean stem to preserve it."}

def analyze(job):
    root = Path(job); path = root/"cache/analysis-16k-mono.wav"
    audio, sr = sf.read(path, dtype="float32")
    if sr != 16000 or audio.ndim != 1: raise ValueError("Run extract first")
    fingerprint = sha(path)
    vp = root/"analysis/vad.json"
    if vp.exists() and load(vp).get("sha256") == fingerprint: spans = load(vp)["spans"]
    else:
        say("Detecting speech locally")
        spans = vad(audio)
        save(vp, {"sha256": fingerprint, "spans": spans})
    windows = classify(audio, root/"analysis/classification.json")
    proposal = propose_regions(spans, windows, job_data(root)["source"]["frames"]/RATE)
    save(root/"analysis/regions-proposal.json", proposal)
    if not (root/"analysis/regions.json").exists(): save(root/"analysis/regions.json", proposal)
    return {"dialogue_episodes": len(proposal["dialogue"]), "mixed_regions": len(proposal["mixed_audio"]),
            "review": str(root/"analysis/regions.json")}

def transcribe(path, output, language, words=True, intervals=None, backend="auto", prompt=""):
    setup_models()
    path = Path(path); output = Path(output)
    audio, sr = sf.read(path, dtype="float32")
    if sr != 16000 or audio.ndim != 1: raise ValueError("ASR input must be 16 kHz mono WAV")
    language = asr_language(language)
    if backend == "auto": backend = "mlx" if platform.system()=="Darwin" and platform.machine()=="arm64" else "faster-whisper"
    if backend not in ("mlx", "faster-whisper"): raise ValueError("Unknown ASR backend")
    ranges = intervals or [[0,len(audio)/sr]]
    fingerprint = digest({"audio": sha(path), "language": language, "words": words,
                          "intervals": ranges, "backend": backend, "prompt": prompt,
                          "model": MLX_MODEL if backend=="mlx" else "large-v3-turbo", "schema": 1})
    saved = load(output) if output.exists() else {}
    if saved.get("fingerprint") != fingerprint:
        saved = {"fingerprint": fingerprint, "language": language, "backend": backend,
                 "word_timestamps": words, "chunks": [], "segments": [], "complete": False}
    if saved["complete"]: return saved
    inference_language=saved.get("detected_language") if language=="auto" else language
    jobs = []
    for lo,hi in ranges:
        for a in np.arange(lo,hi,55.):
            b = min(hi,float(a)+55.)
            jobs.append((float(a),b,max(0,float(a)-1.5),min(len(audio)/sr,b+1.5)))
    model = None
    if backend == "mlx":
        import mlx_whisper
    else:
        from faster_whisper import WhisperModel
        model = WhisperModel("large-v3-turbo", device="auto", compute_type="default",
                             download_root=str(RUNTIME/"models/faster-whisper"))
    for i,(lo,hi,a,b) in enumerate(jobs):
        if i in saved["chunks"]: continue
        say(f"ASR chunk {i+1}/{len(jobs)} ({lo:.1f}-{hi:.1f}s)")
        wave = audio[round(a*sr):round(b*sr)]
        if backend == "mlx":
            data = mlx_whisper.transcribe(wave, path_or_hf_repo=MLX_MODEL, language=inference_language,
                word_timestamps=words, initial_prompt=prompt or None, condition_on_previous_text=False,
                temperature=0, verbose=False)
            rows = data["segments"]
            detected=data.get("language")
        else:
            segments, info = model.transcribe(wave, language=inference_language, word_timestamps=words,
                                           initial_prompt=prompt or None, condition_on_previous_text=False,
                                           vad_filter=False, beam_size=5)
            rows = [{"start":s.start,"end":s.end,"text":s.text,"no_speech_prob":s.no_speech_prob,
                     "avg_logprob":s.avg_logprob,"words":[{"word":w.word,"start":w.start,"end":w.end,
                     "probability":w.probability} for w in (s.words or [])]} for s in segments]
            detected=info.language
        if detected:
            saved["detected_language"]=detected
            if language=="auto": inference_language=detected
        for segment in rows:
            if segment.get("no_speech_prob",0)>.7 and segment.get("avg_logprob",0)<-1: continue
            ws = []
            for w in segment.get("words", []):
                x,y = float(w["start"])+a,float(w["end"])+a
                if lo <= (x+y)/2 < hi:
                    ws.append({"word": w["word"], "start": max(0,x), "end": min(len(audio)/sr,y),
                               "probability": w.get("probability",1.)})
            if words:
                if not ws: continue
                start,end = ws[0]["start"],ws[-1]["end"]
                text = "".join(w["word"] for w in ws).strip()
            else:
                start,end = float(segment["start"])+a,float(segment["end"])+a
                if not lo <= (start+end)/2 < hi: continue
                text = segment["text"].strip()
            if text:
                saved["segments"].append({"start":start,"end":end,"text":text,"words":ws,"chunk":i,
                    "avg_logprob":float(segment.get("avg_logprob",0)),"no_speech_prob":float(segment.get("no_speech_prob",0))})
        saved["chunks"].append(i)
        saved["segments"].sort(key=lambda x:x["start"])
        save(output,saved)
        gc.collect()
    saved["complete"] = True
    save(output,saved)
    output.with_suffix(".txt").write_text("\n".join(f"{s['start']:.3f}\t{s['end']:.3f}\t{s['text']}" for s in saved["segments"])+"\n", encoding="utf-8")
    return saved

def source_asr(job, backend="auto", prompt=""):
    root = Path(job)
    # Include every VAD span, including candidates rejected by the music classifier, for review.
    spans = load(root/"analysis/vad.json")["spans"]
    regions = load(root/"analysis/regions.json")["dialogue"]
    duration = job_data(root)["source"]["frames"]/RATE
    ranges = interval_union(regions+[[max(0,a-.3),min(duration,b+.3)] for a,b in spans], gap=1.)
    data = transcribe(root/"cache/analysis-16k-mono.wav", root/"analysis/transcript.json",
                      job_data(root)["source_language"], intervals=ranges, backend=backend, prompt=prompt)
    cues = []
    for s in data["segments"]:
        cues.append({"id": f"{len(cues)+1:03d}", "start": s["start"], "end": s["end"],
                     "text": s["text"], "speaker": "A", "words": s["words"],
                     "outside_proposed_dialogue": not contains(regions,s["start"],s["end"])})
    save(root/"analysis/cues-proposal.json", {"cues": cues})
    if not (root/"analysis/cues.json").exists(): save(root/"analysis/cues.json", {"cues": cues})
    return {"cues": len(cues), "review": str(root/"analysis/cues.json")}

def speakers(job, count=2):
    """Anonymous voice clusters; uncertain results are proposals, never identity/gender inference."""
    import torch
    from resemblyzer import VoiceEncoder, preprocess_wav
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.metrics import silhouette_score
    root = Path(job); cues = load(root/"analysis/cues.json")["cues"]
    audio, sr = sf.read(root/"cache/analysis-16k-mono.wav", dtype="float32")
    encoder = VoiceEncoder(device="cpu"); torch.set_num_threads(2)
    embeddings=[]; indices=[]
    for i,c in enumerate(cues):
        if c["end"]-c["start"] < 1.5: continue
        wave = preprocess_wav(audio[round(c["start"]*sr):round(c["end"]*sr)], source_sr=sr)
        if len(wave)<sr: continue
        embeddings.append(encoder.embed_utterance(wave)); indices.append(i)
    confidence=0.; assigned=[0]*len(cues)
    if len(embeddings)>count and count>1:
        emb=np.array(embeddings)
        labels=AgglomerativeClustering(n_clusters=count,metric="cosine",linkage="average").fit_predict(emb)
        confidence=float(silhouette_score(emb,labels,metric="cosine"))
        if confidence>=.12:
            names={}; centers=[]
            for label in labels:
                if int(label) not in names: names[int(label)]=len(names)
            for label in names: centers.append(np.mean(emb[labels==label],axis=0))
            for i,label in zip(indices,labels): assigned[i]=names[int(label)]
            for i,c in enumerate(cues):
                if i not in indices:
                    nearest=min(indices,key=lambda j:abs(cues[j]["start"]-c["start"]))
                    assigned[i]=assigned[nearest]
    proposal={"silhouette":confidence,"fallback_one_voice":confidence<.12,"cues":[
        {"id":c["id"],"speaker":chr(65+assigned[i]),"text":c["text"]} for i,c in enumerate(cues)]}
    save(root/"analysis/speakers-proposal.json",proposal)
    return proposal
