# local_politic_scan — OSS Adoption Implementation Plan

**Date:** 2026-05-30
**Source:** consolidates `oss-scouting/2026-05-23-politic-scan-ideas.md` and `2026-05-30-politic-scan-ideas.md`
**Grounding:** file/function references below were verified against the current code on 2026-05-30. Where a scouting report guessed a path that doesn't exist, the real location is noted.

---

## How to read this

Each item lists: **Current state** (what the code does today), **Change**, **New deps**, **Effort**, **Risk**, **Verify**.

Three things were already partially built and change the original scouting framing:

- **State bills already call OpenStates v3.** `scanner/sources/state.py` has `_openstates_get()` + `fetch_state_bills()` with `_scrape_mga_bills()` as fallback — it is *not* hand-scraping mgaleg as the primary path. Item 4 is therefore a *hardening + candidate-linking* task, not a rewrite.
- **Federal already calls Congress.gov.** `scanner/sources/federal.py` (`fetch_bills`, `fetch_member_votes`). GovInfo MCP (item 5) adds a genuinely new signal — Congressional Record floor-speech text — that the bills API does not expose.
- **A 2-host ALEX/JORDAN dialogue already exists** in `scanner/podcast.py`. The monotonous single-voice script the 5/30 report targets is `scanner/deepdive.py` (`generate_deep_dive` → `_write_deep_dive_script`). Item 10 applies there, not to the daily episodes.

---

## Sequencing

**Phase 0 — unblock (do first, prerequisites for reliable rollout)**
- P0a. Atomic writes for `data/candidate_series.json` (`tempfile` + `os.replace`) — already flagged in the 5/28 notify; everything that runs in the drain depends on the registry parsing.
- P0b. Resolve the `politics.db` sqlite-over-FUSE blocker (move DB off the mount path, or pre-export the needed rows to flat JSON). Items 1–5 write to or read from the DB.

**Phase 1 — data quality (cheapest, highest signal)**
- Item 3 (trafilatura article extraction) → fixes junk leaking into TTS + dedupe.
- Item 1 (BoardDocs XML feed) → kills the most fragile parser.

**Phase 2 — data breadth**
- Item 2 (civic-scraper / Legistar), Item 4 (pyopenstates + candidate linking), Item 5 (GovInfo MCP), Item 6 (FEC / Census / SBE finance).

**Phase 3 — podcast quality**
- Item 7 (Kokoro TTS), Item 8 (ffmpeg-normalize), Item 9 (RSS + Podlove chapters), Item 10 (Podcastfy dialogue for deep-dive).

---

## Dependencies to add (`requirements.txt`)

```
trafilatura>=1.8.0          # item 3
civic-scraper>=0.3.0        # item 2
pyopenstates>=2.0.0         # item 4 (optional; raw API already works)
feedgen>=1.0.0              # item 9
podcastparser>=0.6.0        # item 9 (Podlove chapter read/write)
# Kokoro TTS (item 7): install per upstream (torch + kokoro), keep optional/extras
# ffmpeg-normalize (item 8): pip install ffmpeg-normalize ; requires ffmpeg binary on PATH
```

Keep the heavy/optional ones (Kokoro, ffmpeg-normalize) behind a `requirements-podcast.txt` extras file so the core scanner stays light.

---

## DATA INGESTION

### Item 1 — BoardDocs via XML feed (correction to 5/23 "dead end")
**Current state.** `scanner/sources/local_hearings.py::fetch_mcps_boarddocs()` scrapes the HTML listing at `https://go.boarddocs.com/mabe/mcpsmd/Board.nsf/Public` with BeautifulSoup and parses dates out of meeting titles — fragile on any reskin.
**Change.** Hit the structured feed instead: `https://go.boarddocs.com/mabe/mcpsmd/Board.nsf/XML-ActiveMeetings`. Fetch with `requests.get`, parse with `lxml` (already a dep). Each `<meeting>` node yields `unique`, `date`, `name` — map straight into the existing `meetings` table fields that `fetch_mcps_boarddocs` already returns. Pattern to mirror: City-Bureau `city-scrapers` `chi_teacherpension` spider.
**New deps.** None (`requests` + `lxml` already present).
**Effort.** S (½ day). **Risk.** Low — read-only swap, keep HTML scrape as fallback if XML 404s.
**Verify.** Diff the meetings returned by old vs new for one week; confirm count ≥ old and dates parse without the title-regex.

### Item 2 — Standardize council/hearing scraping with civic-scraper + Legistar
**Current state.** `local_hearings.py` hand-rolls `fetch_rockville_council`, `fetch_rockville_planning`, `fetch_mncppc_hearings`, `fetch_wssc_hearings` with per-source BeautifulSoup (`_get`, `_fetch_rockville_feed`). Breaks on every site reskin.
**Change.** Where MoCo Council / Rockville publish via Granicus/Legistar, replace the manual loop with `civic_scraper.platforms.legistar.LegistarSite(url).scrape()`, persisting returned `Asset` objects into the `meetings` table; use `Asset.download()` in place of the custom PDF helper.
**New deps.** `civic-scraper` (pulls `python-legistar-scraper`).
**Effort.** M (1–2 days, per-source URL discovery needed). **Risk.** Medium — confirm each body actually runs Legistar/Granicus before swapping; keep BoardDocs (item 1) and any non-Legistar bodies on their dedicated parsers.
**Verify.** One-week side-by-side per source; spot-check 5 meetings have correct date/agenda/packet links.

### Item 3 — Full-text article extraction with trafilatura  ⭐ highest-leverage data fix
**Current state.** `scanner/sources/news.py` uses `feedparser.parse()` + `_strip_html()` on RSS summaries — no real full-text fetch. (The 5/23 report's `pipeline/fetch.py` / `readability-lxml` path does not exist; this is the real location.) Result: cookie banners / related-link junk reach the relevance scorer and TTS, and dedupe is weak because canonical date/author aren't extracted.
**Change.** Add a fetch-and-extract step after RSS discovery: for each new article URL, `trafilatura.extract(html, include_comments=False, with_metadata=True, output_format='json')`. Store extracted `text`, `date`, `author` on the event row; feed the clean `text` to `scanner/processor.py` relevance scoring and to the podcast script context instead of the RSS blurb. Use `Fundus` only for Bethesda Beat / MoCo360 / WaPo-Maryland (per-outlet extractors); `newspaper4k` as backup if trafilatura misbehaves on a given outlet.
**New deps.** `trafilatura` (optionally `fundus`, `newspaper4k`).
**Effort.** M (1–2 days incl. caching to avoid re-fetch). **Risk.** Low–Medium — add polite rate-limiting + a fetch cache; fall back to RSS summary on extraction failure so a bad fetch never drops a story.
**Verify.** Re-score one day's stories before/after; confirm 0%-relevance false-negatives drop and no banner/nav text remains in stored `text`. (Directly addresses the recurring "state-leg items at 0% relevance" finding in the 5/17 & 5/24 site reviews.)

### Item 4 — Harden state bills with pyopenstates + auto-link to candidates
**Current state.** `sources/state.py::fetch_state_bills()` already calls OpenStates v3 directly (`_openstates_get`) with `_scrape_mga_bills` fallback. So this is *not* a rewrite.
**Change.** (a) Optionally route the raw calls through the maintained `pyopenstates` client for retry/pagination/rate-limit handling: `pyopenstates.search_bills(jurisdiction="md", updated_since=yesterday, q="Montgomery")`. (b) New value-add: cross-reference each bill's `sponsorships[].name` against the `candidates` table and auto-fire the candidate-spotlight feature when a tracked MD-08 / MoCo politician sponsors or votes. Also pull the OpenStates **events/hearings** endpoint to enrich the "Near You" section.
**New deps.** `pyopenstates` (optional — current raw API works).
**Effort.** S–M. **Risk.** Low. **Verify.** Confirm a known recent MoCo sponsor triggers a spotlight; confirm MGA fallback still fires when `OPENSTATES_API_KEY` is unset.

### Item 5 — GovInfo MCP for federal "mentions" signal
**Current state.** `sources/federal.py` queries Congress.gov for bills + member votes only — no floor-speech / Congressional Record text.
**Change.** Add `scanner/sources/federal_mcp.py` that queries GPO's **GovInfo MCP** (public preview) during the nightly federal pass for that day's Congressional Record / Federal Register entries matching `{"Raskin", "Trone", "Maryland-08", "Montgomery County"}`. Surface hits as a new **"Federal mentions"** section — add `_federal_mentions_section_html()` in `scanner/reporter.py` alongside the existing `_references_section_html` / `_event_card_html`, and render it in `_render_html()`.
**New deps.** GovInfo MCP server registration (no pip dep) or its REST equivalent. Honorable-mention alt: `lzinga/us-gov-open-data-mcp` (bundles FEC/FRED/Treasury/Congress).
**Effort.** M. **Risk.** Medium — preview API, may rate-limit; cache daily results and degrade gracefully.
**Verify.** Manually confirm a known Raskin floor statement surfaces; confirm the new section is hidden (not empty-rendered) on no-hit days.

### Item 6 — Campaign-finance & demographic context (honorable mentions)
**Current state.** Candidate-spotlight finance lookup is ad-hoc; dossiers lack precinct demographics.
**Change.** (a) **FEC** via `us-gov-open-data-mcp` for federal candidates. (b) **Maryland State Board of Elections Campaign Finance Database** CSV download for Delegate/Council races FEC doesn't cover — trivial `requests` + `csv` pull into the dossier pipeline (`scanner/dossier.py`). (c) **US Census Bureau MCP** for precinct-level demographic context in dossiers.
**New deps.** None required for the SBE CSV; MCP registrations for FEC/Census.
**Effort.** S each, independent. **Risk.** Low. **Verify.** One dossier shows real finance totals + a demographic line sourced from CSV/Census.

---

## PODCAST PIPELINE

### Item 7 — Kokoro-82M TTS behind the existing provider seam
**Current state.** `scanner/podcast.py::_synthesize_dialogue()` calls OpenAI TTS (`gpt-4o-mini-tts` / `tts-1`, voices `VOICES = {ALEX: onyx, JORDAN: nova}`), per-line, with retry + atomic `.partial` rename. Cloud cost + API has killed episodes this month.
**Change.** Add a `kokoro_tts.py` synthesizer and introduce a thin `TTSProvider` seam so `_synthesize_dialogue` dispatches by config (`openai` | `kokoro`). Map ALEX/JORDAN to two distinct Kokoro voices. Keep OpenAI/Piper as fallback for 60-second alert episodes. Preserve the existing `.partial`→rename crash-safety contract.
**New deps.** `kokoro` + `torch` (CPU works, GPU 36× real-time) — put in `requirements-podcast.txt`.
**Effort.** M. **Risk.** Medium — model download size + first-run latency; gate behind config flag, default OpenAI until validated.
**Verify.** A/B one episode OpenAI vs Kokoro; confirm duration estimate + chunking still hold and atomic rename still leaves no partial on failure.

### Item 8 — Loudness normalization with ffmpeg-normalize
**Current state.** Episodes are concatenated per-line MP3 bytes; playlist mode has jarring volume jumps between voices/segments.
**Change.** After the dialogue MP3 is assembled, shell out: `ffmpeg-normalize episode.mp3 -nt podcast -c:a libmp3lame -b:a 96k -o normalized.mp3` (EBU R128 podcast preset). For the daily playlist, rerun with `--batch` over the 4 files to preserve relative loudness. Hook this as a post-step in `podcast.py` after `_synthesize_dialogue` returns (and in `scanner/playlist.py` for the playlist build).
**New deps.** `ffmpeg-normalize` (pip) + `ffmpeg` binary on PATH.
**Effort.** S. **Risk.** Low. **Verify.** Measure integrated LUFS before/after; confirm consistent loudness across the 4-episode playlist.

### Item 9 — Podcast RSS feed + Podlove chapters (5/30 revises 5/23)
**Current state.** No feed — listeners open the web UI. `podcast.py` already writes `podcast_<date>_index.json` (episode titles/slugs) which is the natural feed source.
**Change.** New `scanner/podcast_rss.py` (or `podcast/rss.py`) using `python-feedgen` to emit `feed.xml` to the static-site output dir, walking the index JSON + `podcasts/*.mp3`. **5/30 correction:** prefer **Podlove Simple Chapters** embedded in the RSS `<item>` (visible before download, what Apple/Overcast honor) over ID3-only CHAP frames — `feedgen` supports the `psc:chapters` extension; `podcastparser` reads/writes v1.1/v1.2. Have the topic-segmenter emit a `chapters.json` sidecar and attach it both to RSS (psc) and optionally the MP3 (belt-and-braces, ~10 lines).
**New deps.** `feedgen`, `podcastparser`.
**Effort.** M. **Risk.** Low. **Verify.** Validate `feed.xml` in a podcast validator; load in Overcast/Apple and confirm chapters appear and jump correctly.

### Item 10 — Multi-speaker dialogue for the deep-dive episode (Podcastfy)
**Current state.** `scanner/deepdive.py::generate_deep_dive()` → `_write_deep_dive_script()` produces one monolithic single-voice exposition (docstring literally says "single-"). Listener feedback flags it as monotonous. (The *daily* episodes already use ALEX/JORDAN — do not touch those.)
**Change.** Replace the monolithic prompt in `_write_deep_dive_script` with Podcastfy's `transcript_to_dialogue` chain — it's LLM-agnostic, so keep the existing Claude calls; just adopt the speaker-turn prompt template + `[HOST_A]`/`[HOST_B]` parser. Route the two host turns to the two voices from item 7 (Kokoro) or current OpenAI voices. Run the result through the existing `scanner/editor.review_script` loop unchanged.
**New deps.** Borrow `podcastfy`'s prompt/parser (Apache-2.0) — vendor the template rather than taking a heavy dep if preferred.
**Effort.** M. **Risk.** Low–Medium — keep word-count target (~3,900 ±300) and the no-URL/no-stage-direction invariants the series pipeline already enforces.
**Verify.** Generate one deep-dive, confirm clean two-speaker turns, length within target, passes editor loop, and TTS routes both voices.

---

## Cross-cutting verification

After each phase: re-run a single day end-to-end (`scan` → drain → podcast) and diff the digest + episode against the prior day. Add a smoke test per new source that asserts non-empty, well-formed output and that each new external call degrades gracefully (fallback path) when the key/endpoint is missing — this is what prevents the silent-empty-section failures the weekly site reviews keep flagging.

## Dependency on the open site-review findings

Item 3 directly closes the recurring **"state-leg items at 0% relevance"** finding (5/17 + 5/24). Items 1–2 address the recurring **"empty sections / Near You"** finding. None of these can be validated in the unattended Linux drain until **P0b** (sqlite-over-FUSE) is resolved, because the verify steps read/write `data/politics.db`.
