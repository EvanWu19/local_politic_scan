# Podcast Backfill Run — May 7–10

You are completing a podcast backfill for the local_politic_scan project at
`C:\Users\evan_\local_politic_scan`. Four days of series podcasts were never
generated because Cowork inbox jobs were not processed. Your job is to write
all the missing scripts and then run TTS on each date.

---

## What's missing

| Date | Candidate | Scripts needed | Dossier |
|------|-----------|---------------|---------|
| May 7 | Vaughn Stewart | ep1 ep2 ep3 ep4 | ✅ exists (`data/candidate_dossiers/vaughn-stewart.md`) |
| May 8 | Sunil Dasgupta | ep1 ep2 ep3 ep4 | ❌ must be built first |
| May 9 | Sebastian Johnson | ep1 ep2 ep3 ep4 | ❌ must be built first |
| May 10 | Gabriel Sorrel | ep3 ep4 | ✅ exists (`data/candidate_dossiers/gabriel-sorrel.md`); ep1+ep2 already written |

---

## Step-by-step instructions

### 1. Build missing dossiers

For **Sunil Dasgupta** and **Sebastian Johnson**, read the full dossier
instructions from their inbox brief, then build the dossier:

- `cowork_inbox/dossier_2026-05-08_sunil-dasgupta.json` → write to
  `data/candidate_dossiers/sunil-dasgupta.md`
- `cowork_inbox/dossier_2026-05-09_sebastian-johnson.json` → write to
  `data/candidate_dossiers/sebastian-johnson.md`

Each brief's `instructions` field has the full research methodology. Follow it:
web-search to pin down the candidate, build the biographical foundation, cover
legislative record, positions, political context. These dossiers are the source
of truth for the episode scripts — thin dossiers produce thin scripts. Mark
each done by writing a `.done.json` file alongside the inbox file after
completing it (copy the brief, add `"status": "done"` and
`"completed_at": "<ISO timestamp>"`).

### 2. Write episode scripts — 14 total

For each series episode brief listed below, read the full brief JSON, read the
candidate's dossier, then write the complete ALEX/JORDAN dialogue to the
`output_file` path in the brief. Follow the instructions exactly — length
target (~3900 words / ~30 min), avoid-list, must-cover items, listener
districts, calibration note. After writing, mark the brief done with a
`.done.json` in cowork_inbox.

Process in this order (earlier dates first):

**May 7 — Vaughn Stewart**
1. `cowork_inbox/series_2026-05-07_vaughn-stewart_ep1.json` → `podcasts/podcast_2026-05-07_series_vaughn-stewart_ep1.txt`
2. `cowork_inbox/series_2026-05-07_vaughn-stewart_ep2.json` → `podcasts/podcast_2026-05-07_series_vaughn-stewart_ep2.txt`
3. `cowork_inbox/series_2026-05-07_vaughn-stewart_ep3.json` → `podcasts/podcast_2026-05-07_series_vaughn-stewart_ep3.txt`
4. `cowork_inbox/series_2026-05-07_vaughn-stewart_ep4.json` → `podcasts/podcast_2026-05-07_series_vaughn-stewart_ep4.txt`

**May 8 — Sunil Dasgupta**
5. `cowork_inbox/series_2026-05-08_sunil-dasgupta_ep1.json` → `podcasts/podcast_2026-05-08_series_sunil-dasgupta_ep1.txt`
6. `cowork_inbox/series_2026-05-08_sunil-dasgupta_ep2.json` → `podcasts/podcast_2026-05-08_series_sunil-dasgupta_ep2.txt`
7. `cowork_inbox/series_2026-05-08_sunil-dasgupta_ep3.json` → `podcasts/podcast_2026-05-08_series_sunil-dasgupta_ep3.txt`
8. `cowork_inbox/series_2026-05-08_sunil-dasgupta_ep4.json` → `podcasts/podcast_2026-05-08_series_sunil-dasgupta_ep4.txt`

**May 9 — Sebastian Johnson**
9.  `cowork_inbox/series_2026-05-09_sebastian-johnson_ep1.json` → `podcasts/podcast_2026-05-09_series_sebastian-johnson_ep1.txt`
10. `cowork_inbox/series_2026-05-09_sebastian-johnson_ep2.json` → `podcasts/podcast_2026-05-09_series_sebastian-johnson_ep2.txt`
11. `cowork_inbox/series_2026-05-09_sebastian-johnson_ep3.json` → `podcasts/podcast_2026-05-09_series_sebastian-johnson_ep3.txt`
12. `cowork_inbox/series_2026-05-09_sebastian-johnson_ep4.json` → `podcasts/podcast_2026-05-09_series_sebastian-johnson_ep4.txt`

**May 10 — Gabriel Sorrel (ep3 + ep4 only; ep1+ep2 already written)**
13. `cowork_inbox/series_2026-05-10_gabriel-sorrel_ep3.json` → `podcasts/podcast_2026-05-10_series_gabriel-sorrel_ep3.txt`
14. `cowork_inbox/series_2026-05-10_gabriel-sorrel_ep4.json` → `podcasts/podcast_2026-05-10_series_gabriel-sorrel_ep4.txt`

Also mark ep1 and ep2 done (scripts already exist):
- Create `cowork_inbox/series_2026-05-10_gabriel-sorrel_ep1.done.json`
- Create `cowork_inbox/series_2026-05-10_gabriel-sorrel_ep2.done.json`

### 3. Run TTS for each date

After all scripts for a date are written, run TTS immediately — don't wait
until all 4 dates are done:

```
cd C:\Users\evan_\local_politic_scan
python -m main tts-publish --date 2026-05-07
python -m main tts-publish --date 2026-05-08
python -m main tts-publish --date 2026-05-09
python -m main tts-publish --date 2026-05-10
```

TTS is idempotent — it skips any mp3 that already exists and is > 1 KB.
If a single episode fails TTS, continue with the others and report the error
at the end.

---

## Script format rules (apply to every episode)

- Every line starts with `ALEX:` or `JORDAN:` — no stage directions, no
  markdown, no emoji, no `[laughs]`, no asterisks, no headers.
- Plain UTF-8 text only. No blank lines between consecutive speaker lines;
  a blank line only where a natural paragraph break occurs.
- Hit the word-count target in the brief's `length_calibration` block.
  The episodes have been running short — extend with deeper biographical
  reads and more JORDAN follow-up, never with filler.
- Do not reuse any framing on the brief's `avoid_list`.
- Cite specific names, dates, schools, bill numbers, employers from the
  dossier — no generalities.

---

## Done-file format

After writing each output file, create `<brief_id>.done.json` in cowork_inbox:

```json
{
  "brief_id": "<same as in the brief>",
  "type": "<same as in the brief>",
  "status": "done",
  "completed_at": "<ISO 8601 UTC timestamp>",
  "output_file": "<path written to>",
  "word_count": <int>
}
```

---

## Completion check

When finished, run:
```
python -m main series status
```
and confirm all four dates show mp3 files in the podcasts/ folder. Report
any dates that still have missing audio and why.
