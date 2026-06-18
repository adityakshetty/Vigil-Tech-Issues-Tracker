from vigil_tracker.aggregate import build_summary_rows
from vigil_tracker.models import IssueCategory, Occurrence


def occ(ts, issue_id, name, reporter, reporter_id):
    return Occurrence(
        message_ts=ts,
        issue_id=issue_id,
        issue_name=name,
        reporter=reporter,
        reporter_id=reporter_id,
        message_permalink=f"https://slack/{ts}",
        task_link="https://studio.mercor.com/annotator/tasks/task_x/",
        bot_diagnosis="diag",
        report_timestamp=f"2026-01-01T00:00:{ts[-2:]}-08:00",
    )


def registry():
    return {
        "stuck_autoqc": IssueCategory("stuck_autoqc", "Stuck AutoQC", "desc a"),
        "incomplete_taiga": IssueCategory("incomplete_taiga", "Incomplete Taiga", "desc b"),
    }


def test_counts_and_distinct_reporters():
    occurrences = [
        occ("100", "stuck_autoqc", "Stuck AutoQC", "Alice", "U1"),
        occ("101", "stuck_autoqc", "Stuck AutoQC", "Alice", "U1"),  # same reporter
        occ("102", "stuck_autoqc", "Stuck AutoQC", "Bob", "U2"),
        occ("103", "incomplete_taiga", "Incomplete Taiga", "Carol", "U3"),
    ]
    rows = build_summary_rows(occurrences, registry())

    # Most common first.
    assert rows[0][0] == "stuck_autoqc"
    assert rows[0][3] == 3          # Occurrences (even with a repeat reporter)
    assert rows[0][4] == 2          # Distinct Reporters
    assert "Alice" in rows[0][5] and "Bob" in rows[0][5]

    assert rows[1][0] == "incomplete_taiga"
    assert rows[1][3] == 1
    assert rows[1][4] == 1


def test_sorted_by_occurrences_descending():
    occurrences = [
        occ("100", "incomplete_taiga", "Incomplete Taiga", "A", "U1"),
        occ("101", "incomplete_taiga", "Incomplete Taiga", "B", "U2"),
        occ("102", "stuck_autoqc", "Stuck AutoQC", "C", "U3"),
    ]
    rows = build_summary_rows(occurrences, registry())
    assert [r[0] for r in rows] == ["incomplete_taiga", "stuck_autoqc"]


def test_first_and_last_reported_use_chronological_bounds():
    occurrences = [
        occ("105", "stuck_autoqc", "Stuck AutoQC", "A", "U1"),
        occ("100", "stuck_autoqc", "Stuck AutoQC", "B", "U2"),
        occ("110", "stuck_autoqc", "Stuck AutoQC", "C", "U3"),
    ]
    rows = build_summary_rows(occurrences, registry())
    # First/Last come from the earliest/latest message ts (100 / 110).
    assert rows[0][6].endswith("00-08:00")  # First Reported -> ts 100
    assert rows[0][7].endswith("10-08:00")  # Last Reported  -> ts 110
    # Example message is the earliest occurrence's permalink.
    assert rows[0][8] == "https://slack/100"


def test_uses_registry_description():
    occurrences = [occ("100", "stuck_autoqc", "Stuck AutoQC", "A", "U1")]
    rows = build_summary_rows(occurrences, registry())
    assert rows[0][2] == "desc a"
