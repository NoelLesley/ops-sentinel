from datetime import date

import pytest

from ops_sentinel import schema as S
from ops_sentinel.engine import Engine
from ops_sentinel.generate import build, write_xlsx
from ops_sentinel.model import build_db, to_date
from ops_sentinel.rules import RULES, inr, run_all
from ops_sentinel.sources import ExcelSource, SheetsSource

AS_OF = date(2026, 11, 7)
PARTNER = ["Test Uni", "UK", "GBP", 0.15, 30, "x@example.edu"]


def case(cid="C1", fee=20000, visa="Approved", approved="2026-08-01", enrolled="Yes",
         inv="", inv_date="", amount="", due=""):
    d = dict(case_id=cid, student="S", counsellor="A", partner="Test Uni", country="UK", currency="GBP",
             intake="Sep 2026", tuition_fee=fee, visa_status=visa, visa_approved_on=approved,
             enrolment_confirmed=enrolled, invoice_no=inv, invoice_date=inv_date, invoice_amount=amount, due_date=due)
    return d


def tabs(cases, remits=(), accruals=()):
    return {S.TRACKER: S.dicts_to_rows(S.TRACKER_HEADERS, cases),
            S.PARTNERS: [S.PARTNER_HEADERS, PARTNER],
            S.FX: [S.FX_HEADERS, ["GBP", 100, "2026-11-01"]],
            S.REMITTANCES: [S.REMITTANCE_HEADERS] + [list(r) for r in remits],
            S.ACCRUALS: [S.ACCRUAL_HEADERS] + [list(a) for a in accruals]}


def checks(t):
    db, unmatched = build_db(t)
    return {f.issue_id: f for f in run_all(db, AS_OF, unmatched)}


def test_inr_and_dates():
    assert inr(5190500) == "₹51,90,500" and inr(999) == "₹999"
    assert to_date("07/11/2026") == "2026-11-07" and to_date(46333) == "2026-11-07" and to_date("") == ""


def test_clean_paid_case_has_no_issues():
    c = case(inv="INV-1", inv_date="2026-08-10", amount=3000, due="2026-09-09")
    assert checks(tabs([c], [["R1", "2026-09-01", "Test Uni", "GBP", 3000, "INV-1", "B", ""]])) == {}


def test_invoice_not_raised_respects_sla():
    found = checks(tabs([case("C1", approved="2026-09-01"), case("C2", approved="2026-11-01")]))
    assert "INVOICE_NOT_RAISED:C1" in found and "INVOICE_NOT_RAISED:C2" not in found
    assert found["INVOICE_NOT_RAISED:C1"].at_risk_inr == 20000 * 0.15 * 100


def test_overdue_ageing_severity():
    t = tabs([case("C1", inv="I1", inv_date="2026-09-01", amount=3000, due="2026-10-25"),
              case("C2", inv="I2", inv_date="2026-06-01", amount=3000, due="2026-07-01")])
    f = checks(t)
    assert f["COMMISSION_OVERDUE:I1"].severity == "medium" and f["COMMISSION_OVERDUE:I2"].severity == "critical"


def test_short_bank_charges_over_and_unapplied():
    cs = [case("C1", inv="I1", inv_date="2026-08-01", amount=3000, due="2026-09-01"),
          case("C2", inv="I2", inv_date="2026-08-01", amount=3000, due="2026-09-01"),
          case("C3", inv="I3", inv_date="2026-08-01", amount=3000, due="2026-09-01")]
    rs = [["R1", "2026-09-01", "Test Uni", "GBP", 2400, "I1", "B", ""],      # 20% short
          ["R2", "2026-09-01", "Test Uni", "GBP", 2975, "I2", "B", ""],      # bank charges
          ["R3", "2026-09-01", "Test Uni", "GBP", 3000, "I3", "B", ""],
          ["R4", "2026-09-05", "Test Uni", "GBP", 3000, "I3", "B", ""],      # paid twice
          ["R5", "2026-09-05", "Test Uni", "GBP", 1000, "I9X", "B", ""]]     # unknown ref
    f = checks(tabs(cs, rs))
    assert f["SHORT_PAYMENT:I1"].severity == "high"
    assert f["BANK_CHARGES:I2"].severity == "low"
    assert f["OVERPAYMENT:I3"].at_risk_inr == 3000 * 100
    assert "UNAPPLIED_CASH:R5" in f


def test_batch_remittance_splits_across_invoices():
    cs = [case("C1", inv="I1", inv_date="2026-08-01", amount=3000, due="2026-09-01"),
          case("C2", inv="I2", inv_date="2026-08-01", amount=1000, due="2026-09-01")]
    f = checks(tabs(cs, [["R1", "2026-09-01", "Test Uni", "GBP", 4000, "I1; I2", "B", ""]]))
    assert not any(k.startswith(("SHORT", "COMMISSION_OVERDUE", "UNAPPLIED")) for k in f)


def test_rate_mismatch_and_duplicate_invoice():
    cs = [case("C1", inv="I1", inv_date="2026-10-01", amount=2400, due="2026-12-01"),   # 12% billed vs 15%
          case("C2", inv="I1", inv_date="2026-10-02", amount=3000, due="2026-12-01")]
    f = checks(tabs(cs))
    assert "RATE_MISMATCH:I1" in f and f["DUPLICATE_INVOICE_NO:I1"].severity == "critical"


def test_lifecycle_keeps_human_edits_auto_resolves_and_reopens(tmp_path):
    p = tmp_path / "t.xlsx"
    t = tabs([case("C1", approved="2026-09-01")])
    t[S.ISSUES] = [S.ISSUE_HEADERS]
    write_xlsx(t, p)

    assert Engine(ExcelSource(p), as_of=AS_OF).run().new == 1
    e = Engine(ExcelSource(p), as_of=AS_OF)
    e.update_issue("INVOICE_NOT_RAISED:C1", status="acknowledged", owner="Priya")
    e = Engine(ExcelSource(p), as_of=AS_OF)
    e.run()
    assert e.issues["INVOICE_NOT_RAISED:C1"]["owner"] == "Priya"            # human edit survives a run

    src = ExcelSource(p)                                                     # someone raises the invoice
    ws = src.wb[S.TRACKER]
    col = {S.key(c.value): c.column for c in ws[1]}
    ws.cell(2, col["invoice_no"]).value, ws.cell(2, col["invoice_date"]).value = "I1", "2026-11-06"
    ws.cell(2, col["invoice_amount"]).value, ws.cell(2, col["due_date"]).value = 3000, "2026-12-06"
    src.save()
    assert Engine(ExcelSource(p), as_of=AS_OF).run().auto_resolved == 1

    e = Engine(ExcelSource(p), as_of=AS_OF)
    tracker = ExcelSource(p).read(S.TRACKER)
    assert tracker[1][tracker[0].index("Recon Status")] == "Invoiced, not due"   # engine filled the tracker


def test_sample_data_trips_every_check(tmp_path):
    p = write_xlsx(build(as_of=AS_OF), tmp_path / "s.xlsx")
    e = Engine(ExcelSource(p), as_of=AS_OF)
    e.run()
    assert {i["check"] for i in e.open_issues()} == {r.code for r in RULES}


def test_sheets_source_with_fake_client():
    class WS:
        def __init__(self, title, rows): self.title, self.rows, self.row_count = title, rows, 100
        def get_all_values(self, **kw): return self.rows
        def clear(self): self.rows = []
        def batch_clear(self, ranges): pass
        def update(self, rows, start, **kw): self.rows = rows if start == "A1" else self.rows

    class SH:
        title = "Fake"
        def __init__(self, tabs): self.ws = {k: WS(k, v) for k, v in tabs.items()}
        def worksheet(self, t):
            if t not in self.ws: raise KeyError(t)
            return self.ws[t]
        def add_worksheet(self, title, rows, cols): self.ws[title] = WS(title, []); return self.ws[title]
        def worksheets(self): return list(self.ws.values())

    class Client:
        def __init__(self, tabs): self.sh = SH(tabs)
        def open_by_key(self, k): return self.sh

    t = tabs([case("C1", approved="2026-09-01")])
    client = Client(t)
    res = Engine(SheetsSource("abc", client=client), as_of=AS_OF).run()
    assert res.new == 1 and client.sh.ws[S.ISSUES].rows[1][0] == "INVOICE_NOT_RAISED:C1"
