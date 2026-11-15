"""Unpack the Claude Design bundle (mockup.bundle.html) into readable sources.

Usage: python3 -I design/mockup/tools/extract.py design/mockup/mockup.bundle.html design/mockup/src

Writes markup.html (the template with {{ bindings }}), logic.js (the component
class that computes those bindings, including all mock data), app.css (theme
tokens and keyframes) and props.json (the mockup's preview switches).
Fonts and the React UMD builds stay inside the bundle.
"""
import base64, gzip, html, json, os, re, sys

src, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
page = open(src, encoding="utf-8").read()


def bundled(kind):
    m = re.search(r'<script type="__bundler/%s">(.*?)</script>' % kind, page, re.S)
    return json.loads(m.group(1))


template = bundled("template")
manifest = bundled("manifest")

css_start = template.index("<style>", template.index("</style>") + 1)
css_end = template.index("</style>", css_start)
script_open = template.index('<script type="text/x-dc"')
script_body = template.index(">", script_open) + 1
script_end = template.index("</script>", script_body)
props = re.search(r'data-props="([^"]*)"', template[script_open:script_body]).group(1)


def write(name, text):
    with open(os.path.join(out, name), "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")


write("app.css", template[css_start + len("<style>"):css_end])
write("markup.html", template[css_end + len("</style>"):script_open].replace("</helmet>", "", 1))
write("logic.js", template[script_body:script_end])
write("props.json", json.dumps(json.loads(html.unescape(props)), indent=2))
fonts = sorted({e["mime"] for e in manifest.values()})
print("extracted to", out, "| bundled asset types:", ", ".join(fonts))
