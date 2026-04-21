# Local Politics Scanner

A daily scanner that pulls federal, state, county, and school-board politics
news for a specific U.S. locale, uses Claude to write plain-English summaries
and relevance scores, and produces an HTML digest plus optional TTS podcast
episodes.

Default sources are wired for **Maryland / Montgomery County**, but the
personal bits (city, ZIP, districts, topic-keyword filter) are read from
`.env` so you can point it at your own address without editing code.

**State bills**: the scanner uses OpenStates, which covers every U.S.
state. Set `USER_STATE` and `USER_STATE_CODE` in `.env` (e.g. `California`
/ `ca`) and add an OpenStates key to pull bills from your own state.
County/school sources are still scraped from Montgomery County, MD — swap
them out in `config.py` and `scanner/sources/` for another county.

## Features

- **Multi-source ingestion**: Congress.gov, OpenStates (MD General Assembly),
  Montgomery County Council + MCPS + Police feeds, 8 local/regional RSS feeds
  (WTOP, Maryland Matters, Bethesda Magazine, Google News, ...).
- **AI enrichment**: Claude summarizes each item in plain English, scores how
  much it affects the user, and tags it by topic (tax, education, China/trade,
  immigration, health, ...).
- **Politician tracker**: links items to the politicians who sponsored or
  voted on them; `main.py politician "Name"` shows their recent activity.
- **Daily HTML report** saved to `reports/digest_YYYY-MM-DD.html`.
- **Podcast mode**: generates ~4 × 30-min dialogue episodes (federal / state /
  county / weekly review) using OpenAI TTS.
- **Local web server + chat**: browse past digests and ask questions about
  them using Claude as the backend.

## Requirements

- Python 3.10+
- API keys (all free except OpenAI):
  - [Anthropic](https://console.anthropic.com/) — required (AI summaries)
  - [Congress.gov](https://api.congress.gov/sign-up/) — required (federal bills)
  - [OpenStates](https://openstates.org/accounts/signup/) — optional (MD state bills)
  - [OpenAI](https://platform.openai.com/api-keys) — optional (podcast TTS only)

## Install

```bash
git clone https://github.com/<you>/local_politic_scan.git
cd local_politic_scan
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env and paste your keys
```

Windows users can run `install.bat` instead of the manual steps.

## Usage

```bash
python main.py scan          # fetch all sources, enrich with AI, write today's report
python main.py report        # regenerate HTML from existing DB (no re-scan)
python main.py politician "Marc Elrich"   # look up a politician's recent activity
python main.py status        # scan history + DB stats
python main.py setup         # register a Windows Task Scheduler daily job
python main.py serve         # start the local web UI
python main.py podcast       # generate TTS podcast episodes from latest report
```

Reports are written to `reports/digest_YYYY-MM-DD.html` — open in any browser.

## Configuration

Edit `config.py` to change location, districts, seeded politicians, RSS feeds,
federal-topic keyword filter, relevance threshold, and podcast settings.
`.env` holds API keys only.

## Data & privacy

- All data is stored locally in `data/politics.db` (SQLite).
- `.env`, `data/`, `reports/`, `podcasts/`, and `knowledge/` are gitignored.
- No telemetry; the only outbound calls are to the news/bill APIs and the
  Anthropic / OpenAI APIs you configure.

## License

MIT — see [LICENSE](LICENSE).
