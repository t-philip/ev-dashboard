"""The shared look of every page: one stylesheet and one page frame.

Plain CSS in the page itself (the policy allows inline styles, no scripts and nothing from outside):
a warm neutral page, ruled stat tiles, a red accent and a dark mode that follows the system setting.
Fonts are the system's own; nothing is fetched. Status is never shown by colour alone: every tile
says it in words as well.
"""
from __future__ import annotations

import html
from typing import Iterable, Optional, Tuple

CSS = """
:root{--bg:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink-2:#52514e;--ink-3:#898781;--rule:#e1e0d9;--rule-soft:#eeede7;
--accent:#d6371f;--accent-dark:#a82a17;--good:#0a7d3c;--warn:#a86800;--bad:#c0392b;--good-bg:#e7f4ec;--warn-bg:#fdf1dc;--bad-bg:#fbe9e6;
--blue:#2a78d6;--font:"Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink-2:#c3c2b7;--ink-3:#898781;--rule:#2c2c2a;
--rule-soft:#222221;--accent:#ff6a4d;--accent-dark:#ff8a72;--good:#3ecf7a;--warn:#f2b441;--bad:#ff7a6b;--good-bg:#12281b;
--warn-bg:#2b2210;--bad-bg:#2e1714;--blue:#6aa7f0;color-scheme:dark}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 var(--font)}
a{color:var(--accent-dark)}
.wrap{max-width:72rem;margin:0 auto;padding:0 24px 48px}
header.top{padding:22px 0 0;display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:12px}
header.top h1{margin:0 0 2px;font-size:1.75rem;font-weight:900;letter-spacing:-.01em;line-height:1.15}
header.top .sub{margin:0;color:var(--ink-2);font-size:.9rem}
.right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
nav.tabs{display:flex;gap:4px;border-bottom:2px solid var(--rule);margin:16px 0 22px}
nav.tabs a{padding:8px 14px;text-decoration:none;color:var(--ink-2);font-weight:600;border-bottom:3px solid transparent;margin-bottom:-2px}
nav.tabs a:hover{color:var(--ink)}
nav.tabs a[aria-current=page]{color:var(--ink);border-bottom-color:var(--accent)}
.pill{display:inline-flex;align-items:center;gap:7px;padding:4px 12px;border:1px solid var(--rule);background:var(--surface);
font-size:.8rem;font-weight:700;text-transform:uppercase;letter-spacing:.03em}
.dot{width:9px;height:9px;border-radius:50%;background:var(--ink-3);display:inline-block;flex:none}
.good .dot,.dot.good{background:var(--good)}.warn .dot,.dot.warn{background:var(--warn)}.bad .dot,.dot.bad{background:var(--bad)}
.btn{display:inline-block;padding:8px 16px;background:var(--accent);color:#fff;border:0;font:inherit;font-weight:700;text-decoration:none;cursor:pointer}
.btn:hover{background:var(--accent-dark)}
.btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--ink-3)}
.banner{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px;margin:0 0 22px;
border-left:5px solid var(--ink-3);background:var(--surface);border-top:1px solid var(--rule);border-right:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.banner.bad{border-left-color:var(--bad);background:var(--bad-bg)}.banner.warn{border-left-color:var(--warn);background:var(--warn-bg)}
.banner strong{display:block}.banner p{margin:2px 0 0;color:var(--ink-2);font-size:.92rem}
h2.sec{margin:28px 0 10px;font-size:.78rem;font-weight:800;text-transform:uppercase;letter-spacing:.07em;color:var(--ink-2)}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:2px;background:var(--rule);
border-top:2px solid var(--rule);border-bottom:1px solid var(--rule)}
.tile{background:var(--surface);padding:14px 16px;display:flex;flex-direction:column;min-height:112px}
.tile .label{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--ink-2);margin-bottom:6px}
.tile .value{font-size:2rem;font-weight:800;line-height:1.1;font-variant-numeric:tabular-nums;margin:auto 0}
.tile .value.small{font-size:1.35rem}
.tile .unit{font-size:.875rem;font-weight:600;margin-left:3px;color:var(--ink-2)}
.tile .note{font-size:.8rem;color:var(--ink-2);margin-top:6px}
.tile .state{display:flex;align-items:center;gap:8px;font-size:1.25rem;font-weight:800;margin:auto 0}
.tile.warn{background:var(--warn-bg)}.tile.bad{background:var(--bad-bg)}
.tile.warn .label{color:var(--warn)}.tile.bad .label{color:var(--bad)}
.bar{position:relative;height:8px;background:var(--rule-soft);margin-top:8px;border:1px solid var(--rule)}
.bar>i{position:absolute;inset:0 auto 0 0;display:block}
.bar>b{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--ink)}
.fresh{margin:22px 0 0;color:var(--ink-2);font-size:.88rem}
.fresh .stale{color:var(--warn);font-weight:700}
details{margin-top:22px;border:1px solid var(--rule);background:var(--surface)}
details>summary{cursor:pointer;padding:10px 16px;font-weight:700;color:var(--ink-2)}
details[open]>summary{border-bottom:1px solid var(--rule)}
details .inner{padding:6px 16px 14px;overflow-x:auto}
details h3{margin:14px 0 4px;font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-2)}
table{border-collapse:collapse;width:100%}
th,td{padding:7px 14px 7px 0;text-align:left;vertical-align:top;border-bottom:1px solid var(--rule-soft);font-size:.92rem}
th{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-2);border-bottom:1px solid var(--rule)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
table.kv th{width:16rem;max-width:45%;text-transform:none;letter-spacing:0;font-size:.92rem;font-weight:600;color:var(--ink-2);border-bottom:1px solid var(--rule-soft)}
details table{table-layout:fixed}
details td:nth-child(1){width:36%;overflow-wrap:anywhere}details td:nth-child(2){width:24%;overflow-wrap:anywhere}
code{font-size:.88em}
.muted{color:var(--ink-3)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}
.chips a,.chips strong{padding:5px 12px;border:1px solid var(--rule);background:var(--surface);text-decoration:none;color:var(--ink-2);font-weight:600;font-size:.9rem}
.chips strong{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.chips a:hover{border-color:var(--ink-3);color:var(--ink)}
.card{background:var(--surface);border:1px solid var(--rule);padding:16px;margin-top:14px}
.tiles.three{grid-template-columns:repeat(3,1fr)}
@media (max-width:900px){.tiles{grid-template-columns:repeat(2,1fr)}}
@media (max-width:640px){.tiles.three{grid-template-columns:1fr}}
@media (max-width:560px){.wrap{padding:0 14px 36px}.tile .value{font-size:1.7rem}header.top h1{font-size:1.45rem}}
"""


def pill(kind: str, text: str) -> str:
    """A small status label: ``kind`` is good, warn, bad or empty for neutral."""
    return f'<span class="pill {kind}"><span class="dot"></span>{html.escape(text)}</span>'


def shell(title: str, body: str, *, active: str = "", show_trips: bool = True, subtitle: str = "",
          right: str = "", heading: Optional[str] = None) -> str:
    """The page frame: header, tab bar and the stylesheet. ``body`` is trusted markup built by the caller."""
    tabs: Iterable[Tuple[str, str, str]] = [("home", "/", "Overview")]
    if show_trips:
        tabs = [*tabs, ("trips", "/trips", "Trips")]
    nav = "".join(f'<a href={href}{" aria-current=page" if key == active else ""}>{label}</a>'
                  for key, href, label in tabs)
    sub = f'<p class="sub">{html.escape(subtitle)}</p>' if subtitle else ""
    return ("<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style>"
            f'<div class="wrap"><header class="top"><div><h1>{html.escape(heading or title)}</h1>{sub}</div>'
            f'<div class="right">{right}</div></header><nav class="tabs">{nav}</nav>'
            f"{body}</div></html>")
