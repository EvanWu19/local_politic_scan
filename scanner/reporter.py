"""
Report generator: produces a daily HTML + Markdown voter digest.
Designed for someone new to elections — plain English, no jargon.
"""
import html as _html
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import List, Dict, Optional

from config import Config as _Cfg

log = logging.getLogger(__name__)

_LOCALE = ", ".join(p for p in [_Cfg.CITY, _Cfg.COUNTY, _Cfg.STATE] if p) or "Your area"
_STATE = _Cfg.STATE or "State"
_COUNTY = _Cfg.COUNTY or "County"

# ── Chat/Notes sidebar ─────────────────────────────────────────────────────────
# Plain string (not f-string) so JS braces don't need escaping.
# DATE_ISO is replaced at render time with the actual report date.
_CHAT_SIDEBAR = """
<style>
  #chat-fab { position:fixed; bottom:24px; right:24px; z-index:1000;
    background:#1a3a5c; color:white; border:none; border-radius:50%;
    width:52px; height:52px; font-size:1.4rem; cursor:pointer;
    box-shadow:0 4px 12px rgba(0,0,0,.25); }
  #chat-fab:hover { background:#2a5a8c; }
  #chat-panel { position:fixed; bottom:88px; right:24px; z-index:999;
    width:340px; background:white; border-radius:12px;
    box-shadow:0 8px 32px rgba(0,0,0,.18); display:none;
    flex-direction:column; max-height:520px; overflow:hidden; }
  #chat-panel.open { display:flex; }
  .cp-header { background:#1a3a5c; color:white; padding:12px 16px;
    border-radius:12px 12px 0 0; display:flex; justify-content:space-between; align-items:center; }
  .cp-header span { font-weight:600; font-size:.95rem; }
  .cp-close { background:none; border:none; color:white; font-size:1.2rem;
    cursor:pointer; padding:0 4px; line-height:1; }
  .cp-body { flex:1; overflow-y:auto; padding:12px; }
  #notes-area { width:100%; min-height:260px; border:1px solid #ddd;
    border-radius:6px; padding:10px; font-size:.88rem; font-family:inherit;
    resize:vertical; line-height:1.5; }
  #notes-status { font-size:.75rem; color:#888; margin-top:6px; display:block; }
  @media (max-width:400px) {
    #chat-panel { right:8px; left:8px; width:auto; }
  }
</style>

<button id="chat-fab" title="Daily Notes" onclick="toggleChat()">📝</button>
<div id="chat-panel">
  <div class="cp-header">
    <span>📝 Daily Notes</span>
    <button class="cp-close" onclick="toggleChat()">✕</button>
  </div>
  <div class="cp-body">
    <textarea id="notes-area" placeholder="What did you think about today's digest? Questions, reactions, topics you want covered more..."></textarea>
    <span id="notes-status"></span>
  </div>
</div>

<script>
(function() {
  var REPORT_DATE = 'DATE_ISO';
  var STORAGE_KEY = 'notes-' + REPORT_DATE;
  var API_BASE = (window.location.protocol === 'file:') ? 'http://localhost:8080' : window.location.origin;
  var NOTES_URL = API_BASE + '/api/daily-notes/' + REPORT_DATE;

  window.toggleChat = function() {
    var p = document.getElementById('chat-panel');
    p.classList.toggle('open');
    if (p.classList.contains('open')) { loadNotes(); }
  };

  function setStatus(msg) {
    var s = document.getElementById('notes-status');
    s.textContent = msg;
    clearTimeout(window._notesFadeTimer);
    if (msg) {
      window._notesFadeTimer = setTimeout(function() { s.textContent = ''; }, 3000);
    }
  }

  function loadNotes() {
    var el = document.getElementById('notes-area');
    el.value = localStorage.getItem(STORAGE_KEY) || '';
    fetch(NOTES_URL)
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(data) {
        if (data && typeof data.text === 'string' && data.text.length >= el.value.length) {
          el.value = data.text;
          localStorage.setItem(STORAGE_KEY, data.text);
        }
      })
      .catch(function() {});
  }

  function saveNotes() {
    var text = document.getElementById('notes-area').value;
    localStorage.setItem(STORAGE_KEY, text);
    setStatus('Saving…');
    fetch(NOTES_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text })
    })
    .then(function(r) { return r.ok ? r.json() : Promise.reject(r.status); })
    .then(function() { setStatus('Saved ' + new Date().toLocaleTimeString()); })
    .catch(function() { setStatus('Saved locally (server unreachable)'); });
  }

  document.getElementById('notes-area').addEventListener('input', function() {
    clearTimeout(window._notesTimer);
    window._notesTimer = setTimeout(saveNotes, 800);
  });
})();
</script>
"""

# ── Podcast player (injected into each digest page) ───────────────────────────
# Plain string so JS braces need no escaping. DATE_ISO replaced at render time.
_PODCAST_PLAYER_JS = """
<div id="pod-section" style="margin-bottom:24px;display:none">
  <div style="display:flex;align-items:center;justify-content:space-between;
              border-bottom:2px solid #1a3a5c;padding-bottom:6px;margin-bottom:12px;">
    <h2 style="font-size:1.1rem;color:#1a3a5c;margin:0;">🎧 Today's Episodes</h2>
    <label style="font-size:.8rem;color:#1a3a5c;display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none;">
      <input type="checkbox" id="pod-autoplay-toggle" style="cursor:pointer;">
      Auto-play next
    </label>
  </div>
  <div id="pod-list" style="display:grid;gap:10px;"></div>
  <p id="pod-none" style="display:none;font-size:.85rem;color:#888;padding:8px 0;">
    No podcast generated yet — run <code>python main.py podcast</code>
  </p>
</div>
<script>
(function() {
  var DATE = 'DATE_ISO';
  var BASE = (window.location.protocol === 'file:') ? 'http://localhost:8080' : window.location.origin;
  var podSection = document.getElementById('pod-section');
  var podList    = document.getElementById('pod-list');
  var podNone    = document.getElementById('pod-none');
  var toggle     = document.getElementById('pod-autoplay-toggle');

  var AUTOPLAY_KEY = 'podAutoplayNext';
  var stored = null;
  try { stored = localStorage.getItem(AUTOPLAY_KEY); } catch (e) {}
  toggle.checked = (stored === null) ? true : (stored === '1');
  toggle.addEventListener('change', function() {
    try { localStorage.setItem(AUTOPLAY_KEY, toggle.checked ? '1' : '0'); } catch (e) {}
  });

  // entries[i] = {item, card, audio}; populated from server discovery.
  var entries = [];

  function setActive(idx) {
    entries.forEach(function(e, i) {
      var on = (i === idx);
      e.card.style.borderLeftColor = on ? '#1a3a5c' : 'transparent';
      e.card.style.background      = on ? '#f4f8fc' : 'white';
    });
  }
  function clearActive(idx) {
    var e = entries[idx]; if (!e) return;
    e.card.style.borderLeftColor = 'transparent';
    e.card.style.background = 'white';
  }

  function fmtMB(bytes) {
    if (!bytes || bytes < 0) return '';
    return ' · ' + (bytes / 1048576).toFixed(1) + ' MB';
  }

  function buildCard(item) {
    var card = document.createElement('div');
    card.style.cssText = 'background:white;border-radius:8px;padding:14px;' +
                          'box-shadow:0 1px 3px rgba(0,0,0,.07);' +
                          'border-left:4px solid transparent;transition:all .2s;';
    var label = item.display_title || item.title || 'Episode';
    card.innerHTML =
      '<div style="font-weight:600;color:#1a3a5c;font-size:.9rem;margin-bottom:8px">' +
        label.replace(/&/g,'&amp;').replace(/</g,'&lt;') +
        '<span style="font-weight:400;color:#888;font-size:.75rem">' + fmtMB(item.size) + '</span>' +
      '</div>' +
      '<audio controls preload="none" style="width:100%">' +
        '<source src="' + BASE + item.url + '" type="audio/mpeg"></audio>';
    return card;
  }

  fetch(BASE + '/api/podcast-files/' + DATE)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var items = (data && data.items) || [];
      podSection.style.display = '';
      if (items.length === 0) { podNone.style.display = ''; return; }

      items.forEach(function(item) {
        var card = buildCard(item);
        var audio = card.querySelector('audio');
        podList.appendChild(card);
        entries.push({item: item, card: card, audio: audio});
      });

      entries.forEach(function(e, idx) {
        e.audio.addEventListener('play',  function() { setActive(idx); });
        e.audio.addEventListener('pause', function() {
          if (!e.audio.ended) clearActive(idx);
        });
        e.audio.addEventListener('ended', function() {
          if (!toggle.checked) { clearActive(idx); return; }
          var next = entries[idx + 1];
          if (!next) { clearActive(idx); return; }
          next.card.scrollIntoView({behavior: 'smooth', block: 'center'});
          var p = next.audio.play();
          if (p && typeof p.catch === 'function') {
            p.catch(function() { setActive(idx + 1); });
          }
        });
      });
    })
    .catch(function() {
      podSection.style.display = '';
      podNone.style.display = '';
    });
})();
</script>
"""

# ── Section labels ─────────────────────────────────────────────────────────────
LEVEL_LABELS = {
    "federal": "🇺🇸 Federal (filtered topics)",
    "state": f"🏛️ {_STATE} State Legislature",
    "county": f"🏘️ {_COUNTY}",
    "school": "🎓 MCPS Board & School Cluster",
    "local": "📍 Near You — Rockville / 20853 Hearings",
}

TYPE_LABELS = {
    "bill": "Bill",
    "hearing": "Public Hearing",
    "lawsuit": "Lawsuit",
    "ordinance": "Ordinance",
    "vote": "Vote",
    "election": "Election",
    "budget": "Budget",
    "news": "News",
}

CATEGORY_TAGS = {
    "tax": "#d4a",
    "education": "#4a9",
    "china": "#a74",
    "visa": "#749",
    "health": "#4a7",
    "police": "#457",
    "fire": "#a44",
    "housing": "#8a4",
    "budget": "#aa4",
    "election": "#a44",
    "environment": "#4a6",
    "transportation": "#446",
}


def generate(events: List[Dict], politician_summaries: List[Dict],
             report_date: Optional[date] = None) -> Dict[str, str]:
    """
    Generate both HTML and Markdown versions of the daily digest.
    Returns {"html": "...", "markdown": "..."}.
    """
    if report_date is None:
        report_date = date.today()

    # Group events by level
    by_level: Dict[str, List[Dict]] = {k: [] for k in LEVEL_LABELS}
    for ev in events:
        level = ev.get("level", "county")
        if level in by_level:
            by_level[level].append(ev)

    html = _render_html(report_date, by_level, politician_summaries)
    markdown = _render_markdown(report_date, by_level, politician_summaries)
    return {"html": html, "markdown": markdown}


# ── HTML renderer ──────────────────────────────────────────────────────────────

def _load_candidate_spotlight(report_date: date) -> Dict:
    """Look up the registry entry scheduled for `report_date` and assemble
    the data the spotlight panel needs (dossier excerpt + per-episode file
    state). Returns {} when no candidate is scheduled or the registry is
    missing — `_render_html` then renders no panel."""
    try:
        from scanner.series import candidate_for_date, _slug
    except Exception:
        return {}

    cand = candidate_for_date(report_date)
    if not cand:
        return {}

    # Full dossier text (no truncation). The renderer formats it into a
    # dedicated card below the politician tracker with clickable [src: URL]
    # footnotes — so we want everything the dossier author wrote, not an
    # excerpt.
    dossier_text = ""
    dossier_path = cand.get("dossier_path")
    if dossier_path:
        p = Path(dossier_path)
        if not p.is_absolute():
            from config import BASE_DIR
            p = BASE_DIR / p
        if p.exists():
            try:
                raw = p.read_text(encoding="utf-8")
                # Strip a YAML front-matter block if present (between two `---`
                # lines at the top of the file). DO NOT strip `---` dividers
                # later in the body — those get rendered as <hr>.
                if raw.startswith("---\n") or raw.startswith("---\r\n"):
                    parts = raw.split("---", 2)
                    if len(parts) >= 3:
                        raw = parts[2].lstrip()
                dossier_text = raw.strip()
            except Exception as e:
                log.warning("Spotlight: failed to read %s — %s", p, e)

    # Per-episode on-disk state
    podcasts_dir = _Cfg.PODCASTS_DIR
    slug = _slug(cand.get("name", ""))
    date_iso = report_date.isoformat()
    EP_TITLES = {1: "Origins", 2: "Career", 3: "Political Record", 4: "This Race"}
    episodes = []
    for n in (1, 2, 3, 4):
        stem = f"podcast_{date_iso}_series_{slug}_ep{n}"
        mp3 = podcasts_dir / f"{stem}.mp3"
        txt = podcasts_dir / f"{stem}.txt"
        if mp3.exists() and mp3.stat().st_size > 1024:
            state = "ready"
        elif txt.exists() and txt.stat().st_size > 200:
            state = "drafted"
        else:
            state = "pending"
        episodes.append({
            "num": n,
            "title": EP_TITLES.get(n, f"Episode {n}"),
            "state": state,
            "url":   f"/podcast/{stem}.mp3" if state == "ready" else "",
        })

    # Recent events for this candidate — used by the fallback when no dossier
    # file is on disk yet, so the panel still says something concrete.
    recent_events: list = []
    try:
        from scanner.database import recent_events_for_politician
        recent_events = recent_events_for_politician(
            _Cfg.DB_PATH, cand.get("name", ""), limit=6
        )
    except Exception as e:
        log.debug("Spotlight: recent_events_for_politician failed — %s", e)

    return {
        "candidate": cand,
        "dossier_excerpt": dossier_text,
        "episodes": episodes,
        "recent_events": recent_events,
    }


# Captures `[src: URL]` citation tokens in dossier markdown. The URL ends
# at the closing bracket; we don't try to URL-decode or validate beyond that.
_SRC_RE = re.compile(r"\[src:\s*(https?://[^\]\s]+)\s*\]")
# Markdown bold — non-greedy, doesn't cross paragraph breaks.
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def _render_spotlight_header(spot: Dict) -> str:
    """Top-of-page header card: name, office/party/district subline, badges.
    Returns '' when no candidate is scheduled for the report date.
    The dossier body is rendered separately by `_render_dossier_body()` and
    placed lower on the page."""
    if not spot:
        return ""
    cand = spot["candidate"]
    name   = _html.escape(cand.get("name", "Unknown"))
    office = _html.escape(cand.get("office", ""))
    party  = _html.escape(cand.get("party", "") or "")
    dist   = _html.escape(cand.get("district", "") or "")

    badges = []
    if cand.get("incumbent"):
        badges.append('<span class="spot-badge spot-badge-inc">Incumbent</span>')
    else:
        badges.append('<span class="spot-badge">Challenger</span>')
    if cand.get("uncontested"):
        badges.append('<span class="spot-badge spot-badge-warn">Uncontested</span>')
    tier = cand.get("tier")
    if tier:
        badges.append(f'<span class="spot-badge">Tier&nbsp;{tier}</span>')

    sub_bits = [b for b in (office, party, dist) if b]
    subline = "&nbsp;·&nbsp;".join(sub_bits)

    return f"""
<div class="spot-panel">
  <h2>🎙️ Today's Candidate Spotlight</h2>
  <div class="spot-name">{name}</div>
  <div class="spot-sub">{subline}</div>
  <div class="spot-badges">{"".join(badges)}</div>
</div>
"""


def _format_dossier_markdown(text: str):
    """Convert dossier markdown → HTML body + numbered sources list.

    Citation handling: every `[src: URL]` token in the source text becomes
    a superscript footnote `<sup><a>[N]</a></sup>` linking to the URL.
    First-occurrence ordering — the same URL repeats get the same number.

    Returns (body_html, ordered_urls). The body_html is safe to inject
    inside a div; ordered_urls is the deduplicated URL list in footnote
    order (caller renders the Sources sub-panel).
    """
    # 1. Walk citations and assign numbers (first occurrence wins).
    url_to_num: Dict[str, int] = {}
    ordered_urls: list = []
    for m in _SRC_RE.finditer(text):
        u = m.group(1)
        if u not in url_to_num:
            url_to_num[u] = len(ordered_urls) + 1
            ordered_urls.append(u)

    # 2. Replace citations with sentinel tokens (use chars that survive
    #    html.escape unchanged so we can substitute real <sup> tags after).
    #    We pick a token shape that's vanishingly unlikely in real text.
    def _to_sentinel(m):
        return f"\x00CITE{url_to_num[m.group(1)]}\x00"
    work = _SRC_RE.sub(_to_sentinel, text)

    # 3. Substitute `---` horizontal-rule lines (--- alone on a line) BEFORE
    #    escaping, so the sentinel `\x00HR\x00` survives unescaped. Match
    #    only horizontal whitespace around the dashes — `\s*` would greedily
    #    eat surrounding `\n` and collapse paragraph breaks.
    work = re.sub(r"(?m)^[ \t]*---+[ \t]*$", "\x00HR\x00", work)

    # 4. Bold sentinel — same trick. `**foo**` → `\x00B foo \x00/B\x00`.
    def _to_bold_sentinel(m):
        return "\x00B\x00" + m.group(1) + "\x00/B\x00"
    work = _BOLD_RE.sub(_to_bold_sentinel, work)

    # 5. HTML-escape the lot.
    work = _html.escape(work)

    # 6. Substitute sentinels back to real tags. Order matters: do citation
    #    + bold + hr in any order, they don't collide.
    work = re.sub(r"\x00CITE(\d+)\x00", lambda m: (
        f'<sup><a href="{_html.escape(ordered_urls[int(m.group(1)) - 1])}"'
        f' target="_blank">[{m.group(1)}]</a></sup>'
    ), work)
    work = work.replace("\x00B\x00", "<strong>").replace("\x00/B\x00", "</strong>")
    work = work.replace("\x00HR\x00", "<hr>")

    # 7. Paragraph-split on blank lines; preserve single newlines as <br>.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", work) if p.strip()]
    body_html = "".join(
        f'<p>{p.replace(chr(10), "<br>")}</p>' for p in paragraphs
    )

    return body_html, ordered_urls


def _render_dossier_body(spot: Dict) -> str:
    """White-card dossier panel placed after the politician tracker.

    Renders the full dossier markdown as paragraphs with clickable
    `[src: URL]` superscript footnotes and a numbered Sources list at the
    bottom. Returns '' when:
      • no candidate scheduled, or
      • candidate has no dossier file on disk (the fallback messaging
        lives in the header panel via `_render_spotlight_header`-adjacent
        UI — keeping this body silent avoids two "no dossier" messages on
        the same page).
    """
    if not spot:
        return ""
    cand = spot["candidate"]
    dossier_text = (spot.get("dossier_excerpt") or "").strip()
    if not dossier_text:
        # No dossier file — render the fallback messaging here so the
        # reader sees one consolidated explanation instead of an empty page.
        try:
            from scanner.dossier import describe_dossier_status
            status = describe_dossier_status(cand.get("name", ""))
        except Exception:
            status = {"state": "unknown", "when": ""}
        recent_events = spot.get("recent_events") or []
        bits = []
        if recent_events:
            evs = "".join(
                f'<li><a href="{_html.escape(e.get("source_url","#"))}" '
                f'target="_blank">{_html.escape(e.get("title","")[:120])}</a> '
                f'<span style="color:#777;font-size:.8rem;">'
                f'{_html.escape(e.get("date",""))}</span></li>'
                for e in recent_events[:4]
            )
            bits.append(
                '<p>Recent activity on file:</p>'
                f'<ul style="margin:6px 0 10px 18px;">{evs}</ul>'
            )
        if status.get("state") == "error":
            bits.append(
                f'<p class="dossier-empty">⚠️ Last dossier research attempt '
                f'failed on {_html.escape(status.get("when",""))}. A fresh '
                f'brief has been queued — check back tomorrow.</p>'
            )
        elif status.get("state") == "queued":
            bits.append(
                f'<p class="dossier-empty">Dossier research queued '
                f'{_html.escape(status.get("when",""))} — Cowork will run it '
                'on its next pass.</p>'
            )
        elif status.get("state") == "missing":
            bits.append(
                '<p class="dossier-empty">No dossier brief on file yet. '
                'Run <code>python main.py dossier --only "'
                f'{_html.escape(cand.get("name",""))}"</code> to queue one.</p>'
            )
        else:
            bits.append(
                '<p class="dossier-empty">Dossier in progress — full research '
                'expected by tomorrow morning.</p>'
            )
        body_html = "".join(bits)
        sources_html = ""
    else:
        body_html, ordered_urls = _format_dossier_markdown(dossier_text)
        # Pull the FULL source list from the DB — this accumulates citations
        # across every dossier run for this candidate, not just the URLs in
        # today's .md file. Footnote numbers in the body still match
        # `ordered_urls` (one .md file's worth). The Sources panel shows the
        # broader record so listeners can see all known evidence.
        from scanner.database import get_candidate_sources
        try:
            db_sources = get_candidate_sources(_Cfg.DB_PATH, cand.get("name", ""))
        except Exception as e:
            log.debug("Spotlight: get_candidate_sources failed — %s", e)
            db_sources = []

        # Index DB rows by URL so we can show richer metadata (title + type)
        # for the in-text footnotes too, not just plain URLs.
        url_to_row = {r["url"]: r for r in db_sources}

        if db_sources:
            # Group by source_type for readable presentation
            from collections import OrderedDict
            buckets: "OrderedDict[str, list]" = OrderedDict()
            type_labels = {
                "official":      "Official records",
                "biography":     "Biographical",
                "voting_record": "Voting record",
                "press":         "Press coverage",
                "campaign":      "Campaign material",
                "other":         "Other",
            }
            # SQL already orders sources by our preferred type order
            for r in db_sources:
                buckets.setdefault(r.get("source_type") or "other", []).append(r)

            sections_html_parts = []
            for stype, rows in buckets.items():
                label = type_labels.get(stype, stype.title())
                items = "".join(
                    f'<li><a href="{_html.escape(r["url"])}" target="_blank">'
                    f'{_html.escape(r.get("title") or r["url"])}</a>'
                    + (f' <span class="dossier-src-summary">— '
                       f'{_html.escape((r.get("summary") or "")[:140])}'
                       f'{"…" if (r.get("summary") or "")[140:] else ""}</span>'
                       if r.get("summary") else '')
                    + '</li>'
                    for r in rows
                )
                sections_html_parts.append(
                    f'<div class="dossier-src-group">'
                    f'<div class="dossier-src-label">{_html.escape(label)} '
                    f'<span class="dossier-src-count">({len(rows)})</span></div>'
                    f'<ul>{items}</ul></div>'
                )
            sources_html = (
                '<div class="dossier-sources">'
                f'<strong>Sources ({len(db_sources)} on file)</strong>'
                + "".join(sections_html_parts)
                + '</div>'
            )
        elif ordered_urls:
            # DB has nothing — fall back to URLs scraped from THIS file.
            items = "".join(
                f'<li><a href="{_html.escape(u)}" target="_blank">'
                f'{_html.escape(u)}</a></li>' for u in ordered_urls
            )
            sources_html = (
                '<div class="dossier-sources">'
                '<strong>Sources</strong>'
                f'<ol>{items}</ol></div>'
            )
        else:
            sources_html = ""

    name = _html.escape(cand.get("name", "Unknown"))
    return f"""
<div class="dossier-panel">
  <h2>📄 Candidate Dossier — {name}</h2>
  <div class="dossier-body">{body_html}</div>
  {sources_html}
</div>
"""


def _render_html(report_date: date, by_level: Dict[str, List[Dict]],
                 politicians: List[Dict]) -> str:
    total = sum(len(v) for v in by_level.values())
    date_str = report_date.strftime("%A, %B %d, %Y")
    date_iso = report_date.isoformat()
    locale = _LOCALE
    chat_sidebar = _CHAT_SIDEBAR.replace("DATE_ISO", date_iso)
    podcast_player = _PODCAST_PLAYER_JS.replace("DATE_ISO", date_iso)
    _spot = _load_candidate_spotlight(report_date)
    spotlight_header = _render_spotlight_header(_spot)
    dossier_body     = _render_dossier_body(_spot)

    sections_html = ""
    for level, label in LEVEL_LABELS.items():
        items = by_level.get(level, [])
        if not items:
            continue
        cards = "".join(_event_card_html(ev) for ev in items)
        sections_html += f"""
        <section class="level-section">
            <h2>{label} <span class="count">{len(items)}</span></h2>
            <div class="cards">{cards}</div>
        </section>"""

    pol_html = _politician_tracker_html(politicians)
    refs_html = _references_section_html(by_level)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="report-date" content="{date_iso}">
<title>Local Politics Digest — {date_str}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          background: #f5f5f0; color: #222; line-height: 1.6; }}
  .header {{ background: #1a3a5c; color: white; padding: 24px 32px; position:relative; }}
  .header h1 {{ font-size: 1.6rem; }}
  .header .subtitle {{ opacity: .8; font-size: .9rem; margin-top: 4px; }}
  .header .stats {{ margin-top: 12px; font-size: .85rem; opacity: .7; }}
  .home-btn {{ position:absolute; top:20px; right:20px; background:rgba(255,255,255,.15);
               color:white; text-decoration:none; padding:8px 14px; border-radius:6px;
               font-size:.85rem; font-weight:500; }}
  .home-btn:hover {{ background:rgba(255,255,255,.25); }}
  .container {{ max-width: 960px; margin: 0 auto; padding: 24px 16px; }}
  .level-section {{ margin-bottom: 36px; }}
  .level-section h2 {{ font-size: 1.2rem; color: #1a3a5c; border-bottom: 2px solid #1a3a5c;
                       padding-bottom: 6px; margin-bottom: 14px; }}
  .count {{ background: #1a3a5c; color: white; border-radius: 12px;
            padding: 2px 8px; font-size: .75rem; vertical-align: middle; }}
  .cards {{ display: grid; gap: 12px; }}
  .card {{ background: white; border-radius: 8px; padding: 16px;
           border-left: 4px solid #1a3a5c; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .card.high-relevance {{ border-left-color: #c0392b; }}
  .card.medium-relevance {{ border-left-color: #e67e22; }}
  .card-title {{ font-size: 1rem; font-weight: 600; color: #1a3a5c; }}
  .card-title a {{ color: inherit; text-decoration: none; }}
  .card-title a:hover {{ text-decoration: underline; }}
  .card-summary {{ margin-top: 8px; font-size: .9rem; color: #444; }}
  .card-meta {{ margin-top: 10px; font-size: .78rem; color: #888;
                display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
  .tag {{ padding: 2px 7px; border-radius: 10px; background: #eee;
          font-size: .72rem; font-weight: 500; color: #555; }}
  .pol-tag {{ background: #dce8f5; color: #1a3a5c; }}
  .relevance-bar {{ height: 4px; border-radius: 2px; background: #eee;
                    margin-top: 8px; overflow: hidden; }}
  .relevance-fill {{ height: 100%; background: #1a3a5c; border-radius: 2px; }}
  /* Spotlight header (top of page) — name + office + badges only */
  .spot-panel {{ background: linear-gradient(135deg,#fdf6e3 0%,#fef9ed 100%);
                  border: 1px solid #e6d9b4; border-radius: 10px;
                  padding: 18px 22px; margin-bottom: 24px;
                  box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  .spot-panel h2 {{ font-size: .85rem; color: #7a6633; letter-spacing: .06em;
                     text-transform: uppercase; font-weight: 600; margin-bottom: 6px; }}
  .spot-name {{ font-size: 1.5rem; color: #1a3a5c; font-weight: 700; line-height: 1.2; }}
  .spot-sub  {{ font-size: .9rem; color: #555; margin-top: 4px; }}
  .spot-badges {{ margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }}
  .spot-badge {{ background: #1a3a5c; color: #fff; font-size: .7rem; font-weight: 600;
                  padding: 3px 9px; border-radius: 11px; letter-spacing: .02em; }}
  .spot-badge-inc  {{ background: #3a7a3a; }}
  .spot-badge-warn {{ background: #a05a1a; }}
  /* Dossier body (below politician tracker) — full dossier with citations */
  .dossier-panel {{ background: white; border-radius: 8px; padding: 20px;
                     margin-bottom: 32px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .dossier-panel h2 {{ font-size: 1.2rem; color: #1a3a5c; border-bottom: 2px solid #1a3a5c;
                        padding-bottom: 6px; margin-bottom: 14px; }}
  .dossier-body {{ font-size: .92rem; color: #333; line-height: 1.65; }}
  .dossier-body p {{ margin-bottom: 10px; }}
  .dossier-body p:last-child {{ margin-bottom: 0; }}
  .dossier-body hr {{ border: none; border-top: 1px solid #ddd; margin: 16px 0; }}
  .dossier-body sup {{ font-size: .7rem; }}
  .dossier-body sup a {{ color: #1a3a5c; text-decoration: none; padding: 0 1px;
                          font-weight: 600; }}
  .dossier-body sup a:hover {{ text-decoration: underline; }}
  .dossier-empty {{ color: #7a6633; font-style: italic; }}
  .dossier-sources {{ margin-top: 20px; padding-top: 14px;
                       border-top: 1px solid #eee; font-size: .82rem; }}
  .dossier-sources > strong {{ display: block; color: #1a3a5c;
                                margin-bottom: 10px;
                                text-transform: uppercase; letter-spacing: .05em;
                                font-size: .78rem; }}
  .dossier-sources ol,
  .dossier-sources ul {{ padding-left: 22px; color: #555;
                          margin-bottom: 12px; }}
  .dossier-sources li {{ padding: 3px 0; word-break: break-word; }}
  .dossier-sources a {{ color: #1a3a5c; text-decoration: none;
                         font-weight: 500; }}
  .dossier-sources a:hover {{ text-decoration: underline; }}
  .dossier-src-group {{ margin-bottom: 12px; }}
  .dossier-src-label {{ font-weight: 600; color: #1a3a5c; font-size: .82rem;
                         margin-bottom: 4px; }}
  .dossier-src-count {{ color: #999; font-weight: 400; font-size: .75rem; }}
  .dossier-src-summary {{ color: #888; font-size: .78rem;
                            font-style: italic; }}
  /* Politician tracker */
  .pol-section {{ background: white; border-radius: 8px; padding: 20px;
                  margin-bottom: 32px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .pol-section h2 {{ font-size: 1.2rem; color: #1a3a5c; margin-bottom: 14px; }}
  /* References section */
  .ref-section {{ background: white; border-radius: 8px; padding: 20px;
                  margin-bottom: 32px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .ref-section h2 {{ font-size: 1.2rem; color: #1a3a5c; border-bottom: 2px solid #1a3a5c;
                     padding-bottom: 6px; margin-bottom: 14px; }}
  .ref-list {{ padding-left: 0; list-style: none; }}
  .ref-list li {{ padding: 6px 0; border-bottom: 1px solid #f0f0f0;
                  font-size: .88rem; display: flex; gap: 8px; align-items: baseline; }}
  .ref-list li:last-child {{ border-bottom: none; }}
  .ref-num {{ color: #1a3a5c; font-weight: 700; min-width: 24px; flex-shrink: 0; }}
  .ref-list a {{ color: #1a3a5c; text-decoration: none; }}
  .ref-list a:hover {{ text-decoration: underline; }}
  .pol-row {{ display: flex; justify-content: space-between; align-items: flex-start;
              padding: 10px 0; border-bottom: 1px solid #f0f0f0; }}
  .pol-row:last-child {{ border-bottom: none; }}
  .pol-name {{ font-weight: 600; font-size: .95rem; }}
  .pol-office {{ font-size: .8rem; color: #888; }}
  .pol-events {{ font-size: .82rem; color: #555; max-width: 500px; text-align: right; }}
  .footer {{ text-align: center; padding: 24px; font-size: .8rem; color: #aaa; }}
  @media (max-width: 600px) {{
    .pol-row {{ flex-direction: column; gap: 6px; }}
    .pol-events {{ text-align: left; }}
  }}
</style>
</head>
<body>
<div class="header">
  <a class="home-btn" href="/">← Home</a>
  <a class="home-btn" href="/playlist" style="right:90px;">🎵 Playlist</a>
  <h1>Local Politics Digest</h1>
  <div class="subtitle">{locale} &nbsp;·&nbsp; {date_str}</div>
  <div class="stats">{total} items tracked today</div>
</div>
<div class="container">

<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;
            padding:12px 16px;margin-bottom:24px;font-size:.85rem;">
  <strong>New to voting?</strong> &nbsp;Items with a red left border are most relevant to your
  daily life. Each card has a plain-English explanation. Use the Politician Tracker below
  to see what your representatives have been doing.
</div>

{spotlight_header}
{podcast_player}

{sections_html}
{pol_html}
{dossier_body}
{refs_html}
</div>
<div class="footer">
  Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} &nbsp;·&nbsp;
  Local Politics Scanner
</div>
{chat_sidebar}
</body>
</html>"""


def _event_card_html(ev: Dict) -> str:
    relevance = ev.get("relevance_score", 0)
    cls = "high-relevance" if relevance >= 0.7 else ("medium-relevance" if relevance >= 0.45 else "")
    fill_pct = int(relevance * 100)
    title = ev.get("title", "No title")
    url = ev.get("source_url", "#")
    summary = ev.get("summary") or ev.get("description") or ""
    source = ev.get("source_name", "")
    date_str = ev.get("date", "")
    event_type = TYPE_LABELS.get(ev.get("type", ""), "News")
    categories = ev.get("categories", [])
    if isinstance(categories, str):
        try:
            categories = json.loads(categories)
        except Exception:
            categories = []

    pol_list = ev.get("politicians", "")
    pol_names = [p.strip() for p in pol_list.split(",") if pol_list and p.strip()] if pol_list else []

    tags_html = ""
    for cat in categories[:4]:
        color = CATEGORY_TAGS.get(cat, "#eee")
        tags_html += f'<span class="tag" style="background:{color}20;color:{color};">{cat}</span>'
    for name in pol_names[:3]:
        tags_html += f'<span class="tag pol-tag">{name}</span>'

    return f"""
    <div class="card {cls}">
      <div class="card-title"><a href="{url}" target="_blank">{title}</a></div>
      {'<div class="card-summary">' + summary + '</div>' if summary else ''}
      <div class="card-meta">
        <span>{event_type}</span>
        {'<span>' + date_str + '</span>' if date_str else ''}
        <span style="color:#aaa">{source}</span>
        {tags_html}
      </div>
      <div class="relevance-bar">
        <div class="relevance-fill" style="width:{fill_pct}%"
             title="Relevance: {relevance:.0%}"></div>
      </div>
    </div>"""


def _references_section_html(by_level: Dict[str, List[Dict]]) -> str:
    """Render references split into 'new today' and 'seen earlier this week'.

    Uses a small SQLite-backed table `digest_references` so URLs that have
    been showing up for days don't keep dominating the section. New URLs
    are highlighted; everything else is collapsed."""
    seen_urls: set = set()
    refs: List[tuple] = []
    for level in LEVEL_LABELS:
        for ev in by_level.get(level, []):
            url = ev.get("source_url", "").strip()
            title = ev.get("title", "").strip()
            if url and url != "#" and url not in seen_urls:
                seen_urls.add(url)
                refs.append((title or url, url))

    if not refs:
        return ""

    # Bucket new vs. recurring using the digest_references table.
    new_today: List[tuple] = []
    recurring: List[tuple] = []
    try:
        from scanner.database import record_digest_references
        first_seen_map = record_digest_references(
            _Cfg.DB_PATH, [u for _, u in refs]
        )
    except Exception as e:
        log.debug("References: record_digest_references failed — %s", e)
        first_seen_map = {}

    today_iso = date.today().isoformat()
    for title, url in refs:
        if first_seen_map.get(url) == today_iso or url not in first_seen_map:
            new_today.append((title, url))
        else:
            recurring.append((title, url))

    def _items(group: List[tuple], offset: int = 0) -> str:
        return "".join(
            f'<li id="ref-{offset + i}">'
            f'<span class="ref-num">{offset + i}.</span> '
            f'<a href="{_html.escape(url)}" target="_blank">'
            f'{_html.escape(title)}</a></li>'
            for i, (title, url) in enumerate(group, 1)
        )

    new_block = (
        f"""<h3 style="font-size:.95rem;color:#1a3a5c;margin-top:8px;">
              🆕 New today ({len(new_today)})</h3>
            <ol class="ref-list">{_items(new_today)}</ol>"""
        if new_today else
        '<p style="color:#888;font-style:italic;">'
        'No new source URLs surfaced today.</p>'
    )

    recur_block = ""
    if recurring:
        recur_block = (
            f"""<details style="margin-top:14px;">
                  <summary style="cursor:pointer;color:#666;font-size:.9rem;">
                    Seen earlier this week ({len(recurring)})
                  </summary>
                  <ol class="ref-list" start="{len(new_today) + 1}">
                    {_items(recurring, offset=len(new_today))}
                  </ol>
                </details>"""
        )

    return f"""
    <div class="ref-section">
      <h2>📎 References</h2>
      <p style="font-size:.85rem;color:#666;margin-bottom:12px;">
        Source articles linked in today's digest — new items highlighted,
        long-running ones collapsed below.
      </p>
      {new_block}
      {recur_block}
    </div>"""


def _politician_tracker_html(politicians: List[Dict]) -> str:
    if not politicians:
        return ""

    from datetime import date as _date, timedelta as _td
    # Only show events first seen in the last 24 hours. Anything older
    # belongs in the dossier, not the daily "what changed" tracker.
    fresh_cutoff = (_date.today() - _td(days=1)).isoformat()

    rows = ""
    for pol in politicians[:20]:
        name = pol.get("name", "")
        office = pol.get("office", "")
        party = pol.get("party", "")
        party = party or ""
        party_color = "#003da5" if "Democrat" in party else ("#c8102e" if "Republican" in party else "#666")
        all_events = pol.get("events", []) or []
        fresh = [
            e for e in all_events
            if (e.get("first_seen") or e.get("date") or "") >= fresh_cutoff
        ][:3]
        if fresh:
            event_text = " &nbsp;|&nbsp; ".join(
                f'<a href="{e.get("source_url","#")}" target="_blank">'
                f'{e.get("role","mentioned").replace("_"," ").title()}: '
                f'{e.get("title","")[:60]}</a>'
                for e in fresh
            )
        elif all_events:
            last = all_events[0]
            event_text = (
                f'<span style="color:#888;font-style:italic;">'
                f'Quiet today — last activity {_html.escape(last.get("date",""))}: '
                f'<a href="{last.get("source_url","#")}" target="_blank">'
                f'{_html.escape(last.get("title","")[:70])}</a></span>'
            )
        else:
            event_text = "No tracked activity yet"

        rows += f"""
        <div class="pol-row">
          <div>
            <div class="pol-name">{name}
              <span style="color:{party_color};font-size:.75rem;margin-left:6px;">{party}</span>
            </div>
            <div class="pol-office">{office}</div>
          </div>
          <div class="pol-events">{event_text}</div>
        </div>"""

    return f"""
    <div class="pol-section">
      <h2>👥 Politician Tracker</h2>
      <p style="font-size:.85rem;color:#666;margin-bottom:12px;">
        Track what your elected officials have been doing. This helps you decide who to vote for.
      </p>
      {rows}
    </div>"""


# ── Markdown renderer ──────────────────────────────────────────────────────────

def _render_markdown(report_date: date, by_level: Dict[str, List[Dict]],
                     politicians: List[Dict]) -> str:
    date_str = report_date.strftime("%A, %B %d, %Y")
    total = sum(len(v) for v in by_level.values())
    lines = [
        f"# Local Politics Digest — {date_str}",
        f"> {_LOCALE} &nbsp;·&nbsp; {total} items\n",
    ]

    for level, label in LEVEL_LABELS.items():
        items = by_level.get(level, [])
        if not items:
            continue
        lines.append(f"\n## {label}\n")
        for ev in items:
            title = ev.get("title", "")
            url = ev.get("source_url", "#")
            summary = ev.get("summary") or ev.get("description") or ""
            source = ev.get("source_name", "")
            date_s = ev.get("date", "")
            relevance = ev.get("relevance_score", 0)
            stars = "⭐" * round(relevance * 5)

            lines.append(f"### [{title}]({url})")
            if summary:
                lines.append(f"> {summary}")
            meta = f"*{source}"
            if date_s:
                meta += f" · {date_s}"
            meta += f" · Relevance: {stars} ({relevance:.0%})*"
            lines.append(meta)
            lines.append("")

    if politicians:
        lines.append("\n## 👥 Politician Tracker\n")
        for pol in politicians[:15]:
            name = pol.get("name", "")
            office = pol.get("office", "")
            party = pol.get("party", "")
            lines.append(f"**{name}** ({party}) — {office}")
            for ev in pol.get("events", [])[:2]:
                role = ev.get("role", "mentioned").replace("_", " ").title()
                ev_title = ev.get("title", "")[:80]
                ev_url = ev.get("source_url", "#")
                lines.append(f"  - {role}: [{ev_title}]({ev_url})")
            lines.append("")

    lines.append(f"\n---\n*Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · Local Politics Scanner*")
    return "\n".join(lines)


def save_html_report(html: str, reports_dir: Path, report_date: date) -> Path:
    """Write HTML report to disk and return the path."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    filename = reports_dir / f"digest_{report_date.strftime('%Y-%m-%d')}.html"
    filename.write_text(html, encoding="utf-8")
    return filename
