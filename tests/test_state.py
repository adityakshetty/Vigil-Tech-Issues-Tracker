from vigil_tracker.models import IssueCategory, NeedsReviewItem, PendingMessage
from vigil_tracker.state import AgentState


def test_round_trip_serialization():
    state = AgentState(cursor="123.456")
    state.ensure_category(IssueCategory("stuck_autoqc", "Stuck AutoQC", "desc"))
    state.mark_processed("100.0", "100.5")
    state.pending["200.0"] = PendingMessage("200.0", "#chan", "task_x", "2026-01-01T00:00:00-08:00")
    state.needs_review["300.0"] = NeedsReviewItem("300.0", "ambiguous_bot_reply", "https://slack/300", "unclear")

    blob = state.to_json()
    restored = AgentState.from_json(blob)

    assert restored.cursor == "123.456"
    assert restored.registry["stuck_autoqc"].name == "Stuck AutoQC"
    assert restored.processed["100.0"] == "100.5"
    assert restored.pending["200.0"].task_id == "task_x"
    assert restored.needs_review["300.0"].reason == "ambiguous_bot_reply"


def test_empty_blob_yields_empty_state():
    state = AgentState.from_json("")
    assert state.cursor is None
    assert state.registry == {}
    assert state.pending == {}


def test_edit_detection():
    state = AgentState()
    state.mark_processed("100.0", "100.5")
    # Same edited ts -> no reprocess.
    assert state.needs_reprocess("100.0", "100.5") is False
    # Changed edited ts -> reprocess.
    assert state.needs_reprocess("100.0", "100.9") is True
    # Unknown message -> not a reprocess candidate.
    assert state.needs_reprocess("999.0", None) is False


def test_seed_is_idempotent():
    state = AgentState()
    cats = [IssueCategory("a", "A", "da"), IssueCategory("b", "B", "db")]
    state.seed(cats)
    # Mutate then reseed: existing entries are not overwritten.
    state.registry["a"] = IssueCategory("a", "A-modified", "da")
    state.seed(cats)
    assert state.registry["a"].name == "A-modified"
    assert len(state.registry) == 2
