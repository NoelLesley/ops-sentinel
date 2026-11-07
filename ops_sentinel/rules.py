"""Operational checks. Each rule is a pure function: (conn, as_of, cfg) -> list[Finding].

Rules are deliberately written as plain SQL so an ops team can read and tune them.
"""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class Finding:
    rule: str
    severity: str
    entity: str
    summary: str
    amount_at_risk: float = 0.0

    @property
    def issue_id(self) -> str:
        return f"{self.rule}:{self.entity}"


@dataclass
class Config:
    deposit_grace_days: int = 0          # overdue the day after due date
    deposit_escalate_days: int = 14      # overdue this long -> high
    invoice_sla_days: int = 7            # visa approved -> invoice raised
    stale_case_days: int = 21            # no stage movement
    volume_z_threshold: float = -2.0     # daily settlement volume vs trailing 14 days
    volume_lookback_days: int = 14


@dataclass
class Rule:
    code: str
    title: str
    why_it_matters: str
    rca_hints: list[str]
    fn: Callable[[sqlite3.Connection, date, Config], list[Finding]] = field(repr=False)


def inr(x: float) -> str:
    """Indian digit grouping: 5190500 -> ₹51,90,500"""
    neg, n = x < 0, f"{abs(round(x)):d}"
    if len(n) > 3:
        head, tail = n[:-3], n[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        n = f"{head},{tail}"
    return f"{'-' if neg else ''}₹{n}"


# ---------------------------------------------------------------- reconciliation
def duplicate_settlement(conn, as_of, cfg):
    rows = conn.execute("""
        SELECT gateway_ref, COUNT(*) n, SUM(amount) total, MIN(amount) one
        FROM ledger WHERE txn_at <= ? GROUP BY gateway_ref HAVING n > 1""", (as_of.isoformat(),))
    return [Finding("DUPLICATE_SETTLEMENT", "critical", r["gateway_ref"],
                    f"{r['n']} settlements for gateway ref {r['gateway_ref']} (total {inr(r['total'])}); likely double charge",
                    r["total"] - r["one"]) for r in rows]


def orphan_settlement(conn, as_of, cfg):
    rows = conn.execute("""
        SELECT l.txn_id, l.gateway_ref, l.amount, l.txn_at FROM ledger l
        LEFT JOIN deposits d ON d.gateway_ref = l.gateway_ref
        WHERE d.deposit_id IS NULL AND l.txn_at <= ?""", (as_of.isoformat(),))
    return [Finding("UNRECONCILED_SETTLEMENT", "high", r["txn_id"],
                    f"{inr(r['amount'])} settled on {r['txn_at']} (ref {r['gateway_ref']}) with no matching deposit",
                    r["amount"]) for r in rows]


def amount_mismatch(conn, as_of, cfg):
    rows = conn.execute("""
        SELECT d.deposit_id, d.case_id, d.amount_expected, l.amount
        FROM deposits d JOIN ledger l ON l.gateway_ref = d.gateway_ref
        WHERE d.status='received' AND ABS(d.amount_expected - l.amount) > 0.5 AND l.txn_at <= ?
        GROUP BY d.deposit_id""", (as_of.isoformat(),))
    return [Finding("AMOUNT_MISMATCH", "high", r["deposit_id"],
                    f"Case {r['case_id']}: expected {inr(r['amount_expected'])}, gateway settled {inr(r['amount'])}",
                    abs(r["amount_expected"] - r["amount"])) for r in rows]


# ---------------------------------------------------------------- collections
def deposit_overdue(conn, as_of, cfg):
    cutoff = (as_of - timedelta(days=cfg.deposit_grace_days)).isoformat()
    rows = conn.execute("""
        SELECT d.deposit_id, d.case_id, d.amount_expected, d.due_date, c.counsellor
        FROM deposits d JOIN cases c USING(case_id)
        WHERE d.status='pending' AND d.due_date < ?""", (cutoff,))
    out = []
    for r in rows:
        days = (as_of - date.fromisoformat(r["due_date"])).days
        sev = "high" if days >= cfg.deposit_escalate_days else "medium"
        out.append(Finding("DEPOSIT_OVERDUE", sev, r["deposit_id"],
                           f"Case {r['case_id']} deposit {inr(r['amount_expected'])} overdue {days}d (owner: {r['counsellor']})",
                           r["amount_expected"]))
    return out


# ---------------------------------------------------------------- revenue
def invoice_not_raised(conn, as_of, cfg):
    cutoff = (as_of - timedelta(days=cfg.invoice_sla_days)).isoformat()
    rows = conn.execute("""
        SELECT c.case_id, c.partner, c.visa_approved_at, d.amount_expected
        FROM cases c JOIN deposits d USING(case_id)
        LEFT JOIN invoices i USING(case_id)
        WHERE c.visa_approved_at IS NOT NULL AND c.visa_approved_at <= ? AND i.invoice_id IS NULL""", (cutoff,))
    out = []
    for r in rows:
        days = (as_of - date.fromisoformat(r["visa_approved_at"])).days
        est = round(r["amount_expected"] * 0.14, -2)          # avg commission rate as estimate
        out.append(Finding("INVOICE_NOT_RAISED", "high", r["case_id"],
                           f"Visa approved {days}d ago, no invoice to {r['partner']} (est. commission {inr(est)})", est))
    return out


def commission_overdue(conn, as_of, cfg):
    rows = conn.execute("""
        SELECT i.invoice_id, i.case_id, i.commission_amount, i.commission_due_date, c.partner
        FROM invoices i JOIN cases c USING(case_id)
        WHERE i.commission_received_at IS NULL AND i.commission_due_date < ?""", (as_of.isoformat(),))
    out = []
    for r in rows:
        days = (as_of - date.fromisoformat(r["commission_due_date"])).days
        out.append(Finding("COMMISSION_OVERDUE", "high" if days > 15 else "medium", r["invoice_id"],
                           f"{r['partner']} commission {inr(r['commission_amount'])} for {r['case_id']} overdue {days}d",
                           r["commission_amount"]))
    return out


# ---------------------------------------------------------------- pipeline health
def stale_case(conn, as_of, cfg):
    cutoff = (as_of - timedelta(days=cfg.stale_case_days)).isoformat()
    rows = conn.execute("""
        SELECT case_id, stage, stage_updated_at, counsellor FROM cases
        WHERE stage IN ('deposit_paid','visa_filed') AND stage_updated_at < ?""", (cutoff,))
    return [Finding("STALE_CASE", "low", r["case_id"],
                    f"Stuck in '{r['stage']}' since {r['stage_updated_at']} (owner: {r['counsellor']})") for r in rows]


def settlement_volume_drop(conn, as_of, cfg):
    start = as_of - timedelta(days=cfg.volume_lookback_days)
    counts = {row["txn_at"]: row["n"] for row in conn.execute(
        "SELECT txn_at, COUNT(*) n FROM ledger WHERE txn_at BETWEEN ? AND ? GROUP BY txn_at",
        (start.isoformat(), as_of.isoformat()))}
    series = [counts.get((start + timedelta(days=k)).isoformat(), 0) for k in range(cfg.volume_lookback_days + 1)]
    history, out = series[:-2], []
    if len(history) < 5 or statistics.pstdev(history) == 0:
        return out
    mu, sd = statistics.mean(history), statistics.pstdev(history)
    for offset, n in ((1, series[-2]), (0, series[-1])):       # check yesterday and today
        z = (n - mu) / sd
        if z <= cfg.volume_z_threshold:
            day = (as_of - timedelta(days=offset)).isoformat()
            out.append(Finding("SETTLEMENT_VOLUME_DROP", "medium", day,
                               f"{n} settlements on {day} vs {mu:.1f}/day normal (z = {z:.1f}); check gateway status"))
    return out


RULES: list[Rule] = [
    Rule("DUPLICATE_SETTLEMENT", "Duplicate settlement",
         "A student may have been charged twice: refund liability and a trust problem.",
         ["Was the payment retried after a gateway timeout?", "Check for a webhook firing twice",
          "Confirm with the gateway before refunding"], duplicate_settlement),
    Rule("UNRECONCILED_SETTLEMENT", "Money received, no matching deposit",
         "Cash on the books that ops can't attribute; it blocks month-end close.",
         ["Was the gateway ref mistyped when the deposit was marked paid?",
          "Is it a payment for a case not yet created in the system?", "Search by amount + date window"],
         orphan_settlement),
    Rule("AMOUNT_MISMATCH", "Short or over settlement",
         "Expected vs actual difference: gateway fees, FX, or partial payment not followed up.",
         ["Is the gap equal to the gateway's fee slab?", "Was a partial payment agreed with the student?",
          "Check the FX rate if paid in foreign currency"], amount_mismatch),
    Rule("DEPOSIT_OVERDUE", "Deposit overdue",
         "Uncollected deposits delay offer confirmation and visa filing.",
         ["Has the counsellor followed up in the last 48h?", "Is the payment link expired?",
          "Group by counsellor to see if it's one person's pipeline"], deposit_overdue),
    Rule("INVOICE_NOT_RAISED", "Visa approved, invoice not raised",
         "Direct revenue leakage: commission isn't claimed until the invoice is raised.",
         ["Is the enrolment confirmation from the partner missing?", "Was the case closed in CRM but not handed to finance?",
          "Check if the partner needs a specific invoice format"], invoice_not_raised),
    Rule("COMMISSION_OVERDUE", "Partner commission overdue",
         "Receivables ageing, which hits cash flow and accrual accuracy.",
         ["Did the partner acknowledge the invoice?", "Is the student's enrolment confirmed on the partner side?",
          "Escalate partners with 3+ overdue invoices"], commission_overdue),
    Rule("STALE_CASE", "Case stuck in stage",
         "Silent pipeline leakage; nobody is chasing it.",
         ["Missing documents?", "Owner on leave or overloaded?"], stale_case),
    Rule("SETTLEMENT_VOLUME_DROP", "Settlement volume anomaly",
         "A sudden drop usually means a gateway or integration failure, not a demand change.",
         ["Check gateway status page and webhook logs", "Compare with payment link clicks",
          "Did a deploy go out yesterday?"], settlement_volume_drop),
]
RULES_BY_CODE = {r.code: r for r in RULES}


def run_all(conn: sqlite3.Connection, as_of: date, cfg: Config | None = None) -> list[Finding]:
    cfg = cfg or Config()
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule.fn(conn, as_of, cfg))
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], -f.amount_at_risk))
