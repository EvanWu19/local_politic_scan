from scanner.sources.local_hearings import _parse_boarddocs_xml

data = open("tmp_boarddocs.xml", "rb").read()
items = _parse_boarddocs_xml(data, 10)
lines = [f"FIXED PARSER returns {len(items)} items:"]
for i in items:
    lines.append(f"  date={i['date']} | url={i['source_url']} | title={i['title'][:55]}")
open("tmp_parser_out.txt", "w", encoding="utf-8").write("\n".join(lines))
