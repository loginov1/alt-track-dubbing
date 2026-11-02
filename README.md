# alt-track-dubbing

An agent skill plus a Python CLI that turns a complete video into full-length, synchronized audio tracks in other languages, ready to upload with YouTube's multi-language audio feature.

Deterministic local tools do the audio work. The agent running the skill does the judgment: translation, speech and music boundaries, speaker assignment and take selection. It signs hash-bound review receipts, so any upstream change invalidates the downstream work that depended on it. Synthesized voices come from Google Cloud Text-to-Speech (Chirp 3 HD) in the exact requested locale only; a language without such a voice goes through a human-recording branch instead.

<!-- Example video: replace this comment with a link to a video on your channel that has alternate audio tracks made with this tool, for example: **Example:** [a video with alternate audio tracks](https://www.youtube.com/watch?v=VIDEO_ID) (choose the audio track in the player settings). -->

## The problem

YouTube lets a creator attach extra audio tracks in other languages to a video. Each track has to be one file that runs for the full length of the video, stays in sync with the picture, and keeps the original music where nobody is speaking. By hand, that means transcribing, translating, voicing, cutting and mixing every line, for every language, in an audio editor.

This project splits that job in two. Local tools produce sample-exact audio and checks that can be verified. An AI agent makes the decisions that need judgment and records what it reviewed. The video and any voice recording stay on the machine; only translated text is sent to the paid TTS API.

## Pipeline

| Stage | Command | Done by | Runs |
|---|---|---|---|
| Job setup: hash the source once, read the exact video timeline, set languages and budget | `init` | Tool | Local |
| Extract 48 kHz stereo PCM on the video timeline (delayed audio, pre-roll and AAC padding handled), plus 16 kHz mono for analysis | `extract` | Tool | Local |
| Speech and music detection: Silero VAD plus an AudioSet classifier propose dialogue episodes and preserved music | `analyze` | Tool proposes, agent reviews | Local |
| Word-timed transcription and cue proposal (MLX Whisper on Apple Silicon, faster-whisper on Linux) | `source-asr` | Tool proposes, agent corrects | Local |
| Anonymous speaker clustering | `speakers` | Tool proposes, agent assigns | Local |
| Source review receipt | `accept-source` | Agent | Local |
| Translation into a template that holds every cue ID | `translation` | Agent | Agent session |
| Voice route and cost estimate: exact-locale Chirp 3 HD, otherwise a human recording | `voices`, `plan` | Tool; agent reports the estimate | Google voice list API |
| Synthesis through a content-addressed cache and a cost ledger | `synthesize` | Tool | Google TTS (translated text only) |
| Human branch: timed reading script, verified recording backup, recording ASR, take alignment | `script`, `ingest`, `recording-asr`, `align` | Tool proposes, agent selects takes | Local |
| Edit review receipt | `accept-edits` | Agent | Local |
| Render: voice-only mastering, sample-exact full-length mix, WAV, MP3, SRT and preview, with built-in checks | `render` | Tool | Local |
| Final QA: independent ASR of every rendered cue, whole-sequence text diff, placement and tempo review | `qa-asr` | Tool flags, agent reviews | Local |
| Release receipt over the output hashes | `release` | Agent | Local |

`render` refuses to finish unless the WAV has exactly the video's sample count, the decoded MP3 has the same length, every sample outside dialogue equals the decoded original, no original audio remains inside dialogue episodes, and no sample between new cues is non-zero. It also refuses clipped voice and overlapping placements.

## How work is split between the agent and the tools

The CLI (`scripts/dub`) is deterministic. It decodes, detects, transcribes, aligns, fits, masters, mixes and measures, and it never decides what is correct. Each detector writes a proposal file next to the decision file it seeds: `regions-proposal.json` and `regions.json`, `cues-proposal.json` and `cues.json`, `edits-proposal.json` and `edits.json`. Running a detector again refreshes the proposal and never overwrites a decision the agent has already edited.

The agent edits the decision files. It fixes ASR mistakes and names, deletes hallucinations over music, splits cues at speaker changes, moves dialogue and music boundaries, writes the translation, and chooses complete takes while excluding failed ones. Then it signs a review receipt with a written note of what it checked:

| Receipt | Command | Bound by hash to |
|---|---|---|
| `analysis/source-reviewed.json` | `accept-source` | The source video's SHA-256, the dialogue and music regions, and every cue's text, timing and speaker |
| `languages/<locale>/edits-reviewed.json` | `accept-edits` | The full edit map and the SHA-256 of every audio file it cuts from |
| `languages/<locale>/release.json` | `release` | The render, the translation, the final QA report and the SHA-256 of every exported file |

Every stage checks the digests of its inputs before it runs. Change one cue and the source receipt no longer matches, so translation, synthesis, rendering and release all refuse until the agent reviews the source again. A translation records the source digest it was written against and must cover every cue exactly once, in order. Replacing a recording changes its hash, which invalidates the edit receipt and the render fingerprint. The QA report carries its own version, so changing the QA criteria forces a fresh report and a new release receipt without re-synthesizing or re-rendering audio. Exports are versioned by render fingerprint, so a revision never destroys an earlier export.

The receipts are the agent's own quality gate, not approval prompts for the user. They make "finished" a state that can be checked: `dub status JOB` reports, per language, whether the release still matches the current render, translation and QA report.

## Cost safety

Google bills Chirp 3 HD per character of input text. As of 2026-11-02, [Google's pricing page](https://cloud.google.com/text-to-speech/pricing) lists Chirp 3: HD voices at US$30 per 1 million characters after a free tier of 1 million characters per month, and counts spaces and newlines. The tool uses US$30 per million (`job.json.settings.chirp_usd_per_million`); check the page before relying on it, since prices change.

- Estimate first: `plan` counts the characters that would be sent, separates cached cues from new ones, and reports the list price before any paid request. The skill tells the agent to report this estimate before synthesis.
- Content-addressed cache: each request is keyed by the SHA-256 of the endpoint plus the complete JSON request (text, locale, voice, speaking rate, audio format). An unchanged line is never paid for twice, and editing one line re-synthesizes only that line.
- Reserve before dispatch: every attempt is written to `cloud-ledger.json`, atomically and fsynced, with its cost before the network call, and updated with the outcome afterwards.
- Timeouts count: Google offers no idempotency key for this call, so a timed-out request may still have been billed. The ledger records it as `network_outcome_unknown` and keeps its cost reserved.
- Hard cap per job: before each of at most three attempts per request, the reserved total is checked against the job's cap (`--max-usd`, default US$5, stored in `job.json`). Retries happen only after a 429 or 5xx response, with backoff.
- No silent voice fallback: only Chirp 3 HD voices of the exact locale are used. If there is none, `auto` mode moves the language to the human-recording branch and `google` mode refuses. A Standard or WaveNet voice, or another region's accent, is never substituted.
- No free tier assumed: the remaining free allowance is not visible to the tool, so estimates use the list price.
- Key hygiene: the key travels in the `x-goog-api-key` header, never in a URL, and is never written to job files. HTTP errors are reduced to the status code and Google's status name before they are shown.

## Privacy

- Video and voice recordings never leave the machine. Decoding, speech and music detection, ASR, speaker clustering, take alignment, mastering, mixing and final QA all run locally.
- Google TTS is a paid API used with your own key. It receives only translated text (plus the voice list request).
- Translation is done by the agent running the skill, so source and translated text pass through whichever model provider that agent uses. No separate translation API is called.
- Other network use: `bootstrap.py` installs Python packages from PyPI through uv, and the first `analyze` and ASR runs download model weights from Hugging Face into the runtime directory. There is no telemetry.
- The tool does not upload to YouTube or change anything on a channel. You upload the finished WAV or MP3 yourself. Whether multi-language audio is available for a channel or video is controlled by YouTube.
- Speaker clustering is anonymous (A, B, ...). The skill tells the agent not to infer anyone's identity or gender.

## Requirements

- macOS on Apple Silicon: validated, with the MLX Whisper backend.
- Linux: the faster-whisper backend is implemented but untested. The unit tests run on Ubuntu in CI.
- Windows: not supported (POSIX file locks and a POSIX shell launcher).
- [uv](https://docs.astral.sh/uv/), which installs a private Python 3.11, and ffmpeg/ffprobe with libmp3lame, as system dependencies.
- Disk space for the runtime and models (PyTorch, Transformers and a Whisper large-v3-turbo checkpoint): several GB.
- On machines with 8 GB RAM, run the heavy stages one at a time; the skill tells the agent to do so.
- For synthesized voices: a Google Cloud project with the Text-to-Speech API and billing enabled, and an API key. The human-recording branch needs no key.
- An agent that supports Agent Skills (`SKILL.md`). The skill has been used with Codex. Claude Code reads the same `SKILL.md` format, but this skill has not been tested with it.

## Install

```sh
# 1. System tools (macOS; on Linux use your package manager for ffmpeg and the uv installer)
brew install uv ffmpeg

# 2. Put the skill where your agent discovers personal skills
git clone https://github.com/loginov1/alt-track-dubbing ~/.agents/skills/alt-track-dubbing
# Claude Code (untested): clone into ~/.claude/skills/alt-track-dubbing instead

# 3. Create the private runtime in ~/.local/share/alt-track-dubbing
python3 ~/.agents/skills/alt-track-dubbing/scripts/bootstrap.py
~/.agents/skills/alt-track-dubbing/scripts/dub doctor
```

`bootstrap.py` creates `~/.local/share/alt-track-dubbing/.venv` with `scripts/requirements.txt`, plus MLX Whisper on Apple Silicon or faster-whisper elsewhere. Models are downloaded on first use into `~/.local/share/alt-track-dubbing/models`. If you already have a working runtime, `--reuse-venv PATH` and `--reuse-models PATH` copy it instead of installing (copy-on-write clones on macOS).

Then store the Google key, only if you want synthesized voices:

```sh
mkdir -p ~/.config/alt-track-dubbing && chmod 700 ~/.config/alt-track-dubbing
touch ~/.config/alt-track-dubbing/google-text-to-speech.json
chmod 600 ~/.config/alt-track-dubbing/google-text-to-speech.json
```

Open that file in an editor and save `{"api_key": "<your Text-to-Speech API key>"}`. In the Google Cloud console, restrict the key to the Text-to-Speech API. Only `api_key` is read, and the file is never copied into a job.

| Environment variable | Default | Purpose |
|---|---|---|
| `ALT_TRACK_DUBBING_HOME` | `~/.local/share/alt-track-dubbing` | Runtime: virtualenv, models, job index, voice catalog cache |
| `ALT_TRACK_DUBBING_GOOGLE_TTS_CREDENTIALS` | `~/.config/alt-track-dubbing/google-text-to-speech.json` | Path of the key file |
| `ALT_TRACK_DUBBING_GOOGLE_TTS_API_KEY` | unset | Key value; takes precedence over the file |

## Use as a skill

You give the agent a video path and a list of languages; it runs the CLI, reviews the evidence and returns finished files. In Codex, mention the skill with `$alt-track-dubbing`. In other agents, name the skill or just describe the task. Example prompts:

> Use $alt-track-dubbing. Here is the video: /full/path/to/video.mp4. Make tracks in Spanish from Spain, French, German, Korean, Chinese and Arabic. For Catalan, prepare a reading script; I will record it myself.

> Use $alt-track-dubbing. Continue the dubbing job in /full/path/Dubbing/video-name-hash. Here is my Catalan recording: /full/path/recording.m4a. Remove the failed takes and build the finished track.

> Use $alt-track-dubbing to dub /full/path/to/video.mp4 into French and German, with a US$1 cap on synthesis.

The agent finds speech and music inserts, checks the timings, translates the text and builds the tracks. For a language you record yourself, it hands you a script of short blocks, each with a guide to its place and duration in the video, plus a `HOW-TO-RECORD.md` guide. You can record everything as one file in Voice Memos. Leave short pauses between blocks, and after a mistake repeat the whole sentence. There is no need to follow the video's timecodes while recording.

For each language you get:

- A full WAV (48 kHz, stereo, 16-bit PCM) and MP3 (192 kbps) with the exact duration of the source video.
- The original music in the music inserts, sample-identical to the decoded original in the WAV.
- New speech in the dialogue episodes; the original voices and fillers do not bleed through.
- A short preview, an SRT with the dubbed timings, an edit list and quality reports.
- Checks for extra words, repeats and omissions, and flags for lines that moved or sped up noticeably.
- Saved translations and recordings, so a later session can resume or fix a single line.

Files are written next to the video, in `Dubbing/<video-name>-<hash>/deliverables/<locale>/<render-hash>/`. Inside the job folder, `languages/<locale>/release.json` points to the current finished export. Earlier versions stay available.

## CLI reference

The agent normally drives the CLI, but it can be run directly. Every command prints JSON; `scripts/dub --help` and `scripts/dub COMMAND --help` give exact arguments. `LOCALE` accepts an explicit locale such as `es-ES`, or a short alias such as `es`, `fr` or `zh` (mapped to `es-ES`, `fr-FR`, `cmn-CN`).

| Command | Arguments | What it does |
|---|---|---|
| `doctor` | | Runtime, ffmpeg and dependency check; whether a Google key is configured |
| `jobs` | | Durable index of known jobs |
| `init` | `VIDEO --languages L [L ...] [--out DIR] [--source-language auto] [--max-usd 5]` | Create a job, or add languages to an existing one |
| `status` | `JOB` | Stage files per language, and whether each release is current |
| `extract` | `JOB` | Original PCM on the video timeline and analysis audio |
| `analyze` | `JOB` | VAD and classifier evidence; dialogue and music proposal |
| `source-asr` | `JOB [--backend auto] [--prompt TEXT]` | Word-timed transcript and cue proposal |
| `speakers` | `JOB [--count 2]` | Anonymous speaker cluster proposal |
| `accept-source` | `JOB --note TEXT` | Source review receipt |
| `translation` | `JOB LOCALE [--mode {auto,google,human}]` | Create the translation template |
| `voices` | `[--refresh]` | Live Chirp 3 HD catalog, counted per locale |
| `plan` | `JOB LOCALE` | Voice route and character and cost estimate |
| `synthesize` | `JOB LOCALE` | Cached Google synthesis and per-cue edit decisions |
| `script` | `JOB LOCALE` | Reading script (TXT, MD, CSV), timing manifest and recording guide |
| `ingest` | `JOB LOCALE RECORDING` | Verified immutable backup of a recording, plus decoded WAVs |
| `recording-asr` | `JOB LOCALE [--backend auto]` | Word-timed transcript of the recording |
| `align` | `JOB LOCALE [--exclude FILE] [--cue-ids ID [ID ...]]` | Take-selection proposal |
| `asr` | `AUDIO_16K OUTPUT --language L [--no-words] [--backend auto] [--prompt TEXT]` | Transcribe a short 16 kHz mono clip for diagnosis |
| `accept-edits` | `JOB LOCALE --note TEXT` | Edit review receipt |
| `render` | `JOB LOCALE` | Master, mix and export with built-in checks |
| `qa-asr` | `JOB LOCALE [--backend auto]` | Final independent ASR and review report |
| `release` | `JOB LOCALE --note TEXT` | Release receipt over the output hashes |

A typical Google-voice run, end to end:

```sh
DUB=~/.agents/skills/alt-track-dubbing/scripts/dub
"$DUB" init "/path/video.mp4" --languages fr-FR --source-language en
JOB="/path/Dubbing/video-<hash>"   # printed by init
"$DUB" extract "$JOB"
"$DUB" analyze "$JOB"
"$DUB" source-asr "$JOB"
"$DUB" speakers "$JOB" --count 2
"$DUB" accept-source "$JOB" --note "Reviewed boundaries, names and speakers"
"$DUB" translation "$JOB" fr-FR    # then fill languages/fr-FR/translation.json
"$DUB" plan "$JOB" fr-FR
"$DUB" synthesize "$JOB" fr-FR
"$DUB" accept-edits "$JOB" fr-FR --note "Reviewed generated cues and voices"
"$DUB" render "$JOB" fr-FR
"$DUB" qa-asr "$JOB" fr-FR
"$DUB" release "$JOB" fr-FR --note "Reviewed final QA groups and timing flags"
```

The full procedure, including the human-recording branch, is in [references/workflow.md](references/workflow.md). State files and invalidation rules are in [references/implementation.md](references/implementation.md).

## Repository layout

| Path | Contents |
|---|---|
| `SKILL.md` | Skill entry point and the agent's operating rules |
| `references/` | Workflow, translation, human-recording editing, implementation and recovery notes |
| `agents/openai.yaml` | Optional Codex UI metadata (display name, default prompt) |
| `scripts/dub`, `scripts/dub.py` | CLI launcher and entry point |
| `scripts/dubkit/` | Pipeline modules: `core` (jobs, digests, receipts), `audio`, `analyze`, `cloud`, `human`, `qa`, `review` |
| `scripts/bootstrap.py`, `scripts/requirements.txt` | Private runtime installer and its dependencies |
| `tests/` | Unit tests and their light dependencies |

## Validation

This is what has actually been verified. The real-media numbers come from the evidence log of the version this repository was extracted from; that media is private and not included here.

Platform for the real-media checks: macOS 26 on Apple Silicon (arm64), Python 3.11, MLX Whisper backend, with torch 2.14, transformers 5.17, silero-vad 6.2, mlx-whisper 0.4.3 and resemblyzer 0.1.4.

| Check | Scope | Result |
|---|---|---|
| Unit tests | 24 tests of the state machine, cost ledger, locale routing, alignment, fitting, mixing and QA, run on this repository in a fresh virtualenv with only numpy, scipy, soundfile and requests, with heavy imports and network access blocked | Pass on macOS arm64 with Python 3.11.15 (2026-11-02) |
| Speech and music boundaries | 72-second fixture with real music and two known dialogue intervals | Detected boundaries within 0.6 s of the expected ones |
| Full-length render, both branches | A real interview video of about 22 minutes with separate music inserts and 120 cues, re-rendered from reviewed decisions through the human-recording branch and the Chirp 3 HD branch (cached clips) | Sample count equal to the video's; preserved music bit-identical to the decoded original; no original audio inside dialogue; no non-zero samples between cues; exact decoded MP3 length; voice mastered to about -17.5 LUFS with true peak at or below -1.5 dBTP; zero paid requests |
| Take alignment | The same human recording | All 120 cues matched; 34 flagged for agent review |
| Final ASR QA | The same human track | All 120 cues covered; 27 flagged and reviewed: number formatting, proper-name spelling and phrasing variants, no missing dialogue |
| Live voice catalog | Google voice list, 2026-09-12 | 30 Chirp 3 HD voices each for es-ES, fr-FR, de-DE, ko-KR, cmn-CN and ar-XA; none for ca-ES, which therefore used the human branch |
| Source language detection | 12-second excerpt | Detected correctly |
| Final QA version 2 (whole-sequence text comparison, placement review) | Unit tests; a synthetic 8-second full render with real ffmpeg export and mastering (ASR mocked); rescoring of 6 saved QA reports | Pass; rescoring found 4 of 40 groups with inserted words that the earlier per-cue check had not flagged (review candidates, not confirmed defects) |
| Skill format | `SKILL.md` frontmatter against the Agent Skills specification and the rules of the Codex and Anthropic skill validators | Pass (2026-11-02) |

Not validated:

- Linux and the faster-whisper backend. The code path exists but has never been run end to end; CI runs only the unit tests on Ubuntu.
- Claude Code as the host agent.
- A clean `bootstrap.py` install on a fresh machine, end to end.
- Native-listener quality of any translation, pronunciation or voice.
- Music playing under speech (see Limitations).
- The full-length render predates later hardening of the cache metadata and the version 2 QA changes, and was not rerun on the final code. Those later changes are covered by the unit tests and the synthetic full render only.
- Arbitrary videos. Boundaries and takes are proposals that the agent has to review.

## Limitations

- No source separation under music. When music plays under speech, the whole dialogue episode is replaced with new speech and silence, so that music is lost inside the episode. Separate music inserts are preserved exactly.
- No native-listener validation. Final QA re-transcribes the dub with an independent ASR pass and compares the text. ASR coverage is evidence, not a quality score, and it cannot judge pronunciation or naturalness.
- Heavy local models. PyTorch, an AudioSet transformer and Whisper large-v3-turbo run locally; the first run downloads several GB, and long videos take a while on a laptop.
- Detectors propose, they do not decide. VAD and classifier boundaries can be off, especially around singing, engine noise or overlapping speakers.
- Timing is fitted per phrase or block, with pitch-preserving tempo changes up to 1.16x for human recordings and 1.25x for synthesized speech by default. There is no word-level synchronization.
- Locale coverage depends on Google's live Chirp 3 HD catalog.
- Trimming pauses cannot repair echo, overload or heavy noise in a recording.

## What this is not

These are deliberate design choices, not missing features:

- No voice cloning. Voices are Google's stock Chirp 3 HD voices or your own recorded voice.
- No lip sync. Speech is fitted to phrase windows, not to mouth movements.
- No video generation or video editing. The source video is read, hashed and never re-encoded.
- No YouTube upload. You upload the finished audio files yourself.

## Third-party software and models

This repository contains only its own source code and documentation. No third-party code, binaries or model weights are bundled. `bootstrap.py` installs the packages below from PyPI; model weights are downloaded on first use or ship inside their pip packages; ffmpeg and uv are system dependencies. Licences were checked against PyPI and Hugging Face metadata on 2026-11-02.

| Package | Used for | Licence |
|---|---|---|
| numpy | Audio arrays | BSD-3-Clause |
| scipy | Constrained fitting of connected clauses | BSD-3-Clause |
| soundfile | WAV reading and writing (its wheels bundle libsndfile, LGPL-2.1) | BSD-3-Clause |
| requests | Google TTS HTTP calls | Apache-2.0 |
| torch | Model runtime | BSD-3-Clause (the wheel also bundles other permissive components) |
| torchaudio | Installed alongside Silero VAD | BSD-2-Clause |
| transformers | AudioSet classifier | Apache-2.0 |
| huggingface-hub | Model downloads | Apache-2.0 |
| silero-vad | Voice activity detection | MIT |
| onnxruntime | Runs the Silero VAD ONNX model (pulls in flatbuffers, Apache-2.0, and protobuf, BSD-3-Clause) | MIT |
| resemblyzer | Speaker embeddings (pulls in librosa, ISC, and webrtcvad, MIT) | Apache-2.0 |
| scikit-learn | Speaker clustering | BSD-3-Clause |
| mlx-whisper (Apple Silicon only) | ASR (pulls in mlx, MIT) | MIT |
| faster-whisper (other platforms) | ASR (pulls in ctranslate2, MIT, and PyAV, BSD-3-Clause, whose wheels bundle FFmpeg libraries) | MIT |
| ffmpeg and ffprobe (system) | Decoding, resampling, tempo, loudness normalization, MP3 encoding | LGPL-2.1-or-later or GPL-2.0-or-later, depending on the build |
| uv (system) | Runtime bootstrap | MIT or Apache-2.0 |

The unit tests need only numpy, scipy, soundfile, requests and ffmpeg.

| Model | Used by | Licence | How it arrives |
|---|---|---|---|
| [MIT/ast-finetuned-audioset-10-10-0.4593](https://huggingface.co/MIT/ast-finetuned-audioset-10-10-0.4593), an Audio Spectrogram Transformer fine-tuned on AudioSet | `analyze` (speech, music and singing scores) | BSD-3-Clause (model card) | Hugging Face download on the first `analyze` |
| [mlx-community/whisper-large-v3-turbo](https://huggingface.co/mlx-community/whisper-large-v3-turbo) | ASR on Apple Silicon | An MLX conversion of OpenAI's [whisper-large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo), which is MIT; the conversion repository declares no licence of its own | Hugging Face download on the first ASR run |
| [mobiuslabsgmbh/faster-whisper-large-v3-turbo](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo) | ASR on Linux (faster-whisper's `large-v3-turbo` alias) | MIT | Hugging Face download on the first ASR run |
| Silero VAD (ONNX) | Voice activity in `analyze` and `recording-asr` | MIT | Ships inside the `silero-vad` package |
| Resemblyzer voice encoder | `speakers` | Apache-2.0 | Ships inside the `resemblyzer` package |
| Google Chirp 3 HD voices | `synthesize` | Paid Google Cloud service under Google's terms | Remote API; nothing is downloaded |

## Acknowledgements

The whole-sequence final QA check and the shared translation context guidance were informed by reviewing [VoiceStudio](https://github.com/debpalash/VoiceStudio) at commit [eaf8bb9](https://github.com/debpalash/VoiceStudio/tree/eaf8bb953855cab3b687d547b3835f86fa38b308). No VoiceStudio code, package or model is included; the checks are implemented independently on this project's own tokenization and timeline. Details are in [references/implementation.md](references/implementation.md).

## License

MIT, see [LICENSE](LICENSE). Third-party packages and models keep their own licences, listed above.
