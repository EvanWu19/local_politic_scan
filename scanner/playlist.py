"""
Playlist page — a multi-day queue player.

Two public entry points consumed by `scanner/server.py`:

  • `build_playlist_index(podcasts_dir)`  → list of {date, label, episodes[]}
                                              for every day with ≥1 ready MP3
  • `render_playlist_page()`              → full HTML document (no template
                                              substitution needed)

The data model the page consumes is intentionally flat per-day-keyed so the
frontend JS can:
  - render checkboxes ordered by date
  - flatten into a single queue once selection + sort are applied
  - rebuild that queue on any change without round-tripping the server

Within each day, episode order is fixed: ep1 → ep2 → ep3 → ep4.
Date order is client-side controlled (ascending = chronological default,
descending = newest first). Backend always returns ascending so the wire
format is stable; frontend reverses for the descending case.
"""

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import List, Dict


# Same titles used by /api/podcast-files/<DATE>. Duplicated here (rather than
# imported from server.py) to avoid a server.py ↔ playlist.py import cycle.
_SERIES_EP_TITLES = {1: "Origins", 2: "Career", 3: "Political Record", 4: "This Race"}
_LEGACY_EP_TITLES = {1: "Episode 1", 2: "Episode 2", 3: "Episode 3", 4: "Episode 4"}


def _slug_to_name_map() -> Dict[str, str]:
    """Look up candidate display names from the series registry. Empty if the
    registry is missing or the series module errors."""
    try:
        from scanner.series import load_registry, _slug
        reg = load_registry()
        return {_slug(c.get("name", "")): c.get("name", "")
                for c in reg.get("candidates", []) if c.get("name")}
    except Exception:
        return {}


def _humanize_date(date_str: str) -> str:
    """'2026-05-14' → 'Wednesday, May 14'.

    Builds the string manually so it works on both Windows (no `%-d`) and
    POSIX (no `%#d`)."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return date_str
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"


def build_playlist_index(podcasts_dir: Path,
                          min_size_bytes: int = 1024) -> List[Dict]:
    """Scan every `podcast_<date>_*.mp3` file across all dates.

    Returns one entry per date that has ≥1 ready episode, sorted ascending
    (oldest first). Each entry has `episodes[]` already sorted by:
      • candidate slug (so series eps for the same candidate stay grouped)
      • ep_num within candidate
      • legacy ep1..ep4 after series
      • deepdive last

    Episodes smaller than `min_size_bytes` (default 1 KB, matching the
    tts-publish "skip if exists" threshold) are excluded so partial / empty
    MP3s never land in the player queue.
    """
    if not podcasts_dir.exists():
        return []

    slug_to_name = _slug_to_name_map()

    # date_str → list of episode dicts (unsorted)
    by_date: Dict[str, List[Dict]] = {}

    file_re = re.compile(r"^podcast_(\d{4}-\d{2}-\d{2})_(.+)$")
    series_re   = re.compile(r"^series_(.+)_ep(\d+)$")
    ep_re       = re.compile(r"^ep(\d+)$")
    deepdive_re = re.compile(r"^deepdive_(.+)$")

    for mp3 in podcasts_dir.glob("podcast_*.mp3"):
        try:
            size = mp3.stat().st_size
        except OSError:
            continue
        if size < min_size_bytes:
            continue
        m = file_re.match(mp3.stem)
        if not m:
            continue
        date_str, rest = m.group(1), m.group(2)
        url = f"/podcast/{mp3.stem}.mp3"

        entry = {"url": url, "size": size}

        sm = series_re.match(rest)
        if sm:
            slug, n = sm.group(1), int(sm.group(2))
            cand = slug_to_name.get(slug) or slug.replace("-", " ").title()
            entry.update({
                "kind": "series",
                "candidate": cand,
                "candidate_slug": slug,
                "ep_num": n,
                "title": _SERIES_EP_TITLES.get(n, f"Part {n}"),
                "display_title": f"{cand} — {_SERIES_EP_TITLES.get(n, f'Part {n}')}",
            })
        elif (em := ep_re.match(rest)):
            n = int(em.group(1))
            entry.update({
                "kind": "ep", "ep_num": n,
                "title": _LEGACY_EP_TITLES.get(n, f"Episode {n}"),
                "display_title": f"Episode {n}",
            })
        elif (dm := deepdive_re.match(rest)):
            slug = dm.group(1)
            cand = slug_to_name.get(slug) or slug.replace("-", " ").title()
            entry.update({
                "kind": "deepdive",
                "candidate": cand,
                "candidate_slug": slug,
                "title": "Deep Dive",
                "display_title": f"{cand} — Deep Dive",
            })
        else:
            entry.update({
                "kind": "other", "title": rest, "display_title": rest,
            })
        by_date.setdefault(date_str, []).append(entry)

    # PERMANENT RULE (2026-05-20): playlist surfaces series episodes only.
    # `_epN` and `_deepdive_` are the retired formats — filter them out so
    # the queue never plays stale legacy audio. Drop any date that has no
    # series episodes left after filtering.
    out: List[Dict] = []
    for date_str in sorted(by_date.keys()):
        eps = [e for e in by_date[date_str] if e.get("kind") == "series"]
        if not eps:
            continue
        eps.sort(key=lambda e: (
            e.get("candidate_slug", ""),
            e.get("ep_num", 0),
            e.get("title", ""),
        ))
        out.append({
            "date":  date_str,
            "label": _humanize_date(date_str),
            "episodes": eps,
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Page renderer
# ──────────────────────────────────────────────────────────────────────────────

# Vanilla JS — no frameworks. The page does one fetch at boot, builds the
# date checklist and the flat queue from the response, and uses a single
# <audio> element re-sourced per episode (cheaper than 50+ <audio> tags and
# avoids browser pre-load thrash).
#
# State model:
#   index          — full list-of-dates from /api/playlist-index
#   sortAsc        — bool: true if oldest→newest
#   selectedDates  — Set<string> of date keys
#   queue          — flat array of {date, label, ep_num, title, url, size}
#                    built from index ∩ selectedDates, in the chosen sort order
#                    (date order × fixed ep1→ep4 within day)
#   currentIdx     — int index into queue; -1 means "stopped"
#
# Resume:
#   On boot, after queue is built, look at localStorage for
#   politic_playlist_{date,ep_idx,pos}. If a queue item matches (date +
#   ep_num), show a resume banner; click resume → seek + play.
#   Save every 5s via timeupdate (throttled).

_PLAYLIST_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#1a3a5c">
<title>🎵 Playlist — Local Politics</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f5f0; color: #222; min-height: 100vh; line-height: 1.5; }
  .header { background: #1a3a5c; color: white; padding: 16px 20px; position: relative; }
  .header h1 { font-size: 1.3rem; }
  .header .sub { font-size: .8rem; opacity: .75; margin-top: 2px; }
  .home-btn { position: absolute; top: 16px; right: 16px;
              background: rgba(255,255,255,.15); color: white;
              text-decoration: none; padding: 6px 12px; border-radius: 6px;
              font-size: .8rem; font-weight: 500; }
  .home-btn:hover { background: rgba(255,255,255,.25); }
  .container { max-width: 760px; margin: 0 auto; padding: 16px; }

  .card { background: white; border-radius: 8px; padding: 16px;
          box-shadow: 0 1px 3px rgba(0,0,0,.07); margin-bottom: 14px; }

  .toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
             margin-bottom: 4px; }
  .toolbar button { background: #1a3a5c; color: white; border: none;
                    border-radius: 6px; padding: 8px 14px; font-size: .85rem;
                    font-weight: 600; cursor: pointer; }
  .toolbar button.ghost { background: white; color: #1a3a5c;
                          border: 1px solid #1a3a5c; }
  .toolbar button:hover { opacity: .88; }
  .toolbar button:disabled { opacity: .4; cursor: not-allowed; }

  .date-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
               gap: 8px; margin-top: 12px; }
  .date-chip { display: flex; align-items: center; gap: 8px;
               padding: 8px 10px; border-radius: 6px; background: #f4f8fc;
               cursor: pointer; user-select: none; font-size: .85rem; }
  .date-chip:hover { background: #e6effa; }
  .date-chip input { cursor: pointer; }
  .date-chip .ep-count { color: #888; font-size: .75rem; margin-left: auto; }

  .resume-banner { background: #fff3cd; border: 1px solid #ffc107;
                   border-radius: 8px; padding: 12px 16px; margin-bottom: 14px;
                   font-size: .9rem; display: flex; align-items: center;
                   justify-content: space-between; gap: 12px; }
  .resume-banner button { background: #1a3a5c; color: white; border: none;
                          border-radius: 6px; padding: 6px 12px; font-size: .85rem;
                          font-weight: 600; cursor: pointer; }
  .resume-banner .dismiss { background: transparent; color: #888;
                            font-size: 1.2rem; line-height: 1; padding: 0 6px; }

  .now-playing { background: linear-gradient(135deg, #3a6ea5 0%, #1a3a5c 100%);
                 color: white; border-radius: 10px; padding: 18px; margin-bottom: 14px; }
  .np-meta { font-size: .75rem; opacity: .8; text-transform: uppercase;
             letter-spacing: .04em; }
  .np-title { font-size: 1.1rem; font-weight: 600; margin-top: 4px; }
  .np-sub { font-size: .85rem; opacity: .85; margin-top: 2px; }

  .seekbar-row { display: flex; align-items: center; gap: 10px; margin-top: 14px; }
  .seekbar { flex: 1; -webkit-appearance: none; appearance: none; height: 6px;
             background: rgba(255,255,255,.25); border-radius: 3px; outline: none;
             cursor: pointer; }
  .seekbar::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none; width: 16px; height: 16px;
    border-radius: 50%; background: white; cursor: pointer;
    box-shadow: 0 1px 3px rgba(0,0,0,.3);
  }
  .seekbar::-moz-range-thumb {
    width: 16px; height: 16px; border-radius: 50%; background: white;
    cursor: pointer; border: none; box-shadow: 0 1px 3px rgba(0,0,0,.3);
  }
  .time-readout { font-size: .8rem; font-variant-numeric: tabular-nums;
                  opacity: .9; min-width: 92px; text-align: right; }

  .controls { display: flex; gap: 8px; margin-top: 14px; justify-content: center; }
  .controls button { background: rgba(255,255,255,.18); color: white; border: none;
                     border-radius: 8px; padding: 10px 20px; font-size: .95rem;
                     font-weight: 600; cursor: pointer; min-width: 90px; }
  .controls button:hover { background: rgba(255,255,255,.28); }
  .controls button:disabled { opacity: .35; cursor: not-allowed; }
  .controls button.play-btn { background: white; color: #1a3a5c; min-width: 110px; }

  .queue-list { max-height: 360px; overflow-y: auto; }
  .queue-item { display: flex; align-items: center; gap: 10px; padding: 10px 12px;
                border-radius: 6px; font-size: .85rem; cursor: pointer;
                border-bottom: 1px solid #f0f0f0; }
  .queue-item:last-child { border-bottom: none; }
  .queue-item:hover { background: #f4f8fc; }
  .queue-item .qi-marker { font-size: 1rem; width: 18px; text-align: center; }
  .queue-item .qi-date { color: #888; font-size: .78rem; min-width: 78px; }
  .queue-item .qi-title { flex: 1; color: #1a3a5c; }
  .queue-item .qi-size { color: #aaa; font-size: .72rem; }
  .queue-item.active { background: #e6effa; font-weight: 600; }
  .queue-item.played .qi-title { color: #888; }

  .empty-state { text-align: center; color: #888; padding: 28px 16px; font-size: .9rem; }
  .footer { text-align: center; color: #aaa; font-size: .75rem; padding: 20px; }

  @media (max-width: 480px) {
    .container { padding: 12px; }
    .controls button { min-width: 0; flex: 1; padding: 10px 8px; }
    .time-readout { min-width: 78px; font-size: .72rem; }
  }
</style></head><body>
<div class="header">
  <a class="home-btn" href="/">← Home</a>
  <h1>🎵 Playlist</h1>
  <div class="sub">Queue play across multiple days</div>
</div>
<div class="container">
  <div id="resume-banner" class="resume-banner" style="display:none">
    <div id="resume-text">Resume last session?</div>
    <div>
      <button id="resume-yes">Resume</button>
      <button class="dismiss" id="resume-no" title="Dismiss">×</button>
    </div>
  </div>

  <div class="card">
    <div class="toolbar">
      <button id="sort-btn">↑ Oldest first</button>
      <button id="play-btn" class="ghost">▶ Play selected</button>
      <button id="select-all" class="ghost">Select all</button>
      <button id="select-none" class="ghost">None</button>
    </div>
    <div id="date-grid" class="date-grid"></div>
  </div>

  <div id="player-card" class="now-playing" style="display:none">
    <div class="np-meta" id="np-meta">—</div>
    <div class="np-title" id="np-title">Nothing playing</div>
    <div class="np-sub" id="np-sub"></div>
    <div class="seekbar-row">
      <input type="range" id="seekbar" class="seekbar" min="0" max="100" step="0.1" value="0">
      <div class="time-readout" id="time-readout">0:00 / 0:00</div>
    </div>
    <div class="controls">
      <button id="prev-btn">⏮ Prev</button>
      <button id="play-pause-btn" class="play-btn">▶ Play</button>
      <button id="next-btn">⏭ Next</button>
    </div>
    <audio id="audio" preload="none" style="display:none"></audio>
  </div>

  <div class="card" id="queue-card" style="display:none">
    <div style="font-weight:600;color:#1a3a5c;margin-bottom:8px;">
      Queue (<span id="queue-count">0</span> episodes)
    </div>
    <div class="queue-list" id="queue-list"></div>
  </div>

  <div id="empty-state" class="empty-state" style="display:none">
    No episodes available yet. Run today's pipeline to populate the playlist.
  </div>
</div>
<div class="footer">Local Politics Scanner · Playlist</div>

<script>
(function() {
  var LS = {
    DATE:   'politic_playlist_date',
    EP_IDX: 'politic_playlist_ep_idx',
    POS:    'politic_playlist_pos',
  };

  // ── State ─────────────────────────────────────────────────────────────────
  var index = [];             // ascending-by-date list-of-days from server
  var sortAsc = true;
  var selectedDates = new Set();
  var queue = [];             // flat array of {date, label, ep_num, title, url, size, date_ep_idx}
  var currentIdx = -1;        // index into queue
  var lastSaveTs = 0;

  // ── Element refs ─────────────────────────────────────────────────────────
  var $sort = document.getElementById('sort-btn');
  var $play = document.getElementById('play-btn');
  var $selAll = document.getElementById('select-all');
  var $selNone = document.getElementById('select-none');
  var $dateGrid = document.getElementById('date-grid');
  var $playerCard = document.getElementById('player-card');
  var $npMeta = document.getElementById('np-meta');
  var $npTitle = document.getElementById('np-title');
  var $npSub = document.getElementById('np-sub');
  var $seek = document.getElementById('seekbar');
  var $time = document.getElementById('time-readout');
  var $prev = document.getElementById('prev-btn');
  var $pp = document.getElementById('play-pause-btn');
  var $next = document.getElementById('next-btn');
  var $audio = document.getElementById('audio');
  var $queueCard = document.getElementById('queue-card');
  var $queueList = document.getElementById('queue-list');
  var $queueCount = document.getElementById('queue-count');
  var $empty = document.getElementById('empty-state');
  var $resume = document.getElementById('resume-banner');
  var $resumeText = document.getElementById('resume-text');
  var $resumeYes = document.getElementById('resume-yes');
  var $resumeNo = document.getElementById('resume-no');

  // ── Utilities ────────────────────────────────────────────────────────────
  function fmtTime(s) {
    if (!isFinite(s) || s < 0) return '0:00';
    s = Math.floor(s);
    var m = Math.floor(s / 60), r = s % 60;
    return m + ':' + (r < 10 ? '0' : '') + r;
  }
  function fmtMB(b) {
    if (!b) return '';
    return (b / 1048576).toFixed(1) + ' MB';
  }
  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function shortDate(label) {
    // "Wednesday, May 14" → "May 14"
    var i = label.indexOf(',');
    return i >= 0 ? label.slice(i + 1).trim() : label;
  }

  // ── Build/rebuild the queue from current selection + sort ────────────────
  function rebuildQueue() {
    queue = [];
    var dates = index.filter(function(d) { return selectedDates.has(d.date); });
    if (!sortAsc) dates = dates.slice().reverse();
    dates.forEach(function(d) {
      d.episodes.forEach(function(ep, ep_idx) {
        queue.push({
          date: d.date,
          label: d.label,
          ep_idx: ep_idx,
          ep_num: ep.ep_num || (ep_idx + 1),
          title: ep.display_title || ep.title || ('Episode ' + (ep_idx + 1)),
          url: ep.url,
          size: ep.size || 0,
        });
      });
    });
    renderQueueList();
    $queueCount.textContent = queue.length;
    $queueCard.style.display = queue.length ? '' : 'none';

    // If we have something playing but it's no longer in the queue, stop.
    if (currentIdx >= 0) {
      var current = $audio.dataset.url;
      var stillIn = queue.findIndex(function(q) { return q.url === current; });
      if (stillIn === -1) {
        $audio.pause(); currentIdx = -1;
        $playerCard.style.display = 'none';
      } else {
        currentIdx = stillIn;
        renderQueueList();
      }
    }
  }

  function renderDateGrid() {
    $dateGrid.innerHTML = '';
    if (!index.length) { $empty.style.display = ''; return; }
    var dates = sortAsc ? index : index.slice().reverse();
    dates.forEach(function(d) {
      var chip = document.createElement('label');
      chip.className = 'date-chip';
      chip.innerHTML =
        '<input type="checkbox" data-date="' + d.date + '"' +
          (selectedDates.has(d.date) ? ' checked' : '') + '>' +
        '<span>' + escHtml(shortDate(d.label)) + '</span>' +
        '<span class="ep-count">' + d.episodes.length + ' ep' +
        (d.episodes.length === 1 ? '' : 's') + '</span>';
      chip.querySelector('input').addEventListener('change', function(e) {
        if (e.target.checked) selectedDates.add(d.date);
        else selectedDates.delete(d.date);
        rebuildQueue();
      });
      $dateGrid.appendChild(chip);
    });
  }

  function renderQueueList() {
    $queueList.innerHTML = '';
    queue.forEach(function(q, idx) {
      var marker, klass = 'queue-item';
      if (idx < currentIdx) { marker = '✓'; klass += ' played'; }
      else if (idx === currentIdx) { marker = '●'; klass += ' active'; }
      else marker = '○';
      var row = document.createElement('div');
      row.className = klass;
      row.innerHTML =
        '<span class="qi-marker">' + marker + '</span>' +
        '<span class="qi-date">' + escHtml(shortDate(q.label)) + '</span>' +
        '<span class="qi-title">Ep ' + q.ep_num + ' · ' + escHtml(q.title) + '</span>' +
        '<span class="qi-size">' + fmtMB(q.size) + '</span>';
      row.addEventListener('click', function() { loadAndPlay(idx); });
      $queueList.appendChild(row);
    });
  }

  // ── Player ────────────────────────────────────────────────────────────────
  function loadAndPlay(idx, opts) {
    opts = opts || {};
    if (idx < 0 || idx >= queue.length) return;
    var q = queue[idx];
    currentIdx = idx;
    $audio.src = q.url;
    $audio.dataset.url = q.url;
    $audio.dataset.date = q.date;
    $audio.dataset.epIdx = q.ep_idx;
    $playerCard.style.display = '';
    $npMeta.textContent = shortDate(q.label) + ' · Ep ' + q.ep_num;
    $npTitle.textContent = q.title;
    $npSub.textContent = fmtMB(q.size);
    renderQueueList();
    $audio.addEventListener('loadedmetadata', function once() {
      $audio.removeEventListener('loadedmetadata', once);
      if (opts.startAt && opts.startAt > 0 && opts.startAt < $audio.duration) {
        $audio.currentTime = opts.startAt;
      }
      var p = $audio.play();
      if (p && p.catch) p.catch(function() {/* autoplay blocked */});
    });
    // Force a load (preload="none" means it won't otherwise)
    $audio.load();
  }

  function playNext() {
    if (currentIdx + 1 < queue.length) loadAndPlay(currentIdx + 1);
    else { $audio.pause(); }
  }
  function playPrev() {
    if (currentIdx > 0) loadAndPlay(currentIdx - 1);
  }

  // ── Wire controls ────────────────────────────────────────────────────────
  $sort.addEventListener('click', function() {
    sortAsc = !sortAsc;
    $sort.textContent = sortAsc ? '↑ Oldest first' : '↓ Newest first';
    renderDateGrid();
    rebuildQueue();
  });
  $play.addEventListener('click', function() {
    if (!queue.length) return;
    // If we're already cued up, just resume; otherwise start at 0.
    if (currentIdx >= 0 && currentIdx < queue.length) {
      var p = $audio.play(); if (p && p.catch) p.catch(function(){});
    } else loadAndPlay(0);
  });
  $selAll.addEventListener('click', function() {
    index.forEach(function(d) { selectedDates.add(d.date); });
    renderDateGrid(); rebuildQueue();
  });
  $selNone.addEventListener('click', function() {
    selectedDates.clear(); renderDateGrid(); rebuildQueue();
  });
  $prev.addEventListener('click', playPrev);
  $next.addEventListener('click', playNext);
  $pp.addEventListener('click', function() {
    if (currentIdx < 0) { if (queue.length) loadAndPlay(0); return; }
    if ($audio.paused) $audio.play(); else $audio.pause();
  });
  $audio.addEventListener('play',  function() { $pp.textContent = '⏸ Pause'; });
  $audio.addEventListener('pause', function() { $pp.textContent = '▶ Play';  });
  $audio.addEventListener('ended', function() { playNext(); });

  // Seekbar driving + readout
  var seekDragging = false;
  $seek.addEventListener('input', function() {
    seekDragging = true;
    if (isFinite($audio.duration)) {
      $audio.currentTime = ($seek.value / 100) * $audio.duration;
    }
  });
  $seek.addEventListener('change', function() { seekDragging = false; });
  $audio.addEventListener('timeupdate', function() {
    if (!seekDragging && isFinite($audio.duration) && $audio.duration > 0) {
      $seek.value = ($audio.currentTime / $audio.duration) * 100;
    }
    $time.textContent = fmtTime($audio.currentTime) + ' / ' + fmtTime($audio.duration);
    // Throttle localStorage writes to once per ~5s
    var now = Date.now();
    if (now - lastSaveTs > 5000 && currentIdx >= 0) {
      lastSaveTs = now;
      var q = queue[currentIdx];
      try {
        localStorage.setItem(LS.DATE,   q.date);
        localStorage.setItem(LS.EP_IDX, String(q.ep_idx));
        localStorage.setItem(LS.POS,    String($audio.currentTime));
      } catch (e) {}
    }
  });

  // ── Resume banner ────────────────────────────────────────────────────────
  function maybeOfferResume() {
    var savedDate, savedEpIdx, savedPos;
    try {
      savedDate   = localStorage.getItem(LS.DATE);
      savedEpIdx  = parseInt(localStorage.getItem(LS.EP_IDX) || '', 10);
      savedPos    = parseFloat(localStorage.getItem(LS.POS) || '');
    } catch (e) {}
    if (!savedDate || isNaN(savedEpIdx) || isNaN(savedPos) || savedPos < 5) return;

    // Find this item in queue (depends on current selection)
    var qIdx = queue.findIndex(function(q) {
      return q.date === savedDate && q.ep_idx === savedEpIdx;
    });
    if (qIdx === -1) {
      // Try without selection filter — look in the full index
      var dayEntry = index.find(function(d) { return d.date === savedDate; });
      if (!dayEntry || savedEpIdx >= dayEntry.episodes.length) return;
      var ep = dayEntry.episodes[savedEpIdx];
      $resumeText.innerHTML = 'Last played: <strong>' + escHtml(shortDate(dayEntry.label)) +
        ' · Ep ' + (ep.ep_num || (savedEpIdx + 1)) + '</strong> at ' + fmtTime(savedPos) +
        '. <em>(Select that date above to enable resume.)</em>';
      $resume.style.display = 'flex';
      $resumeYes.style.display = 'none';
      $resumeNo.addEventListener('click', function() { $resume.style.display = 'none'; });
      return;
    }
    var q = queue[qIdx];
    $resumeText.innerHTML = 'Last played: <strong>' + escHtml(shortDate(q.label)) +
      ' · Ep ' + q.ep_num + '</strong> at ' + fmtTime(savedPos) + ' — Resume?';
    $resume.style.display = 'flex';
    $resumeYes.addEventListener('click', function() {
      $resume.style.display = 'none';
      loadAndPlay(qIdx, {startAt: savedPos});
    });
    $resumeNo.addEventListener('click', function() {
      $resume.style.display = 'none';
      try {
        localStorage.removeItem(LS.DATE);
        localStorage.removeItem(LS.EP_IDX);
        localStorage.removeItem(LS.POS);
      } catch (e) {}
    });
  }

  // ── Boot ──────────────────────────────────────────────────────────────────
  fetch('/api/playlist-index')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      index = Array.isArray(data) ? data : [];
      // Default selection: all dates
      index.forEach(function(d) { selectedDates.add(d.date); });
      renderDateGrid();
      rebuildQueue();
      if (!index.length) { $empty.style.display = ''; return; }
      maybeOfferResume();
    })
    .catch(function(e) {
      $empty.style.display = '';
      $empty.textContent = 'Failed to load playlist: ' + e;
    });
})();
</script>
</body></html>
"""


def render_playlist_page() -> str:
    """Return the full Playlist HTML page. No template substitution — the JS
    fetches all dynamic data from `/api/playlist-index` on boot."""
    return _PLAYLIST_HTML
