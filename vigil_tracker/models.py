"""Core data structures shared across the agent."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class Verdict(str, Enum):
    """Classification of a bot's diagnosis reply."""

    ISSUE = "issue"          # a real issue was diagnosed
    NO_ISSUE = "no_issue"    # bot says there is no issue / task not stuck
    AMBIGUOUS = "ambiguous"  # neither a clear diagnosis nor a clear no-issue verdict


@dataclass
class IssueCategory:
    """A canonical class of technical problem in the issue registry."""

    id: str
    name: str
    description: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "IssueCategory":
        return cls(id=d["id"], name=d["name"], description=d["description"])


@dataclass
class Occurrence:
    """One qualifying report of an issue. One qualifying message == one occurrence.

    Keyed by `message_ts` (the raw Slack timestamp), which is the dedup key.
    """

    message_ts: str
    issue_id: str
    issue_name: str
    reporter: str
    reporter_id: str
    message_permalink: str
    task_link: str
    bot_diagnosis: str
    report_timestamp: str          # ISO-8601 in the configured timezone
    edited_ts: Optional[str] = None  # Slack "edited" ts last seen, for edit detection

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Occurrence":
        return cls(
            message_ts=d["message_ts"],
            issue_id=d["issue_id"],
            issue_name=d["issue_name"],
            reporter=d["reporter"],
            reporter_id=d["reporter_id"],
            message_permalink=d["message_permalink"],
            task_link=d["task_link"],
            bot_diagnosis=d["bot_diagnosis"],
            report_timestamp=d["report_timestamp"],
            edited_ts=d.get("edited_ts"),
        )


@dataclass
class PendingMessage:
    """A message with a task link but no bot reply yet — re-checked next run."""

    message_ts: str
    channel: str
    task_id: str
    first_seen_ts: str  # ISO-8601, for optional expiry/observability

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PendingMessage":
        return cls(
            message_ts=d["message_ts"],
            channel=d["channel"],
            task_id=d["task_id"],
            first_seen_ts=d["first_seen_ts"],
        )


@dataclass
class NeedsReviewItem:
    """A message routed to manual review (ambiguous reply or unmatched bot msg)."""

    message_ts: str
    reason: str
    permalink: str
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "NeedsReviewItem":
        return cls(
            message_ts=d["message_ts"],
            reason=d["reason"],
            permalink=d.get("permalink", ""),
            detail=d.get("detail", ""),
        )


@dataclass
class SlackMessage:
    """A normalized Slack message (top-level or reply)."""

    ts: str
    user: str            # user or bot id
    username: str        # resolved display name (best effort)
    text: str
    thread_ts: Optional[str] = None
    edited_ts: Optional[str] = None
    bot_id: Optional[str] = None
    replies: list = field(default_factory=list)  # list[SlackMessage]

    @property
    def is_threaded_reply(self) -> bool:
        return self.thread_ts is not None and self.thread_ts != self.ts
