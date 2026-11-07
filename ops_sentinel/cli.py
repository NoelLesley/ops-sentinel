"""Command line (for you, cron, or GitHub Actions).

  python -m ops_sentinel.cli sample            # (re)build data/sample_tracker.xlsx with fake data
  python -m ops_sentinel.cli run               # check the tracker and write results back into it
  python -m ops_sentinel.cli report            # run + write an HTML report + print the summary

Which tracker? Set OPS_SOURCE (see sources.py), or pass --source:
  --source xlsx:data/sample_tracker.xlsx
  --source sheets:<google spreadsheet id>
"""
from __future__ import annotations

import argparse
import json
from datetime import date

from .engine import Engine
from .generate import build, write_xlsx
from .report import markdown, write_html
from .sources import ROOT, get_source


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="ops-sentinel")
    p.add_argument("--source", help="xlsx:<path> or sheets:<spreadsheet id>")
    p.add_argument("--as-of", type=date.fromisoformat, help="pretend today is this date (YYYY-MM-DD)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--out", default=str(ROOT / "data" / "sample_tracker.xlsx"))
    sub.add_parser("run")
    r = sub.add_parser("report")
    r.add_argument("--out", default="reports")
    a = p.parse_args(argv)

    if a.cmd == "sample":
        print(f"Wrote {write_xlsx(build(as_of=a.as_of), a.out)}")
        return
    engine = Engine(get_source(a.source), as_of=a.as_of)
    result = engine.run()
    if a.cmd == "run":
        print(json.dumps(result.__dict__, indent=2, default=str))
    else:
        print(f"HTML report: {write_html(engine, a.out)}\n")
        print(markdown(engine))


if __name__ == "__main__":
    main()
