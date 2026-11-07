"""Issue lifecycle + KPI computation."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .db import get_meta
from .rules import Config, Finding, run_all


def as_of_date(conn: sqlite3.Connection) -> date:
    return date.fromisoformat(get_meta(conn, "as_of", date.today().isoformat()))


def sync_issues(conn: sqlite3.Connection, findings: list[Finding], as_of: date) -> dict:
    """Upsert findings into the issue register.

    * new finding           -> open issue
    * still present         -> last_seen bumped (acknowledged stays acknowledged)
    * previously resolved   -> reopened (regression)
    * no longer present     -> auto-resolved
    """
    today = as_of.isoformat()
    seen, opened, reopened = set(), 0, 0
    for f in findings:
        seen.add(f.issue_id)
        row = conn.execute("SELECT status FROM issues WHERE issue_id = ?", (f.issue_id,)).fetchone()
        if row is None:
            conn.execute("""INSERT INTO issues(issue_id, rule, severity, entity, summary, amount_at_risk, first_seen, last_seen)
                            VALUES (?,?,?,?,?,?,?,?)""",
                         (f.issue_id, f.rule, f.severity, f.entity, f.summary, f.amount_at_risk, today, today))
            opened += 1
        else:
            status = "open" if row["status"] == "resolved" else row["status"]
            reopened += row["status"] == "resolved"
            conn.execute("""UPDATE issues SET severity=?, summary=?, amount_at_risk=?, last_seen=?, status=?,
                            resolved_at = CASE WHEN ?='open' AND status='resolved' THEN NULL ELSE resolved_at END
                            WHERE issue_id=?""",
                         (f.severity, f.summary, f.amount_at_risk, today, status, status, f.issue_id))

    auto_resolved = 0
    for row in conn.execute("SELECT issue_id FROM issues WHERE status != 'resolved'").fetchall():
        if row["issue_id"] not in seen:
            conn.execute("UPDATE issues SET status='resolved', resolved_at=?, note=COALESCE(note,'') || ' [auto-resolved: condition cleared]' WHERE issue_id=?",
                         (today, row["issue_id"]))
            auto_resolved += 1
    conn.commit()
    return {"new": opened, "reopened": reopened, "auto_resolved": auto_resolved, "active": len(seen)}


def run_checks(conn: sqlite3.Connection, cfg: Config | None = None) -> dict:
    as_of = as_of_date(conn)
    findings = run_all(conn, as_of, cfg)
    summary = sync_issues(conn, findings, as_of)
    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    summary.update(as_of=as_of.isoformat(), by_severity=by_sev,
                   amount_at_risk=round(sum(f.amount_at_risk for f in findings), 2))
    return summary


def kpis(conn: sqlite3.Connection, window_days: int = 30) -> dict:
    as_of = as_of_date(conn)
    start = (as_of - timedelta(days=window_days)).isoformat()
    end = as_of.isoformat()
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]

    due = q("SELECT COUNT(*) FROM deposits WHERE due_date BETWEEN ? AND ?", start, end)
    collected_on_time = q("SELECT COUNT(*) FROM deposits WHERE due_date BETWEEN ? AND ? AND status='received' AND received_at <= due_date", start, end)
    collected = q("SELECT COUNT(*) FROM deposits WHERE due_date BETWEEN ? AND ? AND status='received'", start, end)
    days_to_collect = q("SELECT AVG(julianday(received_at) - julianday(due_date)) FROM deposits WHERE status='received' AND received_at BETWEEN ? AND ?", start, end)

    settled = q("SELECT COUNT(*) FROM ledger WHERE txn_at BETWEEN ? AND ?", start, end)
    matched = q("""SELECT COUNT(*) FROM ledger l JOIN deposits d ON d.gateway_ref = l.gateway_ref
                   WHERE l.txn_at BETWEEN ? AND ? AND ABS(l.amount - d.amount_expected) < 0.5""", start, end)

    approved = q("SELECT COUNT(*) FROM cases WHERE visa_approved_at BETWEEN ? AND ?", start, end)
    invoiced = q("""SELECT COUNT(*) FROM cases c JOIN invoices i USING(case_id) WHERE c.visa_approved_at BETWEEN ? AND ?""", start, end)
    cycle = q("""SELECT AVG(julianday(i.raised_at) - julianday(c.visa_approved_at)) FROM cases c JOIN invoices i USING(case_id)
                 WHERE i.raised_at BETWEEN ? AND ?""", start, end)

    comm_due = q("SELECT COALESCE(SUM(commission_amount),0) FROM invoices WHERE commission_due_date <= ?", end)
    comm_recv = q("SELECT COALESCE(SUM(commission_amount),0) FROM invoices WHERE commission_due_date <= ? AND commission_received_at IS NOT NULL", end)

    pct = lambda a, b: round(100 * a / b, 1) if b else None
    return {
        "as_of": end,
        "window_days": window_days,
        "deposit_collection_rate_pct": pct(collected, due),
        "on_time_collection_pct": pct(collected_on_time, due),
        "avg_days_vs_due_date": round(days_to_collect, 1) if days_to_collect is not None else None,
        "reconciliation_match_rate_pct": pct(matched, settled),
        "invoice_coverage_pct": pct(invoiced, approved),
        "avg_visa_to_invoice_days": round(cycle, 1) if cycle is not None else None,
        "commission_realisation_pct": pct(comm_recv, comm_due),
        "open_issues": q("SELECT COUNT(*) FROM issues WHERE status != 'resolved'"),
        "amount_at_risk_inr": round(q("SELECT COALESCE(SUM(amount_at_risk),0) FROM issues WHERE status != 'resolved'"), 0),
    }


def kpi_trend(conn: sqlite3.Connection, days: int = 14) -> list[dict]:
    """Daily settlements and collections for the trailing window (for charts)."""
    as_of = as_of_date(conn)
    out = []
    for k in range(days - 1, -1, -1):
        d = (as_of - timedelta(days=k)).isoformat()
        out.append({
            "date": d,
            "settlements": conn.execute("SELECT COUNT(*) FROM ledger WHERE txn_at = ?", (d,)).fetchone()[0],
            "collected_inr": conn.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE txn_at = ?", (d,)).fetchone()[0],
        })
    return out
