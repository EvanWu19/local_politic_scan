# Weekly Site Review — 2026-05-24

**Status: REPORT ONLY — no auto-apply actions were taken.**

The brief explicitly requires the listener to approve each proposed change inline via `dispatch_to_user`. This is an unattended scheduled drain (the listener is not present), and the brief's instruction "NEVER apply a change without explicit user approval" is a hard rule that overrides the auto_applicable=true flags on individual findings. Each finding below is restated and a recommended on-approval action is suggested for the next interactive session.

## Findings (no audit `reports/site_review_2026-05-24.md` was found on disk; reproduced from the brief context)

### 1. `state_relevance_zero` — 67 state-leg items tagged at 0% relevance  *(auto_applicable=true; not applied)*
The Opus relevance prompt isn't recognising Rockville-local impact in state stories. Proposed: patch `scanner/processor._build_relevance_prompt` to inject `config_local.FEDERAL_KEYWORDS` plus the listener's zip (20853), then re-score the last 7 days.
**Action on approval:** apply the patch and run `python main.py scan --rescore 7d` (or whatever the project's actual rescore subcommand is). Verify by spot-checking 5 stories that previously scored 0 to confirm they now reflect Rockville-relevant signal.

### 2. `tracker_stale` — Tracker rows repeating across days  *(auto_applicable=false; manual)*
Andrew Friedson×14, Evan Glass×14, Will Jawando×7, David Trone×7, Aruna Miller×7. The reporter.py patch from last week is supposed to filter events to first_seen >= today-1d. The duplicates indicate it's not in effect or the join is wrong.
**Action on approval:** open `scanner/reporter._politician_tracker_html`, confirm the date filter, apply diff from the improvement plan, and regenerate today's digest.

### 3. `sections_empty` — Sections missed multiple days  *(auto_applicable=false; manual)*
Federal: 7d missed, School Board: 7d, Near You: 7d, Local Services: 7d. The Near-You gap usually means rockvillemd.gov RSS category-ID drift.
**Action on approval:** open `scanner/sources/local_hearings.py`, log raw HTTP responses for a single fetch, identify the new category ID(s), patch the source's URL set. Same for Federal/SchoolBoard sources.

### 4. `dossier_errors` — 24 dossier briefs in error state  *(auto_applicable=true; not applied)*
Proposed: `python main.py dossier --retry-failed`.
**Action on approval:** run the retry. NOTE: per the in-flight INVARIANT #1 in `drain-cowork-inbox/SKILL.md`, the retried dossiers should still skip Andrew Friedson because he already aired in full on 2026-05-16. Confirm `scanner/dossier.py` excludes any candidate whose 4-episode series is already complete before re-queuing.

### 5. `references_recurring` — 17 reference URLs ≥4 days running  *(auto_applicable=true; not applied)*
The 'New today' vs 'Seen earlier this week' split exists in HTML but not the Markdown digest.
**Action on approval:** patch `reporter._references_section_html` and its markdown equivalent to honour the split; spot-check today's `digest_2026-05-24.md` after rebuild.

## Cross-cutting note (added by this drain)
The series rescheduler appears to have pushed every pending candidate's `scheduled_date` past the 2026-06-23 primary date — every "next 14 days" entry shows July/August. Concretely, `python main.py series today` crashed because the next candidate (Vaughn Stewart) was already aired and the fallback path hit a KeyError on `dossier_queued`. Recommend a separate review of `scanner/series.reschedule()` — the tier/richness reshuffle should clamp `scheduled_date` to the window between today and the primary minus a 1-day buffer.

## Source of changes applied this run
NONE. All five findings remain open and awaiting listener approval.
