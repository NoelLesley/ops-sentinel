"""Ops Sentinel MCP server.

Exposes the ops database to any MCP client (Claude desktop, Claude Code, etc.) so an
ops lead can ask in plain English: "what broke overnight?", "who owns the overdue
deposits?", "mark the duplicate on GW123 as acknowledged, assign to Priya".

Run:  python -m ops_sentinel.server          (stdio transport)
"""
from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import MCPServer

from . import engine, report
from .db import connect
from .generate import simulate_day
from .rules import RULES, RULES_BY_CODE, SEVERITY_ORDER

mcp = MCPServer(
    "ops-sentinel",
    instructions=(
        "Payments & receivables operations monitor. Call run_checks first to refresh the issue "
        "register, then get_kpis / list_issues. Use explain_issue before recommending an action. "
        "All data is synthetic."
    ),
)

Severity = Literal["critical", "high", "medium", "low"]
Status = Literal["open", "acknowledged", "resolved"]


def _conn():
    return connect()


@mcp.tool()
def run_checks() -> dict:
    """Run every operational check against the latest data and update the issue register.
    Returns counts of new, reopened and auto-resolved issues plus total amount at risk."""
    with _conn() as c:
        return engine.run_checks(c)


@mcp.tool()
def get_kpis(window_days: int = 30) -> dict:
    """Headline ops KPIs over a trailing window: collection rate, reconciliation match rate,
    invoice coverage, visa-to-invoice cycle time, commission realisation, open issues, ₹ at risk."""
    with _conn() as c:
        return engine.kpis(c, window_days)


@mcp.tool()
def list_issues(severity: Severity | None = None, rule: str | None = None,
                status: Status | None = None, limit: int = 25) -> list[dict]:
    """List issues from the register, most severe and highest ₹ first.
    Filter by severity, rule code (see list_rules) or status. Default excludes resolved."""
    sql, args = "SELECT * FROM issues WHERE 1=1", []
    if severity:
        sql += " AND severity = ?"; args.append(severity)
    if rule:
        sql += " AND rule = ?"; args.append(rule.upper())
    if status:
        sql += " AND status = ?"; args.append(status)
    else:
        sql += " AND status != 'resolved'"
    with _conn() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    rows.sort(key=lambda r: (SEVERITY_ORDER[r["severity"]], -r["amount_at_risk"]))
    return rows[:limit]


@mcp.tool()
def explain_issue(issue_id: str) -> dict:
    """Why an issue matters, root-cause questions to check, and the underlying records."""
    with _conn() as c:
        row = c.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)).fetchone()
        if not row:
            return {"error": f"No issue {issue_id}"}
        rule = RULES_BY_CODE[row["rule"]]
        entity = row["entity"]
        evidence = {}
        for table, col in (("deposits", "deposit_id"), ("cases", "case_id"), ("invoices", "invoice_id"),
                           ("ledger", "txn_id"), ("ledger", "gateway_ref")):
            recs = [dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE {col} = ?", (entity,))]
            if recs:
                evidence.setdefault(table, []).extend(recs)
        return {"issue": dict(row), "check": rule.title, "why_it_matters": rule.why_it_matters,
                "root_cause_checklist": rule.rca_hints, "evidence": evidence}


@mcp.tool()
def update_issue(issue_id: str, status: Status, owner: str | None = None, note: str | None = None) -> dict:
    """Acknowledge, assign or resolve an issue. Resolved issues reopen automatically if the
    condition is still true on the next run_checks."""
    with _conn() as c:
        cur = c.execute("""UPDATE issues SET status=?, owner=COALESCE(?, owner), note=COALESCE(?, note),
                           resolved_at = CASE WHEN ?='resolved' THEN (SELECT value FROM meta WHERE key='as_of') ELSE NULL END
                           WHERE issue_id=?""", (status, owner, note, status, issue_id))
        c.commit()
        if cur.rowcount == 0:
            return {"error": f"No issue {issue_id}"}
        return dict(c.execute("SELECT * FROM issues WHERE issue_id=?", (issue_id,)).fetchone())


@mcp.tool()
def case_timeline(case_id: str) -> dict:
    """Everything known about one case: stage, deposit, gateway settlements, invoice, open issues."""
    with _conn() as c:
        case = c.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not case:
            return {"error": f"No case {case_id}"}
        dep = [dict(r) for r in c.execute("SELECT * FROM deposits WHERE case_id=?", (case_id,))]
        refs = [d["gateway_ref"] for d in dep if d["gateway_ref"]]
        ledger = [dict(r) for r in c.execute(
            f"SELECT * FROM ledger WHERE gateway_ref IN ({','.join('?' * len(refs)) or 'NULL'})", refs)]
        inv = [dict(r) for r in c.execute("SELECT * FROM invoices WHERE case_id=?", (case_id,))]
        ids = [case_id] + [d["deposit_id"] for d in dep] + [i["invoice_id"] for i in inv]
        issues = [dict(r) for r in c.execute(
            f"SELECT issue_id, severity, summary, status FROM issues WHERE entity IN ({','.join('?' * len(ids))})", ids)]
        return {"case": dict(case), "deposits": dep, "settlements": ledger, "invoices": inv, "issues": issues}


@mcp.tool()
def owner_breakdown() -> list[dict]:
    """Open deposit/case issues grouped by counsellor, to spot whose pipeline needs help."""
    with _conn() as c:
        rows = c.execute("""
            SELECT c.counsellor, i.rule, COUNT(*) n, SUM(i.amount_at_risk) amt
            FROM issues i
            JOIN cases c ON c.case_id = i.entity OR c.case_id = (SELECT case_id FROM deposits WHERE deposit_id = i.entity)
            WHERE i.status != 'resolved' GROUP BY c.counsellor, i.rule ORDER BY amt DESC""")
        return [dict(r) for r in rows]


@mcp.tool()
def daily_report(fmt: Literal["markdown", "html"] = "markdown") -> str:
    """Generate the daily ops report. markdown returns the text (paste into Slack/email);
    html writes a standalone file and returns its path."""
    with _conn() as c:
        engine.run_checks(c)
        if fmt == "html":
            return str(report.write_html(c).resolve())
        return report.markdown(c)


@mcp.tool()
def list_rules() -> list[dict]:
    """The checks Ops Sentinel runs, what each catches and why it matters."""
    return [{"code": r.code, "title": r.title, "why_it_matters": r.why_it_matters} for r in RULES]


@mcp.tool()
def simulate_next_day() -> dict:
    """DEMO ONLY: advance the synthetic clock one day (new payments, new cases), then re-run checks."""
    with _conn() as c:
        sim = simulate_day(c)
        sim["checks"] = engine.run_checks(c)
        return sim


@mcp.prompt()
def morning_standup() -> str:
    """Prompt template: the 9am ops stand-up."""
    return ("Run run_checks, then get_kpis. Summarise in 5 bullets: what's new since yesterday, the top 3 "
            "issues by ₹ at risk (use explain_issue on each and give the first thing to check), any KPI "
            "that moved more than 5 points, and which owner needs help (owner_breakdown).")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
