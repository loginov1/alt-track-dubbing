# Translation and voice text

The agent running this skill translates the reviewed source in the current session and edits `translation.json`; no separate translation service is required. The TTS key can only call Google Text-to-Speech. Do not try to repurpose it for Gemini or search unrelated credential files.

Use natural spoken language, concise enough for the original phrase window. Preserve facts, units, comparisons, qualifications and speaker intent. Avoid literal English constructions that sound foreign. Preserve source cue IDs and chronological coverage exactly.

## Context and terminology across batches

Before translating the first batch, read the whole reviewed transcript for subject, speaker intent and recurring names/technical terms. Save a short context brief and approved source-to-target term choices as strings in the existing `translation.json.pronunciation_notes` list. Include pronunciation variants only where needed; explicit user terminology takes precedence. Reuse these notes across batches and when resuming the job, so the same component does not acquire several unrelated translations. This is agent-maintained guidance, not automatic word substitution.

After drafting a batch, compare it with the source and neighboring cues once more: remove stiff phrasing, resolve ambiguous references, and shorten wordy lines without dropping facts, negations, qualifications or units. Check the actual `tts_text` or `human_text` against the available window too, since spoken numbers and units can be much longer than their written abbreviations. An estimate is a review hint; rendered duration is the fit check. Revise only the affected cues when generated speech is too long. The agent performs these passes in-session, with no additional translation API or model installation.

Each translated cue has three text fields:

- `text`: natural translation for timing review, subtitles and the listener.
- `tts_text`: optional text tailored to the stock voice's pronunciation. If blank, the engine uses `text`.
- `human_text`: optional readable spelling of numbers/units and pronunciation-friendly names for the user's recording. If blank, the reading script uses `text`.

Do not add “say slowly”, speaker labels, stage directions or block numbers to spoken text. Do not use `<break>` or other SSML tags in this text-only engine. Chirp's service capabilities can evolve, but this implementation intentionally uses plain text.

## Locale defaults

| Request | Exact locale | Default handling |
|---|---|---|
| Spanish | es-ES | Spain, never silently es-US |
| French | fr-FR | France |
| German | de-DE | Germany |
| Catalan | ca-ES | Human if no Chirp appears in live catalog |
| Korean | ko-KR | Korean |
| Chinese | cmn-CN | Mandarin, simplified-script reading text |
| Arabic | ar-XA | Modern Standard Arabic |

For other variants ask only if the user's intended locale cannot reasonably be inferred. Do not silently equate Mandarin, Cantonese and all varieties of Chinese. An explicit user dialect preference overrides these defaults.

## Practical lessons

- Render power and energy as different units: kW is power, kWh is energy. Verify numbers against the source context before translating.
- A product name, brand or person's surname may need a pronunciation variant but must remain recognizable. Do not introduce an ASR-generated alternative brand.
- A stock voice can read punctuation awkwardly, for example an elided article joined with an apostrophe. A natural sentence rephrase usually solves it. Check actual synthesized words rather than assuming punctuation always works.
- In some languages a unit is spoken more reliably when it is written out in the target script, while a Latin-script brand name can stay in Latin script. Check this per language; it is not a universal phonetic law.
- A Standard or WaveNet voice existing for a locale does not prove it meets the quality bar. Manual narration is the fallback when no exact-locale Chirp 3 HD voice exists.
- A full translated sentence may be shorter than the source. Keep a natural pause; do not slow it dramatically simply to fill every millisecond.

For ASR matching, `alignment_aliases` maps equivalent spellings to a canonical token form. Example:

```json
{"alignment_aliases": {"deu": "10", "quilowatts": "kw"}}
```

Populate these for numbers/units when the language needs them. They affect only matching, not speech or subtitles. Do not use aliases to hide a real missing phrase or changed fact. Multiword aliases are applied within script text; ASR tokens are processed word by word, so verify cases spanning several recognized words.

## Costs and sources

Checked 2026-11-02: the Google pricing page lists Chirp 3: HD voices at USD 30 per million characters after a free tier of 1 million characters per month, counting spaces and newlines. The account's remaining free allowance is not exposed by this pipeline, so estimates conservatively use list price. The default job cap includes retries and attempted requests with unknown outcomes. Use `voices --refresh` for actual supported locales.

- [Google Chirp 3 HD documentation](https://docs.cloud.google.com/text-to-speech/docs/chirp3-hd)
- [Google Text-to-Speech pricing](https://cloud.google.com/text-to-speech/pricing)
- [Voice list API](https://docs.cloud.google.com/text-to-speech/docs/reference/rest/v1/voices/list)

Synthesis sends translated text to Google; detection, transcription, human recording editing and final mixing run locally. A full video or human voice recording is not uploaded by these scripts.
