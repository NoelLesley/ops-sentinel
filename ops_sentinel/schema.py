"""The commission tracker's layout: tab names and column headers.

Headers are the human-readable names you see in the spreadsheet. Internally every
header is turned into a snake_case key ("Invoice No" -> invoice_no) so the code
doesn't care about capitalisation or spacing.
"""
from __future__ import annotations

import re

TRACKER = "Tracker"
PARTNERS = "Partners"
REMITTANCES = "Remittances"
ACCRUALS = "Accruals"
FX = "FX"
ISSUES = "Issues"
KPIS = "KPIs"
KPI_HISTORY = "KPI History"

# Columns ops fills in by hand (the engine only reads these)
TRACKER_INPUT = [
    "Case ID", "Student", "Counsellor", "Partner", "Country", "Currency", "Intake",
    "Tuition Fee", "Visa Status", "Visa Approved On", "Enrolment Confirmed",
    "Invoice No", "Invoice Date", "Invoice Amount", "Due Date", "Remarks",
]
# Columns the engine calculates and writes back on every run
TRACKER_AUTO = [
    "Expected Commission", "Received", "Outstanding", "Outstanding (INR)",
    "Days Overdue", "Ageing", "Recon Status", "Last Checked",
]
TRACKER_HEADERS = TRACKER_INPUT + TRACKER_AUTO

PARTNER_HEADERS = ["Partner", "Country", "Currency", "Commission Rate", "Payment Terms (days)", "Contact"]
REMITTANCE_HEADERS = ["Remittance ID", "Date", "Partner", "Currency", "Amount", "Invoice Ref", "Bank Ref", "Notes"]
ACCRUAL_HEADERS = ["Month", "Partner", "Currency", "Accrued Amount"]
FX_HEADERS = ["Currency", "INR Rate", "As Of"]

# Issues: engine writes the first block; people edit Status / Owner / Note
ISSUE_HEADERS = ["Issue ID", "Check", "Severity", "Ref", "Partner", "Summary", "At Risk (INR)",
                 "Status", "Owner", "Note", "First Seen", "Last Seen", "Resolved On"]
KPI_HEADERS = ["Metric", "Value", "What it means"]


def key(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.lower()).strip("_")


def rows_to_dicts(rows: list[list]) -> list[dict]:
    """First row is the header. Blank rows are skipped."""
    if not rows:
        return []
    keys = [key(str(h)) for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any(str(v).strip() for v in r):
            continue
        r = list(r) + [""] * (len(keys) - len(r))
        out.append(dict(zip(keys, r)))
    return out


def dicts_to_rows(headers: list[str], dicts: list[dict]) -> list[list]:
    keys = [key(h) for h in headers]
    return [headers] + [[d.get(k, "") for k in keys] for d in dicts]
