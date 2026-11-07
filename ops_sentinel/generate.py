"""Synthetic data generator.

Builds ~120 days of payments/deposits operations for a study-abroad / cross-border
payments business, then plants realistic failure modes so the checks have
something to catch:

* short-settled payments (gateway amount != expected)
* duplicate settlements (same gateway ref twice)
* orphan settlements (money in the bank with no matching deposit)
* overdue deposits
* visa approved but invoice never raised (revenue leakage)
* overdue partner commissions
* a settlement-volume dip on the latest day (gateway outage scenario)

All data is fake. Nothing here comes from a real company.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta

from .db import connect, set_meta

FIRST = ["Aarav", "Diya", "Rohan", "Ananya", "Kabir", "Isha", "Vihaan", "Meera", "Arjun", "Sara",
         "Aditya", "Riya", "Kunal", "Tara", "Nikhil", "Pooja", "Rahul", "Sneha", "Varun", "Neha"]
LAST = ["Sharma", "Iyer", "Reddy", "Menon", "Gupta", "Nair", "Rao", "Das", "Patel", "Joshi", "Khan", "Pillai"]
COUNSELLORS = ["Counsellor A", "Counsellor B", "Counsellor C", "Counsellor D", "Counsellor E"]
PARTNERS = [("Univ of Leeds", "UK"), ("Coventry Univ", "UK"), ("Univ of Toronto", "Canada"),
            ("Conestoga College", "Canada"), ("Monash Univ", "Australia"), ("Deakin Univ", "Australia"),
            ("TU Dublin", "Ireland"), ("Arizona State", "USA")]
DEPOSIT_SIZES = [50_000, 75_000, 100_000, 150_000, 250_000]
CHANNELS = ["UPI", "NEFT", "card", "wire"]


def _d(x: date) -> str:
    return x.isoformat()


def generate(db_path=None, as_of: date | None = None, n_cases: int = 800, seed: int = 7) -> sqlite3.Connection:
    rng = random.Random(seed)
    as_of = as_of or date.today()
    conn = connect(db_path)
    for t in ("cases", "deposits", "ledger", "invoices", "issues"):
        conn.execute(f"DELETE FROM {t}")
    set_meta(conn, "as_of", _d(as_of))

    start = as_of - timedelta(days=120)
    txn_seq = 0
    dip_day = as_of - timedelta(days=1)

    def add_txn(ref: str, amount: float, when: date) -> None:
        nonlocal txn_seq
        txn_seq += 1
        conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?)",
                     (f"TXN{txn_seq:06d}", ref, round(amount, 2), _d(when), rng.choice(CHANNELS)))

    for i in range(1, n_cases + 1):
        cid = f"C{i:05d}"
        created = start + timedelta(days=rng.randint(0, 118))
        partner, country = rng.choice(PARTNERS)
        expected = float(rng.choice(DEPOSIT_SIZES))
        due = created + timedelta(days=rng.randint(7, 14))

        paid_on = None
        if due <= as_of and rng.random() < 0.88:
            paid_on = due + timedelta(days=rng.randint(-5, 3))
        elif due > as_of and rng.random() < 0.40:
            paid_on = created + timedelta(days=rng.randint(1, 5))
        if paid_on and paid_on > as_of:
            paid_on = as_of
        # gateway outage: most payments that would land on dip_day slip to "not yet received"
        if paid_on == dip_day and rng.random() < 0.75:
            paid_on = None

        stage, stage_at, visa_at = "offer", created, None
        ref = None
        if paid_on:
            ref = f"GW{rng.randint(10**8, 10**9 - 1)}"
            stage, stage_at = "deposit_paid", paid_on
            settle_amt = expected
            r = rng.random()
            if r < 0.025:                       # short settlement
                settle_amt = expected - rng.choice([1_000, 2_500, 5_000])
            add_txn(ref, settle_amt, paid_on)
            if rng.random() < 0.012:            # duplicate settlement
                add_txn(ref, settle_amt, paid_on + timedelta(days=1) if paid_on < as_of else paid_on)

            filed = paid_on + timedelta(days=rng.randint(7, 12))
            if filed <= as_of:
                stage, stage_at = "visa_filed", filed
                approved = filed + timedelta(days=rng.randint(10, 20))
                if approved <= as_of:
                    if rng.random() < 0.8:
                        stage, stage_at, visa_at = "visa_approved", approved, approved
                    elif rng.random() < 0.7:
                        stage, stage_at = "visa_rejected", approved

        status = "received" if paid_on else "pending"
        if not paid_on and due < as_of - timedelta(days=30) and rng.random() < 0.85:
            status, stage = "cancelled", "dropped"          # student dropped out; written off by ops
        conn.execute("INSERT INTO deposits VALUES (?,?,?,?,?,?,?,?)",
                     (f"D{i:05d}", cid, expected, expected if paid_on else None, _d(due),
                      _d(paid_on) if paid_on else None, ref, status))
        conn.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?)",
                     (cid, f"{rng.choice(FIRST)} {rng.choice(LAST)}", rng.choice(COUNSELLORS), partner, country,
                      stage, _d(stage_at), _d(visa_at) if visa_at else None))

        if visa_at and rng.random() < 0.88:     # ~12% never invoiced -> leakage
            raised = visa_at + timedelta(days=rng.randint(1, 6))
            if raised <= as_of:
                commission = round(expected * rng.uniform(0.10, 0.18), -2)
                cdue = raised + timedelta(days=30)
                received = None
                if cdue <= as_of and rng.random() < 0.75:
                    received = cdue + timedelta(days=rng.randint(-10, 5))
                    received = min(received, as_of)
                conn.execute("INSERT INTO invoices VALUES (?,?,?,?,?,?)",
                             (f"INV{i:05d}", cid, _d(raised), commission, _d(cdue),
                              _d(received) if received else None))

    for _ in range(6):                          # orphan settlements
        add_txn(f"GW{rng.randint(10**8, 10**9 - 1)}", float(rng.choice(DEPOSIT_SIZES)),
                as_of - timedelta(days=rng.randint(0, 20)))

    conn.commit()
    return conn


def simulate_day(conn: sqlite3.Connection, seed: int | None = None) -> dict:
    """Advance the clock one day: some pending deposits get paid, a few new cases arrive,
    and occasionally something goes wrong. Lets the system be demoed as 'live'."""
    from .db import get_meta
    rng = random.Random(seed)
    as_of = date.fromisoformat(get_meta(conn, "as_of")) + timedelta(days=1)
    set_meta(conn, "as_of", _d(as_of))

    paid = 0
    for row in conn.execute("SELECT deposit_id, case_id, amount_expected FROM deposits WHERE status='pending'").fetchall():
        if rng.random() < 0.35:
            ref = f"GW{rng.randint(10**8, 10**9 - 1)}"
            amt = row["amount_expected"] - (2_500 if rng.random() < 0.03 else 0)
            conn.execute("UPDATE deposits SET status='received', amount_received=amount_expected, received_at=?, gateway_ref=? WHERE deposit_id=?",
                         (_d(as_of), ref, row["deposit_id"]))
            conn.execute("UPDATE cases SET stage='deposit_paid', stage_updated_at=? WHERE case_id=?", (_d(as_of), row["case_id"]))
            n = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] + 1
            conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?)", (f"TXN{n:06d}", ref, amt, _d(as_of), rng.choice(CHANNELS)))
            paid += 1

    new = 0
    base = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    for k in range(rng.randint(4, 9)):
        i = base + k + 1
        partner, country = rng.choice(PARTNERS)
        conn.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?,NULL)",
                     (f"C{i:05d}", f"{rng.choice(FIRST)} {rng.choice(LAST)}", rng.choice(COUNSELLORS), partner, country, "offer", _d(as_of)))
        conn.execute("INSERT INTO deposits VALUES (?,?,?,NULL,?,NULL,NULL,'pending')",
                     (f"D{i:05d}", f"C{i:05d}", float(rng.choice(DEPOSIT_SIZES)), _d(as_of + timedelta(days=rng.randint(7, 14)))))
        new += 1
    conn.commit()
    return {"as_of": _d(as_of), "deposits_paid": paid, "new_cases": new}


if __name__ == "__main__":
    c = generate()
    print({t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("cases", "deposits", "ledger", "invoices")})
