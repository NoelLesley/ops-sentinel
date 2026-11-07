"""Loads the tracker tabs into an in-memory SQLite database so the checks can be
plain SQL. Nothing is stored here permanently: the spreadsheet is the source of truth,
and this is a scratch copy rebuilt on every run (like pasting into a working tab).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta

from . import schema as S

DDL = """
CREATE TABLE tracker (row_no INTEGER, case_id TEXT, student TEXT, counsellor TEXT, partner TEXT, country TEXT,
    currency TEXT, intake TEXT, tuition_fee REAL, visa_status TEXT, visa_approved_on TEXT, enrolment_confirmed TEXT,
    invoice_no TEXT, invoice_date TEXT, invoice_amount REAL, due_date TEXT, remarks TEXT);
CREATE TABLE partners (partner TEXT PRIMARY KEY, country TEXT, currency TEXT, commission_rate REAL,
    payment_terms_days INTEGER, contact TEXT);
CREATE TABLE remittances (remittance_id TEXT, date TEXT, partner TEXT, currency TEXT, amount REAL,
    invoice_ref TEXT, bank_ref TEXT, notes TEXT);
CREATE TABLE allocations (remittance_id TEXT, invoice_no TEXT, amount REAL, date TEXT);   -- payment split per invoice
CREATE TABLE accruals (month TEXT, partner TEXT, currency TEXT, accrued_amount REAL);
CREATE TABLE fx (currency TEXT PRIMARY KEY, inr_rate REAL);

-- One row per case with everything the checks need. The first case using an invoice
-- number gets the money (duplicates are flagged separately).
CREATE VIEW cases AS
SELECT t.*, p.commission_rate, p.payment_terms_days, COALESCE(f.inr_rate, 0) AS inr_rate,
       ROUND(t.tuition_fee * p.commission_rate, 2) AS expected_commission,
       CASE WHEN t.invoice_no <> '' AND t.row_no = (SELECT MIN(row_no) FROM tracker d WHERE d.invoice_no = t.invoice_no)
            THEN COALESCE(a.received, 0) ELSE 0 END AS received,
       a.last_paid
FROM tracker t
LEFT JOIN partners p USING (partner)
LEFT JOIN fx f ON f.currency = t.currency
LEFT JOIN (SELECT invoice_no, SUM(amount) AS received, MAX(date) AS last_paid FROM allocations GROUP BY invoice_no) a
       ON a.invoice_no = t.invoice_no;
"""

_NUM = re.compile(r"[^0-9.\-]")


def to_num(v) -> float | None:
    if v in ("", None):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _NUM.sub("", str(v))
    return float(s) if s not in ("", "-", ".") else None


def to_date(v) -> str:
    """Accepts dates, ISO strings, dd/mm/yyyy, or Google/Excel serial numbers. Returns ISO or ''."""
    if v in ("", None):
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (int, float)):
        return (date(1899, 12, 30) + timedelta(days=int(v))).isoformat()
    s = str(v).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def _txt(v) -> str:
    return "" if v is None else str(v).strip()


def split_refs(ref: str) -> list[str]:
    return [r.strip().upper() for r in re.split(r"[;,/]", ref or "") if r.strip()]


def build_db(tabs: dict[str, list[list]]) -> tuple[sqlite3.Connection, list[dict]]:
    """Returns the db and a list of remittances that couldn't be matched to any invoice."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(DDL)

    tracker = S.rows_to_dicts(tabs.get(S.TRACKER, []))
    for n, r in enumerate(tracker, start=2):                 # row 2 = first data row in the sheet
        db.execute("INSERT INTO tracker VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            n, _txt(r.get("case_id")), _txt(r.get("student")), _txt(r.get("counsellor")), _txt(r.get("partner")),
            _txt(r.get("country")), _txt(r.get("currency")).upper(), _txt(r.get("intake")), to_num(r.get("tuition_fee")),
            _txt(r.get("visa_status")).title(), to_date(r.get("visa_approved_on")), _txt(r.get("enrolment_confirmed")).title(),
            _txt(r.get("invoice_no")).upper(), to_date(r.get("invoice_date")), to_num(r.get("invoice_amount")),
            to_date(r.get("due_date")), _txt(r.get("remarks"))))

    for r in S.rows_to_dicts(tabs.get(S.PARTNERS, [])):
        rate = to_num(r.get("commission_rate")) or 0
        db.execute("INSERT OR REPLACE INTO partners VALUES (?,?,?,?,?,?)", (
            _txt(r.get("partner")), _txt(r.get("country")), _txt(r.get("currency")).upper(),
            rate / 100 if rate > 1 else rate, int(to_num(r.get("payment_terms_days")) or 0), _txt(r.get("contact"))))
    for r in S.rows_to_dicts(tabs.get(S.FX, [])):
        db.execute("INSERT OR REPLACE INTO fx VALUES (?,?)", (_txt(r.get("currency")).upper(), to_num(r.get("inr_rate"))))
    for r in S.rows_to_dicts(tabs.get(S.ACCRUALS, [])):
        m = r.get("month")
        m = to_date(m)[:7] if not re.fullmatch(r"\d{4}-\d{2}", str(m)) else str(m)
        db.execute("INSERT INTO accruals VALUES (?,?,?,?)", (m, _txt(r.get("partner")), _txt(r.get("currency")).upper(),
                                                              to_num(r.get("accrued_amount"))))

    # ---- apply each payment to the invoice(s) it references
    invoice_amounts = {row["invoice_no"]: row["invoice_amount"] or 0 for row in
                       db.execute("SELECT invoice_no, invoice_amount FROM tracker WHERE invoice_no <> '' ORDER BY row_no DESC")}
    unmatched = []
    for r in S.rows_to_dicts(tabs.get(S.REMITTANCES, [])):
        rec = (_txt(r.get("remittance_id")), to_date(r.get("date")), _txt(r.get("partner")), _txt(r.get("currency")).upper(),
               to_num(r.get("amount")) or 0, _txt(r.get("invoice_ref")), _txt(r.get("bank_ref")), _txt(r.get("notes")))
        db.execute("INSERT INTO remittances VALUES (?,?,?,?,?,?,?,?)", rec)
        refs = [x for x in split_refs(rec[5]) if x in invoice_amounts]
        if not refs:
            unmatched.append(dict(zip(["remittance_id", "date", "partner", "currency", "amount", "invoice_ref"], rec[:6])))
            continue
        total = sum(invoice_amounts[x] for x in refs) or len(refs)
        for x in refs:                                       # batch payment: split pro rata
            share = rec[4] if len(refs) == 1 else rec[4] * (invoice_amounts[x] or 1) / total
            db.execute("INSERT INTO allocations VALUES (?,?,?,?)", (rec[0], x, round(share, 2), rec[1]))
    db.commit()
    return db, unmatched
