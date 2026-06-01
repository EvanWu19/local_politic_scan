# Claude Code prompt — finish wiring + validate Phase 2 (data breadth) live

You are on Windows in the `local_politic_scan` repo (live network + `data/politics.db` work;
the sandbox where this was written has neither). Phase 2 of `oss-scouting/IMPLEMENTATION_PLAN.md`
added four data-source modules. They are unit-tested offline; your job is to (a) apply the small
wiring edits into `main.py` and the dossier pipeline that were NOT done in the sandbox (large files,
risky to edit there), (b) set the API keys, and (c) validate each against live sources. Do not
commit — leave the tree dirty and report back.

## What already exists (built + offline-tested, do not rewrite)
- `scanner/sources/federal_mentions.py` — `fetch_federal_mentions(api_key, terms, days_back)` →
  GovInfo Congressional Record hits as events at level `federal_mentions`. (item 5)
- `scanner/sources/candidate_linking.py` — `tag_events_with_candidates(events, names)` tags any
  event whose sponsors/text name a tracked candidate (`_matched_candidate`, `spotlight_candidate`). (item 4)
- `scanner/sources/campaign_finance.py` — `finance_summary(name, federal=, fec_api_key=, sbe_csv=)`
  + `format_finance_block(summary)`; SBE CSV parser + openFEC lookup. (item 6)
- `scanner/sources/civic.py` — `fetch_legistar_meetings(client)` via civic-scraper, optional/no-op
  when the lib or `CIVIC_LEGISTAR_CLIENT` is absent. (item 2)
- `config.py` — added `GOVINFO_API_KEY`, `FEC_API_KEY`, `GOVINFO_TERMS` (default
  `Montgomery County, Maryland-08, Rockville`), `SBE_FINANCE_CSV`, `CIVIC_LEGISTAR_CLIENT`.
- `scanner/reporter.py` — added the `federal_mentions` section label to `LEVEL_LABELS`.
- `requirements.txt` — added `civic-scraper>=0.3.0` (optional).

## Step 0 — setup
1. `pip install civic-scraper` (only if you'll test item 2). GovInfo/FEC are keyed REST — no lib.
2. Set keys in `.env`: `GOVINFO_API_KEY` (api.data.gov key; `DEMO_KEY` ok for a smoke test),
   `FEC_API_KEY` (api.data.gov), and optionally `SBE_FINANCE_CSV=<path to a downloaded MD SBE CSV>`
   and `CIVIC_LEGISTAR_CLIENT` (see item 2).
3. `python -m py_compile scanner/sources/federal_mentions.py scanner/sources/candidate_linking.py scanner/sources/campaign_finance.py scanner/sources/civic.py config.py scanner/reporter.py`

## Step 1 — wire the sources into `main.py` (in `cmd_scan`, the fetch section)
Apply these inserts (anchor by surrounding code; line numbers drift):

(a) Right after the federal-bills block that does `all_raw.extend(federal)`:
```python
    # OSS item 5 — GovInfo Congressional Record mentions of the listener's area
    try:
        print("  [2.5] Federal mentions (GovInfo)…", end=" ", flush=True)
        from scanner.sources.federal_mentions import fetch_federal_mentions
        mentions = fetch_federal_mentions(cfg.GOVINFO_API_KEY, cfg.GOVINFO_TERMS,
                                          days_back=cfg.SCAN_DAYS_BACK)
        print(f"✓ {len(mentions)} items")
        all_raw.extend(mentions)
    except Exception as e:
        errors.append(f"federal_mentions: {e}")
        print(f"✗ {e}")
```

(b) In the hyperlocal/county area (near `fetch_all_local_hearings`), add the optional council source:
```python
    # OSS item 2 — Montgomery County Council via Legistar (no-op unless configured)
    try:
        from scanner.sources.civic import fetch_legistar_meetings
        all_raw.extend(fetch_legistar_meetings())
    except Exception as e:
        errors.append(f"civic_legistar: {e}")
```

(c) AFTER all sources have populated `all_raw` and BEFORE AI enrichment (`process_batch`):
```python
    # OSS item 4 — flag bills/news naming a tracked candidate (auto-spotlight)
    try:
        from scanner.sources.candidate_linking import tag_events_with_candidates
        from scanner.series import all_candidate_names
        n = tag_events_with_candidates(all_raw, [x for x in all_candidate_names() if x])
        print(f"  Candidate-linking: tagged {n} event(s)")
    except Exception as e:
        errors.append(f"candidate_linking: {e}")
```

## Step 2 — wire finance into the dossier brief (item 6)
Open `scanner/dossier.py` (`queue_dossier_briefs` / wherever the per-candidate brief context is
assembled) and enrich each candidate's brief with a finance line:
```python
    from scanner.sources.campaign_finance import finance_summary, format_finance_block
    fin = finance_summary(candidate_name, federal=is_federal_office,
                          fec_api_key=cfg.FEC_API_KEY)  # SBE_FINANCE_CSV read from config
    block = format_finance_block(fin)   # '' when no data
    # append `block` to the brief's research-context text the Cowork agent receives
```
`is_federal_office` ≈ office contains "U.S." / "Congress" / "Senate". If you can't cleanly thread it,
pass `federal=False` (SBE-only) for now and note it.

## Step 3 — validate live (report numbers + PASS/FAIL per item)
1. **Item 5 (GovInfo):** `python -c "from config import Config as C; from scanner.sources.federal_mentions import fetch_federal_mentions; import json; print(len(fetch_federal_mentions(C.GOVINFO_API_KEY, C.GOVINFO_TERMS, days_back=30)))"`.
   Confirm the GovInfo search query shape is accepted (HTTP 200) and a recent CREC mention returns.
   If the API rejects the `query`/`sorts` body, adjust `_build_query`/`fetch_federal_mentions` to match
   the current GovInfo `/search` schema and note the change. PASS: ≥1 plausible mention in 30 days,
   each with a working `govinfo.gov/app/details/...` URL. Confirm it renders under the new
   "Federal Mentions" digest section.
2. **Item 4 (linking):** run a scan (or feed recent state/federal bills) and confirm bills sponsored
   by a tracked candidate get `_matched_candidate` set. Spot-check 3. PASS: real sponsor→candidate
   matches, no false hits on a bare surname.
3. **Item 6 (finance):** download a current MD SBE campaign-finance CSV, set `SBE_FINANCE_CSV`, and
   run `finance_summary("Will Jawando")` (use a real on-ballot name). Confirm raised/spent/cash parse.
   The SBE column headers vary by export — if `parse_sbe_csv` returns 0 rows, print the real header row
   and extend the `_SBE_COLS` aliases to match. For a federal name, set `FEC_API_KEY` and test
   `finance_summary(name, federal=True, fec_api_key=...)`. PASS: a real candidate shows nonzero totals
   and `format_finance_block` appears in that candidate's dossier brief.
4. **Item 2 (Legistar):** confirm Montgomery County Council's platform. If it runs Legistar, set
   `CIVIC_LEGISTAR_CLIENT` (the slug in its `*.legistar.com` host) and run
   `python -c "from scanner.sources.civic import fetch_legistar_meetings; print(len(fetch_legistar_meetings()))"`.
   If MoCo is NOT Legistar (e.g. Granicus/CivicPlus), switch `civic.py` to the matching civic-scraper
   platform class and note it. PASS: ≥1 upcoming council meeting with a real URL. If MoCo has no
   civic-scraper-supported platform, report that and leave item 2 as a documented no-op.

## Report back
- PASS/FAIL + live numbers per item; any API-schema fix you had to make (GovInfo body, SBE columns,
  Legistar platform class).
- Confirm a full `python main.py scan` runs end-to-end with all four wired in (note added latency).
- Do NOT commit; leave for `git diff` review. New untracked files: the four `scanner/sources/*.py`
  modules above.
