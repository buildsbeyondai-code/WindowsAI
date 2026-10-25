#!/usr/bin/env python3
"""
Build the SysPerf user manual as a single self-contained HTML file.

The manual is written in Markdown so it stays readable and diffable in the
repository; this script is the only place that knows about presentation.

Design goals:

* **One file, no dependencies at read time.** CSS is inlined and there is no
  JavaScript requirement for the content — the manual opens from disk, works
  offline, and can be attached to an email or served as-is.
* **Navigable.** A sticky sidebar contains a two-level table of contents built
  from the headings; every heading gets a stable anchor and a permalink.
* **Readable and printable.** A light, high-contrast type scale with a print
  stylesheet, so browser "Save as PDF" still produces a clean document.

Usage::

    pip install markdown          # the only build-time dependency
    python docs/build_manual.py                     # -> docs/SysPerf-User-Manual.html
    python docs/build_manual.py --out other.html

The HTML file is a build artefact. Regenerate it after editing the Markdown; do
not edit the HTML directly.
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import date
from pathlib import Path

DOCS = Path(__file__).resolve().parent
SOURCE = DOCS / "USER_MANUAL.md"
DEFAULT_OUT = DOCS / "SysPerf-User-Manual.html"

VERSION = "2.0.0"
TITLE = "SysPerf"
SUBTITLE = "System Performance Agent"
TAGLINE = "A read-only performance dashboard and advisor for Windows 11"

CSS = """
:root{
  --bg:#f7f8fa; --panel:#ffffff; --ink:#1b2028; --muted:#5d6675; --line:#e2e6ec;
  --accent:#1f6feb; --accent-soft:#eaf2ff; --code-bg:#f2f4f8; --code-ink:#1f2b3d;
  --ok:#127a4b; --warn:#8a5a00; --bad:#b3261e;
  --sidebar:300px; --maxtext:78ch;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);
  font:16px/1.68 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased}

/* ---------- layout ---------- */
.layout{display:flex;align-items:flex-start;gap:0}
nav.toc{position:sticky;top:0;flex:0 0 var(--sidebar);width:var(--sidebar);
  height:100vh;overflow-y:auto;background:var(--panel);border-right:1px solid var(--line);
  padding:22px 18px 40px}
nav.toc .brand{font-weight:700;font-size:17px;letter-spacing:-.2px}
nav.toc .brand span{display:block;font-weight:400;font-size:12.5px;color:var(--muted);
  letter-spacing:.02em;text-transform:uppercase;margin-top:2px}
nav.toc .ver{margin:10px 0 16px;font-size:12px;color:var(--muted)}
nav.toc ol{list-style:none;margin:0;padding:0}
nav.toc li{margin:0}
nav.toc a{display:block;padding:5px 9px;border-radius:6px;color:var(--ink);
  text-decoration:none;font-size:13.5px;line-height:1.45}
nav.toc a:hover{background:var(--accent-soft);color:var(--accent)}
nav.toc li.lvl2 a{padding-left:22px;font-size:13px;color:var(--muted)}
nav.toc li.lvl1{margin-top:3px}
nav.toc li.lvl1 a{font-weight:600}

main{flex:1 1 auto;min-width:0;padding:0 0 80px}
.inner{max-width:calc(var(--maxtext) + 120px);margin:0 auto;padding:0 40px}

/* ---------- cover ---------- */
header.cover{background:linear-gradient(135deg,#12233d,#1c3557);color:#fff;
  padding:56px 40px 44px;margin-bottom:38px}
header.cover .wrap{max-width:calc(var(--maxtext) + 120px);margin:0 auto}
header.cover .mark{font-size:12.5px;text-transform:uppercase;letter-spacing:.14em;
  color:#9dc0f0}
header.cover h1{margin:8px 0 4px;font-size:44px;line-height:1.05;letter-spacing:-1px}
header.cover .tagline{margin:0;font-size:17px;color:#d5e3f7;max-width:60ch}
header.cover .meta{margin-top:20px;font-size:13px;color:#9dc0f0}
header.cover .claims{margin-top:24px;display:grid;gap:10px;
  grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
header.cover .claims div{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.14);
  border-radius:9px;padding:12px 14px;font-size:13.5px;color:#e8f0fb}
header.cover .claims b{color:#fff;display:block;margin-bottom:2px}

/* ---------- typography ---------- */
h2,h3,h4{line-height:1.25;scroll-margin-top:20px}
h2{margin:44px 0 14px;padding-bottom:9px;border-bottom:2px solid var(--line);
  font-size:27px;letter-spacing:-.4px}
h3{margin:30px 0 10px;font-size:19.5px}
h4{margin:22px 0 8px;font-size:16.5px;color:var(--muted);
  text-transform:uppercase;letter-spacing:.05em}
p,ul,ol{max-width:var(--maxtext)}
p{margin:0 0 14px}
ul,ol{margin:0 0 14px;padding-left:24px}
li{margin:4px 0}
a{color:var(--accent)}
strong{color:#111721}
hr{border:0;border-top:1px solid var(--line);margin:38px 0}
blockquote{margin:16px 0;padding:12px 18px;background:var(--accent-soft);
  border-left:4px solid var(--accent);border-radius:0 8px 8px 0;max-width:var(--maxtext)}
blockquote p:last-child{margin:0}

/* heading permalinks */
.anchor{float:left;margin-left:-.75em;padding-right:.25em;color:var(--line);
  text-decoration:none;font-weight:400;opacity:0}
h2:hover .anchor,h3:hover .anchor{opacity:1}

/* ---------- code ---------- */
code{font-family:"Cascadia Code",Consolas,"SF Mono",Menlo,monospace;font-size:13.5px;
  background:var(--code-bg);color:var(--code-ink);border:1px solid var(--line);
  border-radius:4px;padding:1px 5px}
pre{background:#0f1724;color:#e6edf7;border-radius:10px;padding:16px 18px;
  overflow-x:auto;margin:0 0 18px;font-size:13.5px;line-height:1.6;
  max-width:calc(var(--maxtext) + 8ch)}
pre code{background:none;border:0;color:inherit;padding:0;font-size:inherit}

/* ---------- tables ---------- */
.tablewrap{overflow-x:auto;margin:0 0 20px;max-width:calc(var(--maxtext) + 16ch)}
table{border-collapse:collapse;width:100%;font-size:14.5px;background:var(--panel);
  border:1px solid var(--line);border-radius:8px}
th{text-align:left;background:#eef1f6;color:#33405a;font-size:12.5px;
  text-transform:uppercase;letter-spacing:.04em;padding:10px 12px;
  border-bottom:1px solid var(--line)}
td{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
tbody tr:nth-child(even){background:#fafbfd}

/* ---------- footer ---------- */
footer{margin-top:52px;padding-top:18px;border-top:1px solid var(--line);
  color:var(--muted);font-size:13px}

/* ---------- responsive ---------- */
@media (max-width:1000px){
  .layout{display:block}
  nav.toc{position:static;width:auto;height:auto;border-right:0;
    border-bottom:1px solid var(--line);max-height:none}
  nav.toc ol{columns:2;column-gap:24px}
  .inner{padding:0 22px}
  header.cover{padding:40px 22px 32px}
  header.cover h1{font-size:34px}
}
@media (max-width:620px){ nav.toc ol{columns:1} }

/* ---------- print ---------- */
@media print{
  :root{--maxtext:100%}
  body{background:#fff;font-size:11pt}
  nav.toc{display:none}
  header.cover{background:#12233d !important;-webkit-print-color-adjust:exact;
    print-color-adjust:exact}
  .inner{max-width:none;padding:0}
  h2{page-break-after:avoid} pre,table,blockquote{page-break-inside:avoid}
  a{color:inherit;text-decoration:none}
  .anchor{display:none}
}
"""


def render_markdown(text: str) -> str:
    try:
        import markdown
    except ImportError:
        sys.exit("The 'markdown' package is required: pip install markdown")
    return markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "toc", "attr_list", "sane_lists",
                    "md_in_html"],
        output_format="html5",
    )


def build_toc(body_html: str) -> str:
    """Two-level contents list from the rendered ``h2``/``h3`` headings."""
    entries = re.findall(r'<h([23])\s+id="([^"]+)"[^>]*>(.*?)</h[23]>', body_html, re.S)
    items = []
    for level, anchor, raw in entries:
        text = re.sub(r"<[^>]+>", "", raw).strip()
        if not text or text.lower() == "contents":
            continue
        cls = "lvl1" if level == "2" else "lvl2"
        items.append(f'<li class="{cls}"><a href="#{anchor}">{html.escape(text)}</a></li>')
    return "<ol>" + "\n".join(items) + "</ol>"


def add_permalinks(body_html: str) -> str:
    """Give every h2/h3 a hover permalink, so sections are easy to share."""
    def sub(m):
        tag, attrs, inner = m.group(1), m.group(2), m.group(3)
        anchor = re.search(r'id="([^"]+)"', attrs)
        if not anchor:
            return m.group(0)
        link = f'<a class="anchor" href="#{anchor.group(1)}" aria-hidden="true">#</a>'
        return f"<{tag}{attrs}>{link}{inner}</{tag}>"
    return re.sub(r"<(h[23])([^>]*)>(.*?)</\1>", sub, body_html, flags=re.S)


def wrap_tables(body_html: str) -> str:
    """Wrap tables so wide ones scroll instead of breaking the layout."""
    return body_html.replace("<table>", '<div class="tablewrap"><table>') \
                    .replace("</table>", "</table></div>")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build the SysPerf user manual as self-contained HTML.")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output HTML path")
    args = ap.parse_args(argv)

    if not SOURCE.is_file():
        sys.exit(f"missing source: {SOURCE}")

    md = SOURCE.read_text(encoding="utf-8")
    # The document title and subtitle are replaced by the cover banner.
    md = re.sub(r"^# SysPerf User Manual\s*\n+(\*\*.*?\*\*\s*\n+)?"
                r"(A read-only.*?\n+)?---\s*\n", "", md, count=1, flags=re.S)

    body = wrap_tables(add_permalinks(render_markdown(md)))
    toc = build_toc(body)
    built = date.today().isoformat()

    document = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{TITLE} {VERSION} — User Manual</title>
<meta name="description" content="{html.escape(TAGLINE)}">
<meta name="generator" content="SysPerf docs/build_manual.py">
<style>{CSS}</style>
</head>
<body>
<header class="cover">
  <div class="wrap">
    <div class="mark">{SUBTITLE} &middot; User Manual</div>
    <h1>{TITLE}</h1>
    <p class="tagline">{TAGLINE}</p>
    <div class="claims">
      <div><b>Measures your machine</b>Baselines and targets come from your own RAM,
        storage and measured process floor — not someone else's laptop.</div>
      <div><b>Never changes a setting</b>Every system call is a query.
        Recommendations are yours to act on.</div>
      <div><b>Nothing leaves the machine</b>No telemetry, no accounts, no
        third-party scripts. It works offline.</div>
    </div>
    <div class="meta">Version {VERSION} &middot; Apache License 2.0 &middot;
      Windows 11 &middot; Python 3.10+ &middot; built {built}</div>
  </div>
</header>

<div class="layout">
  <nav class="toc" aria-label="Contents">
    <div class="brand">{TITLE}<span>User Manual</span></div>
    <div class="ver">Version {VERSION}</div>
    {toc}
  </nav>
  <main>
    <div class="inner">
      {body}
      <footer>
        {TITLE} {VERSION} — {SUBTITLE}. Apache License 2.0.
        Generated from <code>docs/USER_MANUAL.md</code> on {built}.
        Edit the Markdown and re-run <code>python docs/build_manual.py</code>;
        do not edit this file directly.
      </footer>
    </div>
  </main>
</div>
</body>
</html>
"""
    out = Path(args.out)
    out.write_text(document, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
