"""Google Sheets / Drive persistence.

Owns one spreadsheet with three tabs:

* **Issue Summary** — one row per issue, sorted by occurrences descending.
* **Issue Log**     — one row per occurrence (the dedup record store).
* **_State**        — a hidden tab holding the serialized agent state as JSON,
                      chunked across cells in column A so it can grow.

A separate **Needs Review** tab surfaces ambiguous/unmatched items.

All API calls go through a small retry wrapper that backs off on Google rate
limits (HTTP 429).
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from .models import NeedsReviewItem, Occurrence

logger = logging.getLogger(__name__)

SUMMARY_TAB = "Issue Summary"
LOG_TAB = "Issue Log"
STATE_TAB = "_State"
NEEDS_REVIEW_TAB = "Needs Review"

SUMMARY_HEADER = [
    "Issue ID",
    "Issue Name",
    "Description",
    "Occurrences",
    "Distinct Reporters",
    "Reporters",
    "First Reported",
    "Last Reported",
    "Example Message",
    "Example Diagnosis",
]

LOG_HEADER = [
    "Report Timestamp",
    "Issue ID",
    "Issue Name",
    "Reporter",
    "Reporter ID",
    "Message Permalink",
    "Task Link",
    "Bot Diagnosis",
    "Slack Message TS",
]

NEEDS_REVIEW_HEADER = ["Slack Message TS", "Reason", "Permalink", "Detail"]

_STATE_CHUNK = 45000  # well under the 50k per-cell limit
_MAX_RETRIES = 5


class SheetsClient:
    def __init__(self, config_google):
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(
            config_google.credentials_path, scopes=scopes
        )
        self._gc = gspread.authorize(creds)
        self._cfg = config_google
        self._spreadsheet = None

    # ----- retry wrapper -------------------------------------------------

    @staticmethod
    def _retry(fn, *args, **kwargs):
        import gspread

        delay = 2.0
        for attempt in range(_MAX_RETRIES):
            try:
                return fn(*args, **kwargs)
            except gspread.exceptions.APIError as exc:
                status = getattr(exc.response, "status_code", None)
                if status == 429 and attempt < _MAX_RETRIES - 1:
                    logger.warning("Sheets rate limited; sleeping %ss", delay)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise RuntimeError("Sheets call exhausted retries")

    # ----- spreadsheet / tabs -------------------------------------------

    def open(self):
        """Open the configured spreadsheet (by id) or find/create it by name."""
        if self._spreadsheet is not None:
            return self._spreadsheet

        if self._cfg.sheet_id:
            self._spreadsheet = self._retry(self._gc.open_by_key, self._cfg.sheet_id)
        else:
            self._spreadsheet = self._open_or_create_by_name()

        self._ensure_tabs()
        return self._spreadsheet

    def _open_or_create_by_name(self):
        import gspread

        try:
            return self._retry(self._gc.open, self._cfg.sheet_name)
        except gspread.exceptions.SpreadsheetNotFound:
            logger.info("Creating spreadsheet '%s'", self._cfg.sheet_name)
            if self._cfg.drive_folder_id:
                return self._retry(
                    self._gc.create,
                    self._cfg.sheet_name,
                    folder_id=self._cfg.drive_folder_id,
                )
            return self._retry(self._gc.create, self._cfg.sheet_name)

    def _ensure_tabs(self) -> None:
        ss = self._spreadsheet
        existing = {ws.title: ws for ws in self._retry(ss.worksheets)}

        def ensure(title: str, header: Optional[List[str]], hidden: bool = False):
            ws = existing.get(title)
            if ws is None:
                ws = self._retry(
                    ss.add_worksheet, title=title, rows=100, cols=max(10, len(header or []))
                )
                if header:
                    self._retry(ws.update, [header], "A1")
                if hidden:
                    self._retry(ws.update_tab_color, {"red": 0.6, "green": 0.6, "blue": 0.6})
                    try:
                        self._retry(ws.hide)
                    except Exception:
                        pass
            return ws

        ensure(SUMMARY_TAB, SUMMARY_HEADER)
        ensure(LOG_TAB, LOG_HEADER)
        ensure(NEEDS_REVIEW_TAB, NEEDS_REVIEW_HEADER)
        ensure(STATE_TAB, None, hidden=True)

        # Drop the default "Sheet1" if it is empty and unused.
        default = existing.get("Sheet1")
        if default is not None and default.title not in (
            SUMMARY_TAB,
            LOG_TAB,
            STATE_TAB,
            NEEDS_REVIEW_TAB,
        ):
            try:
                if not self._retry(default.get_all_values):
                    self._retry(ss.del_worksheet, default)
            except Exception:
                pass

    def _ws(self, title: str):
        return self._retry(self.open().worksheet, title)

    # ----- Issue Log -----------------------------------------------------

    def read_occurrences(self) -> List[Occurrence]:
        ws = self._ws(LOG_TAB)
        rows = self._retry(ws.get_all_values)
        out: List[Occurrence] = []
        for row in rows[1:]:  # skip header
            if not row or not any(row):
                continue
            cells = (row + [""] * len(LOG_HEADER))[: len(LOG_HEADER)]
            (
                report_ts,
                issue_id,
                issue_name,
                reporter,
                reporter_id,
                permalink,
                task_link,
                diagnosis,
                slack_ts,
            ) = cells
            if not slack_ts:
                continue
            out.append(
                Occurrence(
                    message_ts=slack_ts,
                    issue_id=issue_id,
                    issue_name=issue_name,
                    reporter=reporter,
                    reporter_id=reporter_id,
                    message_permalink=permalink,
                    task_link=task_link,
                    bot_diagnosis=diagnosis,
                    report_timestamp=report_ts,
                )
            )
        return out

    def write_log(self, occurrences: List[Occurrence]) -> None:
        """Replace the Issue Log body with the given occurrences (chronological)."""
        ws = self._ws(LOG_TAB)
        ordered = sorted(occurrences, key=lambda o: float(o.message_ts))
        rows = [LOG_HEADER]
        for o in ordered:
            rows.append(
                [
                    o.report_timestamp,
                    o.issue_id,
                    o.issue_name,
                    o.reporter,
                    o.reporter_id,
                    o.message_permalink,
                    o.task_link,
                    o.bot_diagnosis,
                    o.message_ts,
                ]
            )
        self._retry(ws.clear)
        self._retry(ws.update, rows, "A1")

    # ----- Issue Summary -------------------------------------------------

    def write_summary(self, summary_rows: List[List]) -> None:
        ws = self._ws(SUMMARY_TAB)
        rows = [SUMMARY_HEADER] + summary_rows
        self._retry(ws.clear)
        self._retry(ws.update, rows, "A1")

    # ----- Needs Review --------------------------------------------------

    def write_needs_review(self, items: List[NeedsReviewItem]) -> None:
        ws = self._ws(NEEDS_REVIEW_TAB)
        rows = [NEEDS_REVIEW_HEADER]
        for it in items:
            rows.append([it.message_ts, it.reason, it.permalink, it.detail])
        self._retry(ws.clear)
        self._retry(ws.update, rows, "A1")

    # ----- State ---------------------------------------------------------

    def read_state_blob(self) -> str:
        ws = self._ws(STATE_TAB)
        col = self._retry(ws.col_values, 1)
        return "".join(col)

    def write_state_blob(self, blob: str) -> None:
        ws = self._ws(STATE_TAB)
        chunks = [blob[i : i + _STATE_CHUNK] for i in range(0, len(blob), _STATE_CHUNK)]
        if not chunks:
            chunks = [""]
        self._retry(ws.clear)
        self._retry(ws.update, [[c] for c in chunks], "A1")
