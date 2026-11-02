import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import requests
import soundfile as sf
from dubkit import core, audio, cloud, human, qa
from dubkit.analyze import propose_regions


def fixture(root, duration=8.):
    for x in ["analysis","cache","languages"]: (root/x).mkdir(parents=True,exist_ok=True)
    job={"name":"fixture","source":{"frames":round(duration*48000),"sha256":"test","path":"fixture.mp4"},
         "source_language":"en","languages":["ca-ES"],
         "settings":{"max_usd":1.,"chirp_usd_per_million":30.,"human_max_tempo":1.16,
                     "google_max_tempo":1.25,"target_lufs":-17.5,"true_peak_db":-1.5}}
    core.save(root/"job.json",job)
    core.save(root/"analysis/regions.json",{"dialogue":[[1.,3.],[5.,7.]],"preserve":[[0.,1.],[3.,5.],[7.,duration]]})
    core.save(root/"analysis/cues.json",{"cues":[{"id":"001","start":1.,"end":2.8,"text":"A boat.","speaker":"A"},
                                                    {"id":"002","start":5.,"end":6.8,"text":"A solar roof.","speaker":"B"}]})
    core.accept_source(root,"Fixture timeline reviewed")
    path=Path(core.translation_template(root,"ca-ES","human")); t=core.load(path)
    t["cues"][0]["text"]="Un vaixell.";t["cues"][1]["text"]="Un sostre solar."
    core.save(path,t)
    original=np.random.default_rng(4).integers(-3000,3000,size=(round(duration*48000),2),dtype=np.int16)
    sf.write(root/"cache/original-48k-stereo.wav",original,48000,subtype="PCM_16")
    return original


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()

    def test_locale_and_no_standard_fallback(self):
        self.assertEqual(core.locale("es"),"es-ES")
        self.assertEqual(core.locale("zh"),"cmn-CN")
        voices=[{"name":"es-US-Chirp3-HD-Charon","languageCodes":["es-US"]},
                {"name":"es-ES-Chirp3-HD-Charon","languageCodes":["es-ES"]},
                {"name":"ca-ES-Standard-B","languageCodes":["ca-ES"]}]
        r=cloud.choose_voices(voices,"es",["A"])
        self.assertEqual(r["voices"]["A"],"es-ES-Chirp3-HD-Charon")
        self.assertEqual(cloud.choose_voices(voices,"ca",["A"])["mode"],"human")
        with self.assertRaises(ValueError):cloud.choose_voices(voices,"ca",["A"],"google")

    def test_cache_hash_and_cost_reservation(self):
        fixture(self.root)
        buf=io.BytesIO(); sf.write(buf,np.sin(np.arange(12000)*.1).astype(np.float32)*.1,24000,format="WAV",subtype="PCM_16")
        class Response:
            ok=True;status_code=200
            def json(self):return {"audioContent":base64.b64encode(buf.getvalue()).decode()}
        request=cloud.payload("fr-FR","fr-FR-Chirp3-HD-Charon","Bonjour")
        calls=[]
        def post(*a,**kw):
            # The ledger must already be durable before a request is dispatched.
            ledger=core.load(self.root/"cloud-ledger.json")
            self.assertEqual(ledger["attempts"][-1]["status"],"reserved_before_dispatch")
            calls.append(1);return Response()
        first=cloud.synthesize_cached(self.root,request,post,{})
        second=cloud.synthesize_cached(self.root,request,post,{})
        self.assertEqual(first,second);self.assertEqual(len(calls),1)
        changed=cloud.payload("fr-FR","fr-FR-Chirp3-HD-Orus","Bonjour")
        self.assertNotEqual(cloud.request_key(request),cloud.request_key(changed))
        changed=cloud.payload("fr-FR","fr-FR-Chirp3-HD-Charon","Bonjour",1.05)
        self.assertNotEqual(cloud.request_key(request),cloud.request_key(changed))
        self.assertEqual(len(core.load(self.root/"cloud-ledger.json")["attempts"]),1)

    def test_ambiguous_timeout_counts_against_cap(self):
        fixture(self.root)
        j=core.load(self.root/"job.json");j["settings"]["max_usd"]=.00024;core.save(self.root/"job.json",j)
        request=cloud.payload("fr-FR","fr-FR-Chirp3-HD-Charon","Bonjour")
        def fail(*a,**kw):raise requests.Timeout("secret-never-output")
        with patch("dubkit.cloud.time.sleep"):
            with self.assertRaisesRegex(ValueError,"budget cap"):
                cloud.synthesize_cached(self.root,request,fail,{})
        ledger=core.load(self.root/"cloud-ledger.json")
        self.assertEqual(len(ledger["attempts"]),1)
        self.assertEqual(ledger["attempts"][0]["status"],"network_outcome_unknown")

    def test_source_and_translation_staleness(self):
        fixture(self.root);core.translation_data(self.root,"ca-ES")
        p=self.root/"analysis/cues.json"; c=core.load(p);c["cues"][0]["text"]="A new boat.";core.save(p,c)
        with self.assertRaises(ValueError):core.source_digest(self.root)
        core.accept_source(self.root,"Corrected source wording")
        with self.assertRaises(ValueError):core.translation_data(self.root,"ca-ES")

    def test_incomplete_translation_refused(self):
        fixture(self.root);p=self.root/"languages/ca-ES/translation.json"
        t=core.load(p);t["cues"].pop();core.save(p,t)
        with self.assertRaises(ValueError):core.translation_data(self.root,"ca-ES")

    def test_interleaved_music_and_pause_mask(self):
        spans=[[10.,12.],[14.,16.],[30.,35.]]
        windows=[]
        for a in range(0,40,2):
            speech=.8 if 8<=a<=16 or 28<=a<=34 else .074
            windows.append({"start":float(a),"end":float(a+4),"speech":speech,"music":.8 if speech<.1 else .001})
        r=propose_regions(spans,windows,40)
        self.assertEqual(len(r["dialogue"]),2)
        self.assertTrue(core.contains(r["dialogue"],12.,14.))
        self.assertFalse(core.contains(r["dialogue"],20.,25.))

    def test_repeat_alignment_keeps_complete_take(self):
        expected=human.tokens("one two three four")
        heard=human.tokens("one two wrong restart one two three four")
        pairs,missing=human.align_tokens(expected,heard)
        self.assertEqual([(a,b) for a,b,c in pairs],[(0,4),(1,5),(2,6),(3,7)])
        self.assertEqual(missing,[])
        self.assertEqual(human.tokens("d’aquest sostre"),human.tokens("d' aquest sostre"))
        self.assertEqual(human.tokens("一艘船"),["一","艘","船"])

    def test_quiet_pause_keeps_word_tails(self):
        sound=np.sin(np.arange(48000)*.12).astype(np.float32)*.1
        wave=np.r_[np.zeros(4800),sound,np.zeros(48000),sound,np.zeros(4800)].astype(np.float32)
        ranges,cuts=audio.quiet_ranges(wave,[[0,len(wave)/48000]])
        self.assertEqual(len(cuts),1)
        self.assertAlmostEqual(cuts[0][1]-cuts[0][0],.76,places=2)
        self.assertLess(ranges[0][0],.1);self.assertGreater(ranges[-1][1],3.1)

    def test_chirp_noise_padding_trim_keeps_speech(self):
        padding=np.ones(28800,dtype=np.float32)*.0002
        speech=np.sin(np.arange(48000)*.1).astype(np.float32)*.1
        wave=np.r_[padding,speech,np.zeros(9600,dtype=np.float32)]
        selected=audio.tts_range(wave)[0]
        self.assertAlmostEqual(selected[0],.55,places=2)
        self.assertAlmostEqual(selected[1],1.70,places=2)

    def test_changed_recording_invalidates_review_and_render_cache(self):
        fixture(self.root)
        t=core.translation_data(self.root,"ca-ES")
        path=self.root/"recording.wav"
        wave=np.sin(np.arange(144000)*.1).astype(np.float32)*.1
        sf.write(path,wave,48000,subtype="FLOAT")
        e={"mode":"human","source_digest":core.source_digest(self.root),"translation_digest":core.digest(t),
           "blocks":[{"id":"B1","start":1.,"end":3.},{"id":"B2","start":5.,"end":7.}],
           "cues":[{"id":"001","block":"B1","anchor":1.,"file":"recording.wav","source_ranges":[[0.,1.]],"text":"Un vaixell."},
                   {"id":"002","block":"B2","anchor":5.,"file":"recording.wav","source_ranges":[[2.,3.]],"text":"Un sostre solar."}]}
        core.save(self.root/"languages/ca-ES/edits.json",e)
        audio.accept_edits(self.root,"ca-ES","Initial recording selected")
        first=audio.render_fingerprint(self.root,"ca-ES",e)
        sf.write(path,wave*.5,48000,subtype="FLOAT")
        with self.assertRaises(ValueError):audio.require_edits(self.root,"ca-ES")
        audio.accept_edits(self.root,"ca-ES","Changed recording reviewed")
        self.assertNotEqual(first,audio.render_fingerprint(self.root,"ca-ES",e))

    def test_full_render_release_and_stale_output_guard(self):
        fixture(self.root)
        t=core.translation_data(self.root,"ca-ES");p=self.root/"languages/ca-ES"
        # A voiced envelope, rather than constant DC, exercises mastering and fades.
        phase=np.arange(48000)/48000
        wave=(.08*np.sin(phase*2*np.pi*220)*(np.sin(phase*np.pi)**2)).astype(np.float32)
        for i in [1,2]:sf.write(self.root/f"voice{i}.wav",wave,48000,subtype="FLOAT")
        e={"mode":"human","source_digest":core.source_digest(self.root),"translation_digest":core.digest(t),
           "blocks":[{"id":"B1","start":1.,"end":3.},{"id":"B2","start":5.,"end":7.}],
           "cues":[{"id":"001","block":"B1","anchor":1.,"file":"voice1.wav","source_ranges":[[0.,1.]],"text":"Un vaixell."},
                   {"id":"002","block":"B2","anchor":5.,"file":"voice2.wav","source_ranges":[[0.,1.]],"text":"Un sostre solar."}]}
        core.save(p/"edits.json",e);audio.accept_edits(self.root,"ca-ES","Fixture selections")
        rendered=audio.render(self.root,"ca-ES")
        self.assertEqual(rendered["checks"]["mp3_decoded_frames"],384000)
        # Only ASR is mocked: real extraction, reporting and release guards run.
        with patch("dubkit.qa.transcribe", return_value={"segments": [{"text": "Un vaixell. Un sostre solar. repetim."}]}):
            quality = qa.qa_asr(self.root,"ca-ES")
        self.assertEqual(quality["flagged_groups"],1)
        self.assertEqual(quality["flagged_cues"],0)
        report=core.load(p/"qa-asr.json")
        self.assertEqual(report["groups"][0]["text_check"]["error_counts"]["insertions"],1)
        self.assertEqual(len(report["timing"]["cue_checks"]),2)
        released=qa.release(self.root,"ca-ES","Mocked content review for state-machine regression")
        self.assertEqual(released["outputs"],rendered["outputs"])
        self.assertTrue(core.status(self.root)["languages"]["ca-ES"]["release_current"])
        with patch("dubkit.qa.transcribe", side_effect=AssertionError("Current QA must be reused")):
            self.assertEqual(qa.qa_asr(self.root,"ca-ES"), report)
        # Changed scoring versions must rescore; the existing audio excerpts stay reusable.
        excerpt=Path(report["groups"][0]["audio"]); excerpt_mtime=excerpt.stat().st_mtime_ns
        report["version"]=1;core.save(p/"qa-asr.json",report)
        self.assertFalse(core.status(self.root)["languages"]["ca-ES"]["release_current"])
        with self.assertRaisesRegex(ValueError,"stale"):
            qa.release(self.root,"ca-ES","Must refresh older quality checks")
        with patch("dubkit.qa.transcribe", return_value={"segments": [{"text": "Un vaixell. Un sostre solar."}]}):
            refreshed=qa.qa_asr(self.root,"ca-ES")
        self.assertEqual(refreshed["flagged_groups"],0)
        self.assertEqual(excerpt.stat().st_mtime_ns,excerpt_mtime)
        self.assertFalse(core.status(self.root)["languages"]["ca-ES"]["release_current"])
        qa.release(self.root,"ca-ES","Refreshed mocked review")
        Path(rendered["outputs"]["mp3"]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError,"Export files changed"):
            qa.release(self.root,"ca-ES","Must not accept a damaged export")
        e["cues"][0]["anchor"]=1.1;core.save(p/"edits.json",e)
        audio.accept_edits(self.root,"ca-ES","Moved cue")
        with self.assertRaisesRegex(ValueError,"stale"):
            audio.require_render(self.root,"ca-ES")

    def test_fit_refuses_unreasonable_speed_and_keeps_join(self):
        clips=[np.ones(48000,dtype=np.float32)*.02]*2
        cues=[{"anchor":0.,"continuous_next":True},{"anchor":1.}]
        with self.assertRaises(ValueError):audio.fit_group(clips,cues,0,1.,1.16)
        fitted,starts,tempo=audio.fit_group(clips,cues,0,2.2,1.16)
        self.assertEqual(round(starts[1]*48000),round(starts[0]*48000)+len(fitted[0]))

    def test_mix_preserves_music_and_erases_all_original_dialogue(self):
        original=fixture(self.root)
        dub=np.zeros((len(original),2),np.float32);dub[48000:72000]=.1;dub[240000:264000]=.1
        sf.write(self.root/"voice.wav",dub,48000,subtype="FLOAT")
        placements=[{"output_start":1.,"output_end":1.5},{"output_start":5.,"output_end":5.5}]
        result=audio.mix_exact(self.root/"cache/original-48k-stereo.wav",self.root/"voice.wav",
                              [[1,3],[5,7]],placements,self.root/"full.wav",len(original))
        output,_=sf.read(self.root/"full.wav",dtype="int16")
        self.assertTrue(np.array_equal(output[3*48000:5*48000],original[3*48000:5*48000]))
        self.assertFalse(np.any(output[72000:3*48000]))
        self.assertEqual(result["original_pcm_preserved_seconds"],4.)

    def test_delayed_audio_and_aac_padding_follow_video(self):
        video=self.root/"delayed.mp4"
        core.run(["ffmpeg","-nostdin","-v","error","-y","-f","lavfi","-i","color=s=64x64:r=25:d=3",
                  "-itsoffset","0.5","-f","lavfi","-i","sine=frequency=440:sample_rate=48000:duration=2",
                  "-map","0:v","-map","1:a","-c:v","libx264","-c:a","aac",video])
        job=self.root/"job"
        with patch.object(core,"RUNTIME",self.root/"runtime"):
            core.init_job(video,["ca"],job)
        audio.extract(job)
        y,sr=sf.read(job/"cache/original-48k-stereo.wav")
        self.assertEqual(len(y),144000)
        self.assertLess(np.abs(y[:20000]).max(),.001)
        self.assertGreater(np.abs(y[30000:48000]).max(),.02)
        self.assertLess(np.abs(y[130000:]).max(),.001)

if __name__=="__main__":unittest.main()
