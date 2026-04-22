"""
Central configuration for the local politics scanner.

Non-sensitive defaults live here; personal location, districts, and
topic-interest keywords are read from environment variables (see
`.env.example`) or from an optional `config_local.py` (gitignored)
that can override anything on `Config`.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

BASE_DIR = Path(__file__).parent


class Config:
    # ── User Location (set these in .env) ──────────────────────────────────────
    STATE = os.getenv("USER_STATE", "Maryland")
    STATE_CODE = os.getenv("USER_STATE_CODE", "md")
    COUNTY = os.getenv("USER_COUNTY", "Montgomery County")
    CITY = os.getenv("USER_CITY", "")
    ZIP_CODE = os.getenv("USER_ZIP_CODE", "")

    # Electoral districts (set in .env; leave blank if unknown)
    US_HOUSE_DISTRICT = os.getenv("US_HOUSE_DISTRICT", "")
    STATE_SENATE_DISTRICT = os.getenv("STATE_SENATE_DISTRICT", "")
    STATE_HOUSE_DISTRICT = os.getenv("STATE_HOUSE_DISTRICT", "")
    COUNTY_COUNCIL_DISTRICT = os.getenv("COUNTY_COUNCIL_DISTRICT", "")

    # Ballot-level districts (optional; surfaced to AI prompts so the
    # generated digest/podcast/chat prioritizes contests and offices the
    # user actually votes on). Blank = unknown / not applicable.
    PRECINCT = os.getenv("PRECINCT", "")
    LEGISLATIVE_DISTRICT = os.getenv("LEGISLATIVE_DISTRICT", "")
    COUNCILMANIC_DISTRICT = os.getenv("COUNCILMANIC_DISTRICT", "")
    CIRCUIT_COURT_DISTRICT = os.getenv("CIRCUIT_COURT_DISTRICT", "")
    APPELLATE_CIRCUIT_COURT = os.getenv("APPELLATE_CIRCUIT_COURT", "")
    CENTRAL_COMMITTEE = os.getenv("CENTRAL_COMMITTEE", "")
    ELECTION_DISTRICT = os.getenv("ELECTION_DISTRICT", "")
    SCHOOL_DISTRICT = os.getenv("SCHOOL_DISTRICT", "")
    SENATORIAL_DISTRICT = os.getenv("SENATORIAL_DISTRICT", "")

    @classmethod
    def districts_profile(cls) -> str:
        """Human-readable bullet list of the user's voting districts.

        Returns an empty string if no district fields are configured.
        Used by the AI content generators (processor, podcast, editor,
        chat) so their output prioritizes the contests and offices the
        user actually votes on.
        """
        rows = [
            ("U.S. Congressional District", cls.US_HOUSE_DISTRICT),
            ("MD Legislative District", cls.LEGISLATIVE_DISTRICT),
            ("MD State Senate District", cls.STATE_SENATE_DISTRICT),
            ("MD House of Delegates District", cls.STATE_HOUSE_DISTRICT),
            ("Senatorial District", cls.SENATORIAL_DISTRICT),
            ("County Councilmanic District", cls.COUNCILMANIC_DISTRICT or cls.COUNTY_COUNCIL_DISTRICT),
            ("Election District", cls.ELECTION_DISTRICT),
            ("Precinct", cls.PRECINCT),
            ("Circuit Court District", cls.CIRCUIT_COURT_DISTRICT),
            ("Appellate Circuit Court", cls.APPELLATE_CIRCUIT_COURT),
            ("School District", cls.SCHOOL_DISTRICT),
            ("Party Central Committee", cls.CENTRAL_COMMITTEE),
        ]
        # De-dupe identical (label, value) when STATE_SENATE == LEGISLATIVE, etc.
        seen = set()
        lines = []
        for label, value in rows:
            if not value:
                continue
            key = (label, str(value))
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  - {label}: {value}")
        return "\n".join(lines)

    # ── API Keys ───────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    CONGRESS_API_KEY: str = os.getenv("CONGRESS_API_KEY", "")
    OPENSTATES_API_KEY: str = os.getenv("OPENSTATES_API_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

    # ── Podcast settings ───────────────────────────────────────────────────────
    PODCAST_TARGET_MINUTES = 30           # ~30 min per episode
    PODCAST_EPISODES = 4                  # 4 episodes: federal, state, county, review
    PODCAST_TOP_N_STORIES = 8             # pick top-N per episode
    PODCAST_FILTER_INDIVIDUAL_INCIDENTS = True   # exclude crime/incident stories
    PODCAST_HOST_ALEX_VOICE = "onyx"      # OpenAI voices: alloy/echo/fable/onyx/nova/shimmer
    PODCAST_HOST_JORDAN_VOICE = "nova"
    PODCAST_TTS_MODEL = "tts-1"           # tts-1 ($15/1M chars) or tts-1-hd ($30/1M chars)
    PODCAST_SCRIPT_MODEL = "claude-sonnet-4-5-20250929"   # Claude model for dialogue writing
    PODCASTS_DIR = BASE_DIR / "podcasts"

    # ── Knowledge & chat ───────────────────────────────────────────────────────
    KNOWLEDGE_DIR = BASE_DIR / "knowledge"
    CHAT_MODEL = "claude-sonnet-4-5-20250929"

    # ── Federal Filter: ONLY these topics ─────────────────────────────────────
    # Federal news is massive — only items matching one of these keywords are
    # AI-enriched. Keep the list short and focused on what you actually care
    # about. Override by setting FEDERAL_KEYWORDS (comma-separated) in .env,
    # or by defining FEDERAL_KEYWORDS in config_local.py.
    FEDERAL_KEYWORDS = [
        k.strip() for k in os.getenv(
            "FEDERAL_KEYWORDS",
            # Neutral default: broad civic-impact topics.
            "budget, appropriations, infrastructure, healthcare, "
            "education, housing, transportation, veterans"
        ).split(",") if k.strip()
    ]

    # ── Scope settings ─────────────────────────────────────────────────────────
    SCAN_DAYS_BACK = 3          # How many days back to look for new items
    MAX_ITEMS_PER_SOURCE = 25   # Max items fetched per source per run
    RELEVANCE_THRESHOLD = 0.35  # Skip AI processing for items below this score

    # ── Paths ──────────────────────────────────────────────────────────────────
    DB_PATH = BASE_DIR / "data" / "politics.db"
    REPORTS_DIR = BASE_DIR / "reports"

    # ── News RSS Feeds ─────────────────────────────────────────────────────────
    NEWS_FEEDS = [
        # Local Montgomery County / Maryland
        {
            "name": "Maryland Matters",
            "url": "https://marylandmatters.org/feed/",
            "level": "state",
        },
        {
            "name": "WTOP News - Maryland",
            "url": "https://wtop.com/maryland/feed/",
            "level": "state",
        },
        {
            "name": "Bethesda Magazine",
            "url": "https://bethesdamagazine.com/feed/",
            "level": "county",
        },
        # Montgomery County government news
        {
            "name": "Montgomery County Press Releases",
            "url": "https://www.montgomerycountymd.gov/rss/council-releases.rss",
            "level": "county",
        },
        # Google News searches (no API key needed)
        {
            "name": "Google News: Montgomery County politics",
            "url": "https://news.google.com/rss/search?q=Montgomery+County+Maryland+council+politics&hl=en-US&gl=US&ceid=US:en",
            "level": "county",
        },
        {
            "name": "Google News: Maryland legislation",
            "url": "https://news.google.com/rss/search?q=Maryland+General+Assembly+bill+legislation&hl=en-US&gl=US&ceid=US:en",
            "level": "state",
        },
        {
            "name": "Google News: MCPS school board",
            "url": "https://news.google.com/rss/search?q=MCPS+Montgomery+County+school+board&hl=en-US&gl=US&ceid=US:en",
            "level": "school",
        },
        {
            "name": "Google News: Montgomery County police fire",
            "url": "https://news.google.com/rss/search?q=Montgomery+County+police+fire+department+hospital&hl=en-US&gl=US&ceid=US:en",
            "level": "local",
        },
    ]

    # ── Known Politicians (seeded, updated by AI during scans) ─────────────────
    # Format: {name, office, party, level, district}
    KNOWN_POLITICIANS = [
        # Federal
        {"name": "David Trone", "office": "U.S. Representative", "party": "Democrat", "level": "federal", "district": "MD-6"},
        {"name": "Ben Cardin", "office": "U.S. Senator", "party": "Democrat", "level": "federal", "district": "MD"},
        {"name": "Angela Alsobrooks", "office": "U.S. Senator", "party": "Democrat", "level": "federal", "district": "MD"},
        # Maryland State
        {"name": "Wes Moore", "office": "Governor of Maryland", "party": "Democrat", "level": "state", "district": "MD"},
        {"name": "Aruna Miller", "office": "Lt. Governor of Maryland", "party": "Democrat", "level": "state", "district": "MD"},
        # Montgomery County
        {"name": "Marc Elrich", "office": "Montgomery County Executive", "party": "Democrat", "level": "county", "district": "Montgomery"},
        {"name": "Andrew Friedson", "office": "County Council District 1", "party": "Democrat", "level": "county", "district": "1"},
        {"name": "Marilyn Balcombe", "office": "County Council District 2", "party": "Democrat", "level": "county", "district": "2"},
        {"name": "Sidney Katz", "office": "County Council District 3", "party": "Democrat", "level": "county", "district": "3"},
        {"name": "Nancy Navarro", "office": "County Council District 4", "party": "Democrat", "level": "county", "district": "4"},
        # MCPS Board of Education
        {"name": "Shebra Evans", "office": "MCPS Board of Education", "party": "Nonpartisan", "level": "school", "district": "Montgomery"},
    ]


# ── Optional local overrides ────────────────────────────────────────────────
# If `config_local.py` exists in this directory, any attributes it defines on
# a `Config` class (or at module level) override the defaults above. This file
# is gitignored — use it for personal settings you don't want to publish.
try:
    from config_local import Config as _LocalConfig  # type: ignore
    for _attr in dir(_LocalConfig):
        if not _attr.startswith("_"):
            setattr(Config, _attr, getattr(_LocalConfig, _attr))
except ImportError:
    pass
