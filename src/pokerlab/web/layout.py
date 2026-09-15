"""One shell for every page: sidebar, header, and the shared stylesheet.

Server-rendered on purpose. The pages are tables of numbers with provenance
attached, and a template string per page keeps the number and its caveat in
the same place in the code.
"""

from __future__ import annotations

import html

from fastapi.responses import HTMLResponse

NAV = (
    ("/", "Overview"),
    ("/review", "Review"),
    ("/hands", "Hands"),
    ("/villains", "Villains"),
    ("/postflop", "Postflop"),
    ("/preflop", "Preflop"),
    ("/sessions", "Sessions"),
    ("/grades", "Grading"),
    ("/train", "Drill"),
    ("/ranges", "Ranges"),
)

CSS = """
:root{--bg:#f6f5f2;--fg:#1a1a1a;--dim:#6b6b6b;--line:#e2e0dc;--card:#fff;
--raise:#2f7d4f;--call:#2d5b8a;--fold:#a33a3a;--foldbg:#e2e0dc;--accent:#2d5b8a;
--face:#fffefb;--edge:#d5d2cc;--ink:#1a1a1a;--red:#c0392b;--side:#eeece8;
--felt:#e6e3dc;--cardback:#8a9bb0;--chip:#dcd8d0;--warn:#9a6b12;--warnbg:#fbf3e0;--check:#9aa3ad}
@media(prefers-color-scheme:dark){:root{--bg:#141517;--fg:#e8e6e3;--dim:#9a9a9a;
--line:#2c2e33;--card:#1c1e22;--raise:#5fb37f;--call:#6fa3d8;--fold:#d16a6a;
--foldbg:#2c2e33;--accent:#6fa3d8;--face:#f3f0ea;--edge:#42454c;--ink:#17181b;
--red:#b02f22;--side:#1a1b1e;--felt:#1b1d21;--cardback:#3d4756;--chip:#2c2e33;
--warn:#d9a441;--warnbg:#2a2418;--check:#5c6570}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,
BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;display:flex;min-height:100vh}
a{color:var(--accent)}
.side{width:190px;flex:0 0 190px;background:var(--side);border-right:1px solid var(--line);
padding:1.25rem 0;position:sticky;top:0;height:100vh}
.side .brand{font-weight:700;letter-spacing:-.01em;padding:0 1.25rem .9rem;font-size:1rem}
.side .brand span{color:var(--dim);font-weight:400;font-size:.72rem;display:block}
.side a{display:block;padding:.45rem 1.25rem;color:var(--fg);text-decoration:none;
font-size:.9rem}
.side a:hover{background:var(--line)}
.side a.on{background:var(--card);font-weight:600;border-right:2px solid var(--accent)}
.main{flex:1;min-width:0;padding:1.5rem 2rem 3rem}
.wrap{max-width:1080px}
.wrap.wide{max-width:1080px}
h1{font-size:1.25rem;margin:0 0 .15rem;font-weight:650;letter-spacing:-.01em}
h2{font-size:.95rem;margin:1.6rem 0 .6rem;font-weight:650}
h2:first-child{margin-top:0}
.sub{color:var(--dim);font-size:.82rem;margin-bottom:1.1rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:1.1rem 1.25rem;margin-bottom:1rem;overflow-x:auto}
.card h2{margin-top:0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;
margin-bottom:1rem}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:.8rem 1rem}
.tile .k{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
.tile .v{font-size:1.35rem;font-weight:650;font-variant-numeric:tabular-nums;
letter-spacing:-.01em;margin:.1rem 0}
.tile .n{font-size:.74rem;color:var(--dim)}
.pos{color:var(--raise)} .neg{color:var(--fold)} .win{color:var(--raise)} .lose{color:var(--fold)}
table.t{width:100%;border-collapse:collapse;font-size:.83rem}
table.t th{text-align:right;font-weight:600;color:var(--dim);font-size:.68rem;
text-transform:uppercase;letter-spacing:.05em;padding:.35rem .5rem;
border-bottom:1px solid var(--line);white-space:nowrap}
table.t td{padding:.35rem .5rem;border-bottom:1px solid var(--line);text-align:right;
font-variant-numeric:tabular-nums;white-space:nowrap}
table.t th:first-child,table.t td:first-child,table.t .l{text-align:left}
table.t tr:last-child td{border-bottom:0}
table.t tr:hover td{background:var(--bg)}
table.t a{text-decoration:none}
table.hands{width:100%;border-collapse:collapse;font-size:.82rem}
table.hands th{text-align:left;font-weight:600;color:var(--dim);font-size:.68rem;
text-transform:uppercase;letter-spacing:.05em;padding:.4rem .5rem;
border-bottom:1px solid var(--line)}
table.hands td{padding:.35rem .5rem;border-bottom:1px solid var(--line);
font-variant-numeric:tabular-nums}
table.hands tr:hover td{background:var(--bg)}
table.hands a{text-decoration:none}
.dimrow td{color:var(--dim)}
.bar{display:inline-block;height:8px;background:var(--accent);border-radius:2px;
vertical-align:middle;margin-right:.4rem;opacity:.75}
.bar.g{background:var(--raise)} .bar.r{background:var(--fold)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78rem}
.tag{font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);
border:1px solid var(--line);border-radius:99px;padding:.1rem .55rem;display:inline-block;
margin:0 .25rem .25rem 0;white-space:nowrap}
.badge{display:inline-block;font-size:.64rem;text-transform:uppercase;letter-spacing:.06em;
font-weight:700;padding:.08rem .45rem;border-radius:99px;border:1px solid currentColor;
white-space:nowrap}
.b-ok{color:var(--raise)} .b-no{color:var(--fold)} .b-mid{color:var(--call)}
.b-dim{color:var(--dim)}
.note{color:var(--dim);font-size:.8rem;line-height:1.45}
.warn{background:var(--warnbg);color:var(--warn);border-radius:8px;padding:.6rem .9rem;
font-size:.82rem;margin-bottom:1rem}
.filter{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;font-size:.8rem;
color:var(--dim);margin-bottom:1rem}
.filter label{display:flex;gap:.3rem;align-items:center}
select,input[type=text],input[type=number]{font:inherit;font-size:.8rem;padding:.3rem .45rem;
border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
button{padding:.35rem .8rem;font:inherit;font-size:.8rem;font-weight:600;border-radius:7px;
border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent)}
button.r{color:var(--raise)} button.c{color:var(--call)} button.f{color:var(--fold)}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.btns{display:flex;gap:.6rem;margin-top:1rem}
.btns button{flex:1;padding:.8rem;font-size:.95rem}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:1rem;align-items:start}
.topnav{display:none}
@media(max-width:900px){.cols{grid-template-columns:1fr}.side{display:none}.main{padding:1rem}
body{flex-direction:column}
.topnav{display:flex;gap:.2rem;overflow-x:auto;padding:.5rem .6rem;background:var(--side);
border-bottom:1px solid var(--line);position:sticky;top:0;z-index:2;-webkit-overflow-scrolling:touch}
.topnav a{white-space:nowrap;padding:.35rem .6rem;border-radius:6px;text-decoration:none;
color:var(--fg);font-size:.82rem}
.topnav a.on{background:var(--card);font-weight:600}
.btns{flex-wrap:wrap}.btns button{min-width:44%;padding:.9rem .5rem}
.grids{flex-direction:column}
table.grid td{width:20px;height:20px;font-size:.5rem}}
.spark{vertical-align:middle}
.cards{display:flex;gap:.5rem}
.pc{width:60px;height:84px;border-radius:7px;background:var(--face);
border:1px solid var(--edge);box-shadow:0 1px 3px rgba(0,0,0,.2);position:relative;
display:flex;align-items:center;justify-content:center;font-weight:700}
.pc .rk{position:absolute;top:4px;left:6px;font-size:.95rem;line-height:1}
.pc .pip{font-size:2rem;line-height:1}
.pc .lo{position:absolute;bottom:4px;right:6px;font-size:.8rem;line-height:1;
transform:rotate(180deg)}
.black{color:var(--ink)} .red{color:var(--red)}
.mini{display:inline-flex;gap:2px;vertical-align:middle}
.mini .c{display:inline-block;min-width:22px;padding:0 3px;height:20px;line-height:20px;
border-radius:3px;background:var(--face);border:1px solid var(--edge);text-align:center;
font-weight:700;font-size:.72rem;font-family:ui-monospace,Menlo,monospace}
.verdict{font-weight:600;margin-bottom:.4rem}
.ok{color:var(--raise)} .no{color:var(--fold)} .mix{color:var(--call)}
.dist{font-size:.85rem;font-variant-numeric:tabular-nums;margin:.2rem 0}
.score{color:var(--dim);font-size:.85rem;font-variant-numeric:tabular-nums}
.src{font-size:.72rem;color:var(--dim);margin-top:1rem;line-height:1.45}
.conf{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;font-weight:700}
.spot{display:flex;align-items:center;gap:1rem;flex-wrap:wrap}
table.grid{border-collapse:collapse;margin-top:1rem}
table.grid td{width:26px;height:26px;text-align:center;font-size:.6rem;
border:1px solid var(--bg);color:var(--dim);font-weight:600}
td.lit{color:#fff;text-shadow:0 0 2px rgba(0,0,0,.5)}
table.rg td.out{background:transparent;color:var(--line)}
table.rg.sm td{width:22px;height:22px;font-size:.52rem}
.grids{display:flex;gap:1.5rem;flex-wrap:wrap;align-items:flex-start}
.grids>div{min-width:0}
.grids .gt{font-size:.78rem;font-weight:600;margin-bottom:.2rem}
td.here{outline:2px solid var(--fg);outline-offset:-2px}
.legend{display:flex;gap:1rem;font-size:.72rem;color:var(--dim);margin-top:.6rem;flex-wrap:wrap}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:.3rem;
vertical-align:-1px}
.progress{height:8px;background:var(--line);border-radius:99px;overflow:hidden;margin:.5rem 0}
.progress i{display:block;height:100%;background:var(--accent)}
"""

SUIT_PIP = {"c": "&clubs;", "d": "&diams;", "h": "&hearts;", "s": "&spades;"}
SUIT_INK = {"c": "black", "s": "black", "d": "red", "h": "red"}


def page(body: str, title: str, active: str = "", sub: str = "",
         extra_css: str = "", wide: bool = False) -> HTMLResponse:
    nav = "".join(
        f'<a href="{href}" class="{"on" if href == active else ""}">{label}</a>'
        for href, label in NAV)
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>pokerlab · {html.escape(title)}</title>"
        f"<style>{CSS}{extra_css}</style></head><body>"
        f"<aside class='side'><div class='brand'>pokerlab<span>Betclic NL5 · Deshoke</span></div>"
        f"{nav}</aside><nav class='topnav'>{nav}</nav>"
        f"<div class='main'><div class='wrap{' wide' if wide else ''}'>"
        f"<h1>{html.escape(title)}</h1><div class='sub'>{sub}</div>"
        f"{body}</div></div></body></html>")


def mini_cards(cards: str | tuple[str, ...]) -> str:
    """Compact card glyphs for tables: 'Ah Kd' -> two little tiles."""
    items = cards.split() if isinstance(cards, str) else list(cards)
    if not items:
        return '<span class="note">--</span>'
    out = []
    for c in items:
        if len(c) < 2:
            continue
        out.append(f'<span class="c {SUIT_INK.get(c[1], "black")}">{html.escape(c[0])}'
                   f'{SUIT_PIP.get(c[1], "")}</span>')
    return f'<span class="mini">{"".join(out)}</span>'


def money(cents: int) -> str:
    return f"€{cents / 100:.2f}"


def signed(value: float, unit: str = "", digits: int = 1) -> str:
    cls = "pos" if value > 0 else "neg" if value < 0 else ""
    return f'<span class="{cls}">{value:+.{digits}f}{unit}</span>'


def pct(x: float | None, digits: int = 0) -> str:
    return "--" if x is None else f"{100 * x:.{digits}f}%"


def bar(x: float | None, width: int = 60, cls: str = "") -> str:
    if x is None:
        return ""
    return f'<i class="bar {cls}" style="width:{max(2, int(width * min(max(x, 0), 1)))}px"></i>'


def sparkline(values: list[float], width: int = 120, height: int = 26) -> str:
    """Inline SVG line, theme-coloured via currentColor."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = width / (len(vals) - 1)
    pts = " ".join(f"{i * step:.1f},{height - 2 - (v - lo) / span * (height - 4):.1f}"
                   for i, v in enumerate(vals))
    return (f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{pts}"/></svg>')
