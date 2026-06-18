"""Build the Issue Summary from the occurrence log.

Pure functions over occurrence records — no I/O — so the counting and sorting
rules are unit-testable in isolation.
"""

from __future__ import annotations

from typing import Dict, List

from .models import IssueCategory, Occurrence


def build_summary_rows(
    occurrences: List[Occurrence], registry: Dict[str, IssueCategory]
) -> List[List]:
    """Return Issue Summary rows sorted by occurrence count descending.

    Columns match ``sheets_client.SUMMARY_HEADER``.
    """
    by_issue: Dict[str, List[Occurrence]] = {}
    for occ in occurrences:
        by_issue.setdefault(occ.issue_id, []).append(occ)

    rows = []
    for issue_id, occ_list in by_issue.items():
        ordered = sorted(occ_list, key=lambda o: float(o.message_ts))
        earliest, latest = ordered[0], ordered[-1]

        # Distinct reporters by id; display names preserve first-seen order.
        reporter_names: List[str] = []
        seen_ids = set()
        for occ in ordered:
            if occ.reporter_id and occ.reporter_id not in seen_ids:
                seen_ids.add(occ.reporter_id)
                reporter_names.append(occ.reporter or occ.reporter_id)
            elif not occ.reporter_id and occ.reporter and occ.reporter not in reporter_names:
                reporter_names.append(occ.reporter)

        category = registry.get(issue_id)
        name = category.name if category else (earliest.issue_name or issue_id)
        description = category.description if category else ""

        rows.append(
            {
                "issue_id": issue_id,
                "name": name,
                "description": description,
                "occurrences": len(ordered),
                "distinct_reporters": len(seen_ids) or len(reporter_names),
                "reporters": ", ".join(reporter_names),
                "first_reported": earliest.report_timestamp,
                "last_reported": latest.report_timestamp,
                "example_message": earliest.message_permalink,
                "example_diagnosis": earliest.bot_diagnosis,
            }
        )

    # Most common issues first; deterministic tie-break by name then id.
    rows.sort(key=lambda r: (-r["occurrences"], r["name"].lower(), r["issue_id"]))

    return [
        [
            r["issue_id"],
            r["name"],
            r["description"],
            r["occurrences"],
            r["distinct_reporters"],
            r["reporters"],
            r["first_reported"],
            r["last_reported"],
            r["example_message"],
            r["example_diagnosis"],
        ]
        for r in rows
    ]
