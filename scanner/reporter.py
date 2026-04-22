"""
Report generator: produces a daily HTML + Markdown voter digest.
Designed for someone new to elections — plain English, no jargon.
"""
import json
import logging
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
  .cp-tabs { display:flex; border-bottom:1px solid #eee; }
  .cp-tab { flex:1; padding:9px; border:none; background:none; cursor:pointer;
    font-size:.85rem; color:#666; border-bottom:2px solid transparent; }
  .cp-tab.active { color:#1a3a5c; border-bottom-color:#1a3a5c; font-weight:600; }
  .cp-body { flex:1; overflow-y:auto; padding:12px; }
  #notes-area { width:100%; min-height:220px; border:1px solid #ddd;
    border-radius:6px; padding:10px; font-size:.88rem; font-family:inherit;
    resize:vertical; line-height:1.5; }
  #notes-status { font-size:.75rem; color:#888; margin-top:6px; display:block; }
  #ask-log { min-height:160px; max-height:240px; overflow-y:auto;
    margin-bottom:8px; font-size:.85rem; }
  .ask-bubble { padding:8px 10px; border-radius:8px; margin-bottom:6px;
    line-height:1.45; white-space:pre-wrap; }
  .ask-bubble.user { background:#1a3a5c; color:white; text-align:right; }
  .ask-bubble.bot { background:#f0f4f8; color:#222; }
  .ask-bubble.err { background:#fdecea; color:#c0392b; }
  .cp-input-row { display:flex; gap:6px; }
  #ask-input { flex:1; border:1px solid #ddd; border-radius:6px;
    padding:8px 10px; font-size:.85rem; font-family:inherit; }
  #ask-btn { background:#1a3a5c; color:white; border:none; border-radius:6px;
    padding:8px 12px; cursor:pointer; font-size:.85rem; white-space:nowrap; }
  #ask-btn:disabled { background:#aaa; }
  @media (max-width:400px) {
    #chat-panel { right:8px; left:8px; width:auto; }
  }
</style>

<button id="chat-fab" title="Notes & Ask" onclick="toggleChat()">💬</button>
<div id="chat-panel">
  <div class="cp-header">
    <span>💬 Notes &amp; Ask</span>
    <button class="cp-close" onclick="toggleChat()">✕</button>
  </div>
  <div class="cp-tabs">
    <button class="cp-tab active" id="tab-notes-btn" onclick="switchTab('notes')">📝 Notes</button>
    <button class="cp-tab" id="tab-ask-btn" onclick="switchTab('ask')">🤖 Ask</button>
  </div>
  <div class="cp-body" id="pane-notes">
    <textarea id="notes-area" placeholder="Take notes about today's digest..."></textarea>
    <span id="notes-status"></span>
  </div>
  <div class="cp-body" id="pane-ask" style="display:none">
    <div id="ask-log"></div>
    <div class="cp-input-row">
      <input id="ask-input" placeholder="Ask about today's news..." />
      <button id="ask-btn" onclick="sendQuestion()">Ask</button>
    </div>
  </div>
</div>

<script>
(function() {
  var REPORT_DATE = 'DATE_ISO';
  var STORAGE_KEY = 'notes-' + REPORT_DATE;
  // Use current origin when served over HTTP/HTTPS (works on Tailscale IP too);
  // fall back to localhost only when the HTML was opened as a local file://.
  var CHAT_BASE = (window.location.protocol === 'file:') ? 'http://localhost:8080' : window.location.origin;

  // ── Panel toggle ──────────────────────────────────────────────────────────
  window.toggleChat = function() {
    var p = document.getElementById('chat-panel');
    p.classList.toggle('open');
    if (p.classList.contains('open')) { loadNotes(); }
  };

  // ── Tab switching ─────────────────────────────────────────────────────────
  window.switchTab = function(tab) {
    document.getElementById('pane-notes').style.display = tab === 'notes' ? '' : 'none';
    document.getElementById('pane-ask').style.display  = tab === 'ask'   ? '' : 'none';
    document.getElementById('tab-notes-btn').className = 'cp-tab' + (tab === 'notes' ? ' active' : '');
    document.getElementById('tab-ask-btn').className   = 'cp-tab' + (tab === 'ask'   ? ' active' : '');
    if (tab === 'ask') { document.getElementById('ask-input').focus(); }
  };

  // ── Notes (localStorage) ──────────────────────────────────────────────────
  function loadNotes() {
    var el = document.getElementById('notes-area');
    el.value = localStorage.getItem(STORAGE_KEY) || '';
  }

  function saveNotes() {
    localStorage.setItem(STORAGE_KEY, document.getElementById('notes-area').value);
    var s = document.getElementById('notes-status');
    s.textContent = 'Saved ' + new Date().toLocaleTimeString();
    clearTimeout(window._notesFadeTimer);
    window._notesFadeTimer = setTimeout(function() { s.textContent = ''; }, 3000);
  }

  document.getElementById('notes-area').addEventListener('input', function() {
    clearTimeout(window._notesTimer);
    window._notesTimer = setTimeout(saveNotes, 800);
  });

  // ── Ask (calls POST /chat) ────────────────────────────────────────────────
  function appendBubble(text, cls) {
    var log = document.getElementById('ask-log');
    var d = document.createElement('div');
    d.className = 'ask-bubble ' + cls;
    d.textContent = text;
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
    return d;
  }

  window.sendQuestion = function() {
    var inp = document.getElementById('ask-input');
    var q = inp.value.trim();
    if (!q) return;
    inp.value = '';
    appendBubble(q, 'user');
    var thinking = appendBubble('Thinking\u2026', 'bot');
    var btn = document.getElementById('ask-btn');
    btn.disabled = true;

    fetch(CHAT_BASE + '/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, date: REPORT_DATE })
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      thinking.remove();
      if (data.error) { appendBubble('Error: ' + data.error, 'err'); }
      else            { appendBubble(data.answer, 'bot'); }
    })
    .catch(function(e) {
      thinking.remove();
      appendBubble('Could not reach server. Is `python main.py serve` running? (' + e.message + ')', 'err');
    })
    .finally(function() { btn.disabled = false; inp.focus(); });
  };

  document.getElementById('ask-input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); sendQuestion(); }
  });
})();
</script>
"""

# ── Podcast player (injected into each digest page) ───────────────────────────
# Plain string so JS braces need no escaping. DATE_ISO replaced at render time.
_PODCAST_PLAYER_JS = """
<div id="pod-section" style="margin-bottom:24px;display:none">
  <h2 style="font-size:1.1rem;color:#1a3a5c;border-bottom:2px solid #1a3a5c;
             padding-bottom:6px;margin-bottom:12px;">🎧 Today's Episodes</h2>
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
  var found = 0;
  var checks = [];

  for (var i = 1; i <= 4; i++) {
    (function(epNum) {
      var url = BASE + '/podcast/podcast_' + DATE + '_ep' + epNum + '.mp3';
      checks.push(
        fetch(url, {method: 'HEAD'})
          .then(function(r) {
            if (!r.ok) return;
            found++;
            var card = document.createElement('div');
            card.style.cssText = 'background:white;border-radius:8px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.07)';
            card.innerHTML =
              '<div style="font-weight:600;color:#1a3a5c;font-size:.9rem;margin-bottom:8px" id="ep-lbl-' + epNum + '">Episode ' + epNum + '</div>' +
              '<audio controls preload="none" style="width:100%"><source src="' + url + '" type="audio/mpeg"></audio>';
            podList.appendChild(card);
          })
          .catch(function() {})
      );
    })(i);
  }

  Promise.all(checks).then(function() {
    podSection.style.display = '';
    if (found === 0) { podNone.style.display = ''; return; }
    // Load episode titles from index JSON if available
    fetch(BASE + '/podcast/' + DATE + '-index.json')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        (data.episodes || []).forEach(function(ep) {
          var el = document.getElementById('ep-lbl-' + ep.num);
          if (el && ep.title) el.textContent = 'Episode ' + ep.num + ': ' + ep.title;
        });
      })
      .catch(function() {});
  });
})();
</script>
"""

# ── Section labels ─────────────────────────────────────────────────────────────
LEVEL_LABELS = {
    "federal": "🇺🇸 Federal (filtered topics)",
    "state": f"🏛️ {_STATE} State Legislature",
    "county": f"🏘️ {_COUNTY}",
    "school": "🎓 School Board",
    "local": "🚑 Local Services (Police / Fire / Health)",
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

def _render_html(report_date: date, by_level: Dict[str, List[Dict]],
                 politicians: List[Dict]) -> str:
    total = sum(len(v) for v in by_level.values())
    date_str = report_date.strftime("%A, %B %d, %Y")
    date_iso = report_date.isoformat()
    locale = _LOCALE
    chat_sidebar = _CHAT_SIDEBAR.replace("DATE_ISO", date_iso)
    podcast_player = _PODCAST_PLAYER_JS.replace("DATE_ISO", date_iso)

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
  .header {{ background: #1a3a5c; color: white; padding: 24px 32px; }}
  .header h1 {{ font-size: 1.6rem; }}
  .header .subtitle {{ opacity: .8; font-size: .9rem; margin-top: 4px; }}
  .header .stats {{ margin-top: 12px; font-size: .85rem; opacity: .7; }}
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
  /* Politician tracker */
  .pol-section {{ background: white; border-radius: 8px; padding: 20px;
                  margin-bottom: 32px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .pol-section h2 {{ font-size: 1.2rem; color: #1a3a5c; margin-bottom: 14px; }}
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

{podcast_player}

{sections_html}
{pol_html}
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


def _politician_tracker_html(politicians: List[Dict]) -> str:
    if not politicians:
        return ""

    rows = ""
    for pol in politicians[:20]:
        name = pol.get("name", "")
        office = pol.get("office", "")
        party = pol.get("party", "")
        party = party or ""
        party_color = "#003da5" if "Democrat" in party else ("#c8102e" if "Republican" in party else "#666")
        recent_events = pol.get("events", [])[:3]
        event_text = " &nbsp;|&nbsp; ".join(
            f'<a href="{e.get("source_url","#")}" target="_blank">'
            f'{e.get("role","mentioned").replace("_"," ").title()}: {e.get("title","")[:60]}'
            f'</a>'
            for e in recent_events
        ) or "No recent activity tracked"

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
