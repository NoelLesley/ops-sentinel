"""Daily receivables report: Markdown (for Slack / email / Claude) and a standalone HTML page."""
from __future__ import annotations

import html
from pathlib import Path

from .engine import Engine
from .rules import RULES_BY_CODE, SEVERITY_ORDER, inr


def _by_check(issues: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in issues:
        b = out.setdefault(i["check"], {"count": 0, "amount": 0.0, "severity": i["severity"]})
        b["count"] += 1
        b["amount"] += float(i.get("at_risk_inr") or 0)
        if SEVERITY_ORDER[i["severity"]] < SEVERITY_ORDER[b["severity"]]:
            b["severity"] = i["severity"]
    return dict(sorted(out.items(), key=lambda kv: (SEVERITY_ORDER[kv[1]["severity"]], -kv[1]["amount"])))


def _pct(v):
    return "n/a" if v is None else f"{v}%"


def markdown(engine: Engine, top: int = 10) -> str:
    k = engine.kpis()
    issues = engine.open_issues()
    new = [i for i in issues if i.get("first_seen") == engine.as_of.isoformat()]
    lines = [f"# Commission receivables: {engine.as_of.isoformat()}", "",
             f"**{k['open_issues']} open issues · {inr(k['at_risk_inr'])} at risk · {len(new)} new today**", "",
             "## Position",
             f"- Outstanding: {inr(k['outstanding_inr'])}, of which overdue {inr(k['overdue_inr'])} "
             f"(1-30d {inr(k['ageing_1_30_inr'])} · 31-60d {inr(k['ageing_31_60_inr'])} · 60+d {inr(k['ageing_60_plus_inr'])})",
             f"- Unbilled (enrolled, no invoice): {inr(k['unbilled_inr'])}",
             f"- Unapplied cash: {inr(k['unapplied_cash_inr'])}",
             f"- Collection rate {_pct(k['collection_rate_pct'])} · invoice coverage {_pct(k['invoice_coverage_pct'])} · "
             f"accrual accuracy ({k['accrual_month']}) {_pct(k['accrual_accuracy_pct'])}", "",
             "## Issues by type"]
    for c, b in _by_check(issues).items():
        lines.append(f"- **{RULES_BY_CODE[c].title}** [{b['severity']}]: {b['count']}, {inr(b['amount'])}")
    lines += ["", "## Top issues"]
    lines += [f"- `{i['issue_id']}` [{i['severity']}] {i['summary']}" for i in issues[:top]]
    return "\n".join(lines)


def to_html(engine: Engine) -> str:
    e = html.escape
    k = engine.kpis()
    issues = engine.open_issues()
    tiles = [("Outstanding", inr(k["outstanding_inr"])), ("Overdue", inr(k["overdue_inr"])),
             ("Unbilled (leakage)", inr(k["unbilled_inr"])), ("Unapplied cash", inr(k["unapplied_cash_inr"])),
             ("Collection rate", _pct(k["collection_rate_pct"])), ("Invoice coverage", _pct(k["invoice_coverage_pct"])),
             (f"Accrual accuracy ({k['accrual_month']})", _pct(k["accrual_accuracy_pct"])), ("Open issues", k["open_issues"])]

    buckets = [("Not due", k["ageing_not_due_inr"]), ("1-30 days", k["ageing_1_30_inr"]),
               ("31-60 days", k["ageing_31_60_inr"]), ("60+ days", k["ageing_60_plus_inr"])]
    mx = max(v for _, v in buckets) or 1
    ageing = "".join(f"<div class='brow'><span>{l}</span><div class='track'><div class='bar b{n}' style='width:{100 * v / mx:.1f}%'></div></div>"
                     f"<b>{inr(v)}</b></div>" for n, (l, v) in enumerate(buckets))

    partners = engine.partner_ageing()[:8]
    prow = "".join(f"<tr><td>{e(p['partner'])}</td>" + "".join(f"<td class='n'>{inr(p[b])}</td>" for b in ("Not due", "1-30", "31-60", "60+"))
                   + f"<td class='n'><b>{inr(p['total_inr'])}</b></td></tr>" for p in partners)
    crow = "".join(f"<tr><td>{e(RULES_BY_CODE[c].title)}</td><td><span class='sev {b['severity']}'>{b['severity']}</span></td>"
                   f"<td class='n'>{b['count']}</td><td class='n'>{inr(b['amount'])}</td><td>{e(RULES_BY_CODE[c].why)}</td></tr>"
                   for c, b in _by_check(issues).items())
    irow = "".join(f"<tr><td><span class='sev {i['severity']}'>{i['severity']}</span></td><td>{e(i['summary'])}</td>"
                   f"<td class='n'>{inr(float(i.get('at_risk_inr') or 0))}</td><td>{e(str(i.get('owner') or '-'))}</td>"
                   f"<td>{e(str(i.get('status')))}</td></tr>" for i in issues[:20])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Receivables Report {engine.as_of}</title>
<style>
:root{{--bg:#fff;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--tile:#f6f8fa;--b0:#8c959f;--b1:#bf8700;--b2:#d9480f;--b3:#a61e4d;
--crit:#a61e4d;--high:#d9480f;--med:#9a6700;--low:#57606a}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--tile:#161b22;--crit:#f778ba;--high:#ff8c42;--med:#d29922;--low:#8d96a0}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Arial,sans-serif}}
main{{max-width:1000px;margin:0 auto;padding:24px 16px}} h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 8px}}
.muted{{color:var(--muted)}} .tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:16px}}
.tile{{background:var(--tile);border:1px solid var(--line);border-radius:6px;padding:10px 12px}} .tile b{{display:block;font-size:19px}}
.tile span{{color:var(--muted);font-size:12px}} .scroll{{overflow-x:auto}} table{{border-collapse:collapse;width:100%}}
th,td{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}} th{{font-size:12px;color:var(--muted)}}
td.n{{text-align:right;white-space:nowrap}} .sev{{font-size:11px;font-weight:600;text-transform:uppercase}}
.critical{{color:var(--crit)}} .high{{color:var(--high)}} .medium{{color:var(--med)}} .low{{color:var(--low)}}
.brow{{display:grid;grid-template-columns:90px 1fr 110px;gap:10px;align-items:center;margin:6px 0;max-width:640px}}
.brow b{{text-align:right}} .track{{background:var(--tile);border-radius:3px;height:14px}} .bar{{height:14px;border-radius:3px}}
.b0{{background:var(--b0)}} .b1{{background:var(--b1)}} .b2{{background:var(--b2)}} .b3{{background:var(--b3)}}
</style></head><body><main>
<h1>Commission receivables report</h1>
<div class="muted">As of {engine.as_of} · source: {e(engine.source.label)} · all amounts in INR at tracker FX rates · synthetic data</div>
<div class="tiles">{"".join(f"<div class='tile'><b>{v}</b><span>{e(str(t))}</span></div>" for t, v in tiles)}</div>
<h2>Ageing of outstanding commission</h2>{ageing}
<h2>Partners with the oldest balances</h2><div class="scroll"><table><tr><th>Partner</th><th>Not due</th><th>1-30</th><th>31-60</th><th>60+</th><th>Total</th></tr>{prow}</table></div>
<h2>Issues by type</h2><div class="scroll"><table><tr><th>Check</th><th>Worst</th><th>Count</th><th>At risk</th><th>Why it matters</th></tr>{crow}</table></div>
<h2>Top open issues</h2><div class="scroll"><table><tr><th>Severity</th><th>Issue</th><th>At risk</th><th>Owner</th><th>Status</th></tr>{irow}</table></div>
</main></body></html>"""


def write_html(engine: Engine, out_dir: str | Path = "reports") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"receivables-report-{engine.as_of}.html"
    path.write_text(to_html(engine), encoding="utf-8")
    return path
