import logging, time, requests
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")
from scanner.sources.local_hearings import (
    fetch_mcps_boarddocs, _fetch_mcps_boarddocs_html, HEADERS,
)

out = []

# 1. Live XML fetch
t = time.time()
xml_items = fetch_mcps_boarddocs(10)
out.append(f"=== LIVE fetch_mcps_boarddocs: {len(xml_items)} items in {time.time()-t:.1f}s ===")
for i in xml_items[:5]:
    out.append(f"  {i['date']} | {i['source_url']}")

# 2. Verify 2 goto URLs resolve
out.append("=== goto URL resolution ===")
for i in xml_items[:2]:
    u = i["source_url"]
    try:
        r = requests.get(u, timeout=30, headers=HEADERS, allow_redirects=True)
        body = r.text.lower()
        hit = any(k in body for k in ("meeting", "agenda", "board"))
        out.append(f"  {r.status_code} len={len(r.text)} keyword={hit} {u}")
    except Exception as e:
        out.append(f"  ERR {e} {u}")

# 3. HTML scraper comparison
t = time.time()
html_items = _fetch_mcps_boarddocs_html(10)
out.append(f"=== HTML scraper: {len(html_items)} items in {time.time()-t:.1f}s ===")
for i in html_items[:5]:
    out.append(f"  {i['date']} | {i['title'][:50]} | {i['source_url'][:70]}")

open("tmp_live_out.txt", "w", encoding="utf-8").write("\n".join(out))
print("DONE")
