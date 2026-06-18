"""Slack ingestion: channel history, threaded replies, user names, permalinks.

Wraps ``slack_sdk`` with pagination and rate-limit backoff. The diagnostic-bot
identity check matches on user/bot id or a case-insensitive display name.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from .models import SlackMessage

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5


class SlackClient:
    def __init__(self, bot_token: str, bot_identities: List[str]):
        from slack_sdk import WebClient  # lazy import for testability

        self._client = WebClient(token=bot_token)
        self._bot_identities = {b.lower() for b in bot_identities}
        self._user_name_cache: Dict[str, str] = {}
        self._channel_id: Optional[str] = None

    # ----- low-level call with backoff -----------------------------------

    def _call(self, method: str, **kwargs):
        from slack_sdk.errors import SlackApiError

        for attempt in range(_MAX_RETRIES):
            try:
                return getattr(self._client, method)(**kwargs)
            except SlackApiError as exc:
                resp = getattr(exc, "response", None)
                if resp is not None and resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "1"))
                    logger.warning(
                        "Slack rate limited on %s; sleeping %ss", method, retry_after
                    )
                    time.sleep(retry_after)
                    continue
                raise
        raise RuntimeError(f"Slack call {method} exhausted retries")

    # ----- channel resolution --------------------------------------------

    def resolve_channel_id(self, channel: str) -> str:
        """Accept a channel ID or a (possibly #-prefixed) name and return the ID."""
        if self._channel_id:
            return self._channel_id

        name = channel.lstrip("#")
        # Already an ID.
        if channel and channel[0] in ("C", "G") and channel == name:
            self._channel_id = channel
            return channel

        cursor = None
        while True:
            resp = self._call(
                "conversations_list",
                types="public_channel,private_channel",
                limit=1000,
                cursor=cursor,
            )
            for ch in resp.get("channels", []):
                if ch.get("name") == name:
                    self._channel_id = ch["id"]
                    return ch["id"]
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        raise ValueError(f"Channel not found: {channel}")

    # ----- history -------------------------------------------------------

    def fetch_history(
        self, channel: str, oldest: Optional[str] = None
    ) -> List[SlackMessage]:
        """Fetch top-level messages newer than ``oldest`` (a Slack ts), with replies.

        Returns messages in chronological order. Each top-level message has its
        threaded replies attached.
        """
        channel_id = self.resolve_channel_id(channel)
        top_level: List[SlackMessage] = []

        cursor = None
        while True:
            kwargs = dict(channel=channel_id, limit=200)
            if oldest:
                kwargs["oldest"] = oldest
            if cursor:
                kwargs["cursor"] = cursor
            resp = self._call("conversations_history", **kwargs)
            for raw in resp.get("messages", []):
                # Skip channel-join/leave and other subtype noise but keep
                # bot_message (bots may post top-level diagnoses).
                subtype = raw.get("subtype")
                if subtype and subtype not in ("bot_message", "thread_broadcast"):
                    continue
                top_level.append(self._to_message(raw))
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break

        top_level.sort(key=lambda m: float(m.ts))

        # Hydrate replies for any threaded parent.
        for msg in top_level:
            if msg.thread_ts == msg.ts or self._has_replies(msg):
                msg.replies = self.fetch_replies(channel_id, msg.ts)

        return top_level

    def fetch_replies(self, channel_id: str, thread_ts: str) -> List[SlackMessage]:
        replies: List[SlackMessage] = []
        cursor = None
        while True:
            kwargs = dict(channel=channel_id, ts=thread_ts, limit=200)
            if cursor:
                kwargs["cursor"] = cursor
            resp = self._call("conversations_replies", **kwargs)
            for raw in resp.get("messages", []):
                if raw.get("ts") == thread_ts:
                    continue  # the parent itself
                replies.append(self._to_message(raw))
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        replies.sort(key=lambda m: float(m.ts))
        return replies

    # ----- enrichment ----------------------------------------------------

    def get_permalink(self, channel: str, message_ts: str) -> str:
        channel_id = self.resolve_channel_id(channel)
        try:
            resp = self._call(
                "chat_getPermalink", channel=channel_id, message_ts=message_ts
            )
            return resp.get("permalink", "")
        except Exception:
            logger.warning("Could not fetch permalink for %s", message_ts)
            return ""

    def resolve_user_name(self, user_id: str) -> str:
        if not user_id:
            return ""
        if user_id in self._user_name_cache:
            return self._user_name_cache[user_id]
        try:
            resp = self._call("users_info", user=user_id)
            profile = resp.get("user", {}).get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or resp.get("user", {}).get("name")
                or user_id
            )
        except Exception:
            name = user_id
        self._user_name_cache[user_id] = name
        return name

    def is_diagnostic_bot(self, message: SlackMessage) -> bool:
        candidates = {
            (message.user or "").lower(),
            (message.bot_id or "").lower(),
            (message.username or "").lower(),
        }
        candidates.discard("")
        return bool(candidates & self._bot_identities)

    # ----- helpers -------------------------------------------------------

    @staticmethod
    def _has_replies(msg: SlackMessage) -> bool:
        return msg.thread_ts is not None

    @staticmethod
    def _to_message(raw: dict) -> SlackMessage:
        edited = (raw.get("edited") or {}).get("ts")
        return SlackMessage(
            ts=raw["ts"],
            user=raw.get("user", ""),
            username=raw.get("username", ""),
            text=raw.get("text", ""),
            thread_ts=raw.get("thread_ts"),
            edited_ts=edited,
            bot_id=raw.get("bot_id"),
        )
