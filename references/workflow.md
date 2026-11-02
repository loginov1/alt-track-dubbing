# Operational workflow

The user can simply provide a video path and language list. This is an agent-operated workflow: the agent running this skill prepares translations and resolves edit decisions, while the CLI performs reproducible media work. It does not require a separate translation API or ask the user to run commands.

## 1. Locate or initialize

Use the `dub` executable next to this skill. In examples, `DUB` is its full path and `JOB` is a full job directory path. Quote paths; do not place source text inside shell code.

```sh
"$DUB" doctor
"$DUB" jobs
"$DUB" init "/path/video.mp4" --languages es-ES fr-FR de-DE ca-ES --source-language en
"$DUB" status "$JOB"
```

Default job folder: `<video parent>/Dubbing/<video stem>-<source hash prefix>/`. The source video is referenced, never re-encoded. `init` hashes it once per invocation and recognizes an existing job. Add `--out` for another location. The source language defaults to `auto`, detected from the first speech chunk; use an explicit language if known. Inspect multilingual interviews separately instead of assuming every speaker uses the detected language. Use `--max-usd` for an explicit initial cap; for an existing job, preserve or deliberately adjust `job.json.settings.max_usd` rather than resetting it accidentally. The job index is under `~/.local/share/alt-track-dubbing/jobs.json` (or `$ALT_TRACK_DUBBING_HOME/jobs.json`).

If a language is missing, `init` with the same video adds it. Do not overwrite prior source reviews. Changing an already translated transcript requires reconciling all affected translations and their `source_digest` explicitly.

## 2. Detect and transcribe

```sh
"$DUB" extract "$JOB"
"$DUB" analyze "$JOB"
"$DUB" source-asr "$JOB" --prompt "Names and technical terms, without speculative sentences"
"$DUB" speakers "$JOB" --count 2
```

`extract` decodes 48 kHz stereo PCM on the video time origin, including delayed audio, pre-roll and trailing silence. All delivery files use the video sample count, not the AAC stream's padded length.

`analyze` combines local Silero VAD with an AudioSet speech/music classifier. The source audio remains local. Model download happens once. `source-asr` transcribes conversation candidates and every VAD candidate, including ones the classifier rejected as musical. This makes speech-like sounds outside proposed dialogue visible for review. If `speech_outside_proposed_dialogue` contains a region with no VAD candidate, extract that region and run the generic `asr` command too.

Review these files:

- `analysis/regions.json`: whole dialogue intervals and complementary preserved intervals; classifier evidence and mixed-audio warnings.
- `analysis/cues.json`: ordered cue IDs, source text, start/end, speaker and word timing evidence.
- `analysis/transcript.json/.txt`: original local ASR evidence.
- `analysis/speakers-proposal.json`: anonymous speaker labels and clustering confidence.

Correct source names, numbers and clear ASR mistakes. Delete hallucinations over music. Split cues at actual speaker changes. Merge chopped clauses when appropriate. Copy reviewed speaker labels into cues; if clustering is unreliable, keep A for all cues. Do not infer speaker identity or gender from clustering.

Review **all music transitions**, short musical inserts and any long gap between utterances. Refine classifier/VAD boundaries with the actual waveform and short excerpts. Conversation masks must cover original speech tails, breaths/fillers that should disappear, and inter-utterance pauses. Preserve real musical inserts separately. When editing `dialogue`, recompute `preserve` as its complement using `core.minus`; do not leave overlapping classifications.

With speech over music, the current renderer replaces the entire mixture during speech. It preserves music in separate inserts. Retaining music under speech requires a clean music stem and a deliberate stem-mixing extension; do not pretend the original mixture can be copied without also restoring original voices. The pipeline is designed for material with clean speech and separate music inserts.

```sh
"$DUB" accept-source "$JOB" --note "Describe corrected names, speakers, speech/music boundaries and resolved outside-speech candidates."
```

This receipt is the agent's quality check. The user is not being asked to approve a routine intermediate stage.

## 3. Translate and choose the voice route

For each language:

```sh
"$DUB" translation "$JOB" fr-FR
```

Fill `languages/fr-FR/translation.json` in bounded batches, using a shared context and terminology brief from the whole transcript. Preserve IDs and order. `text` is the natural translated sentence, `tts_text` is an optional pronunciation-friendly variant, `human_text` is an optional fully written-out reading version. Leave the locale and source digest intact. Follow [translation guidance](translation.md). Confirm complete coverage before synthesis.

```sh
"$DUB" voices --refresh
"$DUB" plan "$JOB" fr-FR
```

Read and briefly report the character-count estimate summed across languages. Verify the current official Google pricing page once per new session before quoting a price or making paid requests; if it changed, update `job.json.settings.chirp_usd_per_million`. Default setting: USD 30/million Chirp characters, including spaces (list price checked 2026-11-02). `list_price_usd` estimates the additional uncached synthesis; `full_text_list_price_usd` shows the whole translation's equivalent list price. The ledger includes attempted/retried requests conservatively; remaining free allowance is unknown. Cost is for text sent to TTS, not the music or full video duration. New request variants cost again; unchanged request hashes reuse cached clips.

`mode: auto` selects exact-locale Chirp when present and human otherwise. `mode: human` forces a human reading script. `mode: google` refuses unsupported locales. A 403/network failure is an account/connection issue, not evidence that a language lacks Chirp. If Google is unavailable, continue the independent translation/reading work while reporting the concrete blocker.

## 4A. Google branch

```sh
"$DUB" synthesize "$JOB" fr-FR
"$DUB" accept-edits "$JOB" fr-FR --note "Reviewed all generated cue selections and exact-locale speaker voice mapping."
"$DUB" render "$JOB" fr-FR
```

TTS uses Charon and Orus by default for A/B if present in the catalog. Additional speakers get different available stock voices. Explicit `translation.json.voices` overrides must still belong to the exact locale and Chirp family.

If a phrase exceeds the fit limit, inspect the offending slot and trim report. Prefer a shorter natural translation preserving facts. Change only that cue's `text`/`tts_text`, synthesize again, reaccept edits, and rerender. A modest `speaking_rate` in the translated cue is supported; it changes the request hash and costs another synthesis for that cue. Do not cut speech midword or raise the tempo ceiling just to make a test pass.

If a pronunciation changes, preserve the intended subtitle `text`, and update only `tts_text`. Re-run QA for the resulting output. For genuinely continuous clauses, review whether merging source cues before translation is the better choice.

## 4B. Human branch

```sh
"$DUB" script "$JOB" ca-ES
```

Deliver `reading-script.txt` (or Markdown) and `HOW-TO-RECORD.md`. Reading blocks are short natural groups with a speaker, source time window, duration and IDs. One continuous M4A recording is fine. User reads naturally and repeats whole sentences after mistakes; no need to wait for video timestamps.

When the recording arrives:

```sh
"$DUB" ingest "$JOB" ca-ES "/path/Voice Memo.m4a"
"$DUB" recording-asr "$JOB" ca-ES
"$DUB" align "$JOB" ca-ES
```

Follow [human editing](human-editing.md). The first alignment writes `edits.json` and `edits-proposal.json`. Later runs update the proposal, preserving deliberate edits. Reconcile changes into `edits.json` yourself.

```sh
"$DUB" accept-edits "$JOB" ca-ES --note "Describe chosen complete takes, removed failures/fillers and checked word boundaries."
"$DUB" render "$JOB" ca-ES
```

When waiting for the user's recording, finish every available Google language and deliver its final files, plus the remaining reading script and durable job path. Waiting for a recording is the expected branch boundary, not a reason to leave all other languages unfinished.

## 5. Check and release every language

```sh
"$DUB" qa-asr "$JOB" fr-FR
"$DUB" release "$JOB" fr-FR --note "Summarize the final content, take, pronunciation and boundary checks; explain ASR spelling false alarms."
"$DUB" status "$JOB"
```

`render` verifies exact WAV duration, MP3 decoded sample count, untouched original PCM outside dialogue, zero original audio in the dialogue mask and zero samples between new cues. It refuses clipped new voice or overlapping placements. The original music itself may already reach 0 dBFS; it remains unchanged.

`qa-asr` independently recognizes every rendered cue in consecutive short groups and flags missing/mismatched text. Read **all groups**, focusing on low coverage, first/last words, technical names, units and repaired takes. A short second ASR pass on a suspicious excerpt can distinguish an ASR spelling error from a real omission. A transcript alone cannot certify naturalness or pronunciation with a native listener's confidence.

Inspect `groups[].text_check`: it compares the complete expected and recognized token sequences and lists insertions, deletions and substitutions. A spoken take marker, doubled sentence or trailing filler can have 100% cue coverage and still require review. Every normalized difference is flagged, including a single missing negation or changed number. `flagged_groups` therefore matters independently of `flagged_cues`. Resolve the discrepancy against evidence; never replace the translation with ASR output automatically.

Inspect `timing.cue_checks` in the same report. These compare final clip placements with both source cue starts and deliberate edit anchors. Review hints flag start shifts over 0.5 seconds, endings more than 0.8 seconds past the source cue, or tempo changes over 10%. They do not block release: connected human clauses and deliberate pauses may justify them. The renderer's existing hard fit limits still apply. Clip placement is not measured word onset or lip synchronization, and a shorter translation ending early is not an error.

Review transitions into and out of music. Do not claim lip-level synchronization: this workflow fits phrase/block windows with pitch-preserving tempo changes and does not edit video.

`release` binds the final output, QA, source and edit decisions by hashes. Deliver only the current release. Output folders are versioned by render fingerprint, so revisions do not destroy previous exports.

An older QA format must be refreshed with `qa-asr` before issuing a new release. Unchanged audio excerpts and ASR checkpoints are reused while the text/timing report is rescored; this does not trigger TTS or overwrite old exports. Review the updated report and run `release` again.

Final response: complete WAV/MP3 links, video duration, job directory, and any actual unresolved issue. The MP3 preview is not the full upload file. Do not upload anything to YouTube unless separately asked.
