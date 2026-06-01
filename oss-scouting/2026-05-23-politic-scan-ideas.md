# local_politic_scan — OSS Scouting Report

**Date:** 2026-05-23
**Scope:** GitHub + HN + MCP registries, hunting for adoptable upgrades.

---

## Top actionable ideas

### 1. Replace ad-hoc government scraping with `civic-scraper` + `python-legistar-scraper`
[`civic-scraper`](https://github.com/biglocalnews/civic-scraper) standardizes agenda/minute/packet downloads across **Civic Clerk, Civic Plus, Granicus, Legistar, PrimeGov**. For Legistar specifically, [`opencivicdata/python-legistar-scraper`](https://github.com/opencivicdata/python-legistar-scraper) handles event pagination, vote rolls, and attachments. MoCo Council and Rockville both publish through Granicus/Legistar stacks; our hand-rolled BeautifulSoup parses break on every reskin.
**Integration:** In `scrapers/local_hearings.py`, replace the manual "Upcoming Meetings" loop with `civic_scraper.platforms.legistar.LegistarSite(url).scrape()`, persisting returned `Asset` objects into our existing `meetings` SQLite table; drop our PDF helper for `Asset.download()`.

### 2. Swap article extraction to `trafilatura` (or `Fundus` for tier-1 outlets)
[`trafilatura`](https://github.com/adbar/trafilatura) tops independent benchmarks (F1 ≈ 0.958, ~5× faster than news-please). [`Fundus`](https://github.com/flairNLP/fundus) ships hand-tuned per-outlet extractors — near-perfect where supported. Our TTS prompts currently leak cookie banners and related-link junk; trafilatura also extracts canonical date + author cleanly, killing our dedupe problem at the source.
**Integration:** In `pipeline/fetch.py`, replace the `readability-lxml` call (~line 80) with `trafilatura.extract(html, include_comments=False, with_metadata=True, output_format='json')`. Use Fundus only for Bethesda Beat / MoCo360 / WaPo Maryland where we already maintain extractors.

### 3. Adopt `Kokoro-82M` for TTS
[`Kokoro-TTS-Local`](https://github.com/PierrunoYT/Kokoro-TTS-Local) — 82M params, 36× real-time on a free GPU, fine on CPU, 54 voices, MIT-style license. 4 episodes/day × ~15 min is a real cloud bill, and the API has killed two episodes this month. Side-by-sides sound noticeably more natural than our current Piper voice.
**Integration:** Add `kokoro_tts.py` behind the existing `TTSProvider` interface in `podcast/tts.py`. Keep Piper as fallback for 60-second alert episodes. Pipe straight into `audio_normalize()` (see #4).

### 4. Standardize audio post with [`ffmpeg-normalize`](https://github.com/slhck/ffmpeg-normalize)'s `podcast` preset
Battle-tested EBU R128 normalizer with a built-in podcast profile matching AES loudness recommendations, plus `--batch` to preserve relative loudness across a playlist. Our playlist mode currently has jarring volume jumps between the "headlines" and "deep-dive" voices.
**Integration:** Shell out after `mp3_concat()` in `podcast/assemble.py`: `ffmpeg-normalize episode.mp3 -nt podcast -c:a libmp3lame -b:a 96k -o normalized.mp3`. For the daily playlist, rerun with `--batch` over the 4 files.

### 5. Generate podcast RSS with `python-feedgen` + ID3 chapter markers via `mrmp3`
[`python-feedgen`](https://github.com/lkiesow/python-feedgen) is the de-facto Python lib for iTunes-extension RSS. [`mrmp3`](https://github.com/rich1126/mrmp3) writes ID3v2 CHAP frames honored by Apple Podcasts, Overcast, and Pocket Casts. We have no feed today — users must open the web UI. Chapters let listeners jump to "Council vote on ADU zoning" without scrubbing.
**Integration:** New `podcast/rss.py` walks `episodes/*.mp3`, reads ID3 tags, emits `feed.xml` to the static-site output dir. Have the existing topic-segmenting script emit a `chapters.json` sidecar, then call `mrmp3` to embed.

---

## Honorable mentions

- **[City-Bureau/city-scrapers](https://github.com/City-Bureau/city-scrapers)** — Scrapy architecture (one spider per body + shared pipeline) is cleaner than our per-source script soup; worth studying even without adopting.
- **Contribute Fundus extractors** for Bethesda Beat and MoCo360 — we get free upstream maintenance.
- **[flair NER](https://github.com/flairNLP/flair)** — Extract `PERSON`/`ORG` mentions across the day to auto-surface trending figures for the candidate spotlight.
- **GDELT 2.0 + [Web NGrams 3.0](https://blog.gdeltproject.org/custom-entity-extraction-over-the-news-using-web-ngrams-3-0/)** — Cheap "anything national hit MD-08 today?" signal layered over local sources.
- **[MCP Fetch server](https://github.com/modelcontextprotocol/servers)** — Drop-in if we ever want Claude Desktop to query our digest interactively against SQLite.

---

## Dead ends

- **BoardDocs scrapers** — No maintained Python client. MCPS uses BoardDocs; keep hand-parsing. (Possible side project: contribute a `BoardDocsSite` adapter to `civic-scraper`.)
- **Offline-first civic PWAs** — Orpington News, Newsrack, etc. exist but are generic RSS readers; nothing closer to what we ship. Not worth swapping our static HTML + service worker.
