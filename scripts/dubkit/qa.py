"""Independent local ASR of every rendered cue, followed by an explicit agent review receipt."""
import difflib
from pathlib import Path
import numpy as np
import soundfile as sf
from .analyze import transcribe
from .audio import convert, require_edits, require_render
from .core import RATE, asr_language, digest, lang_dir, load, now, save, sha, translation_data
from .human import align_tokens, tokens
from .review import compare_text, placement_review

QA_VERSION = 2

def require_qa(job, lang, render=None):
    render = render if render is not None else require_render(job, lang)
    report = load(lang_dir(job, lang)/"qa-asr.json")
    if (report.get("version") != QA_VERSION or report.get("render_digest") != digest(render)
            or not report.get("complete")):
        raise ValueError("Final ASR is missing or stale; run qa-asr to refresh the quality report")
    if report.get("covered_cue_ids") != [c["id"] for c in render["placements"]]:
        raise ValueError("Incomplete final speech coverage")
    return report

def qa_asr(job, lang, backend="auto"):
    p=lang_dir(job,lang); render=require_render(job,lang)
    t=translation_data(job,lang)
    directory=p/"qa-work"/render["fingerprint"][:12]; directory.mkdir(parents=True,exist_ok=True)
    fingerprint=digest(render)
    report_path=p/"qa-asr.json"
    old=load(report_path) if report_path.exists() else {}
    if old.get("version")==QA_VERSION and old.get("render_digest")==fingerprint and old.get("complete"): return old
    groups=[]; current=[]; duration=0
    for c in render["placements"]:
        length=c["output_end"]-c["output_start"]
        if current and duration+length>42 and not current[-1].get("continuous_next"):
            groups.append(current); current=[]; duration=0
        current.append(c); duration+=length+.28
    if current: groups.append(current)
    timing=placement_review(load(Path(job)/"analysis/cues.json")["cues"],require_edits(job,lang),render["placements"])
    report={"version":QA_VERSION,"render_digest":fingerprint,"complete":False,
            "groups":[],"covered_cue_ids":[],"timing":timing}
    aliases=t.get("alignment_aliases",{})
    for i,group in enumerate(groups):
        wave=directory/f"group-{i:03d}-48k.wav"; analysis=directory/f"group-{i:03d}-16k.wav"
        if not analysis.exists():
            chunks=[]
            with sf.SoundFile(render["outputs"]["wav"]) as audio:
                for c in group:
                    audio.seek(round(c["output_start"]*RATE))
                    chunks.append(audio.read(round((c["output_end"]-c["output_start"])*RATE),dtype="float32",always_2d=True).mean(axis=1))
                    if not c.get("continuous_next"): chunks.append(np.zeros(round(.28*RATE),dtype=np.float32))
            sf.write(wave,np.concatenate(chunks),RATE,subtype="FLOAT"); convert(wave,analysis,16000)
        heard=transcribe(analysis,directory/f"group-{i:03d}.json",asr_language(lang),words=False,backend=backend)
        recognized=" ".join(s["text"] for s in heard["segments"])
        expected=" ".join(c["text"] for c in group)
        comparison=compare_text(expected,recognized,aliases)
        a=tokens(expected,aliases); b=tokens(recognized,aliases)
        if b: pairs,missing=align_tokens(a,b)
        else: pairs=[]; missing=list(range(len(a)))
        cursor=0; checks=[]
        for c in group:
            n=len(tokens(c["text"],aliases))
            matched=sum(1 for x,y,cost in pairs if cursor<=x<cursor+n and cost<1.)
            checks.append({"id":c["id"],"token_coverage":matched/max(1,n),"review":matched/max(1,n)<.90})
            cursor+=n
        report["groups"].append({"id":i,"cue_ids":[c["id"] for c in group],"expected":expected,
                                 "recognized":recognized,"cue_checks":checks,
                                 "text_similarity":difflib.SequenceMatcher(None,a,b,autojunk=False).ratio(),
                                 "audio":str(analysis),"text_check":comparison,
                                 "review":comparison["review"] or any(c["review"] for c in checks)})
        report["covered_cue_ids"].extend(c["id"] for c in group)
        save(report_path,report)
    if report["covered_cue_ids"] != [c["id"] for c in render["placements"]]: raise ValueError("Final ASR missed cues")
    report["complete"]=True
    report["flagged_groups"]=sum(g["review"] for g in report["groups"])
    save(report_path,report)
    return {"complete":True,"cue_count":len(report["covered_cue_ids"]),
            "flagged_cues":sum(c["review"] for g in report["groups"] for c in g["cue_checks"]),
            "flagged_groups":report["flagged_groups"],"flagged_timing_cues":timing["flagged_cues"],"report":str(report_path)}

def release(job, lang, note):
    if not note.strip(): raise ValueError("Document final transcript/transition review, including resolved ASR flags")
    p=lang_dir(job,lang)
    t=translation_data(job,lang); render=require_render(job,lang); qa=require_qa(job,lang,render)
    if sha(render["outputs"]["wav"])!=render["checks"]["wav_sha256"]: raise ValueError("Rendered WAV changed")
    hashes={kind:sha(path) for kind,path in render["outputs"].items()}
    if hashes!=render.get("output_sha256"): raise ValueError("Export files changed after rendering; regenerate and verify them")
    receipt={"at":now(),"render_digest":digest(render),"translation_digest":digest(t),
             "qa_digest":digest(qa),"note":note,"outputs":render["outputs"],"sha256":hashes}
    save(p/"release.json",receipt)
    out=Path(render["outputs"]["wav"]).parent
    save(out/"release.json",receipt)
    save(out/"speech-qa.json",qa)
    (out/"INFO.txt").write_text(
        f"Language: {lang}\nDuration: {render['checks']['duration_seconds']:.3f} s\n"
        "Use the full WAV or MP3 as the YouTube alternate audio track.\n"
        "WAV: 48 kHz, stereo, 16-bit PCM. MP3: 192 kbps.\n"
        "Outside dialogue episodes, the music in the WAV matches the decoded original.\n"
        "Inside dialogue episodes there are only the new lines and silence.\n"
        "The preview is only a short excerpt. The SRT holds the timings of the finished voice-over.\n\n"
        f"Review: {note}\n",encoding="utf-8")
    return receipt
