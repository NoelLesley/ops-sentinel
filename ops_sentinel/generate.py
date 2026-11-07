"""Builds a fake commission receivables tracker.

The business: a study-abroad company earns commission from partner universities
when a student it placed enrols. Ops raises an invoice in the partner's currency,
then matches incoming payments (remittances) against open invoices.

Every name, university, rate and amount here is invented. The generator plants
known problems at realistic rates so the checks have something to find:

* visa approved + enrolled, but no invoice raised          (revenue leakage)
* visa approved long ago, enrolment never confirmed
* invoice billed at the wrong commission rate
* the same invoice number used on two cases
* overdue commission, aged 1-30 / 31-60 / 60+ days
* short payments (bank charges vs genuine shortfall)
* double payments
* remittances with a missing or mistyped invoice reference (unapplied cash)
* monthly accruals that drift from what was actually invoiced
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

from . import schema as S

# name, country, currency, rate, payment terms (days)
PARTNERS = [
    ("Northbridge University", "UK", "GBP", 0.15, 45),
    ("Kingsmere College London", "UK", "GBP", 0.18, 30),
    ("Riverside University", "UK", "GBP", 0.15, 60),
    ("Lakeshore College", "Canada", "CAD", 0.15, 45),
    ("Maple Ridge Polytechnic", "Canada", "CAD", 0.12, 30),
    ("Westfield University", "Canada", "CAD", 0.10, 60),
    ("Southern Cross University of Tech", "Australia", "AUD", 0.15, 45),
    ("Harbourview University", "Australia", "AUD", 0.12, 60),
    ("Eastgate Institute", "Australia", "AUD", 0.15, 45),
    ("Liffey Technological University", "Ireland", "EUR", 0.12, 30),
    ("Shannon Business School", "Ireland", "EUR", 0.12, 45),
    ("Desert State University", "USA", "USD", 0.10, 60),
]
FEES = {"UK": (15_000, 22_000), "Canada": (16_000, 25_000), "Australia": (30_000, 45_000),
        "Ireland": (12_000, 18_000), "USA": (25_000, 40_000)}
FX_RATES = {"GBP": 112.4, "CAD": 61.2, "AUD": 55.6, "EUR": 97.1, "USD": 84.3}   # sample rates, not live
INTAKES = ["Jan 2027", "May 2027"]          # for cases still waiting on a visa
FIRST = ["Aarav", "Diya", "Rohan", "Ananya", "Kabir", "Isha", "Vihaan", "Meera", "Arjun", "Sara", "Aditya",
         "Riya", "Kunal", "Tara", "Nikhil", "Pooja", "Rahul", "Sneha", "Varun", "Neha", "Ishaan", "Kavya"]
LAST = ["Sharma", "Iyer", "Reddy", "Menon", "Gupta", "Nair", "Rao", "Das", "Patel", "Joshi", "Khan", "Pillai", "Bose"]
COUNSELLORS = ["Counsellor A", "Counsellor B", "Counsellor C", "Counsellor D", "Counsellor E", "Counsellor F"]


def _intake_after(d: date) -> str:
    """Next Jan / May / Sep intake starting at least a month after the visa."""
    m = d + timedelta(days=30)
    for year in (m.year, m.year + 1):
        for month, label in ((1, "Jan"), (5, "May"), (9, "Sep")):
            if date(year, month, 1) >= m:
                return f"{label} {year}"
    return f"Jan {m.year + 1}"


def _code(name: str) -> str:
    return "".join(w[0] for w in name.split())[:3].upper()


def build(as_of: date | None = None, n_cases: int = 320, seed: int = 11) -> dict[str, list[list]]:
    rng = random.Random(seed)
    as_of = as_of or date.today()
    partner_info = {p[0]: p for p in PARTNERS}

    tracker, remits = [], []
    inv_seq: dict[str, int] = {}
    rem_seq = 0
    invoices_for_dupe = []

    def remit(when, partner, ccy, amount, ref, note=""):
        nonlocal rem_seq
        rem_seq += 1
        remits.append([f"REM-{rem_seq:04d}", when, partner, ccy, round(amount, 2), ref,
                       f"{'SWIFT' if ccy != 'INR' else 'UTR'}{rng.randint(10**9, 10**10 - 1)}", note])

    for i in range(1, n_cases + 1):
        name, country, ccy, rate, terms = rng.choice(PARTNERS)
        fee = round(rng.uniform(*FEES[country]), -2)
        row = {k: "" for k in map(S.key, S.TRACKER_INPUT)}
        row.update(case_id=f"C{i:04d}", student=f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                   counsellor=rng.choice(COUNSELLORS), partner=name, country=country, currency=ccy,
                   intake=rng.choice(INTAKES), tuition_fee=fee)

        approved_on = as_of - timedelta(days=rng.randint(0, 210))
        r = rng.random()
        if approved_on > as_of - timedelta(days=20) and r < 0.35:
            row["visa_status"] = "Pending"
        elif r < 0.08:
            row["visa_status"] = "Refused"
        else:
            row["visa_status"] = "Approved"
            row["visa_approved_on"] = approved_on
            row["intake"] = _intake_after(approved_on)
            age = (as_of - approved_on).days
            enrolled = rng.random() < (0.93 if age > 30 else 0.55)
            row["enrolment_confirmed"] = "Yes" if enrolled else "No"
            if enrolled:
                inv_date = approved_on + timedelta(days=rng.randint(6, 20))
                if inv_date <= as_of and rng.random() < 0.88:       # ~12% never invoiced
                    inv_seq[name] = inv_seq.get(name, 0) + 1
                    inv_no = f"INV-{_code(name)}-{inv_seq[name]:03d}"
                    billed_rate = rate if rng.random() > 0.04 else round(rate - 0.03, 2)   # wrong rate
                    amount = round(fee * billed_rate, 2)
                    due = inv_date + timedelta(days=terms)
                    row.update(invoice_no=inv_no, invoice_date=inv_date, invoice_amount=amount, due_date=due)
                    invoices_for_dupe.append(row)

                    # ---- payment behaviour
                    paid_on = None
                    if due <= as_of and rng.random() < (0.9 if due < as_of - timedelta(days=60) else 0.72):
                        paid_on = min(due + timedelta(days=rng.randint(-15, 20)), as_of)
                    elif due > as_of and rng.random() < 0.15:
                        paid_on = min(inv_date + timedelta(days=rng.randint(5, 25)), as_of)
                    if paid_on:
                        p = rng.random()
                        if p < 0.08:
                            remit(paid_on, name, ccy, amount - rng.choice([15, 20, 25, 35]), inv_no, "bank charges deducted")
                        elif p < 0.12:
                            remit(paid_on, name, ccy, round(amount * 0.8, 2), inv_no)
                        elif p < 0.135:
                            remit(paid_on, name, ccy, amount, inv_no)
                            remit(paid_on + timedelta(days=rng.randint(3, 10)), name, ccy, amount, inv_no)
                        elif p < 0.17:
                            typo = inv_no[:-1] + str((int(inv_no[-1]) + 1) % 10) + "X" if rng.random() < 0.5 else ""
                            remit(paid_on, name, ccy, amount, typo, "" if typo else "commission payment")
                        else:
                            remit(paid_on, name, ccy, amount, inv_no)
        tracker.append(row)

    # same invoice number used on a second case (copy-paste error)
    for a, b in rng.sample([(x, y) for x in invoices_for_dupe for y in invoices_for_dupe
                            if x["partner"] == y["partner"] and x is not y][:4000], 2):
        b["invoice_no"] = a["invoice_no"]

    # a couple of payments nobody can place at all
    for _ in range(3):
        name, _, ccy, _, _ = rng.choice(PARTNERS)
        remit(as_of - timedelta(days=rng.randint(1, 40)), name, ccy, round(rng.uniform(1500, 5000), 2), "",
              "remittance advice not received")

    # ---- accruals: finance's monthly forecast of commission earned, by partner
    accruals = []
    month_start = date(as_of.year, as_of.month, 1)
    months = []
    m = month_start
    for _ in range(6):
        m = (m - timedelta(days=1)).replace(day=1)
        months.append(m)
    for m in sorted(months):
        nxt = (m + timedelta(days=32)).replace(day=1)
        for name, _, ccy, rate, _ in PARTNERS:
            exp = sum(t["tuition_fee"] * rate for t in tracker
                      if t["partner"] == name and t["visa_status"] == "Approved" and t["enrolment_confirmed"] == "Yes"
                      and m <= t["visa_approved_on"] < nxt)
            if exp:
                accruals.append([m.strftime("%Y-%m"), name, ccy, round(exp * rng.uniform(0.96, 1.06), -1)])

    rem_rows = sorted(remits, key=lambda r: r[1])
    for n, r in enumerate(rem_rows, start=1):        # number in bank-statement order
        r[0] = f"REM-{n:04d}"
    return {
        S.TRACKER: S.dicts_to_rows(S.TRACKER_HEADERS, tracker),
        S.PARTNERS: [S.PARTNER_HEADERS] + [[n, c, ccy, rate, t, f"partnerships@{_code(n).lower()}.example.edu"]
                                           for n, c, ccy, rate, t in PARTNERS],
        S.REMITTANCES: [S.REMITTANCE_HEADERS] + rem_rows,
        S.ACCRUALS: [S.ACCRUAL_HEADERS] + accruals,
        S.FX: [S.FX_HEADERS] + [[c, r, as_of] for c, r in FX_RATES.items()],
        S.ISSUES: [S.ISSUE_HEADERS],
        S.KPIS: [S.KPI_HEADERS],
        S.KPI_HISTORY: [["Date"]],
    }


HOW_TO = [
    ["How this tracker works"],
    ["All data in this workbook is fake, generated for a portfolio project."],
    [""],
    ["Tracker: one row per student. Ops fills the white columns; Ops Sentinel fills the grey columns (Expected Commission onwards) on every run."],
    ["Partners: commission rate and payment terms per university."],
    ["Remittances: payments received, as they appear on the bank statement. 'Invoice Ref' links a payment to invoices (separate several with ;)."],
    ["Accruals: finance's monthly forecast of commission, by partner."],
    ["FX: sample conversion rates to INR."],
    ["Issues: problems the checks found. Edit Status (open / acknowledged / resolved), Owner and Note; the engine keeps your edits."],
    ["KPIs / KPI History: headline numbers, refreshed every run."],
]


def write_xlsx(tabs: dict[str, list[list]], path: str | Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("How to use")
    for r in HOW_TO:
        ws.append(r)
    ws["A1"].font = Font(bold=True, size=13)
    ws.column_dimensions["A"].width = 120

    bold, grey = Font(bold=True), PatternFill("solid", fgColor="E7E9EC")
    for tab, rows in tabs.items():
        ws = wb.create_sheet(tab)
        for r in rows:
            ws.append(r)
        for c in ws[1]:
            c.font = bold
        if tab == S.TRACKER:
            for c in ws[1][len(S.TRACKER_INPUT):]:
                c.fill = grey
        ws.freeze_panes = "B2"
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col[:50])
            ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 40)
            for c in col[1:]:
                if isinstance(c.value, date):
                    c.number_format = "yyyy-mm-dd"
    wb.save(path)
    return path
