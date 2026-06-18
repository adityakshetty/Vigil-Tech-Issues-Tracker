"""Persistent agent state, serialized to/from the hidden ``_State`` tab.

State is what keeps the agent idempotent across runs:

* ``cursor``       — last successfully processed point in channel history (Slack ts).
* ``processed``    — message ts -> last-seen edited ts, so edits can be detected.
* ``registry``     — issue categories by id.
* ``pending``      — task-link messages with no bot reply yet, re-checked next run.
* ``needs_review`` — ambiguous replies / unmatched separate bot messages.

The occurrence records themselves live in the Issue Log tab (the dedup key is
the Slack message ts); this module tracks everything else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import IssueCategory, NeedsReviewItem, PendingMessage


@dataclass
class AgentState:
    cursor: Optional[str] = None
    processed: Dict[str, Optional[str]] = field(default_factory=dict)
    registry: Dict[str, IssueCategory] = field(default_factory=dict)
    pending: Dict[str, PendingMessage] = field(default_factory=dict)
    needs_review: Dict[str, NeedsReviewItem] = field(default_factory=dict)

    # ----- registry helpers ---------------------------------------------

    def registry_list(self) -> List[IssueCategory]:
        return list(self.registry.values())

    def ensure_category(self, category: IssueCategory) -> None:
        self.registry.setdefault(category.id, category)

    def seed(self, categories: List[IssueCategory]) -> None:
        for cat in categories:
            self.ensure_category(cat)

    # ----- processed/edit tracking --------------------------------------

    def is_processed(self, message_ts: str) -> bool:
        return message_ts in self.processed

    def needs_reprocess(self, message_ts: str, current_edited_ts: Optional[str]) -> bool:
        """True if a previously-processed message was edited since last seen."""
        if message_ts not in self.processed:
            return False
        return self.processed[message_ts] != current_edited_ts

    def mark_processed(self, message_ts: str, edited_ts: Optional[str]) -> None:
        self.processed[message_ts] = edited_ts

    # ----- serialization -------------------------------------------------

    def to_json(self) -> str:
        payload = {
            "version": 1,
            "cursor": self.cursor,
            "processed": self.processed,
            "registry": {k: v.to_dict() for k, v in self.registry.items()},
            "pending": {k: v.to_dict() for k, v in self.pending.items()},
            "needs_review": {k: v.to_dict() for k, v in self.needs_review.items()},
        }
        return json.dumps(payload, separators=(",", ":"))

    @classmethod
    def from_json(cls, blob: str) -> "AgentState":
        if not blob:
            return cls()
        data = json.loads(blob)
        return cls(
            cursor=data.get("cursor"),
            processed=dict(data.get("processed", {})),
            registry={
                k: IssueCategory.from_dict(v)
                for k, v in (data.get("registry", {}) or {}).items()
            },
            pending={
                k: PendingMessage.from_dict(v)
                for k, v in (data.get("pending", {}) or {}).items()
            },
            needs_review={
                k: NeedsReviewItem.from_dict(v)
                for k, v in (data.get("needs_review", {}) or {}).items()
            },
        )
