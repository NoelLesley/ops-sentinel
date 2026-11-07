from datetime import date

import pytest

from ops_sentinel import engine
from ops_sentinel.db import connect, set_meta
from ops_sentinel.generate import generate, simulate_day
from ops_sentinel.rules import RULES, inr, run_all

AS_OF = date(2026, 11, 7)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    set_meta(c, "as_of", AS_OF.isoformat())
    return c


def add_case(c, cid, stage="deposit_paid", stage_at="2026-11-01", visa=None):
    c.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?)", (cid, "Test Student", "Counsellor A", "Uni", "UK", stage, stage_at, visa))


def add_deposit(c, did, cid, amount=100_000, due="2026-11-01", received=None, ref=None, status=None):
    c.execute("INSERT INTO deposits VALUES (?,?,?,?,?,?,?,?)",
              (did, cid, amount, amount if received else None, due, received, ref, status or ("received" if received else "pending")))


def add_txn(c, tid, ref, amount, when="2026-11-01"):
    c.execute("INSERT INTO ledger VALUES (?,?,?,?,?)", (tid, ref, amount, when, "UPI"))


def rules_hit(c):
    return {f.rule for f in run_all(c, AS_OF)}


def test_inr_grouping():
    assert inr(5190500) == "₹51,90,500"
    assert inr(999) == "₹999"
    assert inr(100000) == "₹1,00,000"


def test_clean_case_raises_nothing(conn):
    add_case(conn, "C1")
    add_deposit(conn, "D1", "C1", received="2026-10-30", ref="GW1")
    add_txn(conn, "T1", "GW1", 100_000)
    assert rules_hit(conn) == set()


def test_duplicate_and_mismatch_and_orphan(conn):
    add_case(conn, "C1"); add_deposit(conn, "D1", "C1", received="2026-10-30", ref="GW1")
    add_txn(conn, "T1", "GW1", 100_000); add_txn(conn, "T2", "GW1", 100_000)      # duplicate
    add_case(conn, "C2"); add_deposit(conn, "D2", "C2", received="2026-10-30", ref="GW2")
    add_txn(conn, "T3", "GW2", 97_500)                                              # short
    add_txn(conn, "T4", "GW_UNKNOWN", 50_000)                                       # orphan
    found = {f.issue_id: f for f in run_all(conn, AS_OF)}
    assert found["DUPLICATE_SETTLEMENT:GW1"].amount_at_risk == 100_000
    assert found["AMOUNT_MISMATCH:D2"].amount_at_risk == 2_500
    assert "UNRECONCILED_SETTLEMENT:T4" in found


def test_overdue_severity_escalates(conn):
    add_case(conn, "C1", stage="offer"); add_deposit(conn, "D1", "C1", due="2026-11-04")
    add_case(conn, "C2", stage="offer"); add_deposit(conn, "D2", "C2", due="2026-10-10")
    add_case(conn, "C3", stage="dropped"); add_deposit(conn, "D3", "C3", due="2026-09-01", status="cancelled")
    sev = {f.entity: f.severity for f in run_all(conn, AS_OF) if f.rule == "DEPOSIT_OVERDUE"}
    assert sev == {"D1": "medium", "D2": "high"}


def test_invoice_leakage_respects_sla(conn):
    add_case(conn, "C1", stage="visa_approved", visa="2026-10-20"); add_deposit(conn, "D1", "C1", received="2026-10-01", ref="G1"); add_txn(conn, "T1", "G1", 100_000)
    add_case(conn, "C2", stage="visa_approved", visa="2026-11-05"); add_deposit(conn, "D2", "C2", received="2026-10-01", ref="G2"); add_txn(conn, "T2", "G2", 100_000)
    hits = [f.entity for f in run_all(conn, AS_OF) if f.rule == "INVOICE_NOT_RAISED"]
    assert hits == ["C1"]                      # C2 is still inside the 7-day SLA


def test_issue_lifecycle_auto_resolve_and_reopen(conn):
    add_case(conn, "C1", stage="offer"); add_deposit(conn, "D1", "C1", due="2026-11-01")
    assert engine.run_checks(conn)["new"] == 1
    conn.execute("UPDATE deposits SET status='received', received_at='2026-11-07', amount_received=100000, gateway_ref='G1'")
    add_txn(conn, "T1", "G1", 100_000, "2026-11-07")
    assert engine.run_checks(conn)["auto_resolved"] == 1
    conn.execute("UPDATE deposits SET status='pending', received_at=NULL, gateway_ref=NULL")
    conn.execute("DELETE FROM ledger")
    assert engine.run_checks(conn)["reopened"] == 1


def test_generated_data_trips_every_rule(tmp_path):
    c = generate(tmp_path / "g.db", as_of=AS_OF)
    assert rules_hit(c) >= {r.code for r in RULES} - {"SETTLEMENT_VOLUME_DROP"}


def test_simulate_day_moves_clock(tmp_path):
    c = generate(tmp_path / "g.db", as_of=AS_OF)
    engine.run_checks(c)
    out = simulate_day(c, seed=1)
    assert out["as_of"] == "2026-11-08" and out["new_cases"] > 0
    assert engine.kpis(c)["as_of"] == "2026-11-08"
