"""One run of Ops Sentinel:

  read tracker  ->  build scratch db  ->  run checks  ->  merge with Issues tab
       ->  fill the tracker's auto columns  ->  KPIs  ->  write everything back

People own the Status / Owner / Note columns in the Issues tab; the engine never
overwrites them, except to auto-resolve an issue whose condition has cleared or to
reopen one that was marked resolved but is still broken.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from . import schema as S
from .model import build_db, to_date
from .rules import Config, Finding, SEVERITY_ORDER, run_all
from .sources import Source, get_source

TABS = [S.TRACKER, S.PARTNERS, S.REMITTANCES, S.ACCRUALS, S.FX, S.ISSUES, S.KPI_HISTORY]


@dataclass
class RunResult:
    as_of: str
    source: str
    new: int = 0
    reopened: int = 0
    auto_resolved: int = 0
    open: int = 0
    by_severity: dict = field(default_factory=dict)
    at_risk_inr: float = 0
    kpis: dict = field(default_factory=dict)


class Engine:
    def __init__(self, source: Source | None = None, as_of: date | None = None, cfg: Config | None = None):
        self.source = source or get_source()
        self.as_of = as_of or date.today()
        self.cfg = cfg or Config()
        self.reload()

    # ------------------------------------------------------------------ read
    def reload(self):
        self.tabs = {t: self.source.read(t) for t in TABS}
        self.db, self.unmatched = build_db(self.tabs)
        self.issues = {i["issue_id"]: i for i in S.rows_to_dicts(self.tabs[S.ISSUES]) if i.get("issue_id")}

    # ------------------------------------------------------------------ issues
    def _sync_issues(self, findings: list[Finding], res: RunResult):
        today = self.as_of.isoformat()
        seen = set()
        for f in findings:
            seen.add(f.issue_id)
            cur = self.issues.get(f.issue_id)
            if cur is None:
                self.issues[f.issue_id] = dict(issue_id=f.issue_id, check=f.rule, severity=f.severity, ref=f.ref,
                                               partner=f.partner, summary=f.summary, at_risk_inr=f.at_risk_inr,
                                               status="open", owner="", note="", first_seen=today,
                                               last_seen=today, resolved_on="")
                res.new += 1
                continue
            if str(cur.get("status", "")).lower() == "resolved":
                cur["status"], cur["resolved_on"] = "open", ""
                cur["note"] = (str(cur.get("note") or "") + " [reopened: still failing]").strip()
                res.reopened += 1
            cur.update(severity=f.severity, summary=f.summary, at_risk_inr=f.at_risk_inr, last_seen=today,
                       first_seen=to_date(cur.get("first_seen")) or today)
        for iid, cur in self.issues.items():
            if iid not in seen and str(cur.get("status", "")).lower() != "resolved":
                cur["status"], cur["resolved_on"] = "resolved", today
                cur["note"] = (str(cur.get("note") or "") + " [auto-resolved: condition cleared]").strip()
                res.auto_resolved += 1

    def open_issues(self) -> list[dict]:
        rows = [i for i in self.issues.values() if str(i.get("status", "")).lower() != "resolved"]
        return sorted(rows, key=lambda i: (SEVERITY_ORDER.get(i.get("severity"), 9), -float(i.get("at_risk_inr") or 0)))

    # ------------------------------------------------------------------ tracker auto columns
    def _tracker_auto_rows(self) -> list[list]:
        today, tol = self.as_of, self.cfg.bank_charge_tolerance
        out = [S.TRACKER_AUTO]
        for r in self.db.execute("SELECT * FROM cases ORDER BY row_no"):
            exp = r["expected_commission"]
            if r["invoice_no"]:
                os_ = round((r["invoice_amount"] or 0) - r["received"], 2)
                due = date.fromisoformat(r["due_date"]) if r["due_date"] else None
                overdue = (today - due).days if due and due < today and os_ > tol else 0
                if os_ < -tol:
                    status, ageing = "Overpaid", "Paid"
                elif os_ <= tol and r["received"] > 0:
                    status, ageing = ("Paid" if abs(os_) < 0.01 else "Paid (bank charges)"), "Paid"
                elif r["received"] > 0:
                    status, ageing = "Short paid", self._bucket(overdue)
                elif overdue:
                    status, ageing = "Overdue", self._bucket(overdue)
                else:
                    status, ageing = "Invoiced, not due", "Not due"
                out.append([exp, r["received"], max(os_, 0), round(max(os_, 0) * r["inr_rate"]), overdue or "",
                            ageing, status, today.isoformat()])
            else:
                if r["visa_status"] == "Refused":
                    status = "Visa refused"
                elif r["visa_status"] != "Approved":
                    status = "Visa pending"
                elif r["enrolment_confirmed"] != "Yes":
                    status = "Awaiting enrolment"
                else:
                    status = "To invoice"
                out.append([exp if r["visa_status"] == "Approved" else "", "", "", "", "",
                            "Not invoiced", status, today.isoformat()])
        return out

    @staticmethod
    def _bucket(days: int) -> str:
        return "1-30" if days <= 30 else "31-60" if days <= 60 else "60+"

    # ------------------------------------------------------------------ KPIs
    def kpis(self) -> dict:
        q = lambda sql, *a: self.db.execute(sql, a).fetchone()[0]
        today = self.as_of.isoformat()
        sla_cut = (self.as_of - timedelta(days=self.cfg.invoice_sla_days)).isoformat()
        invoiceable = q("""SELECT COUNT(*) FROM cases WHERE visa_status='Approved' AND enrolment_confirmed='Yes'
                           AND visa_approved_on <= ?""", sla_cut)
        invoiced = q("""SELECT COUNT(*) FROM cases WHERE visa_status='Approved' AND enrolment_confirmed='Yes'
                        AND visa_approved_on <= ? AND invoice_no <> ''""", sla_cut)
        due_inr = q("SELECT COALESCE(SUM(invoice_amount*inr_rate),0) FROM cases WHERE invoice_no<>'' AND due_date<=?", today)
        recv_inr = q("""SELECT COALESCE(SUM(MIN(received, invoice_amount)*inr_rate),0) FROM cases
                        WHERE invoice_no<>'' AND due_date<=?""", today)
        auto = self._tracker_auto_rows()[1:]
        buckets = {"Not due": 0, "1-30": 0, "31-60": 0, "60+": 0}
        for a in auto:
            if a[5] in buckets and a[3] != "":
                buckets[a[5]] += a[3] or 0
        fx = {r["currency"]: r["inr_rate"] for r in self.db.execute("SELECT * FROM fx")}
        unapplied = sum(u["amount"] * fx.get(u["currency"], 0) for u in self.unmatched)
        unbilled = q("""SELECT COALESCE(SUM(expected_commission*inr_rate),0) FROM cases WHERE visa_status='Approved'
                        AND enrolment_confirmed='Yes' AND invoice_no='' AND visa_approved_on <= ?""", sla_cut)
        visa_to_inv = q("""SELECT AVG(julianday(invoice_date)-julianday(visa_approved_on)) FROM cases
                           WHERE invoice_no<>'' AND visa_approved_on<>''""")
        to_collect = q("""SELECT AVG(julianday(last_paid)-julianday(invoice_date)) FROM cases
                          WHERE invoice_no<>'' AND received >= invoice_amount - ?""", self.cfg.bank_charge_tolerance)
        last_closed = ((self.as_of - timedelta(days=self.cfg.accrual_close_days)).replace(day=1) - timedelta(days=1)).isoformat()[:7]
        acc = q("SELECT COALESCE(SUM(a.accrued_amount*f.inr_rate),0) FROM accruals a JOIN fx f USING(currency) WHERE month=?", last_closed)
        act = q("""SELECT COALESCE(SUM(invoice_amount*inr_rate),0) FROM cases
                   WHERE invoice_no<>'' AND substr(visa_approved_on,1,7)=?""", last_closed)
        open_ = self.open_issues()
        pct = lambda a, b: round(100 * a / b, 1) if b else None
        return {
            "invoice_coverage_pct": pct(invoiced, invoiceable),
            "avg_visa_to_invoice_days": round(visa_to_inv, 1) if visa_to_inv else None,
            "collection_rate_pct": pct(recv_inr, due_inr),
            "avg_days_to_collect": round(to_collect, 1) if to_collect else None,
            "outstanding_inr": round(sum(buckets.values())),
            "overdue_inr": round(buckets["1-30"] + buckets["31-60"] + buckets["60+"]),
            "ageing_not_due_inr": round(buckets["Not due"]),
            "ageing_1_30_inr": round(buckets["1-30"]),
            "ageing_31_60_inr": round(buckets["31-60"]),
            "ageing_60_plus_inr": round(buckets["60+"]),
            "unbilled_inr": round(unbilled),
            "unapplied_cash_inr": round(unapplied),
            "accrual_month": last_closed,
            "accrual_accuracy_pct": round(100 - abs(act - acc) / acc * 100, 1) if acc else None,
            "open_issues": len(open_),
            "at_risk_inr": round(sum(float(i.get("at_risk_inr") or 0) for i in open_)),
        }

    # ------------------------------------------------------------------ the run
    def run(self, write: bool = True) -> RunResult:
        res = RunResult(as_of=self.as_of.isoformat(), source=self.source.label)
        findings = run_all(self.db, self.as_of, self.unmatched, self.cfg)
        self._sync_issues(findings, res)
        open_ = self.open_issues()
        res.open = len(open_)
        for i in open_:
            res.by_severity[i["severity"]] = res.by_severity.get(i["severity"], 0) + 1
        res.kpis = self.kpis()
        res.at_risk_inr = res.kpis["at_risk_inr"]
        if write:
            self._write_back(res.kpis)
        return res

    def _write_issues(self):
        open_ = self.open_issues()
        open_ids = {i["issue_id"] for i in open_}
        closed = [i for i in self.issues.values() if i["issue_id"] not in open_ids]
        ordered = open_ + sorted(closed, key=lambda i: str(i.get("resolved_on")), reverse=True)
        self.source.write(S.ISSUES, S.dicts_to_rows(S.ISSUE_HEADERS, ordered))

    def update_issue(self, issue_id: str, status: str | None = None, owner: str | None = None,
                     note: str | None = None) -> dict | None:
        """A person's edit (via Claude). Written straight to the Issues tab."""
        cur = self.issues.get(issue_id)
        if cur is None:
            return None
        if status:
            cur["status"] = status
            cur["resolved_on"] = self.as_of.isoformat() if status == "resolved" else ""
        if owner is not None:
            cur["owner"] = owner
        if note:
            cur["note"] = note
        self._write_issues()
        self.source.save()
        return cur

    def _write_back(self, kpis: dict):
        self.source.write_columns(S.TRACKER, len(S.TRACKER_INPUT) + 1, self._tracker_auto_rows())
        self._write_issues()
        self.source.write(S.KPIS, [S.KPI_HEADERS] + [[k, "" if v is None else v, KPI_NOTES.get(k, "")] for k, v in kpis.items()])
        hist = S.rows_to_dicts(self.tabs.get(S.KPI_HISTORY) or [])
        hist = [h for h in hist if to_date(h.get("date")) != self.as_of.isoformat()]
        hist.append({"date": self.as_of.isoformat(), **kpis})
        cols = ["Date"] + list(kpis)
        self.source.write(S.KPI_HISTORY, [cols] + [[h.get(S.key(c), "") for c in cols] for h in hist])
        self.source.save()

    # ------------------------------------------------------------------ lookups (used by MCP tools)
    def case(self, ref: str) -> dict | None:
        ref = ref.strip().upper()
        row = self.db.execute("SELECT * FROM cases WHERE UPPER(case_id)=? OR invoice_no=?", (ref, ref)).fetchone()
        if not row:
            return None
        pays = [dict(p) for p in self.db.execute(
            "SELECT r.remittance_id, r.date, r.amount, r.currency, r.invoice_ref, a.amount AS applied "
            "FROM allocations a JOIN remittances r USING(remittance_id) WHERE a.invoice_no=?", (row["invoice_no"],))] if row["invoice_no"] else []
        issues = [i for i in self.issues.values() if i.get("ref") in (row["case_id"], row["invoice_no"])]
        return {"case": dict(row), "payments": pays, "issues": issues}

    def partner_ageing(self) -> list[dict]:
        auto = self._tracker_auto_rows()[1:]
        rows = list(self.db.execute("SELECT partner FROM cases ORDER BY row_no"))
        agg: dict[str, dict] = {}
        for r, a in zip(rows, auto):
            if a[3] in ("", None):
                continue
            d = agg.setdefault(r["partner"], {"partner": r["partner"], "Not due": 0, "1-30": 0, "31-60": 0, "60+": 0, "total_inr": 0})
            d[a[5]] = d.get(a[5], 0) + a[3]
            d["total_inr"] += a[3]
        return sorted(agg.values(), key=lambda d: -(d["31-60"] + d["60+"]))


KPI_NOTES = {
    "invoice_coverage_pct": "Enrolled cases past the invoice SLA that have been invoiced",
    "avg_visa_to_invoice_days": "Average days from visa approval to invoice",
    "collection_rate_pct": "Share of commission due to date that has been received (INR)",
    "avg_days_to_collect": "Average days from invoice to full payment",
    "outstanding_inr": "Invoiced but not yet received",
    "overdue_inr": "Outstanding and past due date",
    "unbilled_inr": "Commission on enrolled cases with no invoice yet (leakage)",
    "unapplied_cash_inr": "Received but not matched to any invoice",
    "accrual_accuracy_pct": "Last closed month: how close invoicing came to the accrual",
    "at_risk_inr": "Sum of 'At Risk' across open issues",
}
