from lxml import etree

root = etree.parse("tmp_boarddocs.xml").getroot()
m = [el for el in root if etree.QName(el).localname == "meeting"][-1]  # most recent
out = []
out.append(f"tag: {m.tag} | has namespace: {'}' in m.tag}")
out.append(f"id attr: {m.get('id')} | unique attr: {m.get('unique')}")
out.append(f"direct child link: {m.findtext('link')}")
out.append(f"start/date: {m.findtext('start/date')}")
out.append(f"name: {m.findtext('name')}")
out.append(f"attrs: {dict(m.attrib)}")
open("tmp_inspect_out.txt", "w", encoding="utf-8").write("\n".join(out))
