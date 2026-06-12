# local_politic_scan — OSS Scouting Report

**Date:** 2026-06-06
**Scope:** New finds only. Skips items already covered in the 2026-05-23 / 2026-05-30 reports (city-scrapers BoardDocs, GovInfo MCP, pyopenstates, Podlove chapters, Podcastfy, Kokoro, newspaper4k). Read against the live tree (`scanner/sources/`, `scanner/podcast.py`, `scanner/processor.py`).

---

## Top actionable ideas

### 1. Add semantic cross-source dedup with `semhash` — the pipeline currently has none
[`MinishLab/semhash`](https://github.com/MinishLab/semhash) (MIT, 2025) does near-duplicate clustering via Model2Vec static embeddings + ANN — CPU-only, no GPU, sub-second on a few hundred items. Right now `scanner/sources/news.py` keeps every RSS entry, and the only dedup is DB-level `UNIQUE(name,url)` (`database.py:218`) plus `processor.py`'s candidate substring filter (~line 103). So the same story from Bethesda Beat, MoCo360, and WTOP enters the digest and podcast script three times.
**Integration:** After `fetch_rss_feeds()` in `main.py` (~line 191), run `SemHash.from_records([e["title"]+" "+e["summary"] for e in news]).self_deduplicate(threshold=0.85)` and collapse clusters to the earliest-published item, stashing the others as `_also_covered_by`. Kills the most visible quality issue — repeated headlines — before LLM spend in `processor.py`.

### 2. Two-pass loudness normalization with `ffmpeg-normalize` (built-in `podcast` preset)
[`slhck/ffmpeg-normalize`](https://github.com/slhck/ffmpeg-normalize) wraps `loudnorm` with a one-line CLI, a `podcast` preset (≈ −16 LUFS, AES), and `--batch` to preserve relative loudness across files. `scanner/podcast.py` synthesizes 4 episodes/day from OpenAI TTS (`alloy`/`nova`), whose levels drift between voices and chunks — listeners ride the volume knob.
**Integration:** After `_synthesize_dialogue()` writes each MP3, shell out: `ffmpeg-normalize ep.mp3 -o ep.mp3 -f -t -16 -c:a libmp3lame`. One subprocess call, ~5 lines, no Python deps. Do it as a final pass over the day's `podcasts/` dir so all 4 match.

### 3. Generate a real subscribable podcast feed straight from `podcasts/` with `folderpodgen`
There is **no podcast RSS feed today** (grep: no `feedgen`/`podgen`/`enclosure` anywhere in `scanner/`). Last week's plan coupled feed-gen to the pipeline; [`folderpodgen`](https://pypi.org/project/folderpodgen/) (built on [`python-podgen`](https://github.com/tobinus/python-podgen) + `mutagen`) instead reads ID3 tags from a directory and emits valid RSS — decoupled and self-healing if a run is missed.
**Integration:** New `scanner/feed.py` (or a `server.py` route) that points folderpodgen at `podcasts/` and writes `podcasts/feed.xml`, served by the existing web UI. Makes the 4 daily episodes subscribable in Apple/Overcast/Spotify — a genuine distribution win the playlist feature can't give.

---

## Honorable mentions

- **[`fritshermans/deduplipy`](https://github.com/fritshermans/deduplipy)** — active-learning entity resolution; heavier fallback to idea #1 if Model2Vec under-merges on local-name variants ("MoCo Council" vs "Montgomery County Council").
- **[`citronalco/mp3-to-rss2feed`](https://github.com/citronalco/mp3-to-rss2feed)** — reference for writing ID3v2 `CHAP` + `TLEN` frames; pairs with the chapters work already planned.
- **[`mp3chapters.github.io`](https://mp3chapters.github.io/)** — browser tool to hand-verify chapter output during dev; not a dependency.
- **Google News MCP / Tavily MCP** (in [`modelcontextprotocol/servers`](https://github.com/modelcontextprotocol/servers) community list) — categorized news search as a discovery supplement to the curated `NEWS_FEEDS`.
- **[`CivicPress/civicpress`](https://github.com/CivicPress/civicpress)** — Git-backed civic records/meetings store (alpha). Reference architecture only; not adoptable yet.

---

## Dead ends

- **"council meeting tracker / public hearing monitor" GitHub, 2025** — nothing adoptable. Only CivicPress (alpha) and generic awesome-lists. The XML-feed approach already in `local_hearings.py` remains the right pattern.
- **MCP server for audio generation/normalization** — none exists. The only audio MCPs are SuperCollider (synthesis) and Audioscrape (podcast *search*). Stick with local `ffmpeg-normalize` (idea #2); don't keep searching here.
- **Live MoCo Legistar client** — still confirmed dead (`civic.py` header): the public instance is frozen at 2023. Granicus RSS direct-read stays the workaround.
