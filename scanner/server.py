"""
Tailscale-served web app: daily digests + 2hr podcast audio + chat Q&A.

Endpoints:
  GET  /                          → Mobile-friendly index (reports + today's podcast)
  GET  /latest                    → 302 to most recent digest
  GET  /report/<YYYY-MM-DD>.html  → A specific day's digest
  GET  /podcast/<YYYY-MM-DD>.mp3  → Stream that day's podcast audio
  GET  /podcasts                  → List all podcasts
  GET  /chat                      → Chat UI (new conversation)
  GET  /chat/<conv_id>            → Chat UI loading existing conversation
  POST /api/chat                  → {message, conversation_id?} → {reply, conversation_id, ...}
  GET  /api/conversations         → List past conversations
  GET  /api/conversation/<id>     → Full conversation messages
  GET  /api/events.json?days=N    → Recent event JSON (existing)
  GET  /api/notes                 → List knowledge notes
  GET  /knowledge/<path>          → Serve saved .md note files
"""
import json
import logging
import os
import re
import socket
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config import Config as _Cfg
_LOCALE = ", ".join(p for p in [_Cfg.CITY, _Cfg.COUNTY, _Cfg.STATE] if p) or "your area"
_SITE_TITLE = f"{_Cfg.CITY or _Cfg.COUNTY or 'Local'} Politics" if (_Cfg.CITY or _Cfg.COUNTY) else "Local Politics"
_SITE_SUB = ", ".join(p for p in [_Cfg.COUNTY, _Cfg.STATE_CODE.upper() if _Cfg.STATE_CODE else _Cfg.STATE] if p) or "Local digests"
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlparse, parse_qs

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Tailscale IP detection
# ──────────────────────────────────────────────────────────────────────────────

def get_tailscale_ip() -> str:
    try:
        import subprocess
        r = subprocess.run(["tailscale", "ip", "-4"],
                           capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0]
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        for addr in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = addr[4][0]
            if ip.startswith("100."):
                return ip
    except Exception:
        pass
    return ""


# ──────────────────────────────────────────────────────────────────────────────
# Report / podcast listing helpers
# ──────────────────────────────────────────────────────────────────────────────

def _list_reports(reports_dir: Path) -> List[Tuple[str, Path]]:
    pattern = re.compile(r"digest_(\d{4}-\d{2}-\d{2})\.html$")
    items: List[Tuple[str, Path]] = []
    if not reports_dir.exists():
        return items
    for p in reports_dir.glob("digest_*.html"):
        m = pattern.match(p.name)
        if m:
            items.append((m.group(1), p))
    items.sort(key=lambda x: x[0], reverse=True)
    return items


def _list_podcast_files(podcasts_dir: Path) -> List[Tuple[str, Path]]:
    """Match both legacy podcast_YYYY-MM-DD.mp3 and new podcast_YYYY-MM-DD_epN_slug.mp3."""
    pattern = re.compile(r"podcast_(\d{4}-\d{2}-\d{2}).*\.mp3$")
    items: List[Tuple[str, Path]] = []
    if not podcasts_dir.exists():
        return items
    for p in podcasts_dir.glob("podcast_*.mp3"):
        m = pattern.match(p.name)
        if m:
            items.append((m.group(1), p))
    items.sort(key=lambda x: (x[0], x[1].name), reverse=True)
    return items


# ──────────────────────────────────────────────────────────────────────────────
# HTML renderers
# ──────────────────────────────────────────────────────────────────────────────

def _render_index(reports: List[Tuple[str, Path]],
                   podcasts: List[Tuple[str, Path]]) -> str:
    today = date.today().isoformat()
    today_podcast = next((p for d, p in podcasts if d == today), None)
    podcast_card = ""
    if today_podcast:
        size_mb = today_podcast.stat().st_size / 1_048_576
        podcast_card = f"""
        <div class="podcast-card">
          <div class="podcast-emoji">🎧</div>
          <div class="podcast-body">
            <div class="podcast-title">Today's Podcast</div>
            <div class="podcast-sub">2-hour two-host digest · {size_mb:.1f} MB</div>
          </div>
          <audio controls preload="none" style="width:100%;margin-top:10px;">
            <source src="/podcast/{today}.mp3" type="audio/mpeg">
          </audio>
        </div>"""
    elif podcasts:
        latest_d, _ = podcasts[0]
        podcast_card = f"""
        <div class="podcast-card">
          <div class="podcast-emoji">🎧</div>
          <div class="podcast-body">
            <div class="podcast-title">Latest Podcast — {latest_d}</div>
            <div class="podcast-sub">Today's episode not ready yet. Play latest:</div>
          </div>
          <audio controls preload="none" style="width:100%;margin-top:10px;">
            <source src="/podcast/{latest_d}.mp3" type="audio/mpeg">
          </audio>
        </div>"""

    if not reports:
        rows_html = """
        <div style='padding:32px;text-align:center;color:#888;'>
          <p>No reports yet. Run <code>python main.py scan</code>.</p>
        </div>"""
    else:
        rows_html = ""
        for date_str, path in reports[:30]:
            is_today = date_str == today
            size_kb = path.stat().st_size // 1024
            has_pod = any(d == date_str for d, _ in podcasts)
            pod_badge = ' 🎧' if has_pod else ''
            badge = '<span class="badge new">TODAY</span>' if is_today else ""
            rows_html += f"""
            <a class="row" href="/report/{date_str}.html">
              <div class="row-main">
                <div class="row-date">{date_str} {badge}{pod_badge}</div>
                <div class="row-meta">{size_kb} KB digest{' · audio available' if has_pod else ''}</div>
              </div>
              <div class="row-arrow">›</div>
            </a>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#1a3a5c">
<title>{_SITE_TITLE}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f5f0; color: #222; min-height: 100vh; }}
  .header {{ background: #1a3a5c; color: white; padding: 20px; position: sticky;
             top: 0; z-index: 10; }}
  .header h1 {{ font-size: 1.3rem; }}
  .header .sub {{ font-size: .8rem; opacity: .75; margin-top: 2px; }}
  .container {{ max-width: 720px; margin: 0 auto; padding: 16px; }}
  .quick-actions {{ display: flex; gap: 8px; margin-bottom: 16px; }}
  .quick-actions a {{ flex: 1; background: white; border-radius: 10px;
                       padding: 14px 10px; text-align: center;
                       text-decoration: none; color: #1a3a5c; font-weight: 600;
                       font-size: .85rem; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  .quick-actions a.primary {{ background: #1a3a5c; color: white; }}
  .podcast-card {{ background: linear-gradient(135deg, #3a6ea5 0%, #1a3a5c 100%);
                    color: white; border-radius: 12px; padding: 18px;
                    margin-bottom: 16px; }}
  .podcast-card .podcast-emoji {{ font-size: 1.8rem; float: left; margin-right: 10px; }}
  .podcast-title {{ font-weight: 700; font-size: 1.05rem; }}
  .podcast-sub {{ font-size: .8rem; opacity: .8; margin-top: 2px; }}
  .podcast-body {{ overflow: hidden; }}
  .section-title {{ font-size: .82rem; text-transform: uppercase; letter-spacing: 1px;
                     color: #888; margin: 18px 4px 10px; }}
  .row {{ display: flex; align-items: center; padding: 16px 18px;
          background: white; border-radius: 10px; margin-bottom: 8px;
          text-decoration: none; color: inherit;
          box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  .row:active {{ transform: scale(.98); }}
  .row-main {{ flex: 1; }}
  .row-date {{ font-weight: 600; font-size: 1rem; color: #1a3a5c;
               display: flex; align-items: center; gap: 8px; }}
  .row-meta {{ font-size: .78rem; color: #888; margin-top: 3px; }}
  .row-arrow {{ color: #ccc; font-size: 1.8rem; line-height: 1; }}
  .badge {{ font-size: .68rem; font-weight: 700; padding: 2px 7px; border-radius: 10px; }}
  .badge.new {{ background: #c0392b; color: white; }}
  .footer {{ text-align: center; color: #aaa; font-size: .75rem; padding: 24px 16px; }}
</style></head><body>
<div class="header">
  <h1>📰 {_SITE_TITLE}</h1>
  <div class="sub">{_SITE_SUB} · {len(reports)} digests · {len(podcasts)} podcasts</div>
</div>
<div class="container">
  <div class="quick-actions">
    <a class="primary" href="/latest">Latest Digest</a>
    <a href="/chat">💬 Ask Agent</a>
    <a href="/api/events.json?days=3">JSON</a>
  </div>
  {podcast_card}
  <div class="section-title">Recent Digests</div>
  {rows_html}
</div>
<div class="footer">Local Politics Scanner · Tailscale</div>
</body></html>"""


def _render_chat_ui(conversation_id: int = 0) -> str:
    """Mobile-first chat page. Conversation state is client-side + POSTed to /api/chat."""
    return """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="theme-color" content="#1a3a5c">
<title>Ask the Agent</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; overflow: hidden; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f5f0; color: #222; display: flex; flex-direction: column; }
  .header { background: #1a3a5c; color: white; padding: 14px 16px;
            display: flex; justify-content: space-between; align-items: center; }
  .header .title { font-weight: 600; font-size: 1rem; }
  .header .subtitle { font-size: .72rem; opacity: .7; }
  .header a { color: white; text-decoration: none; font-size: .85rem;
              padding: 6px 10px; background: rgba(255,255,255,.12); border-radius: 6px; }
  #messages { flex: 1; overflow-y: auto; padding: 14px; }
  .msg { max-width: 88%; padding: 11px 14px; border-radius: 14px;
         margin-bottom: 10px; font-size: .93rem; line-height: 1.45;
         white-space: pre-wrap; word-wrap: break-word; }
  .msg.user { background: #1a3a5c; color: white; margin-left: auto;
              border-bottom-right-radius: 4px; }
  .msg.assistant { background: white; color: #222; margin-right: auto;
                   border-bottom-left-radius: 4px;
                   box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  .msg.assistant a { color: #1a3a5c; }
  .msg.assistant strong { color: #1a3a5c; }
  .msg.assistant ul, .msg.assistant ol { margin: 8px 0 8px 20px; }
  .msg.assistant h1, .msg.assistant h2, .msg.assistant h3 {
      margin: 10px 0 6px; color: #1a3a5c; font-size: 1rem; }
  .msg.typing { font-style: italic; color: #888; background: transparent;
                box-shadow: none; padding: 4px 12px; }
  .note-toast { background: #e8f3e8; color: #2a5a2a; padding: 8px 12px;
                border-radius: 8px; margin: 6px 0; font-size: .78rem;
                display: flex; align-items: center; gap: 6px; }
  .empty-state { text-align: center; padding: 40px 20px; color: #888; }
  .empty-state h2 { font-size: 1.1rem; color: #1a3a5c; margin-bottom: 8px; }
  .empty-state .prompts { margin-top: 20px; display: flex; flex-direction: column; gap: 8px; }
  .empty-state .prompts button { background: white; border: 1px solid #ddd;
      padding: 10px 14px; border-radius: 20px; font-size: .85rem;
      color: #1a3a5c; cursor: pointer; }
  #composer { background: white; padding: 10px; display: flex; gap: 8px;
              border-top: 1px solid #e0e0e0; padding-bottom: env(safe-area-inset-bottom, 10px); }
  #input { flex: 1; border: 1px solid #ccc; border-radius: 20px;
           padding: 10px 14px; font-size: .95rem; resize: none;
           font-family: inherit; max-height: 100px; }
  #input:focus { outline: 2px solid #1a3a5c; outline-offset: -1px; }
  #send { background: #1a3a5c; color: white; border: none;
          padding: 0 18px; border-radius: 20px; font-weight: 600;
          cursor: pointer; font-size: .9rem; }
  #send:disabled { background: #aaa; }
</style></head><body>
<div class="header">
  <div>
    <div class="title">💬 Ask the Agent</div>
    <div class="subtitle">About today's digest</div>
  </div>
  <a href="/">← Home</a>
</div>
<div id="messages">
  <div class="empty-state" id="empty">
    <h2>Ask about anything from today</h2>
    <p style="font-size:.85rem;">I have context from your recent digest. Try:</p>
    <div class="prompts">
      <button onclick="sendPreset('What are the 3 most important things I should know from today?')">📌 What matters most today?</button>
      <button onclick="sendPreset('Which upcoming public hearings should I consider attending?')">🗓️ Upcoming hearings</button>
      <button onclick="sendPreset('Summarize what my county council did this week.')">🏛️ County Council recap</button>
      <button onclick="sendPreset('How does the current state legislative session affect me as a local resident?')">📋 State session recap</button>
      <button onclick="sendPreset('Any federal news on the topics I follow that I should care about?')">🇺🇸 Federal topics</button>
    </div>
  </div>
</div>
<form id="composer" onsubmit="event.preventDefault(); send();">
  <textarea id="input" placeholder="Ask a question..." rows="1" autofocus></textarea>
  <button id="send" type="submit">Send</button>
</form>
<script>
let conversationId = CONVERSATION_ID_PLACEHOLDER;

const msgs = document.getElementById('messages');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const empty = document.getElementById('empty');

input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 100) + 'px';
});

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});

function renderMarkdown(text) {
  // Tiny markdown: bold, links, bullets, headings
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>')
    .replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, '<a href="$2" target="_blank">$1</a>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/^\\s*[-*]\\s+(.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.+<\\/li>\\n?)+/g, m => '<ul>' + m + '</ul>');
}

function appendMsg(role, text, cls='') {
  if (empty) empty.style.display = 'none';
  const el = document.createElement('div');
  el.className = 'msg ' + role + (cls ? ' ' + cls : '');
  el.innerHTML = role === 'assistant' ? renderMarkdown(text) : text;
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
  return el;
}

function appendToast(text) {
  const el = document.createElement('div');
  el.className = 'note-toast';
  el.textContent = '📝 Saved as knowledge note: "' + text + '"';
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
}

function sendPreset(text) { input.value = text; send(); }

async function send() {
  const text = input.value.trim();
  if (!text) return;
  input.value = ''; input.style.height = 'auto';
  sendBtn.disabled = true;
  appendMsg('user', text);
  const typing = appendMsg('assistant', 'Thinking...', 'typing');

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text, conversation_id: conversationId})
    });
    const data = await resp.json();
    typing.remove();
    if (data.error) {
      appendMsg('assistant', 'Error: ' + data.error);
    } else {
      conversationId = data.conversation_id;
      history.replaceState(null, '', '/chat/' + conversationId);
      appendMsg('assistant', data.reply);
      if (data.note_saved) appendToast(data.note_title);
    }
  } catch (e) {
    typing.remove();
    appendMsg('assistant', 'Network error: ' + e.message);
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

// Load existing conversation if we have an ID
if (conversationId) {
  fetch('/api/conversation/' + conversationId)
    .then(r => r.json())
    .then(data => {
      if (data.messages) {
        for (const m of data.messages) {
          appendMsg(m.role, m.content);
        }
      }
    });
}
</script></body></html>""".replace("CONVERSATION_ID_PLACEHOLDER", str(conversation_id))


# ──────────────────────────────────────────────────────────────────────────────
# Request handler
# ──────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    # Set at server startup
    reports_dir: Path = Path("reports")
    podcasts_dir: Path = Path("podcasts")
    knowledge_dir: Path = Path("knowledge")
    db_path: Path = Path("data/politics.db")
    anthropic_key: str = ""
    chat_model: str = "claude-sonnet-4-5-20250929"

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    # ── GET ────────────────────────────────────────────────────────────────

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._serve_index()
            if path == "/latest":
                return self._redirect_latest()
            if path.startswith("/report/") and path.endswith(".html"):
                return self._serve_report(path[len("/report/"):-len(".html")])
            if path.startswith("/podcast/") and path.endswith(".mp3"):
                stem = path[len("/podcast/"):-len(".mp3")]
                return self._serve_podcast(stem)
            if path == "/podcasts":
                return self._serve_podcast_list()
            if path == "/chat":
                return self._send(200, "text/html; charset=utf-8",
                                    _render_chat_ui(0).encode())
            if path.startswith("/chat/"):
                try:
                    cid = int(path[len("/chat/"):])
                except ValueError:
                    cid = 0
                return self._send(200, "text/html; charset=utf-8",
                                    _render_chat_ui(cid).encode())
            if path == "/api/conversations":
                return self._serve_conv_list()
            if path.startswith("/api/conversation/"):
                try:
                    cid = int(path[len("/api/conversation/"):])
                except ValueError:
                    return self._send_404()
                return self._serve_conv(cid)
            if path == "/api/notes":
                return self._serve_notes()
            if path.startswith("/api/events.json"):
                return self._serve_events_json()
            if path.startswith("/knowledge/"):
                return self._serve_knowledge(path[len("/knowledge/"):])
            if path == "/favicon.ico":
                self.send_response(204); self.end_headers(); return
            return self._send_404()
        except Exception as e:
            log.exception("GET %s failed", path)
            self._send(500, "text/plain", f"Server error: {e}".encode())

    # ── POST ───────────────────────────────────────────────────────────────

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            if path == "/api/chat":
                return self._handle_chat(body)
            if path == "/chat":
                return self._handle_digest_chat(body)
            return self._send_404()
        except Exception as e:
            log.exception("POST %s failed", path)
            self._send(500, "application/json",
                        json.dumps({"error": str(e)}).encode())

    # ── Route handlers ─────────────────────────────────────────────────────

    def _serve_index(self):
        reports = _list_reports(self.reports_dir)
        podcasts = _list_podcast_files(self.podcasts_dir)
        html = _render_index(reports, podcasts).encode("utf-8")
        self._send(200, "text/html; charset=utf-8", html)

    def _redirect_latest(self):
        reports = _list_reports(self.reports_dir)
        target = f"/report/{reports[0][0]}.html" if reports else "/"
        self._send(302, "text/plain", b"", {"Location": target})

    def _serve_report(self, date_str: str):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
            return self._send_404()
        path = self.reports_dir / f"digest_{date_str}.html"
        if not path.exists():
            return self._send_404()
        self._send(200, "text/html; charset=utf-8", path.read_bytes())

    def _serve_podcast(self, filename: str):
        # Accept podcast_YYYY-MM-DD.mp3 and podcast_YYYY-MM-DD_epN_slug.mp3
        if not re.fullmatch(r"podcast_[\w-]+", filename):
            return self._send_404()
        path = self.podcasts_dir / f"{filename}.mp3"
        if not path.exists():
            return self._send_404()
        self._serve_file_with_range(path, "audio/mpeg")

    def _serve_podcast_list(self):
        items = _list_podcast_files(self.podcasts_dir)
        listing = "".join(
            f'<li><a href="/podcast/{d}.mp3">{d}</a> '
            f'<small>({p.stat().st_size // 1_048_576} MB)</small></li>'
            for d, p in items
        )
        html = f"<!DOCTYPE html><meta charset='utf-8'><title>Podcasts</title>" \
               f"<h1>All Podcasts</h1><ul>{listing}</ul><p><a href='/'>← Home</a></p>"
        self._send(200, "text/html; charset=utf-8", html.encode())

    def _handle_chat(self, body: bytes):
        data = json.loads(body.decode() or "{}")
        message = (data.get("message") or "").strip()
        conv_id = data.get("conversation_id") or None
        if not message:
            return self._send(400, "application/json",
                                json.dumps({"error": "empty message"}).encode())

        if not self.anthropic_key:
            return self._send(500, "application/json",
                                json.dumps({"error": "ANTHROPIC_API_KEY not set"}).encode())

        from scanner.chat import handle_message
        result = handle_message(
            db_path=self.db_path,
            knowledge_dir=self.knowledge_dir,
            anthropic_key=self.anthropic_key,
            user_message=message,
            conversation_id=conv_id if conv_id else None,
            chat_model=self.chat_model,
        )
        self._send(200, "application/json",
                    json.dumps(result, ensure_ascii=False).encode())

    def _handle_digest_chat(self, body: bytes):
        """
        POST /chat  — sidebar Q&A tied to a specific day's digest.
        Request:  {"question": "...", "date": "YYYY-MM-DD"}
        Response: {"answer": "..."}
        """
        data = json.loads(body.decode() or "{}")
        question = (data.get("question") or "").strip()
        report_date = (data.get("date") or "").strip()

        if not question:
            return self._send(400, "application/json",
                               json.dumps({"error": "empty question"}).encode())
        if not self.anthropic_key:
            return self._send(500, "application/json",
                               json.dumps({"error": "ANTHROPIC_API_KEY not configured"}).encode())

        # Load digest markdown as context
        context = ""
        if report_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
            md_path = self.reports_dir / f"digest_{report_date}.md"
            if md_path.exists():
                context = md_path.read_text(encoding="utf-8")
        if not context:
            today = date.today().isoformat()
            md_path = self.reports_dir / f"digest_{today}.md"
            if md_path.exists():
                context = md_path.read_text(encoding="utf-8")

        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=self.anthropic_key)

        system = (
            f"You are a political analyst assistant for a voter in {_LOCALE}. "
            "Answer questions about the local political news digest provided. "
            "Be specific, cite events from the digest, and connect issues to "
            f"the voter's daily life in {_LOCALE}."
        )
        user_content = (
            f"Today's political digest:\n\n{context[:8000]}\n\n---\n\nQuestion: {question}"
            if context else question
        )

        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
            answer = resp.content[0].text
            self._send(200, "application/json; charset=utf-8",
                        json.dumps({"answer": answer}, ensure_ascii=False).encode())
        except Exception as e:
            log.error("digest_chat error: %s", e)
            self._send(500, "application/json",
                        json.dumps({"error": str(e)}).encode())

    def _serve_conv_list(self):
        from scanner.database import list_conversations
        items = list_conversations(self.db_path, limit=50)
        self._send(200, "application/json",
                    json.dumps({"conversations": items}, default=str).encode())

    def _serve_conv(self, cid: int):
        from scanner.database import get_conversation
        conv = get_conversation(self.db_path, cid)
        self._send(200, "application/json",
                    json.dumps(conv or {}, default=str).encode())

    def _serve_notes(self):
        from scanner.database import list_knowledge_notes
        notes = list_knowledge_notes(self.db_path, limit=100)
        self._send(200, "application/json",
                    json.dumps({"notes": notes}, default=str).encode())

    def _serve_knowledge(self, rel_path: str):
        # Prevent directory traversal
        if ".." in rel_path or rel_path.startswith("/"):
            return self._send_404()
        path = self.knowledge_dir / rel_path
        if not path.exists() or not path.is_file():
            return self._send_404()
        content_type = "text/markdown; charset=utf-8" if rel_path.endswith(".md") \
                       else "text/plain; charset=utf-8"
        self._send(200, content_type, path.read_bytes())

    def _serve_events_json(self):
        qs = parse_qs(urlparse(self.path).query)
        days = int(qs.get("days", ["7"])[0])
        from scanner.database import get_recent_events
        events = get_recent_events(self.db_path, days=days, min_relevance=0.0)
        trimmed = [{
            "title": e.get("title"),
            "level": e.get("level"),
            "type": e.get("type"),
            "date": e.get("date"),
            "url": e.get("source_url"),
            "source": e.get("source_name"),
            "summary": e.get("summary") or e.get("description", "")[:200],
            "relevance": round(e.get("relevance_score", 0), 2),
            "politicians": e.get("politicians"),
        } for e in events]
        self._send(200, "application/json; charset=utf-8",
                    json.dumps({"count": len(trimmed), "events": trimmed},
                                ensure_ascii=False).encode())

    # ── HTTP Range support for audio seeking ───────────────────────────────

    def _serve_file_with_range(self, path: Path, content_type: str):
        file_size = path.stat().st_size
        range_header = self.headers.get("Range", "")
        if range_header and range_header.startswith("bytes="):
            try:
                start_s, end_s = range_header[len("bytes="):].split("-")
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else file_size - 1
                end = min(end, file_size - 1)
                length = end - start + 1
                with open(path, "rb") as f:
                    f.seek(start)
                    body = f.read(length)
                self.send_response(206)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception:
                pass
        # Full file
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(file_size))
        self.end_headers()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return

    # ── Helpers ────────────────────────────────────────────────────────────

    def _send(self, code: int, content_type: str, body: bytes, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_404(self):
        self._send(404, "text/html; charset=utf-8",
                     b"<h1>404 Not Found</h1><p><a href='/'>&larr; Home</a></p>")


# ──────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────

def run_server(reports_dir: Path, db_path: Path,
               podcasts_dir: Path = None, knowledge_dir: Path = None,
               anthropic_key: str = "",
               chat_model: str = "claude-sonnet-4-5-20250929",
               host: str = "0.0.0.0", port: int = 8765):
    Handler.reports_dir = reports_dir
    Handler.podcasts_dir = podcasts_dir or (reports_dir.parent / "podcasts")
    Handler.knowledge_dir = knowledge_dir or (reports_dir.parent / "knowledge")
    Handler.db_path = db_path
    Handler.anthropic_key = anthropic_key or os.getenv("ANTHROPIC_API_KEY", "")
    Handler.chat_model = chat_model

    Handler.podcasts_dir.mkdir(parents=True, exist_ok=True)
    Handler.knowledge_dir.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((host, port), Handler)
    ts_ip = get_tailscale_ip()
    hostname = socket.gethostname()

    print("\n── Politics Digest Server ──────────────────────────────")
    print(f"   Reports  : {reports_dir}")
    print(f"   Podcasts : {Handler.podcasts_dir}")
    print(f"   Knowledge: {Handler.knowledge_dir}")
    print(f"   Bind     : {host}:{port}")
    print()
    print("   Phone (Tailscale):")
    if ts_ip:
        print(f"     📱  http://{ts_ip}:{port}/              (main)")
        print(f"     💬  http://{ts_ip}:{port}/chat          (ask the agent)")
    else:
        print(f"     📱  http://<your-tailscale-ip>:{port}/")
    print(f"     💻  http://{hostname}:{port}/")
    print(f"     🖥️   http://localhost:{port}/")
    print("\n   Ctrl+C to stop.")
    print("────────────────────────────────────────────────────────\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server…")
        server.shutdown()
