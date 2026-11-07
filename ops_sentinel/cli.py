"""Command line entry point (for cron / GitHub Actions).

  python -m ops_sentinel.cli seed            # build fresh synthetic data as of today
  python -m ops_sentinel.cli check           # run checks, print summary
  python -m ops_sentinel.cli report          # run checks, write HTML + print markdown
  python -m ops_sentinel.cli tick            # advance synthetic clock one day
"""
from __future__ import annotations

import argparse
import json
from datetime import date

from . import engine, report
from .db import connect
from .generate import generate, simulate_day


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="ops-sentinel")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed"); s.add_argument("--as-of", type=date.fromisoformat); s.add_argument("--seed", type=int, default=7)
    sub.add_parser("check")
    r = sub.add_parser("report"); r.add_argument("--out", default="reports")
    sub.add_parser("tick")
    a = p.parse_args(argv)

    if a.cmd == "seed":
        conn = generate(as_of=a.as_of, seed=a.seed)
        print(json.dumps(engine.run_checks(conn), indent=2))
        return
    conn = connect()
    if a.cmd == "check":
        print(json.dumps(engine.run_checks(conn), indent=2))
    elif a.cmd == "report":
        engine.run_checks(conn)
        print(f"HTML report: {report.write_html(conn, a.out)}\n")
        print(report.markdown(conn))
    elif a.cmd == "tick":
        out = simulate_day(conn)
        out["checks"] = engine.run_checks(conn)
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
