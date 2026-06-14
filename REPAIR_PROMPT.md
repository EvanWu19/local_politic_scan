# Repair prompt — digest page reconciliation (2026-06-12)

Copy everything below into Claude Code, run from the project root
(`C:\Users\evan_\local_politic_scan`).

---

A full audit (`reports/audit_page_vs_episodes_2026-06-12.md` — read it first)
found that the digest pages for 2026-05-25 through 2026-06-12 show the wrong
"Today's Candidate Spotlight": the registry's forward schedule was corrupted
during that window, while the nightly drain substituted the correct candidates.
The registry has since been reconciled (scheduled_date = actual airing date)
and `scanner/reporter.py` now renders ALL of a day's candidates. The audio MP3s
are complete and correct — only the static HTML pages need re-rendering.

Do the following, in order:

## 1. Commit the pending fixes first

Working-tree changes on `phase2-wire-and-validate-data-sources` that must be
committed before anything else (so the re-render runs from a clean, fixed tree):

- `scanner/series.py` — multi-per-day queueing (`queue_today_series_multi`,
  `candidates_for_date`, `skip_processed`) + the episodes_queued TypeError fix
- `scanner/cowork_bridge.py` — office-primer brief_id collision fix +
  RETURNING-LISTENER / REPEAT-OFFICE anti-repetition rules
- `scanner/reporter.py` — multi-candidate spotlight (`_load_candidate_spotlights`,
  `_build_spotlight`, multi-block `_render_spotlight_header`)
- `data/candidate_series.json` — ballot reconciliation (83 entries; backups
  `*.bak-2026-06-11` exist, do not commit backups)

Run `python -m py_compile scanner/series.py scanner/cowork_bridge.py
scanner/reporter.py` and `python -c "import json;
json.load(open('data/candidate_series.json', encoding='utf-8'))"` before
committing. Commit with a message describing the above. Do NOT push unless the
repo already has a remote workflow.

## 2. Re-render the affected digest pages

```
for %d in (2026-05-25 2026-05-26 2026-05-27 2026-05-28 2026-05-29 2026-05-30 2026-05-31 2026-06-01 2026-06-02 2026-06-03 2026-06-04 2026-06-05 2026-06-06 2026-06-07 2026-06-08 2026-06-09 2026-06-10 2026-06-11 2026-06-12) do python main.py report --date %d
```

(`%%d` if you put it in a .bat file.) If any date errors (the sqlite/OneDrive
blocker is intermittent), note it and continue with the rest — do not abort.

## 3. Verify every page now matches disk

For each date 2026-04-16 → 2026-06-12, extract `class="spot-name">(.+?)<` from
`reports/digest_<date>.html` and compare (slugified) against
`podcasts/podcast_<date>_series_<slug>_ep*.mp3` (≥1 KB). Expected:

- May 4 – Jun 11: page names == aired candidates for that date, with these
  exceptions: 05-26 has episodes (Peter James duplicate re-air) but NO
  spotlight expected; gap days 05-28, 05-30, 06-01, 06-03 should have NO
  spotlight and no episodes.
- 06-11 must list all 5: Wes Moore, Eric S. Felber, LaTrece Hawkins Lytes,
  Mithun Banerjee, Van Free.
- 06-12 must list all 5: Stephen Alan Leon, Boris Kabel Velasquez, Radwan
  Chowdhury, Jim McNulty, Jeremiah Pope (episodes pending tonight — MP3
  absence for 06-12 is NOT a failure).

## 4. Quick pipeline health checks while you're there

- `cowork_inbox/` must contain the Leon ep4 recovery brief
  `office_primer_2026-06-12_us-representative-md-8_stephen-alan-leon_ep4.json`
  AND `..._boris-kabel-velasquez_ep4.json` (collision fix). Confirm both.
- `python main.py series status` — confirm the forward schedule shows 5
  candidates/day for every date 2026-06-13 → 2026-06-20 (Dem Central Committee
  names fill 06-18 onward), 2 on 06-21 (Romero, Weaver), nothing on 06-22
  (catch-up buffer), and that NO already-aired candidate appears on any
  future date.

## 5. Report back to the Cowork session

The Cowork (desktop Claude) session that wrote this prompt will review your
results. Write the report where it can read it:

- `reports/repair_report_2026-06-12.md` containing:
  - git commit hash(es) and files committed
  - per-date re-render result (ok / error+reason)
  - per-date verification table: page names vs disk slugs vs expected,
    PASS/FAIL
  - the two inbox brief confirmations and the `series status` schedule check
  - anything unexpected (sqlite blocker dates, any page whose spotlight still
    mismatches — include exact names, any compile/commit problem)
- Also copy the same file to `cowork_outbox/repair_report_2026-06-12.md` so it
  lands in the normal Windows→Cowork handoff channel.

Print a short summary at the end of the run. The user will then tell the
Cowork session "review the repair report" — make the file self-contained
enough that it needs no other context.
