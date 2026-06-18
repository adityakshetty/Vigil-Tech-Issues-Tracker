import time

from vigil_tracker.classifier import IssueClassifier, VerdictDetector
from vigil_tracker.config import Config
from vigil_tracker.models import SlackMessage
from vigil_tracker.tracker import Tracker

HEX = "0123456789abcdef0123456789abcdef"
TASK_ID = f"task_{HEX}"
TASK_URL = f"https://studio.mercor.com/annotator/tasks/{TASK_ID}/"
BASE = time.time()


def ts(offset):
    return f"{BASE + offset:.6f}"


# ----- fakes -------------------------------------------------------------


class FakeLLM:
    def complete_json(self, system, user, schema):
        props = schema["properties"]
        low = user.lower()
        if "verdict" in props:
            if "unclear" in low:
                return {"verdict": "ambiguous", "diagnosis_summary": "unclear"}
            return {"verdict": "issue", "diagnosis_summary": "diagnosed"}
        if "autoqc" in low:
            return {
                "matched_existing": True,
                "matched_id": "stuck_autoqc",
                "confidence": 0.95,
                "diagnosis_summary": "stuck autoqc",
            }
        return {
            "matched_existing": False,
            "confidence": 0.2,
            "new_name": "Other Issue",
            "new_description": "something else",
            "diagnosis_summary": "other",
        }


class FakeSlack:
    def __init__(self, messages, bot_ids):
        self.messages = messages
        self._by_ts = {m.ts: m for m in messages}
        self._bot_ids = {b.lower() for b in bot_ids}

    def fetch_history(self, channel, oldest=None):
        msgs = [
            m
            for m in self.messages
            if oldest is None or float(m.ts) > float(oldest)
        ]
        return sorted(msgs, key=lambda m: float(m.ts))

    def resolve_channel_id(self, channel):
        return "C1"

    def fetch_replies(self, channel_id, ts):
        m = self._by_ts.get(ts)
        return list(m.replies) if m else []

    def get_permalink(self, channel, ts):
        return f"https://slack/{ts}"

    def resolve_user_name(self, uid):
        return uid or ""

    def is_diagnostic_bot(self, msg):
        cands = {
            (msg.user or "").lower(),
            (msg.bot_id or "").lower(),
            (msg.username or "").lower(),
        }
        cands.discard("")
        return bool(cands & self._bot_ids)

    def _call(self, method, **kwargs):
        if method == "conversations_replies":
            t = kwargs["ts"]
            m = self._by_ts.get(t)
            raw = {
                "ts": t,
                "user": m.user if m else "",
                "text": m.text if m else "",
                "thread_ts": t,
            }
            if m and m.edited_ts:
                raw["edited"] = {"ts": m.edited_ts}
            return {"messages": [raw]}
        return {}

    def _to_message(self, raw):
        edited = (raw.get("edited") or {}).get("ts")
        return SlackMessage(
            ts=raw["ts"],
            user=raw.get("user", ""),
            username=raw.get("username", ""),
            text=raw.get("text", ""),
            thread_ts=raw.get("thread_ts"),
            edited_ts=edited,
        )


class FakeSheets:
    def __init__(self):
        self.state_blob = ""
        self.occurrences = []
        self.summary = None
        self.needs_review = None

    def open(self):
        return self

    def read_state_blob(self):
        return self.state_blob

    def write_state_blob(self, blob):
        self.state_blob = blob

    def read_occurrences(self):
        return list(self.occurrences)

    def write_log(self, occ):
        self.occurrences = list(occ)

    def write_summary(self, rows):
        self.summary = rows

    def write_needs_review(self, items):
        self.needs_review = items


# ----- helpers -----------------------------------------------------------


def make_config():
    return Config.from_dict(
        {
            "slack": {
                "bot_token": "x",
                "channel": "#vigil-technical-issues",
                "bot_identities": ["Justinbot", "Cursor"],
            },
            "classification": {"api_key": "y", "match_confidence_threshold": 0.75},
            "google": {"sheet_id": "z"},
            "no_issue_detection": {"phrases": ["no issue", "not stuck"]},
            "seed_categories": [
                {"id": "stuck_autoqc", "name": "Stuck AutoQC", "description": "desc"}
            ],
        }
    )


def make_tracker(messages, sheets=None):
    cfg = make_config()
    slack = FakeSlack(messages, cfg.slack.bot_identities)
    sheets = sheets or FakeSheets()
    llm = FakeLLM()
    tracker = Tracker(
        config=cfg,
        slack=slack,
        sheets=sheets,
        verdict_detector=VerdictDetector(cfg.no_issue_phrases, llm=llm),
        classifier=IssueClassifier(llm, cfg.classification.match_confidence_threshold),
    )
    return tracker, slack, sheets


def user_msg(offset, text, replies=None, user="U1", edited=None):
    parent_ts = ts(offset)
    m = SlackMessage(
        ts=parent_ts,
        user=user,
        username="",
        text=text,
        thread_ts=parent_ts if replies else None,
        edited_ts=edited,
    )
    m.replies = replies or []
    return m


def bot_reply(offset, text, username="Justinbot"):
    return SlackMessage(
        ts=ts(offset), user="B1", username=username, text=text, thread_ts=None
    )


# ----- tests -------------------------------------------------------------


def test_qualifying_message_creates_occurrence():
    msg = user_msg(
        1,
        f"AutoQC seems stuck {TASK_URL}",
        replies=[bot_reply(2, "AutoQC pipeline is stuck at stage 2")],
    )
    tracker, _, sheets = make_tracker([msg])
    stats = tracker.run()

    assert stats["qualified"] == 1
    assert len(sheets.occurrences) == 1
    occ = sheets.occurrences[0]
    assert occ.issue_id == "stuck_autoqc"
    assert occ.task_link == TASK_URL
    # Summary has one row for the issue.
    assert sheets.summary[0][0] == "stuck_autoqc"
    assert sheets.summary[0][3] == 1


def test_no_issue_reply_is_ignored():
    msg = user_msg(
        1,
        f"is this stuck? {TASK_URL}",
        replies=[bot_reply(2, "No issue here, the task is not stuck")],
    )
    tracker, _, sheets = make_tracker([msg])
    stats = tracker.run()
    assert stats["no_issue"] == 1
    assert sheets.occurrences == []


def test_message_without_bot_reply_is_deferred():
    msg = user_msg(1, f"AutoQC stuck {TASK_URL}", replies=[])
    tracker, _, sheets = make_tracker([msg])
    stats = tracker.run()
    assert stats["pending"] == 1
    assert sheets.occurrences == []
    # Pending is persisted in state for the next run.
    assert TASK_ID in sheets.state_blob


def test_ambiguous_reply_routed_to_needs_review():
    msg = user_msg(
        1,
        f"something with {TASK_URL}",
        replies=[bot_reply(2, "this is unclear, hard to say")],
    )
    tracker, _, sheets = make_tracker([msg])
    stats = tracker.run()
    assert stats["needs_review"] == 1
    assert sheets.occurrences == []
    assert sheets.needs_review[0].reason == "ambiguous_bot_reply"


def test_rerun_does_not_double_count():
    msg = user_msg(
        1,
        f"AutoQC stuck {TASK_URL}",
        replies=[bot_reply(2, "AutoQC stuck")],
    )
    tracker, _, sheets = make_tracker([msg])
    tracker.run()
    first = list(sheets.occurrences)
    # Second run with identical history must not add a duplicate occurrence.
    tracker.run()
    assert len(sheets.occurrences) == len(first) == 1


def test_pending_resolves_when_bot_replies_next_run():
    msg = user_msg(1, f"AutoQC stuck {TASK_URL}", replies=[])
    sheets = FakeSheets()
    tracker, slack, sheets = make_tracker([msg], sheets=sheets)

    stats1 = tracker.run()
    assert stats1["pending"] == 1

    # A bot replies between runs; the parent is now older than the cursor and
    # only the pending re-check should resolve it.
    msg.replies = [bot_reply(2, "AutoQC stuck at stage 1")]

    stats2 = tracker.run()
    assert stats2["qualified"] == 1
    assert len(sheets.occurrences) == 1
    # No longer pending.
    assert TASK_ID not in sheets.state_blob or '"pending":{}' in sheets.state_blob


def test_edit_reclassification_adjusts_counts():
    # Drive the evaluate path directly to exercise the in-place edit handling.
    msg = user_msg(1, f"AutoQC stuck {TASK_URL}", replies=[])
    tracker, _, _ = make_tracker([msg])

    from vigil_tracker.state import AgentState
    from vigil_tracker.task_links import TaskLinkParser

    state = AgentState()
    state.seed(tracker._cfg.seed_categories)
    occurrences = {}
    stats = {k: 0 for k in (
        "qualified", "no_issue", "pending", "needs_review", "reprocessed", "new_categories"
    )}
    link = TaskLinkParser(tracker._cfg.task_link_pattern).first(msg.text)

    # First diagnosis: a real issue -> occurrence recorded.
    tracker._evaluate(msg, bot_reply(2, "AutoQC stuck"), link, state, occurrences, stats)
    assert msg.ts in occurrences

    # Edited so the bot now says no issue -> occurrence removed, count adjusted.
    tracker._evaluate(
        msg, bot_reply(2, "No issue, not stuck"), link, state, occurrences, stats
    )
    assert msg.ts not in occurrences
