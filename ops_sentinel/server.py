"""Ops Sentinel MCP server: lets Claude (or any MCP client) work the commission tracker.

Each tool call re-reads the tracker, so Claude always sees the latest edits people
made in the spreadsheet. Ask things like:
  "What's overdue from UK partners, oldest first?"
  "Why is INV-KCL-021 flagged, and what should I check?"
  "Assign the unapplied cash items to Priya and mark them acknowledged."

Run:  python -m ops_sentinel.server     (stdio; Claude desktop starts it for you)
"""
from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import MCPServer

from .engine import Engine
from .report import markdown, write_html
from .rules import RULES, RULES_BY_CODE

mcp = MCPServer(
    "ops-sentinel",
    instructions=(
        "Commission receivables monitor over a spreadsheet tracker (cases, invoices, partner payments, accruals). "
        "Call run_checks first to refresh, then use get_kpis / list_issues. Call explain_issue before recommending "
        "an action. Amounts in issues are INR; invoice amounts are in the partner's currency. Data is synthetic."
    ),
)

Severity = Literal["critical", "high", "medium", "low"]
Status = Literal["open", "acknowledged", "resolved"]


def _engine() -> Engine:
    return Engine()


@mcp.tool()
def run_checks() -> dict:
    """Run every check on the tracker and write results back to it (tracker status columns, Issues, KPIs).
    Returns how many issues are new, reopened and auto-resolved, plus INR at risk."""
    r = _engine().run()
    return {k: v for k, v in r.__dict__.items() if k != "kpis"}


@mcp.tool()
def get_kpis() -> dict:
    """Headline receivables KPIs: outstanding and overdue (with ageing buckets), unbilled commission,
    unapplied cash, collection rate, invoice coverage, days to invoice / collect, accrual accuracy. INR."""
    return _engine().kpis()


@mcp.tool()
def list_issues(severity: Severity | None = None, check: str | None = None, partner: str | None = None,
                status: Status | None = None, limit: int = 25) -> list[dict]:
    """Issues from the Issues tab, most severe and highest INR first. Open + acknowledged by default.
    Filter by severity, check code (see list_checks), partner name (partial match) or status."""
    e = _engine()
    rows = list(e.issues.values()) if status else e.open_issues()
    if status:
        rows = [i for i in rows if str(i.get("status")).lower() == status]
    if severity:
        rows = [i for i in rows if i.get("severity") == severity]
    if check:
        rows = [i for i in rows if i.get("check") == check.upper()]
    if partner:
        rows = [i for i in rows if partner.lower() in str(i.get("partner", "")).lower()]
    return rows[:limit]


@mcp.tool()
def explain_issue(issue_id: str) -> dict:
    """Why an issue matters, what to check first, and the underlying tracker rows and payments."""
    e = _engine()
    i = e.issues.get(issue_id)
    if not i:
        return {"error": f"No issue {issue_id}. Use list_issues to see IDs."}
    rule = RULES_BY_CODE[i["check"]]
    evidence = e.case(i["ref"]) if i["check"] not in ("UNAPPLIED_CASH", "ACCRUAL_VARIANCE") else None
    if i["check"] == "UNAPPLIED_CASH":
        evidence = {"remittance": next((dict(r) for r in e.db.execute("SELECT * FROM remittances WHERE remittance_id=?",
                                                                       (i["ref"],))), None),
                    "possible_matches": [dict(r) for r in e.db.execute(
                        """SELECT case_id, invoice_no, invoice_amount, due_date FROM cases WHERE partner=? AND invoice_no<>''
                           AND received=0 ORDER BY ABS(invoice_amount - (SELECT amount FROM remittances WHERE remittance_id=?))
                           LIMIT 3""", (i["partner"], i["ref"]))]}
    return {"issue": i, "check": rule.title, "why_it_matters": rule.why, "check_first": rule.first_checks,
            "evidence": evidence}


@mcp.tool()
def update_issue(issue_id: str, status: Status | None = None, owner: str | None = None, note: str | None = None) -> dict:
    """Acknowledge, assign or resolve an issue; saved to the Issues tab. A 'resolved' issue reopens
    automatically on the next run if the tracker still shows the problem."""
    r = _engine().update_issue(issue_id, status, owner, note)
    return r or {"error": f"No issue {issue_id}"}


@mcp.tool()
def lookup(ref: str) -> dict:
    """Everything about one case or invoice (e.g. C0107 or INV-KCL-012): tracker row, payments applied, issues."""
    return _engine().case(ref) or {"error": f"Nothing found for {ref}"}


@mcp.tool()
def partner_ageing() -> list[dict]:
    """Outstanding commission per partner split into Not due / 1-30 / 31-60 / 60+ days (INR), oldest first."""
    return _engine().partner_ageing()


@mcp.tool()
def daily_report(fmt: Literal["markdown", "html"] = "markdown") -> str:
    """Run the checks and produce the daily report. markdown returns text to paste into email or Slack;
    html writes a standalone file and returns its path."""
    e = _engine()
    e.run()
    return str(write_html(e).resolve()) if fmt == "html" else markdown(e)


@mcp.tool()
def list_checks() -> list[dict]:
    """The checks Ops Sentinel runs, what each catches and why it matters."""
    return [{"code": r.code, "title": r.title, "why_it_matters": r.why} for r in RULES]


@mcp.prompt()
def morning_standup() -> str:
    """The 9am receivables stand-up."""
    return ("Run run_checks, then get_kpis and partner_ageing. In 6 bullets: what's new since yesterday; the 3 biggest "
            "issues by INR at risk (use explain_issue on each and say what to check first); which partner needs an "
            "escalation email; unbilled commission to invoice today; anything unassigned that's critical.")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
