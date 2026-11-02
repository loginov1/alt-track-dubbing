#!/usr/bin/env python3
"""Command entry point. The agent running the skill handles text and judgment; these commands handle audio."""
import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from dubkit.core import RUNTIME, init_job, lang_dir, load, locale, lock, save, status

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("doctor"); sub.add_parser("jobs")
    v=sub.add_parser("voices"); v.add_argument("--refresh",action="store_true")
    q=sub.add_parser("init"); q.add_argument("video"); q.add_argument("--languages",nargs="+",required=True)
    q.add_argument("--out"); q.add_argument("--source-language",default="auto"); q.add_argument("--max-usd",type=float,default=5.)
    for name in ["extract","analyze","status","accept-source","source-asr","speakers"]:
        q=sub.add_parser(name); q.add_argument("job")
        if name=="accept-source": q.add_argument("--note",required=True)
        if name=="source-asr": q.add_argument("--backend",default="auto"); q.add_argument("--prompt",default="")
        if name=="speakers": q.add_argument("--count",type=int,default=2)
    for name in ["translation","plan","synthesize","script","ingest","recording-asr","align","accept-edits","render","qa-asr","release"]:
        q=sub.add_parser(name); q.add_argument("job"); q.add_argument("language",type=locale)
        if name=="translation": q.add_argument("--mode",choices=["auto","google","human"],default="auto")
        if name=="ingest": q.add_argument("recording")
        if name in ("recording-asr","qa-asr"): q.add_argument("--backend",default="auto")
        if name=="align": q.add_argument("--exclude"); q.add_argument("--cue-ids",nargs="+")
        if name in ("accept-edits","release"): q.add_argument("--note",required=True)
    q=sub.add_parser("asr"); q.add_argument("audio_16k"); q.add_argument("output"); q.add_argument("--language",required=True)
    q.add_argument("--no-words",action="store_true"); q.add_argument("--backend",default="auto"); q.add_argument("--prompt",default="")
    return p

def dispatch(a):
    cmd=a.command
    if cmd=="doctor":
        return {"python":sys.executable,"runtime":str(RUNTIME),"ffmpeg":shutil.which("ffmpeg"),"ffprobe":shutil.which("ffprobe"),
                "dependencies":{x:importlib.util.find_spec(x) is not None for x in ["numpy","scipy","soundfile","requests","torch","transformers","silero_vad","mlx_whisper","faster_whisper","resemblyzer"]},
                "google_key_configured":bool(os.environ.get("ALT_TRACK_DUBBING_GOOGLE_TTS_API_KEY")) or Path(os.environ.get("ALT_TRACK_DUBBING_GOOGLE_TTS_CREDENTIALS",Path.home()/".config/alt-track-dubbing/google-text-to-speech.json")).exists(),
                "jobs_index":str(RUNTIME/"jobs.json")}
    if cmd=="jobs": return load(RUNTIME/"jobs.json") if (RUNTIME/"jobs.json").exists() else {}
    if cmd=="voices":
        from dubkit.cloud import catalog
        data=catalog(a.refresh)
        locales={}
        for v in data["voices"]:
            for lang in v.get("languageCodes",[]):
                locales.setdefault(lang,{"chirp":0,"other":0})["chirp" if "Chirp3-HD-" in v["name"] else "other"]+=1
        return {"retrieved_at":data["retrieved_at"],"locales":locales}
    if cmd=="init":
        if a.max_usd<0: raise ValueError("Budget cannot be negative")
        return init_job(a.video,a.languages,a.out,a.source_language,a.max_usd)
    if cmd=="asr":
        from dubkit.analyze import transcribe
        result=transcribe(a.audio_16k,a.output,a.language,not a.no_words,backend=a.backend,prompt=a.prompt)
        return {"complete":result["complete"],"output":a.output}
    if cmd=="status": return status(a.job)
    from dubkit import audio, analyze, cloud, core, human, qa
    functions={
        "extract":lambda:audio.extract(a.job),"analyze":lambda:analyze.analyze(a.job),
        "source-asr":lambda:analyze.source_asr(a.job,a.backend,a.prompt),"speakers":lambda:analyze.speakers(a.job,a.count),
        "accept-source":lambda:core.accept_source(a.job,a.note),
        "translation":lambda:core.translation_template(a.job,a.language,a.mode),
        "plan":lambda:cloud.plan(a.job,a.language),"synthesize":lambda:cloud.synthesize(a.job,a.language),
        "script":lambda:human.script(a.job,a.language),"ingest":lambda:human.ingest(a.job,a.language,a.recording),
        "recording-asr":lambda:human.recording_asr(a.job,a.language,a.backend),
        "align":lambda:human.align_recording(a.job,a.language,a.exclude,a.cue_ids),
        "accept-edits":lambda:audio.accept_edits(a.job,a.language,a.note),"render":lambda:audio.render(a.job,a.language),
        "qa-asr":lambda:qa.qa_asr(a.job,a.language,a.backend),"release":lambda:qa.release(a.job,a.language,a.note),
    }
    with lock(a.job): return functions[cmd]()

def main():
    try:
        result=dispatch(parser().parse_args())
        print(json.dumps(result if result is not None else {"ok":True},ensure_ascii=False,indent=2,allow_nan=False))
    except (ValueError,RuntimeError,FileNotFoundError) as e:
        print(f"Dubbing: {e}",file=sys.stderr); sys.exit(2)

if __name__=="__main__": main()
