"""Google TTS only: exact locales, content-addressed cache and conservative cost ledger."""
import base64
import json
import os
import time
from pathlib import Path
import requests
import soundfile as sf
from .audio import convert, tts_range
from .core import (RUNTIME, RATE, contains, digest, job_data, lang_dir, load, locale,
                   now, save, say, sha, source_digest, translation_data)

ENDPOINT = "https://texttospeech.googleapis.com/v1"

def headers():
    key = os.environ.get("ALT_TRACK_DUBBING_GOOGLE_TTS_API_KEY")
    if not key:
        path = Path(os.environ.get("ALT_TRACK_DUBBING_GOOGLE_TTS_CREDENTIALS", Path.home()/".config/alt-track-dubbing/google-text-to-speech.json"))
        if not path.exists(): raise ValueError("Google TTS credential file is missing; human recording remains available")
        data = load(path)
        key = data.get("api_key")
    if not key: raise ValueError("Google TTS API key is missing")
    return {"x-goog-api-key": key, "Content-Type": "application/json"}

def safe_error(response):
    try:
        error = response.json().get("error", {})
        status = error.get("status", "UNKNOWN")
    except (ValueError, AttributeError): status = "UNKNOWN"
    if status not in {"INVALID_ARGUMENT","UNAUTHENTICATED","PERMISSION_DENIED","RESOURCE_EXHAUSTED",
                      "NOT_FOUND","INTERNAL","UNAVAILABLE","DEADLINE_EXCEEDED","UNKNOWN"}: status="UNKNOWN"
    return {"http_status":response.status_code,"status":status}

def catalog(refresh=False):
    path = RUNTIME/"google-voices.json"
    if not refresh and path.exists() and time.time()-path.stat().st_mtime<86400: return load(path)
    try: response = requests.get(ENDPOINT+"/voices", headers=headers(), timeout=(15,60))
    except requests.RequestException: raise RuntimeError("Google voice catalog network request failed; retry without exposing credentials") from None
    if not response.ok: raise RuntimeError(f"Google voice catalog unavailable: {safe_error(response)}")
    voices = response.json().get("voices")
    if not isinstance(voices,list): raise ValueError("Unexpected Google voice catalog schema")
    result = {"retrieved_at":now(),"voices":voices}
    save(path,result)
    return result

def choose_voices(voices, lang, speakers, mode="auto", preferred=None):
    lang = locale(lang)
    choices = sorted(v["name"] for v in voices if lang in v.get("languageCodes",[]) and "Chirp3-HD-" in v["name"])
    if mode == "human": return {"mode":"human","locale":lang,"reason":"Human recording requested"}
    if not choices:
        if mode == "google": raise ValueError(f"No Chirp 3 HD voice for {lang}; use human mode")
        return {"mode":"human","locale":lang,"reason":"No Chirp 3 HD voice in the live catalog"}
    mapping = {}; preferences = ["Charon","Orus","Puck","Fenrir","Kore","Aoede"]
    for i,speaker in enumerate(dict.fromkeys(speakers)):
        explicit = (preferred or {}).get(speaker)
        if explicit:
            if explicit not in choices: raise ValueError(f"Requested voice is not Chirp for exact locale {lang}")
            mapping[speaker] = explicit; continue
        suffix = preferences[i%len(preferences)]
        mapping[speaker] = next((x for x in choices if x.endswith("-"+suffix)),choices[i%len(choices)])
    return {"mode":"google","locale":lang,"voices":mapping}

def plan(job, lang, voices=None):
    t = translation_data(job, lang)
    if t["mode"]=="human": route={"mode":"human","locale":locale(lang),"reason":"Human recording requested"}
    else:
        rows = (voices if voices is not None else catalog()["voices"])
        route=choose_voices(rows,lang,[c.get("speaker","A") for c in load(Path(job)/"analysis/cues.json")["cues"]],
                            t["mode"],t.get("voices"))
    count = sum(len(c.get("tts_text") or c["text"]) for c in t["cues"])
    price=job_data(job)["settings"]["chirp_usd_per_million"]
    new_characters=0; cached=0
    if route["mode"]=="google":
        source=load(Path(job)/"analysis/cues.json")["cues"]
        for c,s in zip(t["cues"],source):
            text=c.get("tts_text") or c["text"]
            request=payload(lang,route["voices"][s.get("speaker","A")],text,float(c.get("speaking_rate",1.)))
            key=request_key(request); cache=Path(job)/"cache/google"
            if (cache/f"{key}.wav").exists() and (cache/f"{key}.json").exists(): cached+=1
            else: new_characters+=len(text)
    route.update({"characters":count,"new_characters":new_characters,"cached_cues":cached,
                  "list_price_usd":round(new_characters*price/1e6,6),
                  "full_text_list_price_usd":round(count*price/1e6,6) if route["mode"]=="google" else 0,
                  "free_tier_remaining":"unknown", "source_digest":source_digest(job),"translation_digest":digest(t)})
    save(lang_dir(job,lang)/"route.json",route)
    return route

def payload(lang, voice, text, rate=1.):
    if not text.strip(): raise ValueError("Empty TTS text")
    if len(text.encode("utf-8")) > 4800: raise ValueError("Cue exceeds conservative 4,800-byte TTS request limit; split it")
    if not .25<=rate<=2.: raise ValueError("Google speaking rate must be .25-2")
    return {"input":{"text":text},"voice":{"languageCode":locale(lang),"name":voice},
            "audioConfig":{"audioEncoding":"LINEAR16","sampleRateHertz":24000,"speakingRate":rate}}

def request_key(request):
    return digest({"endpoint":ENDPOINT+"/text:synthesize","request":request})

def synthesize_cached(job, request, post=None, credential_headers=None):
    root=Path(job); cache=root/"cache/google"; cache.mkdir(parents=True,exist_ok=True)
    key=request_key(request); wav=cache/f"{key}.wav"; meta=cache/f"{key}.json"
    ledger_path=root/"cloud-ledger.json"
    ledger=load(ledger_path) if ledger_path.exists() else {"attempts":[]}
    if wav.exists():
        try:
            info=sf.info(wav)
            valid=info.frames>0 and info.samplerate==24000 and info.channels==1
            if meta.exists(): valid=valid and load(meta).get("audio_sha256")==sha(wav)
            else: valid=valid and any(a["request_key"]==key for a in ledger["attempts"])
            if valid:
                if not meta.exists(): save(meta,{"request":request,"audio_sha256":sha(wav),"recovered":True})
                return wav
        except (RuntimeError,ValueError): pass
        raise ValueError("Cached TTS audio is damaged; inspect it before paying for replacement")
    data=job_data(root); settings=data["settings"]
    cost=len(request["input"]["text"])*settings["chirp_usd_per_million"]/1e6
    cap=float(settings["max_usd"])
    auth=credential_headers if credential_headers is not None else headers()
    post=post or requests.post
    for retry in range(3):
        used=sum(a["reserved_usd"] for a in ledger["attempts"])
        if used+cost>cap+1e-9: raise ValueError(f"Job budget cap ${cap:.2f} reached; reserved ${used:.4f}")
        entry={"request_key":key,"at":now(),"characters":len(request["input"]["text"]),
               "reserved_usd":cost,"status":"reserved_before_dispatch"}
        ledger["attempts"].append(entry); save(ledger_path,ledger)
        try:
            response=post(ENDPOINT+"/text:synthesize",headers=auth,json=request,timeout=(15,90))
        except requests.RequestException:
            entry["status"]="network_outcome_unknown"; save(ledger_path,ledger)
            # Google does not support an idempotency key here. Count ambiguous attempts too.
            if retry==2: raise RuntimeError("TTS network outcome unknown after three attempts; see cost ledger") from None
            time.sleep(2**retry); continue
        if not response.ok:
            entry["status"]="http_error"; entry["error"]=safe_error(response); save(ledger_path,ledger)
            if response.status_code not in (429,500,502,503,504) or retry==2:
                raise RuntimeError(f"Google synthesis failed: {safe_error(response)}")
            time.sleep(2**retry); continue
        try:
            raw=base64.b64decode(response.json()["audioContent"],validate=True)
            temp=wav.with_suffix(".partial.wav"); temp.write_bytes(raw)
            info=sf.info(temp)
            if info.frames<=0 or info.channels!=1 or info.samplerate!=24000: raise ValueError("Unexpected audio format")
            os.replace(temp,wav)
        except (ValueError,KeyError,RuntimeError):
            entry["status"]="invalid_audio_response"; save(ledger_path,ledger)
            raise RuntimeError("Google returned invalid audio; inspect the reserved request before retrying") from None
        save(meta,{"request":request,"audio_sha256":sha(wav),"seconds":info.duration})
        entry["status"]="saved"; save(ledger_path,ledger)
        return wav
    raise AssertionError("Unreachable")

def synthesize(job, lang):
    root=Path(job); p=lang_dir(root,lang); t=translation_data(root,lang); route=plan(root,lang)
    if route["mode"]=="human": return {**route,"next":"Run script to prepare a timed human reading script"}
    source=load(root/"analysis/cues.json")["cues"]
    regions=load(root/"analysis/regions.json")["dialogue"]
    selected=[]; blocks=[]
    for i,(c,translated) in enumerate(zip(source,t["cues"])):
        text=translated.get("tts_text") or translated["text"]
        voice=route["voices"][c.get("speaker","A")]
        request=payload(lang,voice,text,float(translated.get("speaking_rate",1.)))
        say(f"Google {lang}: cue {i+1}/{len(source)} (cached clips are reused)")
        raw=synthesize_cached(root,request)
        mono=raw.with_name(raw.stem+"-48k.wav")
        if not mono.exists(): convert(raw,mono)
        wave,sr=sf.read(mono,dtype="float32")
        ranges=tts_range(wave)
        block_id=f"G{i+1:04d}"
        hi=next(b for a,b in regions if contains([[a,b]],c["start"],c["end"]))
        if i+1<len(source): hi=min(hi,source[i+1]["start"]-.025)
        # Allow natural spare silence after a cue but not arbitrary drift into later sentences.
        hi=min(hi,c["end"]+.8)
        blocks.append({"id":block_id,"start":c["start"],"end":hi})
        selected.append({"id":str(c["id"]),"block":block_id,"anchor":c["start"],
                         "file":str(mono.relative_to(root)),"source_ranges":ranges,
                         "text":translated["text"],"shorten_pauses":False,"trim_edges":False})
    edits={"mode":"google","source_digest":source_digest(root),"translation_digest":digest(t),
           "blocks":blocks,"cues":selected,"route":route,"review_flags":[]}
    save(p/"edits.json",edits)
    return {"cues":len(selected),"edits":str(p/"edits.json"),"next":"accept-edits then render"}
