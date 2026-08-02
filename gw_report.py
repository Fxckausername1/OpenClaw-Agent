"""gw_report.py — shared HTML shell for Groundwork review docs.

Every agent's --html output uses this so the whole stack renders in one
consistent forest/sage/gold identity (matching deal_snapshot.py and the demo
Snapshots). Pure stdlib; agents build their inner sections and call page().
All outputs are DRAFTS for expert (Rey's) review — the footer says so.
"""
from html import escape

BRAND_CSS = """
:root{--forest:#2e4a3f;--sage:#7a9b76;--paper:#f7f5f0;--ink:#2b2b2b;--muted:#5d6b62;--gold:#b08641;--line:#e3ded3;
--green:#2e7d32;--greenbg:#e6f4e6;--amber:#9a7235;--amberbg:#f7efdd;--red:#b3261e;--redbg:#f7e4e2;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:var(--paper);color:var(--ink);line-height:1.5;padding:24px;}
.sheet{max-width:860px;margin:0 auto;background:#fff;border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:0 8px 30px rgba(46,74,63,.08);}
.top{background:var(--forest);color:#fff;padding:24px 28px;}
.brand{font-family:Georgia,serif;letter-spacing:3px;font-size:13px;color:var(--sage);}
.top h1{font-family:Georgia,serif;font-size:22px;margin-top:6px;}
.top .sub{font-size:13px;color:#bcccc2;margin-top:4px;}
.badge{display:inline-block;margin-top:14px;font-weight:700;font-size:15px;padding:7px 16px;border-radius:8px;}
.badge.green{background:var(--greenbg);color:var(--green);} .badge.amber{background:var(--amberbg);color:var(--amber);} .badge.red{background:var(--redbg);color:var(--red);}
.kphead{background:var(--gold);color:#fff;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;padding:7px 28px;font-weight:700;}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:1px;background:var(--line);}
.kpi{background:#fbfaf6;padding:16px 18px;text-align:center;}
.kl{display:block;font-size:11px;letter-spacing:.5px;text-transform:uppercase;color:var(--muted);}
.kv{display:block;font-family:Georgia,serif;font-size:24px;color:var(--forest);margin-top:4px;}
.body{padding:20px 28px;} .sec{margin-bottom:22px;}
.stage{background:var(--forest);color:var(--sage);font-family:Georgia,serif;letter-spacing:1.5px;font-size:12px;text-transform:uppercase;padding:9px 28px;margin:6px -28px 18px;font-weight:700;}
.stage:first-child{margin-top:-20px;}
.sec h2{font-family:Georgia,serif;color:var(--forest);font-size:14px;text-transform:uppercase;letter-spacing:1px;border-bottom:2px solid var(--line);padding-bottom:6px;margin-bottom:10px;}
.r{display:flex;justify-content:space-between;gap:14px;padding:7px 0;border-bottom:1px dashed var(--line);font-size:14px;}
.r:last-child{border-bottom:none;} .r .v{font-weight:700;color:var(--forest);} .r .v.warn{color:var(--gold);} .r .v.tot{border-top:2px solid var(--forest);}
table{width:100%;border-collapse:collapse;font-size:13.5px;} th,td{padding:10px 12px;text-align:right;border-bottom:1px solid var(--line);}
th{background:#243a31;color:#fff;font-family:Georgia,serif;font-weight:normal;font-size:12px;letter-spacing:.4px;}
td.l,th.l{text-align:left;} td.nm{font-weight:700;color:var(--forest);text-align:left;} tr.top1 td{background:#eef6ee;}
ul{margin:4px 0 0 18px;font-size:13.5px;} li{margin-bottom:6px;}
ul.err li{color:var(--red);} ul.warn li{color:var(--amber);} ul.note li{color:var(--muted);}
.draftcard{border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:14px;background:#fbfaf6;}
.draftcard h3{font-family:Georgia,serif;color:var(--forest);font-size:15px;margin-bottom:8px;}
.draftcard pre{white-space:pre-wrap;font-family:inherit;font-size:13.5px;color:var(--ink);margin:6px 0;}
.draftcard .lbl{font-size:10px;letter-spacing:1px;text-transform:uppercase;color:var(--gold);font-weight:700;margin-top:10px;}
.foot{background:#faf8f3;padding:14px 28px;font-size:11.5px;color:var(--muted);border-top:1px solid var(--line);font-style:italic;}
"""

DEFAULT_FOOT = ("DRAFT for expert review. Every figure is an ESTIMATE pending verification "
                "against primary sources. Not tax, legal, or investment advice.")


def page(kicker, title, subtitle, body_html, badge=None, kpis=None, foot=None):
    """badge=(text, 'green'|'amber'|'red'); kpis=[(label,value),...]."""
    badge_html = f'<div class="badge {badge[1]}">{escape(badge[0])}</div>' if badge else ""
    kp = ""
    if kpis:
        tiles = "".join(f'<div class="kpi"><span class="kl">{escape(str(l))}</span>'
                        f'<span class="kv">{escape(str(v))}</span></div>' for l, v in kpis)
        kp = f'<div class="kpis">{tiles}</div>'
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Groundwork — {escape(title)}</title>
<style>{BRAND_CSS}</style></head><body><div class="sheet">
<div class="top"><div class="brand">{escape(kicker)}</div><h1>{escape(title)}</h1>
<div class="sub">{escape(subtitle)}</div>{badge_html}</div>
{kp}
<div class="body">{body_html}</div>
<div class="foot">{escape(foot or DEFAULT_FOOT)}</div>
</div></body></html>"""


def section(title, inner_html):
    return f'<div class="sec"><h2>{escape(title)}</h2>{inner_html}</div>'


def stage(label):
    """Full-width divider that splits a multi-agent packet into stages."""
    return f'<div class="stage">{escape(label)}</div>'


def ul(items, cls="note"):
    lis = "".join(f"<li>{escape(str(x))}</li>" for x in items)
    return f'<ul class="{cls}">{lis}</ul>'


def kv_rows(pairs):
    """pairs=[(label, value, cls)]; cls in {'', 'warn', 'tot'}."""
    out = ""
    for label, value, *rest in pairs:
        cls = rest[0] if rest else ""
        out += (f'<div class="r"><span>{escape(str(label))}</span>'
                f'<span class="v {cls}">{escape(str(value))}</span></div>')
    return out


def table(headers, rows, top1=False):
    """headers=[(text, align)] align in {'l','r'}; rows=[[cells...]] (HTML-escaped here).
    Cell may be a tuple (text, css_class) to set td class."""
    ths = "".join(f'<th class="{a}">{escape(str(h))}</th>' for h, a in headers)
    trs = ""
    for i, row in enumerate(rows):
        tds = ""
        for cell in row:
            if isinstance(cell, tuple):
                txt, cls = cell
                tds += f'<td class="{cls}">{escape(str(txt))}</td>'
            else:
                tds += f"<td>{escape(str(cell))}</td>"
        cls = ' class="top1"' if (top1 and i == 0) else ""
        trs += f"<tr{cls}>{tds}</tr>"
    return f"<table><thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table>"
