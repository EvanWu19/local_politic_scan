# Claude Code prompt — validate Phase 1 (BoardDocs XML + trafilatura) against live sources

You are running on Windows inside the `local_politic_scan` repo, where live network and
`data/politics.db` both work (unlike the sandbox where this code was written). Phase 1 of the
OSS plan was implemented but only unit-tested against fixtures. Validate it against the LIVE
BoardDocs feed and REAL article URLs, then report findings. Do not commit anything — leave the
working tree dirty and summarize for me to review.

## What changed (the code under test)
- `scanner/sources/extract.py` (new) — `extract_article(url)` / `extract_from_html(html, url)`:
  trafilatura wrapper with on-disk cache at `data/cache/articles/`, never raises, returns
  `{text,title,date,author,url}` or `None`.
- `scanner/sources/news.py` — `fetch_rss_feeds()` now runs `_maybe_enrich_fulltext()`, which
  rewrites each item's `raw_content` with the clean article body and backfills `date`/`author`.
- `scanner/sources/local_hearings.py` — `fetch_mcps_boarddocs()` now parses
  `Board.nsf/XML-ActiveMeetings` via `_parse_boarddocs_xml()`, falling back to
  `_fetch_mcps_boarddocs_html()` on failure/empty.
- `config.py` — `FULLTEXT_EXTRACT` (default True), `FULLTEXT_MAX_ARTICLES` (80),
  `FULLTEXT_MAX_CHARS` (6000). `requirements.txt` — added `trafilatura>=1.8.0`.

## Step 0 — setup
1. Install the new dep into the project venv: `pip install trafilatura` (then `pip show trafilatura`).
2. `python -m py_compile config.py scanner/sources/extract.py scanner/sources/news.py scanner/sources/local_hearings.py`

## Step 1 — BoardDocs live feed
1. Run `python -m scanner.sources.local_hearings` and capture the log + printed items.
2. Confirm the log shows `local_hearings: MCPS Board (XML) — N items` (the XML path, not the HTML
   fallback). If it shows `(HTML)`, the XML feed failed — capture why.
3. Independently fetch `https://go.boarddocs.com/mabe/mcpsmd/Board.nsf/XML-ActiveMeetings` and
   inspect the real XML. Confirm `_parse_boarddocs_xml` extracts the right element names — the
   parser assumes `<meeting>` nodes with a `unique` attr or `<unique>` child, `<name>`/`<description>`,
   and a date in `<start>/<date>` or the name. If the real schema differs, fix the parser to match
   and note exactly what you changed.
4. For 2–3 returned items, confirm the `date` is correct and the `source_url`
   (`.../Board.nsf/goto?open&id=<uid>`) actually opens that meeting in a browser/fetch.
5. Compare item count and dates against the old HTML scraper: temporarily call
   `_fetch_mcps_boarddocs_html(10)` and diff. XML count should be ≥ HTML and dates should parse
   without the title-regex.
6. Verify the fallback: temporarily point `MCPS_XML` at a bad URL, confirm it logs the failure and
   returns HTML-scraped items, then revert.

## Step 2 — article extraction live
1. Write a throwaway script (`tmp_validate_extract.py`, delete after) that calls
   `fetch_rss_feeds(Config.NEWS_FEEDS, days_back=Config.SCAN_DAYS_BACK, max_per_feed=8)` and reports:
   total items, how many have `full_text_extracted == True`, and wall-clock time.
2. For 3 different outlets (e.g. Maryland Matters, WTOP, Bethesda Magazine) print the first 600 chars
   of `raw_content`. Confirm: no cookie-banner / nav / "related links" junk, body is the real article
   (longer/cleaner than the old RSS summary), and `date`/`author` are populated where available.
3. Confirm the cache works: check `data/cache/articles/*.json` is populated; run the script a second
   time and confirm it's noticeably faster (cache hits) and produces identical text.
4. Note total added latency vs a `FULLTEXT_EXTRACT=0` run. If a normal `python main.py scan` becomes
   too slow, recommend a `FULLTEXT_MAX_ARTICLES` value.
5. Confirm graceful degradation: set `FULLTEXT_EXTRACT=0` in env and confirm the scan still produces
   items (RSS-summary `raw_content`, no extraction). Then `pip uninstall trafilatura` in a scratch
   step (or simulate ImportError) and confirm `fetch_rss_feeds` still returns items with a single
   warning — re-install afterward.

## Step 3 — relevance sanity (optional but valuable)
Pick 5 state-leg stories that previously scored 0% relevance (the recurring site-review finding).
Confirm their `raw_content` is now full clean text. You don't need to re-run scoring, just verify the
scorer would now see real Rockville/MoCo signal instead of an RSS blurb.

## Step 4 — re-validate the two extraction fixes (added 2026-05-31)
Two issues found in the first validation pass were addressed in `scanner/sources/extract.py`:
- **Bethesda 403** → `_HEADERS["User-Agent"]` is now a desktop-Chrome string.
- **Google News blind feeds** → `extract_article` now calls `_resolve_url()`, which tries to decode
  the `news.google.com/rss/articles/<base64>` segment to the publisher URL, with an HTTP-redirect
  fallback, before fetching.

### Known result before you start (verified locally 2026-05-31)
An OFFLINE check ran `_decode_google_news_url()` against **105 real current-format links pulled from
`reports/digest_*.md`: it decoded 0/105 (0%)**. Google's current `CBMi…` links embed an OPAQUE
TOKEN, not the URL — the decoded blob is `\x08\x13"…AU_yq…` with no `http` substring. The token must
be exchanged via Google's `batchexecute` endpoint, which the homegrown decoder does not do. So treat
the base64 decoder as effectively dead for live links, and don't waste time re-measuring its
hit-rate — it's ~0 by construction. The HTTP-redirect fallback likely also fails (these links return
Google's 200 consent shell, not a 3xx redirect); confirm that once, then move on.

### What to actually validate
1. **Bethesda UA fix:** `python -m scanner.sources.extract "<a current bethesdamagazine.com article URL>"`
   → expect a clean body, not a 403. PASS criterion: body text returned.
2. **Google News, real fix:** install `googlenewsdecoder` (`pip install googlenewsdecoder`) and wire it
   into `_resolve_url()` as the PRIMARY resolver (call `gnewsdecoder(url)`; keep the existing base64
   decode + redirect as fallbacks for non-token/older links). Then pick 5 real
   `news.google.com/rss/articles/...` links from `Config.NEWS_FEEDS` output, run them through
   `_resolve_url()`, and report how many resolve to an off-google publisher URL and then extract a
   body. PASS criterion: ≥ 4/5 resolve + extract. (If `googlenewsdecoder` is unacceptable as a dep,
   the alternative is implementing the `batchexecute` POST by hand — more code, same effect.)
3. **End-to-end:** re-run the Step 2 throwaway script and report the new extraction rate (was 8/30)
   and specifically how many Google-News + Bethesda items now succeed. With the decoder wired in,
   expect the Google-News feeds (≈half of `NEWS_FEEDS`) to go from ~0 to mostly extracting.
4. Note any added latency: `googlenewsdecoder` does an extra network round-trip per link, so confirm a
   full `python main.py scan` still finishes in acceptable time; lower `FULLTEXT_MAX_ARTICLES` if not.

## Report back
- PASS/FAIL per step with the actual numbers (item counts, extraction rate, timing).
- Any parser fix you had to make to `_parse_boarddocs_xml` (with the real XML snippet).
- The Google News resolution hit-rate after wiring in `googlenewsdecoder`.
- Recommended `FULLTEXT_MAX_ARTICLES` if the default 80 makes the scan too slow.
- Anything that errored. Do NOT commit; leave changes for me to review with `git diff`.
