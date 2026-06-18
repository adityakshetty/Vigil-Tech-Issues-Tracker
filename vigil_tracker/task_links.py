"""Mercor Studio task-link detection and normalization.

A message qualifies only if it contains a task link of the canonical form:

    https://studio.mercor.com/annotator/tasks/task_<32-hex>/

An optional ``?from_view=...`` query string may follow and is tolerated. The
stable identifier is the ``task_<id>`` segment; the query string is stripped
during normalization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class TaskLink:
    """A normalized task link extracted from message text."""

    task_id: str       # e.g. "task_0123...ef" (the stable identifier)
    normalized_url: str  # canonical URL with the query string stripped
    raw: str           # exactly as it appeared in the message


class TaskLinkParser:
    """Compiles the configurable task-link pattern and extracts task links."""

    def __init__(self, pattern: str):
        # The pattern's capture group 1 must be the `task_<id>` identifier.
        self._regex = re.compile(pattern)

    def find_all(self, text: str) -> List[TaskLink]:
        """Return every task link found in ``text`` (de-duplicated by task id)."""
        if not text:
            return []
        seen = set()
        out: List[TaskLink] = []
        for match in self._regex.finditer(text):
            task_id = match.group(1)
            if task_id in seen:
                continue
            seen.add(task_id)
            out.append(
                TaskLink(
                    task_id=task_id,
                    normalized_url=self._normalize(task_id),
                    raw=match.group(0),
                )
            )
        return out

    def first(self, text: str) -> Optional[TaskLink]:
        """Return the first task link in ``text``, or ``None``."""
        links = self.find_all(text)
        return links[0] if links else None

    def contains_task_link(self, text: str) -> bool:
        return self.first(text) is not None

    @staticmethod
    def _normalize(task_id: str) -> str:
        return f"https://studio.mercor.com/annotator/tasks/{task_id}/"
