"""Timed reading sheets, immutable recordings, take alignment and acoustic cut proposals."""
import csv
import difflib
import re
import shutil
import unicodedata
from pathlib import Path
import numpy as np
import soundfile as sf
from .analyze import transcribe, vad
from .audio import convert, rms_frames, runs
from .core import (RATE, asr_language, digest, lang_dir, load, make_blocks, minus, now,
                   save, say, sha, source_digest, stamp, translation_data)

def script(job, lang):
    root=Path(job); p=lang_dir(root,lang); t=translation_data(root,lang)
    cues=load(root/"analysis/cues.json")["cues"]
    regions=load(root/"analysis/regions.json")["dialogue"]
    blocks=make_blocks(cues,regions)
    translated={str(c["id"]):c for c in t["cues"]}
    lines=[]
    for b in blocks:
        text=" ".join(translated[i].get("human_text") or translated[i]["text"] for i in b["cue_ids"])
        b["text"]=text
        lines.extend([f"{b['id']} | {b['speaker']} | {stamp(b['start'])} - {stamp(b['end'])} | {b['end']-b['start']:.1f} s",text,""])
    save(p/"reading-manifest.json",{"source_digest":source_digest(root),"translation_digest":digest(t),"blocks":blocks})
    (p/"reading-script.txt").write_text("\n".join(lines),encoding="utf-8")
    (p/"reading-script.md").write_text("# Reading script\n\n"+"\n\n".join(
        f"## {b['id']} · {b['speaker']} · {b['end']-b['start']:.1f} s\n\nVideo: {stamp(b['start'])} → {stamp(b['end'])}\n\n{b['text']}" for b in blocks)+"\n",encoding="utf-8")
    with (p/"reading-blocks.csv").open("w",encoding="utf-8-sig",newline="") as f:
        writer=csv.writer(f); writer.writerow(["block","speaker","video_start","video_end","seconds","cue_ids","text"])
        writer.writerows([[b['id'],b['speaker'],b['start'],b['end'],round(b['end']-b['start'],3),",".join(b['cue_ids']),b['text']] for b in blocks])
    guide="""# How to record the voice-over

Read reading-script.txt in order, in a natural voice. Each block shows its place in the video and a target duration. You do not need to wait for the matching timecode or make a recording as long as the whole video: the edit adds the music and pauses.

1. Use a quiet room and keep a steady distance of 15-25 cm from the microphone. Turn off music and notifications. Avoid echo and the sound of your hands on the phone.
2. Record 5 seconds of room silence first. Leave 1-2 seconds between blocks. You may say the block number and then pause. Reading the numbers is optional.
3. If you make a mistake, stop for 2 seconds and repeat the whole last sentence, or the whole block if you can. Do not repeat only half a word. You may say "repeat" before a new take.
4. Do not try to hit the timing to a fraction of a second. Read calmly, roughly within the given window. The edit removes long pauses and adjusts the tempo slightly without changing the pitch of your voice.
5. You can record everything as one file in Voice Memos. M4A is fine. If you can choose a format, use WAV, 48 kHz, mono, 24-bit. Do not add music, reverb or heavy noise reduction.
6. Send the recording and the path to this dubbing folder. For a partial re-recording, give the block numbers. The original recording is kept separately.

Read technical names and numbers as they are written in the script (it uses human_text where provided). If a spelling is unclear, check it before recording. Editing never replaces the original meaning with new text that has no matching recording.
"""
    (p/"HOW-TO-RECORD.md").write_text(guide,encoding="utf-8")
    return {"script":str(p/"reading-script.txt"),"guide":str(p/"HOW-TO-RECORD.md"),"blocks":len(blocks)}

def ingest(job, lang, recording):
    root=Path(job); p=lang_dir(root,lang); source=Path(recording).expanduser().resolve(strict=True)
    fingerprint=sha(source); directory=root/"recordings"/fingerprint[:16]; directory.mkdir(parents=True,exist_ok=True)
    backup=directory/("original"+source.suffix.lower())
    if not backup.exists():
        tmp=backup.with_suffix(backup.suffix+".partial"); shutil.copy2(source,tmp)
        if sha(tmp)!=fingerprint: raise ValueError("Recording copy did not verify")
        tmp.replace(backup)
    elif sha(backup)!=fingerprint: raise ValueError("Stored original recording is damaged")
    mono=directory/"mono-48k.wav"; analysis=directory/"mono-16k.wav"
    if not mono.exists(): convert(backup,mono)
    if not analysis.exists(): convert(backup,analysis,16000)
    data={"recording_id":fingerprint[:16],"sha256":fingerprint,"original_path":str(source),
          "backup":str(backup),"mono":str(mono),"analysis":str(analysis),"created_at":now()}
    save(directory/"recording.json",data); save(p/"current-recording.json",data)
    return data

def recording_asr(job, lang, backend="auto"):
    p=lang_dir(job,lang); recording=load(p/"current-recording.json"); directory=Path(recording["mono"]).parent
    audio,sr=sf.read(recording["analysis"],dtype="float32")
    vp=directory/"vad.json"
    if not vp.exists(): save(vp,{"spans":vad(audio,sr)})
    return transcribe(recording["analysis"],directory/f"transcript-{lang}.json",asr_language(lang),backend=backend)

def tokens(text, aliases=None):
    text=unicodedata.normalize("NFKD",text.lower())
    text="".join(c for c in text if not unicodedata.combining(c))
    # Han characters are units; alphabetic languages keep words. Apostrophes split consistently.
    text=re.sub(r"([\u3400-\u9fff])",r" \1 ",text)
    result=re.findall(r"[^\W_]+",text,flags=re.UNICODE)
    if aliases:
        table={" ".join(tokens(k)):tokens(v) for k,v in aliases.items()}
        expanded=[]; i=0; max_n=max([len(k.split()) for k in table],default=1)
        while i<len(result):
            for n in range(min(max_n,len(result)-i),0,-1):
                key=" ".join(result[i:i+n])
                if key in table:
                    expanded.extend(table[key]); i+=n; break
            else: expanded.append(result[i]); i+=1
        result=expanded
    return result

def align_tokens(expected, heard):
    """Semiglobal alignment with cheap inserted retakes and a slight late-take tie preference.

    Each row is vectorized. Backtrace preserves unmatched prefixes and inserted words as evidence.
    This proposes bounds; it cannot decide whether a spoken paraphrase changes meaning.
    """
    n,m=len(expected),len(heard)
    if not n or not m: raise ValueError("Empty script or recognized recording")
    if n*m>120_000_000: raise ValueError("Recording too large for one alignment; process reading blocks separately")
    vocab_a=list(dict.fromkeys(expected)); vocab_b=list(dict.fromkeys(heard))
    ai={x:i for i,x in enumerate(vocab_a)}; bi={x:i for i,x in enumerate(vocab_b)}
    cost=np.empty((len(ai),len(bi)),dtype=np.float32)
    for x,ix in ai.items():
        for y,iy in bi.items():
            if x==y: value=0.
            elif min(len(x),len(y))<3: value=1.12
            else:
                ratio=difflib.SequenceMatcher(None,x,y,autojunk=False).ratio()
                value=.12+(1-ratio) if ratio>=.8 else .5+(1-ratio) if ratio>=.6 else 1.12
            cost[ix,iy]=value
    indices=np.array([bi[x] for x in heard]); j=np.arange(1,m+1)
    previous=np.zeros(m+1,dtype=np.float64)  # Leading recording slates/silence/false starts are free.
    trace=np.zeros((n+1,m+1),dtype=np.uint8)
    trace[0,1:]=2
    insertion=.32; deletion=.9
    for i,word in enumerate(expected,1):
        substitution=previous[:-1]+cost[ai[word],indices]-1e-8*j
        deleted=previous[1:]+deletion
        base=np.minimum(substitution,deleted)
        first=previous[0]+deletion
        # D[j] = min(base[j], D[j-1] + insertion).
        candidates=np.r_[first,base-insertion*j]
        row=np.minimum.accumulate(candidates)+insertion*np.arange(m+1)
        choice=np.where(substitution<=deleted,0,1).astype(np.uint8)
        choice[row[1:]<base-1e-9]=2
        trace[i,0]=1; trace[i,1:]=choice; previous=row
    end=int(np.argmin(previous))
    i=n; j=end; pairs=[]; missing=[]
    while i or j:
        move=trace[i,j]
        if i and j and move==0:
            pairs.append((i-1,j-1,float(cost[ai[expected[i-1]],bi[heard[j-1]]]))); i-=1; j-=1
        elif i and (move==1 or not j): missing.append(i-1); i-=1
        else: j-=1
    return list(reversed(pairs)),list(reversed(missing))

def align_recording(job, lang, exclusions=None, cue_ids=None):
    root=Path(job); p=lang_dir(root,lang); t=translation_data(root,lang)
    if not (p/"reading-manifest.json").exists(): script(root,lang)
    reading=load(p/"reading-manifest.json")
    if reading["translation_digest"]!=digest(t) or reading["source_digest"]!=source_digest(root):
        raise ValueError("Reading script is stale; reconcile it before aligning a recording")
    recording=load(p/"current-recording.json"); directory=Path(recording["mono"]).parent
    transcript=load(directory/f"transcript-{lang}.json")
    if not transcript.get("complete",True): raise ValueError("Recording ASR is incomplete")
    excluded=load(exclusions) if exclusions else []
    cuts=[[float(e["start"]),float(e["end"])] for e in excluded]
    source=load(root/"analysis/cues.json")["cues"]
    rows=t["cues"]
    if cue_ids:
        subset=set(cue_ids); rows=[c for c in rows if str(c["id"]) in subset]
        if not (p/"edits.json").exists(): raise ValueError("Partial rerecording requires existing full edit decisions")
    expected=[]; cue_token_ranges={}; aliases=t.get("alignment_aliases",{})
    for c in rows:
        a=len(expected); expected.extend(tokens(c.get("human_text") or c["text"],aliases))
        cue_token_ranges[str(c["id"])]=(a,len(expected))
    heard=[]; word_refs=[]
    for s in transcript["segments"]:
        for w in s.get("words",[]):
            if any(a<=(w["start"]+w["end"])/2<b for a,b in cuts): continue
            for token in tokens(w["word"],aliases): heard.append(token); word_refs.append(w)
    say(f"Aligning {len(expected)} script tokens with {len(heard)} recording tokens")
    pairs,missing=align_tokens(expected,heard)
    audio,sr=sf.read(recording["mono"],dtype="float32")
    energy,step=rms_frames(audio)
    vads=load(directory/"vad.json")["spans"]
    block_for={i:b for b in reading["blocks"] for i in b["cue_ids"]}
    source_for={str(c["id"]):c for c in source}
    proposed=[]; evidence=[]
    for c in rows:
        cue=str(c["id"]); lo,hi=cue_token_ranges[cue]
        hits=[x for x in pairs if lo<=x[0]<hi and x[2]<1.]
        flags=[]
        if not hits:
            evidence.append({"id":cue,"flags":["missing_complete_cue"],"expected":c.get("human_text") or c["text"]}); continue
        first,last=hits[0][1],hits[-1][1]
        start=float(word_refs[first]["start"]); end=float(word_refs[last]["end"])
        matched=len(hits); coverage=matched/max(1,hi-lo)
        internal=[heard[j] for j in range(first,last+1) if j not in {h[1] for h in hits}]
        if coverage<.92: flags.append("missing_or_mismatched_words")
        if len(internal)>max(2,.15*(hi-lo)): flags.append("possible_retakes_inside_selected_span")
        if any(w in {"um","umm","uh","uhm","erm","mmm","mm","eh","eeh","ee","repeteixo"} for w in internal):
            flags.append("unmatched_filler_or_take_marker")
        if end-start>max(4.,(hi-lo)*.8): flags.append("long_span_or_pause")
        if hits[0][0]>lo: flags.append("unmatched_sentence_prefix")
        if hits[-1][0]<hi-1: flags.append("unmatched_sentence_suffix")
        evidence.append({"id":cue,"flags":flags,"coverage":coverage,"expected":c.get("human_text") or c["text"],
                         "recognized_span":" ".join(heard[first:last+1]),"unmatched_inside":internal,
                         "missing_script_tokens":[expected[k] for k in range(lo,hi) if k not in {h[0] for h in hits}],
                         "word_start":start,"word_end":end})
        proposed.append({"id":cue,"block":block_for[cue]["id"],"anchor":source_for[cue]["start"],
                         "file":str(Path(recording["mono"]).relative_to(root)),"text":c["text"],
                         "asr_start":start,"asr_end":end,"cut_start":max(0,start-.18),
                         "cut_end":min(len(audio)/sr,end+.24),"flags":flags,
                         "linked_next":not bool(re.search(r"[.!?。！？][\"'’”]*$",c["text"].strip()))})
    if len(proposed)!=len(rows):
        save(p/"alignment-review.json",{"cues":evidence,"exclusions":excluded})
        raise ValueError("Some cues have no matched recording; inspect alignment-review.json and short ASR before requesting rerecording")
    for i,c in enumerate(proposed):
        following=proposed[i+1]["asr_start"] if i+1<len(proposed) else len(audio)/sr
        if following-c["asr_end"]>.7:
            limit=min(c["asr_end"]+1.15,following-.22,len(audio)/sr)
            for a,b in cuts:
                if c["asr_end"]<a<limit: limit=a
            candidates=[b+.09 for a,b in vads if a<=c["asr_end"]+.12 and b>=c["asr_end"]-.16 and b+.09<=limit]
            if candidates: c["cut_end"]=max(c["cut_end"],max(candidates))
    for left,right in zip(proposed,proposed[1:]):
        gap=right["asr_start"]-left["asr_end"]
        if -.1<=gap<=.7:
            lo=max(0,left["asr_end"]-.09); hi=min(len(audio)/sr,right["asr_start"]+.25)
            a=max(0,int(lo*sr/step)); b=min(len(energy),int(hi*sr/step))
            if b<=a: continue
            target=(left["asr_end"]+right["asr_start"])/2+.05
            quiet=[]
            for x,y in runs(energy[a:b]<10**(-44/20)):
                if (y-x)*step/sr>=.015:
                    quiet.append(float(np.clip(target,(a+x)*step/sr+.005,(a+y)*step/sr-.005)))
            boundary=min(quiet,key=lambda x:abs(x-target)) if quiet else (a+int(np.argmin(energy[a:b]))+.5)*step/sr
            boundary=round(boundary*RATE)/RATE
            left["cut_end"]=boundary; right["cut_start"]=boundary
            if not quiet and left["block"]==right["block"]:
                left["continuous_next"]=True
                left["flags"].append("continuous_join_check")
            elif not quiet:
                left["flags"].append("hot_cut_at_block_boundary")
    for c in proposed:
        c["source_ranges"]=minus([[c.pop("cut_start"),c.pop("cut_end")]],cuts)
        if not c["source_ranges"]: raise ValueError(f"Exclusions removed all of cue {c['id']}")
    if cue_ids:
        old=load(p/"edits.json")
        replacements={c["id"]:c for c in proposed}
        proposed=[replacements.get(str(c["id"]),c) for c in old["cues"]]
    edits={"mode":"human","source_digest":source_digest(root),"translation_digest":digest(t),
           "recording_sha256":recording["sha256"],"blocks":reading["blocks"],"cues":proposed,
           "excluded_takes":excluded,"review_flags":[{"id":e["id"],"flags":e["flags"]} for e in evidence if e["flags"]]}
    save(p/"alignment-review.json",{"cues":evidence,"exclusions":excluded})
    # Always keep a proposal; preserve the last edited map when rerunning the aligner.
    save(p/"edits-proposal.json",edits)
    if not (p/"edits.json").exists(): save(p/"edits.json",edits)
    return {"cues":len(proposed),"flagged":len(edits["review_flags"]),"proposal":str(p/"edits-proposal.json"),
            "review":str(p/"alignment-review.json"),"next":"Reconcile proposal into edits.json, review all flags, accept-edits"}
