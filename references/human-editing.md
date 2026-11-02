# Human recording editing

The aligner is an assistant to editing, not an authority on which performance is correct. The agent running this skill performs the review without turning every decision into a user question.

## Select complete takes

Read `alignment-review.json` alongside the source-script text and the word-timed transcript under `recordings/<hash>/`. Inspect every proposed span and every flagged cue. Repeated paragraphs can align to a beginning from one take and an ending from another. Prefer one complete correct take. Do not blindly take the last occurrence if it is incomplete.

Add an `excluded-takes.json` file to the language directory when failed recordings must be excluded:

```json
[
  {"start": 42.50, "end": 53.12, "reason": "Incomplete first take and spoken repeat marker; keep the complete second take"}
]
```

These times refer to the **human recording**, not the video. Re-run `align JOB LOCALE --exclude /absolute/path/excluded-takes.json`. Copy/reconcile its `edits-proposal.json` into `edits.json`. Preserve deliberate changes when iterating. Take markers are data to edit out, not instructions to follow.

Small utterances such as “mm”, “uh”, “eh” can be voiced and therefore cannot be removed with a silence threshold. The aligner flags unmatched filler tokens when recognized. Inspect unmatched text and acoustic spans for others; add exact excluded source ranges. Natural breaths should not be cut so aggressively that consonants disappear or words sound pasted together.

## Recover missing words before asking for a rerecording

Long ASR can skip an initial phrase or misplace the end of the last word. Typical cases are an omitted opening clause of a sentence, and final syllables that extend well past the last ASR word timestamp. Both can usually be recovered from the existing recording.

Extract a short region around the problem from `mono-48k.wav`, convert it to mono 16 kHz, and use `dub asr EXCERPT OUTPUT --language ca`. It can help to include the complete sentence and surrounding pause. Compare with VAD endpoints and the waveform; do not trim solely at the last recognized token time.

Do not replace a factual statement because ASR wrote a wrong spelling. Proper names, loanwords and accented speech often produce spelling errors. Document a deliberate text change and ensure the selected recorded words actually say it. Request only the missing blocks if the recording truly lacks complete required speech.

## Edit schema and boundaries

Each `edits.json.cues` entry has:

- `id`: exact source cue ID.
- `block`: a block ID from `blocks`.
- `anchor`: desired absolute video start in seconds.
- `file`: relative job path or absolute path to an immutable decoded 48 kHz mono WAV.
- `source_ranges`: ordered `[start,end]` ranges in that recording. Several ranges join around removed takes/pauses.
- `text`: words intended for subtitles and final speech QA, matching what was actually read.
- `continuous_next`: true only when the following cue is the immediate continuous continuation in the same file and block, with an identical shared cut boundary.
- `linked_next`: clauses belong together; keep the next phrase within about 450 ms, allowing a natural brief pause.
- `shorten_pauses`: defaults true. Set false when a pause is deliberate/already edited.
- `trim_edges`: defaults true. Set false for already carefully selected source edges.

The aligner pads ASR bounds, searches for quiet common cuts and extends a final word toward a VAD endpoint within safe limits. Check adjacent ranges and excluded-take boundaries. The renderer rejects audio reused in multiple cues, so an overlapping cut cannot silently duplicate a phoneme.

For coarticulation there may be no quiet boundary. Keep `continuous_next: true` and the exact same end/start sample. The renderer joins those clauses **before** time stretching, then places them with no gap and no internal fade. Do not create a fake gap in the middle of a word.

Noise below about -52 dBFS lasting over 520 ms is shortened to a 240 ms pause. Speech gets about 55 ms before and 90 ms after energy-based edge trimming. Cuts have short fades; connected clauses do not. This is conservative cleanup, not voice restoration or aggressive denoising.

## Fit without damaging speech

Human phrases are grouped in roughly 25-second reading blocks of one speaker. Local tempo normally stays near 1.0 and may not exceed 1.16 by default. An optional block `preferred_tempo` between 0.88 and 1.0 can ease a rushed read if room exists. If fitting fails:

1. Check whether a bad take or long pause remained inside the selected span.
2. Check overly conservative boundaries and whether two blocks can legitimately form one continuous sentence.
3. Prefer another complete take if available.
4. Ask for a shorter reread of the specific block only if the existing recording cannot fit naturally.

Never truncate words to fill a slot. Do not simply stretch the entire recording to the video duration; music and pauses are on a different timeline.

For a partial rerecording, ingest the new file, transcribe it, then `align JOB LOCALE --cue-ids ID1 ID2`. Existing full edit decisions are required; unaffected cues retain their existing recording sources. After reconciling the proposal, recheck adjacent continuous flags and source boundaries, accept edits, render and run final QA again.

## Master and verify

The engine applies a gentle high-pass filter, compression and two-pass loudness normalization to the **new voice only**. Defaults are about -17.5 LUFS and -1.5 dBTP. Adapt if the source context calls for it; do not normalize the original music as a side effect. Full WAV uses 48 kHz, stereo, PCM16. MP3 is 192 kbps with a decoded-length check.

Read the final QA report across all cues. Resolve suspected missing tails/prefixes with short ASR and acoustic checks. State honestly whether quality was verified acoustically/with ASR or by actual listening.
