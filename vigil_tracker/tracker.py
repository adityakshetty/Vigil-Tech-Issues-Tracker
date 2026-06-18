"""Run orchestration: scan Slack, qualify, classify, persist to Sheets.

A single ``run()`` is idempotent. The occurrence store (Issue Log) is keyed by
Slack message ts, so re-processing a message overwrites its row rather than
double-counting; the Summary and counts are always recomputed from that store,
which makes edit handling fall out naturally (reclassify -> overwrite; becomes
no-issue -> delete; counts recompute).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

try:  # Python 3.9+ stdlib
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from .aggregate import build_summary_rows
from .classifier import IssueClassifier, VerdictDetector
from .config import Config
from .models import (
    NeedsReviewItem,
    Occurrence,
    PendingMessage,
    SlackMessage,
    Verdict,
)
from .state import AgentState

logger = logging.getLogger(__name__)


class Tracker:
    def __init__(
        self,
        config: Config,
        slack,
        sheets,
        verdict_detector: VerdictDetector,
        classifier: IssueClassifier,
    ):
        self._cfg = config
        self._slack = slack
        self._sheets = sheets
        self._verdicts = verdict_detector
        self._classifier = classifier
        self._tz = self._load_tz(config.timezone)

    # ----- public API ----------------------------------------------------

    def run(self) -> Dict[str, int]:
        """Execute one scan/classify/persist cycle. Returns a small stats dict."""
        self._sheets.open()

        state = AgentState.from_json(self._sheets.read_state_blob())
        state.seed(self._cfg.seed_categories)

        occurrences: Dict[str, Occurrence] = {
            o.message_ts: o for o in self._sheets.read_occurrences()
        }

        stats = {"qualified": 0, "no_issue": 0, "pending": 0, "needs_review": 0,
                 "reprocessed": 0, "new_categories": 0}

        oldest = self._determine_oldest(state)
        history = self._slack.fetch_history(self._cfg.slack.channel, oldest=oldest)

        # 1. Re-check previously deferred messages first; a bot may have replied.
        self._recheck_pending(state, occurrences, stats)

        # 2. Process newly fetched history.
        max_ts = state.cursor
        user_msgs_by_task: Dict[str, List[SlackMessage]] = {}
        separate_bot_msgs: List[SlackMessage] = []

        for msg in history:
            if self._slack.is_diagnostic_bot(msg) and not msg.is_threaded_reply:
                separate_bot_msgs.append(msg)
                max_ts = _max_ts(max_ts, msg.ts)
                continue

            self._process_user_message(msg, state, occurrences, stats, user_msgs_by_task)
            max_ts = _max_ts(max_ts, msg.ts)

        # 3. Associate separate top-level bot diagnoses by task id.
        self._associate_separate_bots(
            separate_bot_msgs, user_msgs_by_task, state, occurrences, stats
        )

        # 4. Commit everything BEFORE advancing the cursor, so a failed run is
        #    safely retryable.
        self._commit(state, occurrences, max_ts)

        logger.info("Run complete: %s", stats)
        return stats

    # ----- message processing -------------------------------------------

    def _process_user_message(
        self,
        msg: SlackMessage,
        state: AgentState,
        occurrences: Dict[str, Occurrence],
        stats: Dict[str, int],
        user_msgs_by_task: Dict[str, List[SlackMessage]],
    ) -> None:
        from .task_links import TaskLinkParser

        parser: TaskLinkParser = self._parser()
        task_link = parser.first(msg.text)
        if task_link is None:
            return  # no task link -> ignore permanently

        user_msgs_by_task.setdefault(task_link.task_id, []).append(msg)

        # Edit detection: skip if already processed and unchanged.
        already = state.is_processed(msg.ts)
        if already and not state.needs_reprocess(msg.ts, msg.edited_ts):
            return
        if already:
            stats["reprocessed"] += 1

        bot_reply = self._latest_bot_reply(msg)
        if bot_reply is None:
            # Defer: task link but no bot reply yet.
            state.pending[msg.ts] = PendingMessage(
                message_ts=msg.ts,
                channel=self._cfg.slack.channel,
                task_id=task_link.task_id,
                first_seen_ts=self._now_iso(),
            )
            stats["pending"] += 1
            return

        self._evaluate(
            msg, bot_reply, task_link, state, occurrences, stats
        )

    def _evaluate(
        self,
        user_msg: SlackMessage,
        bot_reply: SlackMessage,
        task_link,
        state: AgentState,
        occurrences: Dict[str, Occurrence],
        stats: Dict[str, int],
    ) -> None:
        verdict, summary = self._verdicts.detect(user_msg.text, bot_reply.text)

        # A resolved message leaves the pending/needs-review limbo.
        state.pending.pop(user_msg.ts, None)
        state.needs_review.pop(user_msg.ts, None)

        if verdict == Verdict.NO_ISSUE:
            # Ignore permanently; drop any prior occurrence (covers edits).
            occurrences.pop(user_msg.ts, None)
            state.mark_processed(user_msg.ts, user_msg.edited_ts)
            stats["no_issue"] += 1
            return

        if verdict == Verdict.AMBIGUOUS:
            occurrences.pop(user_msg.ts, None)
            state.needs_review[user_msg.ts] = NeedsReviewItem(
                message_ts=user_msg.ts,
                reason="ambiguous_bot_reply",
                permalink=self._permalink(user_msg.ts),
                detail=summary,
            )
            state.mark_processed(user_msg.ts, user_msg.edited_ts)
            stats["needs_review"] += 1
            return

        # ISSUE: classify and record/overwrite the occurrence.
        result = self._classifier.classify(
            user_text=user_msg.text,
            task_link=task_link.normalized_url,
            bot_diagnosis=summary,
            registry=state.registry_list(),
        )
        if result.is_new:
            state.ensure_category(result.category)
            stats["new_categories"] += 1

        occurrences[user_msg.ts] = Occurrence(
            message_ts=user_msg.ts,
            issue_id=result.category.id,
            issue_name=result.category.name,
            reporter=user_msg.username or self._slack.resolve_user_name(user_msg.user),
            reporter_id=user_msg.user,
            message_permalink=self._permalink(user_msg.ts),
            task_link=task_link.normalized_url,
            bot_diagnosis=result.diagnosis_summary,
            report_timestamp=self._ts_to_iso(user_msg.ts),
            edited_ts=user_msg.edited_ts,
        )
        state.mark_processed(user_msg.ts, user_msg.edited_ts)
        stats["qualified"] += 1

    # ----- pending re-check ---------------------------------------------

    def _recheck_pending(
        self,
        state: AgentState,
        occurrences: Dict[str, Occurrence],
        stats: Dict[str, int],
    ) -> None:
        if not state.pending:
            return
        channel_id = self._slack.resolve_channel_id(self._cfg.slack.channel)
        parser = self._parser()

        for ts, pending in list(state.pending.items()):
            replies = self._slack.fetch_replies(channel_id, ts)
            parent = SlackMessage(ts=ts, user="", username="", text="", thread_ts=ts)
            parent.replies = replies

            bot_reply = self._latest_bot_reply_from(replies)
            if bot_reply is None:
                continue  # still pending

            # Rebuild the parent's text/user by reading the thread parent.
            parent_msg = self._fetch_parent(channel_id, ts, replies)
            task_link = parser.first(parent_msg.text) if parent_msg.text else None
            if task_link is None:
                # Parent lost its task link (edited); stop tracking.
                state.pending.pop(ts, None)
                continue
            self._evaluate(parent_msg, bot_reply, task_link, state, occurrences, stats)

    def _fetch_parent(
        self, channel_id: str, ts: str, replies: List[SlackMessage]
    ) -> SlackMessage:
        """Best-effort fetch of the thread parent message."""
        try:
            resp = self._slack._call(
                "conversations_replies", channel=channel_id, ts=ts, limit=1
            )
            for raw in resp.get("messages", []):
                if raw.get("ts") == ts:
                    return self._slack._to_message(raw)
        except Exception:
            logger.warning("Could not refetch pending parent %s", ts)
        return SlackMessage(ts=ts, user="", username="", text="", thread_ts=ts)

    # ----- separate top-level bot diagnoses ------------------------------

    def _associate_separate_bots(
        self,
        bot_msgs: List[SlackMessage],
        user_msgs_by_task: Dict[str, List[SlackMessage]],
        state: AgentState,
        occurrences: Dict[str, Occurrence],
        stats: Dict[str, int],
    ) -> None:
        parser = self._parser()
        window = self._cfg.slack.reply_match_window_minutes * 60.0

        for bot_msg in bot_msgs:
            link = parser.first(bot_msg.text)
            if link is None:
                continue
            candidates = [
                u
                for u in user_msgs_by_task.get(link.task_id, [])
                if 0 <= (float(bot_msg.ts) - float(u.ts)) <= window
                and self._latest_bot_reply(u) is None  # no threaded bot reply
            ]
            if len(candidates) == 1:
                user_msg = candidates[0]
                if state.is_processed(user_msg.ts) and not state.needs_reprocess(
                    user_msg.ts, user_msg.edited_ts
                ):
                    continue
                self._evaluate(user_msg, bot_msg, link, state, occurrences, stats)
            else:
                # Zero or many matches: route to review rather than guess.
                state.needs_review[bot_msg.ts] = NeedsReviewItem(
                    message_ts=bot_msg.ts,
                    reason="unmatched_separate_bot_message",
                    permalink=self._permalink(bot_msg.ts),
                    detail=f"task={link.task_id}; {len(candidates)} candidate user messages",
                )
                stats["needs_review"] += 1

    # ----- commit --------------------------------------------------------

    def _commit(
        self,
        state: AgentState,
        occurrences: Dict[str, Occurrence],
        max_ts: Optional[str],
    ) -> None:
        occ_list = list(occurrences.values())
        # Write data tabs first; only advance the cursor once they're committed.
        self._sheets.write_log(occ_list)
        self._sheets.write_summary(build_summary_rows(occ_list, state.registry))
        self._sheets.write_needs_review(list(state.needs_review.values()))

        if max_ts is not None:
            state.cursor = max_ts
        self._sheets.write_state_blob(state.to_json())

    # ----- helpers -------------------------------------------------------

    def _latest_bot_reply(self, user_msg: SlackMessage) -> Optional[SlackMessage]:
        return self._latest_bot_reply_from(user_msg.replies)

    def _latest_bot_reply_from(
        self, replies: List[SlackMessage]
    ) -> Optional[SlackMessage]:
        bot_replies = [r for r in replies if self._slack.is_diagnostic_bot(r)]
        if not bot_replies:
            return None
        # Most recent bot reply (containing a diagnosis) decides the verdict.
        bot_replies.sort(key=lambda r: float(r.ts))
        return bot_replies[-1]

    def _determine_oldest(self, state: AgentState) -> Optional[str]:
        if state.cursor:
            return state.cursor
        lookback = timedelta(hours=self._cfg.first_run_lookback_hours)
        return f"{(datetime.now(timezone.utc) - lookback).timestamp():.6f}"

    def _parser(self):
        from .task_links import TaskLinkParser

        if not hasattr(self, "_parser_cache"):
            self._parser_cache = TaskLinkParser(self._cfg.task_link_pattern)
        return self._parser_cache

    def _permalink(self, ts: str) -> str:
        return self._slack.get_permalink(self._cfg.slack.channel, ts)

    def _ts_to_iso(self, ts: str) -> str:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        if self._tz is not None:
            dt = dt.astimezone(self._tz)
        return dt.isoformat()

    def _now_iso(self) -> str:
        dt = datetime.now(timezone.utc)
        if self._tz is not None:
            dt = dt.astimezone(self._tz)
        return dt.isoformat()

    @staticmethod
    def _load_tz(name: str):
        if ZoneInfo is None:
            return None
        try:
            return ZoneInfo(name)
        except Exception:
            logger.warning("Unknown timezone %s; using UTC", name)
            return timezone.utc


def _max_ts(current: Optional[str], candidate: str) -> Optional[str]:
    if current is None:
        return candidate
    return candidate if float(candidate) > float(current) else current
