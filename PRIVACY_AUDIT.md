# Privacy / PII Audit Report
**Project:** `local_politic_scan`  
**Date:** 2026-04-27  
**Scope:** All non-binary files, reviewed for location data, API keys, network identifiers, and personal identifiers before public GitHub publication.

---

## TL;DR — Must-Do Before Making Repo Public

| Priority | Action |
|---|---|
| 🔴 IMMEDIATE | Revoke GitHub OAuth token in `settings.local.json` (line 171) |
| 🔴 IMMEDIATE | Rotate all 4 API keys in `.env` — they are live credentials |
| 🟡 BEFORE PUSH | Scrub county-level defaults from `config.py` and `.env.example` |
| 🟡 BEFORE PUSH | Sanitize `scanner/sources/candidates.py` candidate list (specific districts) |
| 🟢 CONFIRMED SAFE | `config_local.py`, `.env`, `.claude/`, logs, podcasts, reports, knowledge — all gitignored |
| 🟢 CONFIRMED SAFE | None of those sensitive files appear in git history |

---

## Section 1 — .gitignore Review

The `.gitignore` is **well-structured**. All the most sensitive files are covered:

| Pattern | Covers |
|---|---|
| `.env` | Live API keys |
| `config_local.py` | City, ZIP, all district/precinct data |
| `.claude/` | Tailscale IP, hostname, OAuth token, username |
| `data/` | SQLite databases |
| `reports/` | Generated digests (contain "Rockville") |
| `podcasts/` | Generated scripts/audio (contain "Rockville") |
| `knowledge/` | Chat notes (contain "Rockville homeowner") |
| `*.log` | All log files (contain Tailscale IP, Windows username, city) |

**Git history check:** `git log` confirms `.env`, `config_local.py`, and `.claude/settings.local.json` were **never committed** — your history is clean for those files. ✅

---

## Section 2 — Findings by File

---

### 🔴 FILE: `.env`
**Status: GITIGNORED ✅ — but contains LIVE credentials that must be rotated**

Even though this file will never be committed, these keys are live secrets. They should be rotated at all four providers before the repo goes public, as a precaution against any future accidental `git add -f`.

| Line | Content | Action |
|---|---|---|
| 5 | `ANTHROPIC_API_KEY=sk-ant-api03-MPn8C3RgdT1Ib_qTK6VN…` | **Rotate** at console.anthropic.com → API Keys |
| 8 | `CONGRESS_API_KEY=PPe9hYVXwLL7xDVcOrtpjAXNRfTvaNvb4b3PpOgM` | **Rotate** at api.congress.gov |
| 11 | `OPENSTATES_API_KEY=aaa22164-1b70-46df-9bb7-a584546edefd` | **Rotate** at openstates.org/accounts |
| 14 | `OPENAI_API_KEY=sk-proj-uEWzUdeXD1H3zmSC5ze5Zt…` | **Rotate** at platform.openai.com/api-keys |

---

### 🔴 FILE: `.claude/settings.local.json`
**Status: GITIGNORED ✅ — but contains a LIVE GitHub OAuth token that must be revoked NOW**

| Line(s) | Content | Risk | Action |
|---|---|---|---|
| 171 | `[REDACTED-SEE-NOTE]` | **Live GitHub OAuth token** — full repo access | **Revoke immediately** at github.com → Settings → Developer settings → Tokens |
| 27, 44, 96–98, 108–112 | `100.82.204.59` | Tailscale IPv4 address | Gitignored; rotate via Tailscale admin if you change network config |
| 108–112 | `desktop-btfk9s0.tail931e86.ts.net` | Tailscale MagicDNS hostname | Gitignored; identifies your machine |
| 143 | `EvanWu19` | GitHub username in API URL | Gitignored; no action needed |
| 44 | `"Rockville homeowner"` in curl body | City name | Gitignored; no action needed |
| 48, 86–87, 95, 104, 115, 140, 155 | `C:\\Users\\evan_\\local_politic_scan` | Windows username `evan_` in full paths | Gitignored; no action needed |

---

### 🟡 FILE: `config_local.py`
**Status: GITIGNORED ✅ — but this is where all your precise personal data lives**

This file is correctly excluded. Listed here for completeness and to confirm nothing leaked into committed files.

| Line | Content | Risk Level |
|---|---|---|
| 10 | `CITY = "Rockville"` | City-level location |
| 11 | `ZIP_CODE = "20853"` | ZIP code — narrows to ~40,000 people |
| 15 | `US_HOUSE_DISTRICT = "MD-8"` | Congressional district |
| 16 | `STATE_SENATE_DISTRICT = "19"` | State senate district |
| 17 | `STATE_HOUSE_DISTRICT = "19"` | State house district |
| 18 | `COUNTY_COUNCIL_DISTRICT = "7"` | County council district — narrows further |
| 20 | `PRECINCT = "08-008"` | **Precinct number — this is extremely precise** |
| 26 | `CENTRAL_COMMITTEE = "MCC19"` | Party central committee assignment |
| 27 | `ELECTION_DISTRICT = "8"` | Election district |
| 28 | `SCHOOL_DISTRICT = "005"` | MCPS cluster code |

**The combination of these fields (especially ZIP + precinct + councilmanic district 7) could uniquely identify your neighborhood block.** This file must never be committed. It currently is not. ✅

---

### 🟡 FILE: `config.py`
**Status: COMMITTED — contains county-level location defaults**

This file is committed and public. It contains no ZIP codes, street addresses, or precise identifiers, but its hardcoded defaults do reveal your county:

| Line(s) | Content | Risk | Suggested Replacement |
|---|---|---|---|
| 20 | `os.getenv("USER_STATE", "Maryland")` | Reveals state | Change default to `""` — users fill in `.env` |
| 21 | `os.getenv("USER_STATE_CODE", "md")` | Reveals state code | Change default to `""` |
| 22 | `os.getenv("USER_COUNTY", "Montgomery County")` | Reveals county | Change default to `""` |
| 127–170 | `NEWS_FEEDS` list — 8 feeds hardwired to Montgomery County / MD | Reveals county | These are public URLs; acceptable to keep as documented starting points |
| 174–190 | `KNOWN_POLITICIANS` list with District 1, 2, 3, 4 council members | Reveals county | These are public officials; acceptable, though comments make clear it's for Montgomery County |

**Recommended fix for lines 20–22:**
```python
STATE = os.getenv("USER_STATE", "")
STATE_CODE = os.getenv("USER_STATE_CODE", "")
COUNTY = os.getenv("USER_COUNTY", "")
```
Add a note in comments that the defaults in `.env.example` show Maryland/Montgomery County as an *example*.

---

### 🟡 FILE: `.env.example`
**Status: COMMITTED — contains county-level defaults**

| Line | Content | Risk | Suggested Replacement |
|---|---|---|---|
| 22 | `USER_STATE=Maryland` | Reveals state | `USER_STATE=YourState` or leave blank |
| 23 | `USER_STATE_CODE=md` | Reveals state code | `USER_STATE_CODE=xx` or leave blank |
| 24 | `USER_COUNTY=Montgomery County` | Reveals county | `USER_COUNTY=YourCounty` or leave blank |

The lower half of the file (city, ZIP, districts) is already left blank — that's correct. Just swap the top three location defaults to generic placeholders.

---

### 🟡 FILE: `scanner/sources/candidates.py`
**Status: COMMITTED — candidate seed list identifies your county and specific watched districts**

The `CANDIDATES` list (lines 14–78) is committed and public. It contains:

| Lines | Content | Risk |
|---|---|---|
| 14–78 | `Angela Alsobrooks`, `Larry Hogan`, `Wes Moore`, `Marc Elrich`, `Sidney Katz` | These are publicly known Maryland/Montgomery County officials — low risk on their own |
| 54–68 | `"TBD — MD House District 15A"` and `"TBD — MD House District 15B"` | **District 15A/15B are specific sub-districts of Montgomery County** — these are not your district (you're in 19), so this is placeholder seed data, not personal |
| 71–78 | `Sidney Katz, County Council District 3` | District 3 is not your district (you're in 7) |

**Assessment:** The committed candidates list does not actually expose *your* district. The District 15A/15B placeholders are just example stubs. However, the full list does fingerprint the project as Montgomery County-focused. This is acceptable if you're comfortable disclosing you live in Montgomery County — the README already does this. No change strictly required, but you could generalize the comments from "for your area" to remove the Montgomery County-specific examples.

---

### 🟢 FILE: `main.py`
**Status: COMMITTED — no personal PII found**

Reviewed all 1,027 lines. References to "Montgomery County council + hearings" and "MCPS school board" appear only as source labels passed to fetch functions — these match the public official defaults in `config.py` and contain no personal identifiers. Clean. ✅

---

### 🟢 FILE: `scanner/server.py`
**Status: COMMITTED — no personal PII found**

No hardcoded IPs, hostnames, credentials, or location identifiers. The Tailscale IP display in `run_server()` (lines 803–808) is dynamically fetched at runtime via `get_tailscale_ip()` — never hardcoded. Clean. ✅

---

### 🟢 FILES: All other `scanner/*.py` and `scanner/sources/*.py`
**Status: COMMITTED — no personal PII found**

Files reviewed: `analyst.py`, `ballot.py`, `chat.py`, `database.py`, `deepdive.py`, `editor.py`, `pm.py`, `podcast.py`, `processor.py`, `reporter.py`, `sources/__init__.py`, `sources/federal.py`, `sources/state.py`, `sources/montgomery.py`, `sources/news.py`, `sources/news_backfill.py`, `sources/candidate_discover.py`

All API keys are received as function parameters (from `Config` / environment), never hardcoded. No IPs, hostnames, usernames, or location strings embedded. Clean. ✅

---

### 🟢 FILE: `README.md`
**Status: COMMITTED — mentions Maryland/Montgomery County as defaults, acceptable**

Describes the project as "wired for **Maryland / Montgomery County**" by default. This is consistent with the repo being a county-level news scanner template. No personal PII (no city, ZIP, email, username, IP). ✅

---

### 🟢 FILE: `QUICKSTART.txt`
**Status: COMMITTED — mentions Maryland/Montgomery County as defaults, acceptable**

References Maryland General Assembly, Montgomery County Council, MCPS. Same pattern as README — template documentation for the default locale. No personal PII. ✅

---

### 🟢 FILE: `install.bat`
**Status: COMMITTED — no PII found** ✅

---

### 🟢 GITIGNORED: Log files (`scan.log`, `server_err.log`, `server_run.log`, `podcast_run.log`, `podcast_apr21.log`)
**Status: GITIGNORED ✅ — contain PII but will not be committed**

For your awareness, these logs contain:
- `Rockville, Montgomery County, Maryland` (scan.log, multiple lines)
- `100.82.204.59` (scan.log, server_err.log, podcast_run.log, podcast_apr21.log)
- `C:\Users\evan_\local_politic_scan\...` (all log files — exposes Windows username)

All covered by `*.log` in `.gitignore`. ✅

---

### 🟢 GITIGNORED: `podcasts/` text files
**Status: GITIGNORED ✅ — contain "Rockville" references in AI-generated scripts**

The generated podcast `.txt` and `.draft.txt` files frequently reference "Rockville" (confirmed in >20 files) because the AI prompts pass the user's city. All covered by `podcasts/` in `.gitignore`. ✅

---

### 🟢 GITIGNORED: `knowledge/` notes
**Status: GITIGNORED ✅ — contains city reference**

`knowledge/2026-04/2026-04-20_md-legislative-calendar-vs-county-budget-timing-for-homeowne.md` line 17: *"as a Rockville homeowner"* — chat session content. Covered by `knowledge/` in `.gitignore`. ✅

---

### 🟢 GITIGNORED: `data/` databases
**Status: GITIGNORED ✅**

SQLite databases in `data/` are covered by `data/` in `.gitignore`. These likely contain accumulated event data referencing the user's locale. ✅

---

## Section 3 — Summary: What to Do Before Making Public

### Immediate actions (before anything else):
1. **Revoke the GitHub OAuth token** `[REDACTED-SEE-NOTE]` at github.com → Settings → Developer settings → Personal access tokens
2. **Rotate all 4 API keys** in your `.env` file — ANTHROPIC, CONGRESS, OPENSTATES, OPENAI

### Code changes needed in committed files:
3. **`config.py` lines 20–22** — change the three location defaults from `"Maryland"` / `"md"` / `"Montgomery County"` to `""` (empty string). Users will fill them in via `.env`.
4. **`.env.example` lines 22–24** — change `USER_STATE=Maryland`, `USER_STATE_CODE=md`, `USER_COUNTY=Montgomery County` to generic placeholders like `USER_STATE=YourState`, `USER_STATE_CODE=xx`, `USER_COUNTY=YourCounty`.

### Optional but recommended:
5. **`scanner/sources/candidates.py`** — the placeholder entries for `District 15A / 15B` and the `Sidney Katz / District 3` entry don't expose *your* district, but they fingerprint the tool as Montgomery County-specific. You can leave them as documented examples or generalize the comments.

### Verify before push:
6. Run `git status` and confirm none of the following are staged:
   - `.env`
   - `config_local.py`
   - `.claude/`
   - `*.log`
   - `data/`
   - `reports/`
   - `podcasts/`
   - `knowledge/`
7. Run `git log --all --full-history -- .env config_local.py` to confirm these were never committed in any prior commit (currently confirmed clean).

---

## Section 4 — What This Repo DOES Reveal When Public

Even after the above fixes, the public repo will disclose:
- You live in **Maryland**, in **Montgomery County** (from README, config defaults, RSS feeds, candidate seed data)
- You track **MCPS** school board, **Montgomery County Council**, **Maryland General Assembly**
- Your federal keyword interests include taxation (SALT deduction, capital gains), trade/China, H-1B/work visas, and immigration

Montgomery County has ~1 million residents. These disclosures are consistent with a publicly shared local-news tool and do not in themselves narrow your home address. If you are uncomfortable with even county-level disclosure, you would need to generalize the RSS feeds, README, and candidate seed data — but that would also make the repo less useful as a template.

---

*Audit complete. No PII was found in committed files beyond the county-level location defaults noted above. The gitignore is well-configured. The two actions requiring immediate attention are the live GitHub token and the four API keys.*
