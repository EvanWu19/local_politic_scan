# local_politic_scan — OSS Scouting Report

**Date:** 2026-05-30
**Scope:** New finds not already in 2026-05-23 report; one correction to last week's "dead ends."

---

## Top actionable ideas

### 1. BoardDocs is NOT a dead end — `city-scrapers`' chi_teacherpension spider hits the XML feed directly
Correction to last week. [`City-Bureau/city-scrapers`](https://github.com/City-Bureau/city-scrapers) ships a working BoardDocs parser at [`city_scrapers/spiders/chi_teacherpension.py`](https://github.com/City-Bureau/city-scrapers/blob/main/city_scrapers/spiders/chi_teacherpension.py). It calls `https://www.boarddocs.com/<state>/<org>/board.nsf/XML-ActiveMeetings` and parses with Scrapy's `XMLFeedSpider`. Same NSF view exists for MCPS: `https://go.boarddocs.com/mabe/mcpsmd/Board.nsf/XML-ActiveMeetings`.
**Integration:** In `scanner/local_hearings.py`, replace the BoardDocs HTML-scrape branch with a single `requests.get(XML_URL)` + `lxml` pass mirroring `_parse_boarddocs`. Each `<meeting>` node gives `meeting-id`, `date`, `name`, `unique` — feed straight into our `meetings` table. Kills our most fragile parser.

### 2. Adopt the GovInfo MCP server for federal context on local stories
[GPO's official GovInfo MCP](https://www.govinfo.gov/features/mcp-public-preview) (public preview, Jan 2026) exposes Federal Register, Congressional Record, bills, and committee reports as MCP tools. Our daily digest currently misses "Raskin floor speech on X mentioned MoCo" type signals.
**Integration:** Add a `scanner/federal_mcp.py` that, during `scanner/federal.py`'s nightly pass, queries the GovInfo MCP for the day's Congressional Record entries matching `{"Raskin", "Trone", "Maryland-08", "Montgomery County"}`. Surface hits as a new "Federal mentions" section in `reporter.py`'s digest template.

### 3. Track Maryland bills with `pyopenstates` (v3) instead of hand-scraping mgaleg.maryland.gov
[`openstates/pyopenstates`](https://github.com/openstates/pyopenstates) wraps Open States API v3. Maryland is fully covered with bill text, sponsors, votes, actions, and event/hearing endpoints.
**Integration:** New `scanner/state_bills.py` calling `pyopenstates.search_bills(jurisdiction="md", updated_since=yesterday, q="Montgomery")`. Cross-reference returned `sponsorships` to our `candidates` table — auto-fires the candidate-spotlight feature when a tracked MD-08/MoCo politician sponsors or votes on something. Drops the brittle MGA HTML parse in `scanner/state.py`.

### 4. Switch podcast chapters from ID3 CHAP to Podlove Simple Chapters + RSS-side embed
Last week's plan used `mrmp3` to write ID3v2 CHAP frames. Problem: Apple Podcasts and Overcast actually prefer **Podlove Simple Chapters** embedded in the RSS `<item>` (chapter metadata visible before download). [`podcastparser`](https://podcastparser.readthedocs.io/) reads/writes both v1.1 and v1.2; [`python-feedgen`](https://github.com/lkiesow/python-feedgen) already supports the `psc:chapters` extension.
**Integration:** In the planned `podcast/rss.py`, attach a `PodloveSimpleChaptersExtension` per episode using the same `chapters.json` we'd feed to `mrmp3`. Embed in both RSS and MP3 — belt and braces, ~10 lines.

### 5. Steal Podcastfy's multi-speaker dialogue prompt for the deep-dive episode
[`souzatharsis/podcastfy`](https://github.com/souzatharsis/podcastfy) (Apache-2.0) generates a two-host conversation transcript from arbitrary text, then routes lines to distinct TTS voices. The prompt template + speaker-turn parser is the valuable bit. Our current deep-dive episode is single-voice exposition that listener feedback flags as monotonous.
**Integration:** In `scanner/podcast.py`'s `generate_deepdive_script()`, replace the monolithic prompt with Podcastfy's `transcript_to_dialogue` chain (LLM-agnostic — works with our existing Claude calls). Pipe `[HOST_A]` / `[HOST_B]` turns to two different Kokoro voices from idea #3 last week.

---

## Honorable mentions

- **[`lzinga/us-gov-open-data-mcp`](https://github.com/lzinga/us-gov-open-data-mcp)** — 40+ federal APIs (FEC, FRED, Treasury, Congress) in one MCP. FEC alone could replace our candidate-spotlight finance lookup.
- **[`uscensusbureau/us-census-bureau-data-api-mcp`](https://github.com/uscensusbureau/us-census-bureau-data-api-mcp)** — Official Census MCP. Drop into the dossier pipeline for precinct-level demographic context.
- **[`AndyTheFactory/newspaper4k`](https://github.com/AndyTheFactory/newspaper4k)** — Maintained newspaper3k fork. If trafilatura (last week's pick) misbehaves on Bethesda Magazine, this is the proven backup.
- **[`Goekdeniz-Guelmez/Local-NotebookLM`](https://github.com/Goekdeniz-Guelmez/Local-NotebookLM)** — Ollama-backed, fully offline NotebookLM. Useful reference architecture for the "playlist" feature.
- **Maryland State Board of Elections [Campaign Finance Database](https://elections.maryland.gov/campaign_finance/campaign_finance_database.html)** — Direct CSV download for state-level (FEC doesn't cover Delegates/Council). No GitHub lib; trivial to scrape.

---

## Dead ends

- **MD-08–specific GitHub tooling** — Nothing dedicated exists. FEC + Maryland SBE remain the only routes; no shortcut.
- **Hacker News search for "civic tech 2025/2026"** — Algolia returns mostly meta-AI threads. Skip HN for civic-specific OSS; GitHub topic pages are higher signal.
