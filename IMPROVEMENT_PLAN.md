# Local Politics Scanner — Improvement Plan (2026-05-15)

This plan addresses the four issues you flagged. Diagnosis is grounded in
the actual code in `scanner/`, `cowork_inbox/`, and the May 14 digest.

---

## Issue 1 — "Today's Candidate Spotlight" never refreshes with the dossier

### Root cause

In `scanner/reporter.py` (`_load_candidate_spotlight`, lines 290–365):

1. The function looks up which candidate is scheduled for today from
   `data/candidate_series.json`.
2. It tries to read `data/candidate_dossiers/<slug>.md`.
3. If the file is missing, it falls back to the literal message
   *"Dossier in progress — Cowork is researching this candidate tonight.
   Check back tomorrow morning…"* and that's where it stops. Forever.

The dossier is supposed to come from the Cowork agent, which drains
`cowork_inbox/dossier_*.json` briefs. Looking at the inbox, several recent
briefs are stuck as `.error.json`:

- `dossier_2026-05-11_aaron-penman.error.json`
- `dossier_2026-05-11_adrian-boafo.error.json`
- `dossier_2026-05-11_adrienne-a-mandel-melnyk.error.json`
- `dossier_2026-05-11_alec-stone.error.json` (and several .done.json siblings)

The `.error.json` rename means Cowork tried and failed (most likely because
the candidate's `office`, `party`, and `district` were empty in
`politicians`, which makes the Step-1 "resolve the person" instruction in
`dossier.py:queue_dossier_briefs` fail). **There is no retry logic** —
`queue_dossier_briefs` skips any candidate whose dossier file *exists*, but
it does not check for prior `.error.json` outcomes and does not requeue.

### Fix

Three changes in `scanner/dossier.py` and `scanner/reporter.py`:

1. **Detect stuck/errored briefs and requeue them with stronger instructions.**
   A new helper `_brief_failed_recently(slug)` looks for
   `cowork_inbox/dossier_*_<slug>.error.json` within the last 14 days and
   forces a fresh enqueue using the new Opus 4.7 default.
2. **Make the brief self-correcting.** When `office/party/district` are
   empty in the DB, the new brief explicitly tells the agent to web-search
   to fill those gaps *before* refusing — the prior brief required them as
   input.
3. **Spotlight panel never says "in progress" forever.** When the dossier
   file is missing, the panel now falls back to:
   - whatever the DB already knows (office, party, district, recent events),
   - a visible "Last research attempt: 2026-05-11 — failed. Retry queued for
     tonight." status line so you can see the system is unstuck,
   - a one-click "Force re-research now" link that drops a fresh brief.

The retry helper is also exposed as `python main.py dossier --retry-failed`.

---

## Issue 2 — "Maryland State Legislature" misses local zone hearings near you

### Root cause

`scanner/sources/` pulls from these feeds (and nothing more local):

- `marylandmatters.org` RSS — statewide political reporting.
- `news.google.com` RSS keyed on "Maryland legislation" — also statewide.
- `bethesdamagazine.com` — county-wide.
- `montgomery.py:fetch_county_hearings` — keyword filter on Montgomery County
  press releases (which is *not* the hearings calendar; the real calendar at
  `montgomerycountymd.gov/council/agenda/` is JavaScript-rendered, so the
  current scraper misses it).

There is **no source** for:

- Rockville City Council / Mayor agendas (`rockvillemd.gov`)
- Rockville Planning Commission / zoning hearings
- MCPS Board of Education agendas (your Wootton concern lives here)
- Maryland-National Capital Park & Planning Commission hearings
- WSSC public hearings
- 20853 civic association calendars

### Fix

New module `scanner/sources/local_hearings.py` adds five fetchers:

| Fetcher                       | Source                                                                                  | Level    |
| ----------------------------- | --------------------------------------------------------------------------------------- | -------- |
| `fetch_rockville_council()`   | `rockvillemd.gov/AgendaCenter/` (Mayor & Council)                                       | `local`  |
| `fetch_rockville_planning()`  | `rockvillemd.gov/AgendaCenter/` (Planning Commission)                                   | `local`  |
| `fetch_mcps_board()`          | `go.boarddocs.com/mabe/mcpsmd/Board.nsf/Public` (MCPS Board of Ed)                       | `school` |
| `fetch_mncppc_hearings()`     | `montgomeryplanningboard.org/agenda/`                                                    | `local`  |
| `fetch_wssc_hearings()`       | `wsscwater.com/about-us/public-meetings`                                                 | `local`  |

These are wired into `main.py` `cmd_scan` so every run pulls them. Items are
tagged `level="local"` (which already exists in `LEVEL_LABELS` as
*"🚑 Local Services"* — relabeled to *"📍 Near You — Rockville / 20853"*),
and a new `proximity_score` field is added per event so the rendered HTML
shows hearings within walking distance with a 🏠 badge.

A keyword amplifier in `processor.py` also boosts relevance for any event
whose body contains "Rockville", "20853", "King Farm", "Twinbrook",
"Aspen Hill", "Wootton", or your registered precinct `08-008`.

---

## Issue 3 — Politician Tracker and References look identical every day

### Root cause

Both sections show *accumulated* data, not *what's new today*:

- `_politician_tracker_html` in `reporter.py:641-680` reads each
  politician's three most-recent events from the DB regardless of date.
  Those events were tagged days or weeks ago and rarely change.
- `_references_section_html` (`reporter.py:607-639`) deduplicates URLs
  *within today's digest only* — but since `cmd_scan` pulls the same RSS
  feeds and many items recur for several days running, the same URLs
  surface again every morning.

### Fix

1. **Tracker becomes a "what changed in the last 24 h" view.** Filter to
   events where `events.first_seen >= today - 1 day`. Add a small "Streak"
   pill showing how many days running this politician has been in the
   digest. Politicians with zero new events are still listed but collapsed
   under a *"Quiet today — last activity 3 days ago"* line.
2. **References split into "New today" and "Recurring (this week)".** Maintain
   a small SQLite-backed `digest_references` table (URL, first_appeared,
   last_appeared, days_seen). The HTML shows "🆕 New today" items at the
   top and a collapsible "Seen earlier this week" group below.

Both changes are in `scanner/reporter.py` and a tiny migration in
`scanner/database.py` to add the `digest_references` table.

---

## Issue 4 — Weekly site-review routine with Cowork dispatch

### What I'm building

A new file `weekly_review.py` runs every Sunday morning (via the Cowork
`schedule` skill). It:

1. Reads the seven most recent `reports/digest_*.md` files.
2. Audits per-section quality:
   - Spotlight panels that stayed on the "in progress" fallback.
   - Maryland State Legislature items with `relevance=0%` (currently every
     story — that means the relevance model isn't tagging Rockville-local
     impact correctly).
   - Tracker rows that repeated identically across ≥3 of the last 7 days.
   - References that haven't rotated.
   - Dossier briefs in `.error.json` state.
   - Empty `local` and `school` sections (sign the new local sources broke).
3. Writes a markdown report to
   `cowork_inbox/site_review_<date>.md` and uses the Cowork bridge's
   `dispatch_to_user(...)` helper (new) to push a notification:
   *"Site review ready — 4 proposed changes, approve each below."*
4. Each proposed change is a checkbox with the exact diff or config edit it
   would apply. Approving routes the change through a one-shot Opus 4.7
   brief that applies the patch and writes a `weekly_review_<date>.done.json`
   audit file.

This is registered as a scheduled task via the `schedule` skill so it
shows up in your Cowork sidebar.

---

## AI model swap to Opus 4.7

Per your choice:

| Job                          | Old model                  | New model           | File                    |
| ---------------------------- | -------------------------- | ------------------- | ----------------------- |
| Daily event tagging          | `claude-haiku-4-5-20251001`| `claude-opus-4-7`   | `scanner/processor.py`  |
| Politician analyst           | `claude-sonnet-4-6`        | `claude-opus-4-7`   | `scanner/analyst.py`    |
| Podcast author + editor      | `claude-sonnet-4-6`        | `claude-opus-4-7`   | `config.py`             |
| Chat sidebar                 | `claude-sonnet-4-6`        | `claude-opus-4-7`   | `config.py`             |
| Candidate dossier (via Cowork)| (Cowork default)          | `claude-opus-4-7` (explicit) | `dossier.py` |

If the slug `claude-opus-4-7` is not yet available on your Anthropic API
key, fall back to `claude-opus-4-6` — both env vars are read first, code
default is `claude-opus-4-7`.

**Cost note:** processor runs hottest (≈30–100 calls/day). Switching it from
Haiku to Opus is roughly a 30× per-token bump. If the daily cost gets
uncomfortable, the cleanest hybrid is to keep Haiku for the cheap "tag
event level / extract politician name" pass and only upgrade the
"relevance score + summary" pass to Opus — `processor.py` is already split
that way internally and the patch leaves the seam in place via a
`PROCESSOR_TAG_MODEL` env var.

---

## Files I'm changing today

```
NEW:    scanner/sources/local_hearings.py     — Rockville + MCPS + MNCPPC + WSSC fetchers
NEW:    weekly_review.py                       — Sunday audit + Cowork dispatch
EDIT:   scanner/reporter.py                    — spotlight fallback, tracker/references date filters
EDIT:   scanner/dossier.py                     — retry errored briefs, Opus 4.7 default, fill gaps
EDIT:   scanner/database.py                    — add digest_references table
EDIT:   scanner/processor.py                   — relevance amplifier for Rockville keywords; Opus
EDIT:   scanner/analyst.py                     — Opus 4.7 default
EDIT:   config.py                              — Opus 4.7 default for scripts + chat
EDIT:   main.py                                — wire fetchers + dossier --retry-failed + weekly-review
```

All edits are minimal — they replace specific lines, not whole functions —
so a `git diff` after the patches will be readable in one sitting.

---

## Addendum (2026-05-15, later) — Cowork-only enforcement

Per listener directive, every AI role under `scanner/` must route through
a Cowork brief (drained by the `drain-cowork-inbox` Cowork task). Direct
Anthropic API calls from inside scanner/* are no longer allowed.

### What changed

| File                        | What                                                                        |
| --------------------------- | --------------------------------------------------------------------------- |
| `config.py`                 | `USE_COWORK_FOR_AI` hard-pinned to `True`. Env override removed.            |
| `scanner/processor.py`      | Direct-API fallback removed; reaching it raises a notification.             |
| `scanner/analyst.py`        | `analyze_all` + `analyze_one` queue briefs; API path retired.               |
| `scanner/pm.py`             | Weekly themes always queued; API path retired.                              |
| `scanner/editor.py`         | Review pass always queued; on failure surfaces a notification.              |
| `scanner/chat.py`           | Always queued; API path retired.                                            |
| `scanner/podcast.py`        | Author + rewrite escalation both queued unconditionally.                    |
| `scanner/server.py`         | Digest chat always queued; API path retired.                                |
| `scanner/deepdive.py`       | Cowork hand-off mandatory; if it returns None we surface a notification.    |
| `scanner/notifications.py`  | NEW. `notify(...)` writes to file + SQLite + a Cowork-drained daily rollup. |
| `weekly_review.py`          | Calls `scan_failed_briefs()` so every error in the past week gets surfaced. |
| `main.py`                   | New `python main.py notifications` command + `weekly-review` entry point.   |

### How error surfacing works

When a role hits trouble (brief queue fails, Cowork drain returns nothing,
or the disabled fallback path is reached), it calls
`scanner.notifications.notify(role, message, severity="error", context=...)`.
That helper:

1. Appends one line to `notifications.log` at project root.
2. Inserts a row in the new `scanner_notifications` SQLite table.
3. Appends a bullet to `cowork_inbox/notify_<date>.md`. The
   drain-cowork-inbox task picks that file up and surfaces the bullets
   to the listener via `dispatch_to_user`.

### Commands you now have

```
python main.py notifications              # list unseen issues
python main.py notifications --scan       # also scan cowork_inbox/*.error.json first
python main.py notifications --mark-seen  # clear the queue after reading
python main.py weekly-review              # run Sunday audit manually
python main.py weekly-review --dry-run    # preview without dispatching
python main.py dossier --retry-failed     # requeue stuck dossier briefs
```

### Cowork-side scheduled tasks (existing + recommended)

You should have these registered via the `schedule` skill in Cowork:

| Schedule task            | Cadence            | What it does                                    |
| ------------------------ | ------------------ | ----------------------------------------------- |
| `drain-cowork-inbox`     | hourly             | Pre-existing. Processes every `*.json` brief.   |
| `weekly-site-review`     | Sundays 08:00      | `python weekly_review.py` — audit + dispatch.   |
| `nightly-error-scan`     | nightly 23:30      | `python main.py notifications --scan`.          |
| `dossier-retry-failed`   | nightly 23:45      | `python main.py dossier --retry-failed`.        |

Register the latter three with one-liners via the `/schedule` skill — they
all just shell out to `python main.py …`, so they cost almost nothing to
run and they keep the inbox honest.

### If you ever need to bypass Cowork in development

There is no env override anymore — that was deliberate. To temporarily
run a role against the direct Anthropic API, edit `config.py` and set
`USE_COWORK_FOR_AI = False`, then re-enable it before committing. The
direct-API code paths are kept inert in each file so a diff still shows
the prompt structure if you need to revive them.
