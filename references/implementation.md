# Implementation, recovery and validation

## Locations

- Personal skill: the directory containing this SKILL.md, for example `~/.agents/skills/alt-track-dubbing`.
- Runtime: `~/.local/share/alt-track-dubbing/.venv`, with a stable Python interpreter outside temporary storage.
- Models: `~/.local/share/alt-track-dubbing/models`; MLX Whisper and AST are cached locally.
- Google credential: `~/.config/alt-track-dubbing/google-text-to-speech.json`, a JSON object with an `api_key` field. Only `api_key` is read. Do not copy this file into a job, skill or export. Never print its value or include it in a URL.
- Optional overrides: `ALT_TRACK_DUBBING_HOME` (runtime directory), `ALT_TRACK_DUBBING_GOOGLE_TTS_CREDENTIALS` (credential file path), `ALT_TRACK_DUBBING_GOOGLE_TTS_API_KEY` (key value, takes precedence over the file).
- Per-video jobs: next to the input under `Dubbing/`, unless `--out` overrides.

The skill and runtime do not upload anything to YouTube or change any YouTube setting.

## Runtime setup

`python3 scripts/bootstrap.py` creates a private Python 3.11 environment with uv. ffmpeg/ffprobe are system dependencies. On an existing machine, optional `--reuse-venv` and `--reuse-models` copy a previously working runtime and models into persistent storage; macOS uses copy-on-write clones. The bootstrap does not copy old virtualenv launcher scripts with stale shebangs.

This version has been exercised on macOS Apple Silicon with MLX Whisper. The Linux/faster-whisper code path is provided but has not been tested. Do not claim remote processing is configured or tested. To process on another host, install the skill and runtime on that host and transfer only the required source audio/job data, within hosts the user has authorized.

Use separate processes for ASR, classification and rendering to release model memory. The CLI locks each job against simultaneous mutations. On machines with 8 GB RAM, run heavy jobs sequentially rather than overlapping several models. Resuming an ASR command uses stored chunk checkpoints.

## Commands

Run `dub --help` or `dub COMMAND --help` for exact arguments.

| Command | Result |
|---|---|
| doctor, jobs, status | Runtime health, durable job index, stage files |
| init | Source identity, exact video timeline, language list, budget |
| extract | Stereo original PCM and mono analysis audio |
| analyze | VAD, AST classification, dialogue/music proposal |
| source-asr | Word-timed local transcript and cue proposal |
| speakers | Anonymous voice cluster proposal, fallback confidence |
| accept-source | Hash-bound source quality review |
| translation | Editable language template with complete cue IDs |
| voices, plan | Live Chirp availability and character estimate |
| synthesize | Cached Google clips and per-cue edit decisions |
| script | Human reading TXT/MD/CSV, timing manifest, recording guide |
| ingest | Verified immutable recording backup and decoded WAVs |
| recording-asr, align | Human transcript and take-selection proposal |
| asr | Short-clip diagnosis from a 16 kHz mono WAV |
| accept-edits | Hash-bound take, boundary and source-file review |
| render | Voice mastering, exact full mix, WAV/MP3/SRT/preview/checks |
| qa-asr | Independent ASR, whole-sequence text differences and placement/tempo review |
| release | Output hashes and final review receipt |

## State and invalidation

JSON files are written atomically and contain no secrets. Source identity, reviewed regions, cue text and translations are hashed. An edit map covers every source cue exactly once. Changing upstream data invalidates downstream receipts. Changing text/voice/rate produces a different TTS cache key; unrelated clips remain reusable. Changed source audio hashes invalidate an accepted edit map and render key.

TTS request keys cover the entire JSON request and endpoint. A request reservation is persisted **before** the network call. Network timeouts may still have been billed, so the ledger conservatively counts them and checks the cap before each of at most three attempts. Google does not provide idempotency for this operation; unchanged complete WAVs are reused, including recovery from a crash between WAV and metadata writes. HTTP errors expose only sanitized status codes. API keys are sent in a header.

`translation.json.source_digest` must match the current accepted source. When revising source text after translation, back up the old files, reconcile the translated cues by ID, then update the digest intentionally. Do not change only the digest to silence a validation error.

`edits-proposal.json` is disposable analysis output. `edits.json` is the current deliberate edit. A repeated alignment does not overwrite manual decisions. Reconcile the proposal, accept it and render.

Exports are versioned in `deliverables/<locale>/<render hash>/`. Consult `languages/<locale>/release.json` for the current finished files. If there is no current release, inspect stage state and resume the remaining work.

QA reports carry a separate `version` (currently 2). Changing QA criteria invalidates the report for a new release without invalidating synthesized speech or the audio render. `qa-asr` can rescore cached ASR results. `status` also compares the release's QA digest with the current report; review a refreshed report before reissuing its release receipt.

## Important limitations to report accurately

- VAD/classifier boundaries are proposals, usually within a fraction of a second but sometimes farther off. Music, singing, engine noise and overlapping speakers require inspection. No detector guarantees perfect classification of arbitrary videos.
- The renderer replaces complete dialogue episodes with new speech and silence. It does not currently separate speech from music playing underneath it. Separate music inserts are preserved exactly in the WAV.
- ASR matching can flag legitimate paraphrases, brand spellings and numbers. Coverage is evidence, not a native-speaker quality score.
- Ordinary M4A recordings are fine. Loud echoes, overload or severe noise cannot be repaired into studio sound by trimming pauses.
- Phrase/block timing is exact at the output sample level. Word-level lip synchronization is out of scope.
- The original music may already contain peak-level samples; preserving it does not certify that the source itself was mastered perfectly.

## Regression tests

```sh
PYTHONPATH="$SKILL/scripts" "$RUNTIME/.venv/bin/python" -m unittest discover -s "$SKILL/tests" -v
```

Tests cover locale routing, lack of silent Standard fallback, cached request reuse, full request fingerprinting, budget reservation before dispatch, ambiguous timeout accounting, stale/incomplete translation rejection, alternating music/dialogue masks, repeated-take alignment, Unicode tokenization, quiet-pause edits, connected clause fitting, exact preserved PCM and delayed AAC audio on a video timeline. Final QA tests additionally cover extra slates/fillers/repeated sentences, single missing negations, changed numbers, word order, explicit aliases, Chinese characters, placement drift, natural early endings, QA version invalidation and refreshed release receipts.

The tests need only numpy, scipy, soundfile, requests and a system ffmpeg; heavy model libraries are imported lazily inside the functions that use them, and network synthesis is mocked.

The Validation section of the repository README records what has actually been verified. Future code changes should rerun relevant tests and update that evidence, without claiming old validation covers new audio behavior.

## VoiceStudio ideas evaluated

Reviewed [VoiceStudio at eaf8bb9](https://github.com/debpalash/VoiceStudio/tree/eaf8bb953855cab3b687d547b3835f86fa38b308) on 2026-09-13. Its [second-pass QA](https://github.com/debpalash/VoiceStudio/blob/eaf8bb953855cab3b687d547b3835f86fa38b308/backend/services/dub_qc.py) and [shared translation context](https://github.com/debpalash/VoiceStudio/blob/eaf8bb953855cab3b687d547b3835f86fa38b308/backend/services/translation_quality.py) informed the checks and guidance above. The skill implements its own checks using its existing tokenization and timeline; no VoiceStudio source, package or model is bundled.

Keep the existing audio fit rules. VoiceStudio's [Smart Fit](https://github.com/debpalash/VoiceStudio/blob/eaf8bb953855cab3b687d547b3835f86fa38b308/backend/services/fit_planner.py) can retime video and trim residual overflow, which is incompatible with full-duration alternate audio and intact words. Its Demucs integration is a possible future route for music underneath speech, but is not installed or validated here. Test separation on a representative mixed excerpt before adopting it; source separation can leave speech residue and change the music. The project's [benchmark table](https://github.com/debpalash/VoiceStudio/blob/eaf8bb953855cab3b687d547b3835f86fa38b308/docs/benchmarks.md) had no verified rows at this revision, so it does not establish a speed or quality gain over this runtime.

## Sources

- [Agent Skills specification](https://agentskills.io/specification)
- [Codex skill discovery and personal skill location](https://learn.chatgpt.com/docs/build-skills)
- [AudioSet AST model](https://huggingface.co/MIT/ast-finetuned-audioset-10-10-0.4593)
- [Silero VAD repository](https://github.com/snakers4/silero-vad)
- [MLX Whisper repository](https://github.com/ml-explore/mlx-examples/tree/main/whisper)
- [FFmpeg audio filters](https://ffmpeg.org/ffmpeg-filters.html)

AST's loaded checkpoint exposes 527 output labels on this runtime. The implementation selects speech/music/singing classes by their names, not hardcoded class indices.
