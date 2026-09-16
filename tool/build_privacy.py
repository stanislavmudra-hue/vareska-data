"""Render the app's docs/PRIVACY.md (Vareska repo) to docs/privacy.html.

Usage (from the vareska-data root):
    python tool/build_privacy.py [path/to/PRIVACY.md]

The default source is ../Jidlo/docs/PRIVACY.md (the app repo next to this
one; C:/AI/Jídlo on the maintainer's machine). The page is what the app's
sign-up consent links to (kPrivacyPolicyUrl =
https://okolnik.cz/vareska-data/privacy.html), so re-run it whenever the
Markdown changes and commit docs/privacy.html.
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "privacy.html"
DEFAULT_SRC = [
    ROOT.parent / "Jídlo" / "docs" / "PRIVACY.md",
    ROOT.parent / "Jidlo" / "docs" / "PRIVACY.md",
]

CSS = """
:root { --bg:#f6f7f9; --surface:#fff; --text:#1c1e21; --text-2:#5c6370; --border:#dfe3ea; --primary:#b3410f; --code:#eef0f4; }
@media (prefers-color-scheme: dark) { :root { --bg:#121417; --surface:#1c1f24; --text:#e8eaed; --text-2:#9aa0a6; --border:#343a43; --primary:#f2a07b; --code:#262a31; } }
* { box-sizing: border-box; }
body { margin:0; font-family:"Segoe UI", Roboto, system-ui, sans-serif; font-size:16px; line-height:1.55; color:var(--text); background:var(--bg); }
main { max-width: 860px; margin: 0 auto; padding: 24px 16px 48px; }
article { background: var(--surface); border:1px solid var(--border); border-radius:14px; padding: 24px; }
h1 { font-size: 28px; margin: 0 0 12px; }
h2 { font-size: 20px; margin: 28px 0 8px; border-bottom: 1px solid var(--border); padding-bottom: 4px; }
p, li { margin: 0 0 8px; }
a { color: var(--primary); }
code { background: var(--code); padding: 1px 5px; border-radius: 5px; font-size: .92em; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 14px; font-size: 14.5px; display:block; overflow-x:auto; }
th, td { border: 1px solid var(--border); padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: var(--code); }
hr { border: 0; border-top: 1px solid var(--border); margin: 28px 0; }
footer { text-align:center; color: var(--text-2); font-size: 13px; padding: 12px; }
"""


def inline(md: str) -> str:
    """Escape, then bold / code / links (the only inline syntax PRIVACY.md uses)."""
    s = html.escape(md, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"(?<![\"'>=])(https://[^\s<)]+)", r'<a href="\1">\1</a>', s)
    return s


def render(md: str) -> str:
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(inline(x) for x in para) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_para()
            i += 1
            continue
        if stripped.startswith("---"):
            flush_para()
            out.append("<hr>")
            i += 1
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if stripped.startswith("|"):
            flush_para()
            rows: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            body = [r for r in cells if not all(re.fullmatch(r":?-+:?", c or "-") for c in r)]
            if not body:
                continue
            head, rest = body[0], body[1:]
            t = ["<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"]
            for r in rest:
                t.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
            continue
        if stripped.startswith("- "):
            flush_para()
            items: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(lines[i].strip()[2:])
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
            continue
        para.append(stripped)
        i += 1
    flush_para()
    return "\n".join(out)


def build(src: Path) -> str:
    body = render(src.read_text(encoding="utf-8"))
    return (
        "<!DOCTYPE html>\n<html lang=\"cs\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<meta name=\"color-scheme\" content=\"light dark\">\n"
        "<title>Zásady ochrany soukromí – Vareska</title>\n"
        f"<style>{CSS}</style>\n</head>\n<body>\n<main>\n<article>\n{body}\n</article>\n</main>\n"
        "<footer>Vareska · generováno z docs/PRIVACY.md aplikace (tool/build_privacy.py)</footer>\n"
        "</body>\n</html>\n"
    )


def main(argv: list[str]) -> int:
    src = Path(argv[1]) if len(argv) > 1 else next((p for p in DEFAULT_SRC if p.exists()), None)
    if src is None or not src.exists():
        print("PRIVACY.md not found; pass the path as the first argument", file=sys.stderr)
        return 1
    OUT.write_text(build(src), encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({OUT.stat().st_size} B) from {src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
