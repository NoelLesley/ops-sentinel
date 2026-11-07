"""Where the tracker lives. The engine only talks to a Source, so swapping a local
Excel file for a live Google Sheet changes one setting, not the logic.

Choose with the OPS_SOURCE environment variable:
    OPS_SOURCE=xlsx:data/sample_tracker.xlsx        (default; no setup needed)
    OPS_SOURCE=sheets:<spreadsheet id>              (needs a Google service account key)
"""
from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = f"xlsx:{ROOT / 'data' / 'sample_tracker.xlsx'}"


class Source(Protocol):
    def read(self, tab: str) -> list[list]: ...
    def write(self, tab: str, rows: list[list]) -> None: ...                     # replace whole tab
    def write_columns(self, tab: str, first_col: int, rows: list[list]) -> None: ...  # replace a block of columns (1-based), from row 1
    def tabs(self) -> list[str]: ...
    def save(self) -> None: ...
    @property
    def label(self) -> str: ...


# ---------------------------------------------------------------- Excel
class ExcelSource:
    def __init__(self, path: str | Path):
        from openpyxl import load_workbook
        self.path = Path(path)
        self.wb = load_workbook(self.path)

    @property
    def label(self) -> str:
        return f"Excel file {self.path.name}"

    def tabs(self):
        return self.wb.sheetnames

    def read(self, tab):
        if tab not in self.wb.sheetnames:
            return []
        out = []
        for row in self.wb[tab].iter_rows(values_only=True):
            out.append(["" if v is None else (v.date().isoformat() if isinstance(v, datetime) else
                                              v.isoformat() if isinstance(v, date) else v) for v in row])
        return out

    def _sheet(self, tab):
        return self.wb[tab] if tab in self.wb.sheetnames else self.wb.create_sheet(tab)

    def write(self, tab, rows):
        ws = self._sheet(tab)
        ws.delete_rows(1, ws.max_row)
        for r in rows:
            ws.append(r)

    def write_columns(self, tab, first_col, rows):
        ws = self._sheet(tab)
        width = max(len(r) for r in rows)
        for i in range(len(rows), ws.max_row + 1):          # clear leftovers below
            for j in range(width):
                ws.cell(row=i + 1, column=first_col + j).value = None
        for i, r in enumerate(rows, start=1):
            for j, v in enumerate(r):
                ws.cell(row=i, column=first_col + j).value = v

    def save(self):
        self.wb.save(self.path)


# ---------------------------------------------------------------- Google Sheets
class SheetsSource:
    """Reads/writes a live Google Sheet with a service account.

    Setup (once): create a service account in Google Cloud, download its JSON key,
    set GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json, and share the Sheet with the
    service account's email as Editor. The key file must never be committed.
    """

    def __init__(self, spreadsheet_id: str, client=None):
        if client is None:
            import gspread
            client = gspread.service_account(filename=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
        self.sh = client.open_by_key(spreadsheet_id)
        self._cache: dict[str, object] = {}

    @property
    def label(self) -> str:
        return f"Google Sheet '{self.sh.title}'"

    def _ws(self, tab, create=False):
        if tab not in self._cache:
            try:
                self._cache[tab] = self.sh.worksheet(tab)
            except Exception:
                if not create:
                    return None
                self._cache[tab] = self.sh.add_worksheet(title=tab, rows=200, cols=20)
        return self._cache[tab]

    def tabs(self):
        return [ws.title for ws in self.sh.worksheets()]

    def read(self, tab):
        ws = self._ws(tab)
        if ws is None:
            return []
        return ws.get_all_values(value_render_option="UNFORMATTED_VALUE",
                                 date_time_render_option="FORMATTED_STRING")

    def write(self, tab, rows):
        ws = self._ws(tab, create=True)
        ws.clear()
        ws.update(rows, "A1", value_input_option="USER_ENTERED")

    def write_columns(self, tab, first_col, rows):
        import gspread.utils as u
        ws = self._ws(tab, create=True)
        width = max(len(r) for r in rows)
        start = u.rowcol_to_a1(1, first_col)
        end = u.rowcol_to_a1(max(ws.row_count, len(rows)), first_col + width - 1)
        ws.batch_clear([f"{start}:{end}"])
        ws.update(rows, start, value_input_option="USER_ENTERED")

    def save(self):
        pass  # every write is already live


def get_source(spec: str | None = None) -> Source:
    spec = spec or os.environ.get("OPS_SOURCE", DEFAULT_SOURCE)
    kind, _, target = spec.partition(":")
    if kind == "xlsx":
        return ExcelSource(target)
    if kind == "sheets":
        return SheetsSource(target)
    raise ValueError(f"Unknown OPS_SOURCE '{spec}'. Use xlsx:<path> or sheets:<spreadsheet id>.")
