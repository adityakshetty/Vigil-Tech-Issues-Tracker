"""Issue qualification and classification.

Two responsibilities:

1. **Verdict detection** — decide whether a bot reply is an "issue diagnosed",
   a "no issue / not stuck" verdict, or "ambiguous". A fast, configurable
   phrase list handles the clear cases offline; Claude is the tie-breaker for
   replies that match no phrase.

2. **Issue categorization** — given a qualifying message, decide whether it
   matches an existing registry category above a confidence threshold, or
   warrants a new category. The bot diagnosis is weighted as the strongest
   signal.

The Anthropic client is injectable so the surrounding orchestration logic can be
unit-tested without network access.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Protocol

from .models import IssueCategory, Verdict

logger = logging.getLogger(__name__)

# Phrases that, on their own, signal a clear issue diagnosis. Used only as a
# cheap shortcut; the LLM is the authority for anything not obviously no-issue.
_AFFIRMATIVE_ISSUE_HINTS = (
    "stuck",
    "failed",
    "error",
    "not started",
    "did not start",
    "incomplete",
    "blocked",
    "hung",
    "stalled",
    "timed out",
)


@dataclass
class ClassificationResult:
    category: IssueCategory
    is_new: bool
    confidence: float
    diagnosis_summary: str


class LLMClient(Protocol):
    """Minimal protocol so the classifier can be tested with a fake client."""

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        ...


class AnthropicLLMClient:
    """Thin wrapper over the Anthropic Messages API with structured outputs."""

    def __init__(self, api_key: str, model: str):
        import anthropic  # imported lazily so tests need no SDK/network

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text)


# --------------------------------------------------------------------------
# Verdict detection
# --------------------------------------------------------------------------

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["issue", "no_issue", "ambiguous"]},
        "diagnosis_summary": {"type": "string"},
    },
    "required": ["verdict", "diagnosis_summary"],
    "additionalProperties": False,
}

_VERDICT_SYSTEM = (
    "You classify a diagnostic bot's reply to a user's report about a Mercor "
    "Studio annotator task. Decide whether the bot diagnosed a real technical "
    "issue, stated there is no issue (e.g. the task is not stuck / working as "
    "expected), or was ambiguous (neither a clear diagnosis nor a clear "
    "no-issue verdict). Base the verdict primarily on the bot reply. Provide a "
    "short (one sentence) summary of the diagnosis. Respond as JSON."
)


class VerdictDetector:
    def __init__(self, no_issue_phrases: List[str], llm: Optional[LLMClient] = None):
        self._phrases = [p.lower() for p in no_issue_phrases]
        self._llm = llm

    def matches_no_issue_phrase(self, bot_text: str) -> bool:
        text = (bot_text or "").lower()
        return any(phrase in text for phrase in self._phrases)

    def detect(self, user_text: str, bot_text: str) -> tuple[Verdict, str]:
        """Return (verdict, diagnosis_summary).

        Rule-based fast paths first; the LLM is consulted only when the simple
        rules can't confidently decide.
        """
        bot_text = bot_text or ""

        # 1. Clear no-issue verdict from the configured phrase list.
        if self.matches_no_issue_phrase(bot_text):
            return Verdict.NO_ISSUE, _summarize(bot_text)

        # 2. Without an LLM available, fall back to a conservative heuristic.
        if self._llm is None:
            lowered = bot_text.lower()
            if any(hint in lowered for hint in _AFFIRMATIVE_ISSUE_HINTS):
                return Verdict.ISSUE, _summarize(bot_text)
            return Verdict.AMBIGUOUS, _summarize(bot_text)

        # 3. Ask the LLM to adjudicate.
        try:
            result = self._llm.complete_json(
                system=_VERDICT_SYSTEM,
                user=_render_verdict_prompt(user_text, bot_text),
                schema=_VERDICT_SCHEMA,
            )
            verdict = Verdict(result.get("verdict", "ambiguous"))
            summary = result.get("diagnosis_summary") or _summarize(bot_text)
            return verdict, summary
        except Exception:  # network / parse failures must not crash a run
            logger.exception("Verdict LLM call failed; routing to ambiguous")
            return Verdict.AMBIGUOUS, _summarize(bot_text)


# --------------------------------------------------------------------------
# Issue categorization
# --------------------------------------------------------------------------

_CATEGORIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "matched_existing": {"type": "boolean"},
        "matched_id": {"type": "string"},
        "confidence": {"type": "number"},
        "new_name": {"type": "string"},
        "new_description": {"type": "string"},
        "diagnosis_summary": {"type": "string"},
    },
    "required": ["matched_existing", "confidence", "diagnosis_summary"],
    "additionalProperties": False,
}

_CATEGORIZE_SYSTEM = (
    "You maintain a registry of distinct technical-issue categories for Mercor "
    "Studio annotator tasks. Given a qualifying report (the user's message, the "
    "task link, and the diagnostic bot's diagnosis), decide whether it matches "
    "one of the EXISTING categories or describes a NEW category.\n\n"
    "Weight the bot diagnosis as the strongest signal. Two reports match when "
    "they describe the same underlying technical problem, even if the wording "
    "differs.\n\n"
    "If it matches an existing category, set matched_existing=true, matched_id "
    "to that category's id, and confidence in [0,1] for how sure you are. If it "
    "is a genuinely new class of problem, set matched_existing=false and provide "
    "a short new_name (a few words, like 'Stuck AutoQC') and a new_description "
    "(one to two sentences describing the issue). Always include a one-sentence "
    "diagnosis_summary. Respond as JSON."
)


class IssueClassifier:
    def __init__(self, llm: LLMClient, match_confidence_threshold: float):
        self._llm = llm
        self._threshold = match_confidence_threshold

    def classify(
        self,
        user_text: str,
        task_link: str,
        bot_diagnosis: str,
        registry: List[IssueCategory],
    ) -> ClassificationResult:
        """Attach to an existing category or mint a new one.

        Raises on unrecoverable LLM failure so the caller can avoid advancing
        the cursor past an uncommitted message.
        """
        result = self._llm.complete_json(
            system=_CATEGORIZE_SYSTEM,
            user=_render_categorize_prompt(
                user_text, task_link, bot_diagnosis, registry
            ),
            schema=_CATEGORIZE_SCHEMA,
        )

        confidence = float(result.get("confidence", 0.0))
        summary = result.get("diagnosis_summary") or _summarize(bot_diagnosis)
        matched_existing = bool(result.get("matched_existing"))
        matched_id = result.get("matched_id")

        registry_by_id = {c.id: c for c in registry}

        if (
            matched_existing
            and matched_id in registry_by_id
            and confidence >= self._threshold
        ):
            return ClassificationResult(
                category=registry_by_id[matched_id],
                is_new=False,
                confidence=confidence,
                diagnosis_summary=summary,
            )

        # New category. Generate a stable id from the proposed name.
        name = (result.get("new_name") or "Uncategorized issue").strip()
        description = (result.get("new_description") or summary).strip()
        new_id = _make_category_id(name, existing_ids=set(registry_by_id.keys()))
        return ClassificationResult(
            category=IssueCategory(id=new_id, name=name, description=description),
            is_new=True,
            confidence=confidence,
            diagnosis_summary=summary,
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _summarize(text: str, limit: int = 280) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _make_category_id(name: str, existing_ids: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "issue"
    candidate = base
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _render_verdict_prompt(user_text: str, bot_text: str) -> str:
    return (
        f"User message:\n{user_text or '(empty)'}\n\n"
        f"Bot reply (diagnosis):\n{bot_text or '(empty)'}"
    )


def _render_categorize_prompt(
    user_text: str,
    task_link: str,
    bot_diagnosis: str,
    registry: List[IssueCategory],
) -> str:
    registry_lines = (
        "\n".join(f"- id={c.id} | name={c.name} | {c.description}" for c in registry)
        or "(registry is empty)"
    )
    return (
        f"EXISTING categories:\n{registry_lines}\n\n"
        f"--- New report ---\n"
        f"Task link: {task_link}\n"
        f"User message:\n{user_text or '(empty)'}\n\n"
        f"Bot diagnosis (strongest signal):\n{bot_diagnosis or '(empty)'}"
    )
