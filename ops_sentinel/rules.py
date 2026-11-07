"""The checks. Each one is a short SQL query over the `cases` view (see model.py)
that returns the rows breaking a rule. Thresholds live in Config so ops can tune them.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def inr(x: float) -> str:
    """Indian digit grouping: 5190500 -> ₹51,90,500"""
    neg, n = x < 0, f"{abs(round(x or 0)):d}"
    if len(n) > 3:
        head, tail = n[:-3], n[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        n = ",".join(parts) + "," + tail
    return f"{'-' if neg else ''}₹{n}"


def money(amount: float, ccy: str) -> str:
    sym = {"GBP": "£", "USD": "$", "EUR": "€", "CAD": "C$", "AUD": "A$"}.get(ccy, ccy + " ")
    return f"{sym}{amount:,.0f}"


def code(name: str) -> str:
    return "".join(w[0] for w in name.split())[:3].upper()


@dataclass
class Config:
    invoice_sla_days: int = 21          # visa approved -> invoice should be raised
    enrolment_chase_days: int = 30      # visa approved but enrolment still unconfirmed
    bank_charge_tolerance: float = 50   # shortfall up to this (in invoice currency) = bank charges
    rate_tolerance_pct: float = 1.0     # invoice vs fee x partner rate
    accrual_variance_pct: float = 25.0  # actual vs accrued, per partner-month
    accrual_close_days: int = 30        # only judge months that ended this long ago
    accrual_min_gap_inr: float = 100_000  # ignore small partner-months


@dataclass
class Finding:
    rule: str
    severity: str
    ref: str
    partner: str
    summary: str
    at_risk_inr: float = 0.0

    @property
    def issue_id(self) -> str:
        return f"{self.rule}:{self.ref}"


@dataclass
class Rule:
    code: str
    title: str
    why: str
    first_checks: list[str]
    fn: Callable = field(repr=False)


Ctx = tuple[sqlite3.Connection, date, Config, list[dict]]   # db, as_of, config, unmatched remittances


def _iso(d: date) -> str:
    return d.isoformat()


def duplicate_invoice_no(db, as_of, cfg, _):
    rows = db.execute("""SELECT invoice_no, partner, GROUP_CONCAT(case_id, ', ') ids, COUNT(*) n,
                                SUM(invoice_amount * inr_rate) - MAX(invoice_amount * inr_rate) extra
                         FROM cases WHERE invoice_no <> '' GROUP BY invoice_no HAVING n > 1""")
    return [Finding("DUPLICATE_INVOICE_NO", "critical", r["invoice_no"], r["partner"],
                    f"Invoice {r['invoice_no']} is used on {r['n']} cases ({r['ids']}); payments can't be matched reliably",
                    r["extra"]) for r in rows]


def invoice_not_raised(db, as_of, cfg, _):
    cutoff = _iso(as_of - timedelta(days=cfg.invoice_sla_days))
    rows = db.execute("""SELECT * FROM cases WHERE visa_status = 'Approved' AND enrolment_confirmed = 'Yes'
                         AND invoice_no = '' AND visa_approved_on <> '' AND visa_approved_on <= ?""", (cutoff,))
    return [Finding("INVOICE_NOT_RAISED", "high", r["case_id"], r["partner"],
                    f"{r['case_id']} enrolled, visa approved {(as_of - date.fromisoformat(r['visa_approved_on'])).days}d ago, "
                    f"no invoice to {r['partner']} (expected {money(r['expected_commission'], r['currency'])})",
                    r["expected_commission"] * r["inr_rate"]) for r in rows]


def enrolment_unconfirmed(db, as_of, cfg, _):
    cutoff = _iso(as_of - timedelta(days=cfg.enrolment_chase_days))
    rows = db.execute("""SELECT * FROM cases WHERE visa_status = 'Approved' AND enrolment_confirmed <> 'Yes'
                         AND invoice_no = '' AND visa_approved_on <> '' AND visa_approved_on <= ?""", (cutoff,))
    return [Finding("ENROLMENT_UNCONFIRMED", "medium", r["case_id"], r["partner"],
                    f"{r['case_id']} visa approved {(as_of - date.fromisoformat(r['visa_approved_on'])).days}d ago; "
                    f"enrolment not confirmed by {r['partner']}, so it can't be invoiced",
                    r["expected_commission"] * r["inr_rate"]) for r in rows]


def commission_overdue(db, as_of, cfg, _):
    rows = db.execute("""SELECT *, invoice_amount - received AS os FROM cases
                         WHERE invoice_no <> '' AND received = 0 AND due_date <> '' AND due_date < ?""", (_iso(as_of),))
    out = []
    for r in rows:
        days = (as_of - date.fromisoformat(r["due_date"])).days
        sev = "medium" if days <= 30 else "high" if days <= 60 else "critical"
        out.append(Finding("COMMISSION_OVERDUE", sev, r["invoice_no"], r["partner"],
                           f"{r['invoice_no']} ({r['case_id']}) {money(r['os'], r['currency'])} overdue {days}d from {r['partner']}",
                           r["os"] * r["inr_rate"]))
    return out


def short_payment(db, as_of, cfg, _):
    rows = db.execute("""SELECT *, invoice_amount - received AS os FROM cases
                         WHERE invoice_no <> '' AND received > 0 AND invoice_amount - received > 0.005""")
    out = []
    for r in rows:
        if r["os"] <= cfg.bank_charge_tolerance:
            out.append(Finding("BANK_CHARGES", "low", r["invoice_no"], r["partner"],
                               f"{r['invoice_no']} paid {money(r['os'], r['currency'])} short; within bank-charge tolerance, "
                               f"write off or claim back", r["os"] * r["inr_rate"]))
        else:
            pct = 100 * r["os"] / r["invoice_amount"]
            out.append(Finding("SHORT_PAYMENT", "high", r["invoice_no"], r["partner"],
                               f"{r['invoice_no']} received {money(r['received'], r['currency'])} of "
                               f"{money(r['invoice_amount'], r['currency'])} ({pct:.0f}% short)", r["os"] * r["inr_rate"]))
    return out


def overpayment(db, as_of, cfg, _):
    rows = db.execute("""SELECT *, received - invoice_amount AS extra FROM cases
                         WHERE invoice_no <> '' AND received - invoice_amount > ?""", (cfg.bank_charge_tolerance,))
    return [Finding("OVERPAYMENT", "high", r["invoice_no"], r["partner"],
                    f"{r['invoice_no']} received {money(r['received'], r['currency'])} against "
                    f"{money(r['invoice_amount'], r['currency'])}; likely paid twice, refund or credit note needed",
                    r["extra"] * r["inr_rate"]) for r in rows]


def unapplied_cash(db, as_of, cfg, unmatched):
    fx = {r["currency"]: r["inr_rate"] for r in db.execute("SELECT * FROM fx")}
    return [Finding("UNAPPLIED_CASH", "high", u["remittance_id"], u["partner"],
                    f"{money(u['amount'], u['currency'])} from {u['partner']} on {u['date']} "
                    f"{'references unknown invoice ' + repr(u['invoice_ref']) if u['invoice_ref'] else 'has no invoice reference'}",
                    u["amount"] * fx.get(u["currency"], 0)) for u in unmatched]


def rate_mismatch(db, as_of, cfg, _):
    rows = db.execute("""SELECT * FROM cases WHERE invoice_no <> '' AND expected_commission > 0
                         AND ABS(invoice_amount - expected_commission) > expected_commission * ? / 100""",
                      (cfg.rate_tolerance_pct,))
    out = []
    for r in rows:
        billed = r["invoice_amount"] / r["tuition_fee"] * 100
        out.append(Finding("RATE_MISMATCH", "medium", r["invoice_no"], r["partner"],
                           f"{r['invoice_no']} billed at {billed:.1f}% vs agreed {r['commission_rate'] * 100:.0f}% "
                           f"({money(r['invoice_amount'], r['currency'])} vs {money(r['expected_commission'], r['currency'])})",
                           max(r["expected_commission"] - r["invoice_amount"], 0) * r["inr_rate"]))
    return out


def accrual_variance(db, as_of, cfg, _):
    closed_before = (as_of - timedelta(days=cfg.accrual_close_days)).replace(day=1).isoformat()[:7]
    rows = db.execute("""
        SELECT a.month, a.partner, a.currency, a.accrued_amount, COALESCE(SUM(c.invoice_amount), 0) actual,
               MAX(f.inr_rate) inr_rate
        FROM accruals a
        LEFT JOIN cases c ON c.partner = a.partner AND substr(c.visa_approved_on, 1, 7) = a.month AND c.invoice_no <> ''
        LEFT JOIN fx f ON f.currency = a.currency
        WHERE a.month < ? GROUP BY a.month, a.partner""", (closed_before,))
    out = []
    for r in rows:
        if not r["accrued_amount"]:
            continue
        var = 100 * (r["actual"] - r["accrued_amount"]) / r["accrued_amount"]
        gap_inr = abs(r["actual"] - r["accrued_amount"]) * (r["inr_rate"] or 0)
        if abs(var) > cfg.accrual_variance_pct and gap_inr >= cfg.accrual_min_gap_inr:
            out.append(Finding("ACCRUAL_VARIANCE", "medium", f"{r['month']}/{code(r['partner'])}", r["partner"],
                               f"{r['month']} {r['partner']}: accrued {money(r['accrued_amount'], r['currency'])}, "
                               f"invoiced {money(r['actual'], r['currency'])} ({var:+.0f}%)",
                               0))      # the money sits on the underlying cases; counting it here would double it
    return out


RULES: list[Rule] = [
    Rule("DUPLICATE_INVOICE_NO", "Same invoice number on two cases",
         "Payments get applied to the wrong student and the partner may reject both invoices.",
         ["Which case was invoiced first? Re-number the other", "Was a row copy-pasted in the tracker?"], duplicate_invoice_no),
    Rule("INVOICE_NOT_RAISED", "Enrolled, but not invoiced",
         "Direct revenue leakage: commission isn't claimed until the invoice is raised.",
         ["Is the partner's enrolment confirmation filed?", "Was the case handed over from counselling to finance?",
          "Does the partner need a specific invoice format or portal upload?"], invoice_not_raised),
    Rule("ENROLMENT_UNCONFIRMED", "Visa approved, enrolment not confirmed",
         "Can't invoice until the partner confirms; the longer it waits, the more likely the student deferred or dropped.",
         ["Check the partner portal for enrolment status", "Ask the counsellor if the student deferred",
          "Chase the partner contact in one batch email"], enrolment_unconfirmed),
    Rule("COMMISSION_OVERDUE", "Commission overdue",
         "Receivables ageing: hits cash flow, and 60+ day items are at real risk of never being paid.",
         ["Did the partner acknowledge the invoice?", "Check unapplied cash from the same partner: it may already be paid",
          "Escalate partners with 3+ overdue invoices"], commission_overdue),
    Rule("SHORT_PAYMENT", "Short payment",
         "Received less than invoiced beyond bank charges: wrong rate on their side, partial scholarship, or a dispute.",
         ["Compare with the partner's remittance advice", "Did the student get a fee discount that changes the commission base?",
          "Raise a balance-due note"], short_payment),
    Rule("BANK_CHARGES", "Small shortfall (bank charges)",
         "Intermediary bank fees on international transfers; small, but they add up across hundreds of invoices.",
         ["Write off under the bank-charges threshold", "Ask the partner to pay charges as 'OUR' on the SWIFT transfer"], short_payment),
    Rule("OVERPAYMENT", "Overpayment / paid twice",
         "Money that isn't ours: needs a refund or a credit note before audit.",
         ["Was the same invoice paid twice, a few days apart?", "Offset against the partner's next invoice, with a credit note"],
         overpayment),
    Rule("UNAPPLIED_CASH", "Payment with no matching invoice",
         "Cash in the bank that ops can't attribute; meanwhile the invoice it was meant for shows as overdue.",
         ["Look for an overdue invoice from the same partner with the same amount",
          "Check for typos in the invoice reference", "Request the remittance advice"], unapplied_cash),
    Rule("RATE_MISMATCH", "Billed at the wrong commission rate",
         "Under-billing loses revenue; over-billing gets the invoice disputed and paid late.",
         ["Is the partner agreement rate up to date in Partners?", "Was an old rate template used?"], rate_mismatch),
    Rule("ACCRUAL_VARIANCE", "Accrual vs actual variance",
         "Finance booked revenue that hasn't been invoiced (or vice versa): month-end numbers are off.",
         ["Which cases from that month are not invoiced or enrolled yet?", "Was the accrual built on an outdated case list?"],
         accrual_variance),
]
RULES_BY_CODE = {r.code: r for r in RULES}


def run_all(db: sqlite3.Connection, as_of: date, unmatched: list[dict], cfg: Config | None = None) -> list[Finding]:
    cfg = cfg or Config()
    seen, findings = set(), []
    for rule in RULES:
        for f in rule.fn(db, as_of, cfg, unmatched):
            if f.issue_id not in seen:          # SHORT_PAYMENT / BANK_CHARGES share one function
                seen.add(f.issue_id)
                f.at_risk_inr = round(f.at_risk_inr or 0, 0)
                findings.append(f)
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], -f.at_risk_inr))
