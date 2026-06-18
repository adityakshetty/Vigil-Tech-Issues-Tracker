"""Configuration loading and validation.

Config is read from a YAML file, with environment variables overriding secrets
so credentials never need to live in the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

import yaml

from .models import IssueCategory


@dataclass
class SlackConfig:
    channel: str
    bot_token: str
    bot_identities: List[str] = field(default_factory=list)
    reply_match_window_minutes: int = 120


@dataclass
class ClassificationConfig:
    api_key: str
    model: str = "claude-opus-4-8"
    match_confidence_threshold: float = 0.75


@dataclass
class GoogleConfig:
    credentials_path: Optional[str] = None
    sheet_id: Optional[str] = None
    sheet_name: str = "Vigil Technical Issues Tracker"
    drive_folder_id: Optional[str] = None


@dataclass
class ScheduleConfig:
    interval_minutes: Optional[int] = 15
    cron: Optional[str] = None


@dataclass
class Config:
    slack: SlackConfig
    classification: ClassificationConfig
    google: GoogleConfig
    schedule: ScheduleConfig
    task_link_pattern: str
    no_issue_phrases: List[str]
    first_run_lookback_hours: int = 24
    timezone: str = "America/Los_Angeles"
    seed_categories: List[IssueCategory] = field(default_factory=list)

    # ----- loading -------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        slack_raw = raw.get("slack", {}) or {}
        slack = SlackConfig(
            channel=slack_raw.get("channel", "#vigil-technical-issues"),
            bot_token=_resolve_secret(slack_raw.get("bot_token"), "SLACK_BOT_TOKEN"),
            bot_identities=list(slack_raw.get("bot_identities", []) or []),
            reply_match_window_minutes=int(
                slack_raw.get("reply_match_window_minutes", 120)
            ),
        )

        cls_raw = raw.get("classification", {}) or {}
        classification = ClassificationConfig(
            api_key=_resolve_secret(cls_raw.get("api_key"), "ANTHROPIC_API_KEY"),
            model=cls_raw.get("model", "claude-opus-4-8"),
            match_confidence_threshold=float(
                cls_raw.get("match_confidence_threshold", 0.75)
            ),
        )

        google_raw = raw.get("google", {}) or {}
        google = GoogleConfig(
            credentials_path=_resolve_secret(
                google_raw.get("credentials_path"),
                "GOOGLE_APPLICATION_CREDENTIALS",
                required=False,
            ),
            sheet_id=google_raw.get("sheet_id"),
            sheet_name=google_raw.get("sheet_name", "Vigil Technical Issues Tracker"),
            drive_folder_id=google_raw.get("drive_folder_id"),
        )

        sched_raw = raw.get("schedule", {}) or {}
        interval = sched_raw.get("interval_minutes", 15)
        schedule = ScheduleConfig(
            interval_minutes=int(interval) if interval is not None else None,
            cron=sched_raw.get("cron"),
        )

        no_issue = (raw.get("no_issue_detection", {}) or {}).get("phrases", []) or []

        seed_categories = [
            IssueCategory.from_dict(c) for c in (raw.get("seed_categories", []) or [])
        ]

        cfg = cls(
            slack=slack,
            classification=classification,
            google=google,
            schedule=schedule,
            task_link_pattern=raw.get(
                "task_link_pattern",
                r"https://studio\.mercor\.com/annotator/tasks/(task_[0-9a-f]{32})/?(\?\S*)?",
            ),
            no_issue_phrases=[p.lower() for p in no_issue],
            first_run_lookback_hours=int(raw.get("first_run_lookback_hours", 24)),
            timezone=raw.get("timezone", "America/Los_Angeles"),
            seed_categories=seed_categories,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        errors = []
        if not self.slack.bot_token:
            errors.append("slack.bot_token (or SLACK_BOT_TOKEN) is required")
        if not self.classification.api_key:
            errors.append("classification.api_key (or ANTHROPIC_API_KEY) is required")
        if not (self.google.sheet_id or self.google.sheet_name):
            errors.append("google.sheet_id or google.sheet_name is required")
        if self.schedule.cron is None and not self.schedule.interval_minutes:
            errors.append("schedule.cron or schedule.interval_minutes is required")
        if not 0.0 <= self.classification.match_confidence_threshold <= 1.0:
            errors.append("classification.match_confidence_threshold must be in [0, 1]")
        if errors:
            raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(errors))


def _resolve_secret(
    value: Optional[str], env_var: str, required: bool = False
) -> Optional[str]:
    """Environment variables take precedence over the config file value."""
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val
    if value:
        return value
    return None if not required else None
