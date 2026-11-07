"""Daily ops report: Markdown (for chat / Slack / email) and HTML (standalone file)."""
from __future__ import annotations

import html
import sqlite3
from pathlib import Path

from .engine import kpi_trend, kpis
from .rules import RULES_BY_CODE, SEVERITY_ORDER, inr


def _issues(conn: sqlite3.Connection, limit: int = 15):
    rows = conn.execute("SELECT * FROM issues WHERE status != 'resolved'").fetchall()
    rows = sorted(rows, key=lambda r: (SEVERITY_ORDER[r["severity"]], -r["amount_at_risk"]))
    by_rule: dict[str, dict] = {}
    for r in rows:
        b = by_rule.setdefault(r["rule"], {"count": 0, "amount": 0.0, "severity": r["severity"]})
        b["count"] += 1
        b["amount"] += r["amount_at_risk"]
        if SEVERITY_ORDER[r["severity"]] < SEVERITY_ORDER[b["severity"]]:
            b["severity"] = r["severity"]
    by_rule = dict(sorted(by_rule.items(), key=lambda kv: (SEVERITY_ORDER[kv[1]["severity"]], -kv[1]["amount"])))
    new_today = [r for r in rows if r["first_seen"] == r["last_seen"]]
    return rows[:limit], by_rule, new_today


def _fmt(v, suffix=""):
    return "n/a" if v is None else f"{v}{suffix}"


def markdown(conn: sqlite3.Connection) -> str:
    k = kpis(conn)
    top, by_rule, new_today = _issues(conn, 10)
    lines = [f"# Ops daily report: {k['as_of']}", "",
             f"**{k['open_issues']} open issues · {inr(k['amount_at_risk_inr'])} at risk · {len(new_today)} new today**", "",
             "## KPIs (trailing 30 days)",
             f"- Deposit collection rate: {_fmt(k['deposit_collection_rate_pct'], '%')} (on time {_fmt(k['on_time_collection_pct'], '%')})",
             f"- Reconciliation match rate: {_fmt(k['reconciliation_match_rate_pct'], '%')}",
             f"- Invoice coverage after visa approval: {_fmt(k['invoice_coverage_pct'], '%')} (avg {_fmt(k['avg_visa_to_invoice_days'], 'd')} to invoice)",
             f"- Commission realisation: {_fmt(k['commission_realisation_pct'], '%')}", "",
             "## Issues by type"]
    for code, b in by_rule.items():
        lines.append(f"- **{RULES_BY_CODE[code].title}** [{b['severity']}]: {b['count']} cases, {inr(b['amount'])}")
    lines += ["", "## Top issues"]
    for r in top:
        lines.append(f"- `{r['issue_id']}` [{r['severity']}] {r['summary']}")
    return "\n".join(lines)


def _bars_svg(trend: list[dict]) -> str:
    w, h, pad = 560, 140, 24
    mx = max((t["settlements"] for t in trend), default=1) or 1
    bw = (w - pad) / len(trend)
    bars = []
    for i, t in enumerate(trend):
        bh = (h - 30) * t["settlements"] / mx
        x = pad + i * bw
        low = t["settlements"] < 0.5 * (sum(x["settlements"] for x in trend) / len(trend))
        bars.append(f'<rect x="{x + 3:.1f}" y="{h - 18 - bh:.1f}" width="{bw - 6:.1f}" height="{bh:.1f}" '
                    f'class="{"bar low" if low else "bar"}"><title>{t["date"]}: {t["settlements"]}</title></rect>'
                    f'<text x="{x + bw / 2:.1f}" y="{h - 4}" class="ax">{t["date"][8:]}</text>'
                    f'<text x="{x + bw / 2:.1f}" y="{h - 22 - bh:.1f}" class="ax">{t["settlements"]}</text>')
    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Daily settlements, last 14 days">{"".join(bars)}</svg>'


def to_html(conn: sqlite3.Connection) -> str:
    k = kpis(conn)
    top, by_rule, new_today = _issues(conn, 20)
    e = html.escape
    tiles = [("Open issues", k["open_issues"]), ("Amount at risk", f"{inr(k['amount_at_risk_inr'])}"),
             ("Collection rate", _fmt(k["deposit_collection_rate_pct"], "%")),
             ("Recon match rate", _fmt(k["reconciliation_match_rate_pct"], "%")),
             ("Invoice coverage", _fmt(k["invoice_coverage_pct"], "%")),
             ("Commission realised", _fmt(k["commission_realisation_pct"], "%"))]
    rule_rows = "".join(
        f"<tr><td>{e(RULES_BY_CODE[c].title)}</td><td><span class='sev {b['severity']}'>{b['severity']}</span></td>"
        f"<td class='n'>{b['count']}</td><td class='n'>{inr(b['amount'])}</td><td>{e(RULES_BY_CODE[c].why_it_matters)}</td></tr>"
        for c, b in by_rule.items())
    issue_rows = "".join(
        f"<tr><td><code>{e(r['issue_id'])}</code></td><td><span class='sev {r['severity']}'>{r['severity']}</span></td>"
        f"<td>{e(r['summary'])}</td><td>{e(r['status'])}</td><td>{e(r['first_seen'])}</td></tr>" for r in top)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Ops Daily Report {k['as_of']}</title>
<style>
:root{{--bg:#fff;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--tile:#f6f8fa;--bar:#4c6ef5;--low:#d9480f;
--crit:#a61e4d;--high:#d9480f;--med:#b08800;--lowsev:#57606a}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--tile:#161b22;--bar:#748ffc;--low:#ff8c42}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Arial,sans-serif}}
main{{max-width:1000px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 8px}} .muted{{color:var(--muted)}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:16px}}
.tile{{background:var(--tile);border:1px solid var(--line);border-radius:6px;padding:10px 12px}}
.tile b{{display:block;font-size:20px}} .tile span{{color:var(--muted);font-size:12px}}
.scroll{{overflow-x:auto}} table{{border-collapse:collapse;width:100%}} th,td{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}
th{{font-size:12px;color:var(--muted);font-weight:600}} td.n{{text-align:right;white-space:nowrap}}
.sev{{font-size:11px;font-weight:600;text-transform:uppercase}} .critical{{color:var(--crit)}} .high{{color:var(--high)}} .medium{{color:var(--med)}} .low{{color:var(--lowsev)}}
svg{{width:100%;max-width:560px;height:auto}} .bar{{fill:var(--bar)}} .bar.low{{fill:var(--low)}} .ax{{font-size:9px;fill:var(--muted);text-anchor:middle}}
code{{font-size:12px}}
</style></head><body><main>
<h1>Ops daily report</h1><div class="muted">As of {k['as_of']} · trailing {k['window_days']} days · {len(new_today)} new issues today · synthetic data</div>
<div class="tiles">{"".join(f"<div class='tile'><b>{v}</b><span>{e(t)}</span></div>" for t, v in tiles)}</div>
<h2>Daily settlements (last 14 days)</h2>{_bars_svg(kpi_trend(conn))}
<h2>Issues by type</h2><div class="scroll"><table><tr><th>Check</th><th>Worst</th><th>Cases</th><th>At risk</th><th>Why it matters</th></tr>{rule_rows}</table></div>
<h2>Top open issues</h2><div class="scroll"><table><tr><th>ID</th><th>Severity</th><th>Summary</th><th>Status</th><th>First seen</th></tr>{issue_rows}</table></div>
</main></body></html>"""


def write_html(conn: sqlite3.Connection, out_dir: str | Path = "reports") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    k = kpis(conn)
    path = out / f"ops-report-{k['as_of']}.html"
    path.write_text(to_html(conn), encoding="utf-8")
    return path
