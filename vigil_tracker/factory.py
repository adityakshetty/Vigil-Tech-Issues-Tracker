"""Wire concrete clients into a Tracker from a Config."""

from __future__ import annotations

from .classifier import AnthropicLLMClient, IssueClassifier, VerdictDetector
from .config import Config
from .sheets_client import SheetsClient
from .slack_client import SlackClient
from .tracker import Tracker


def build_tracker(config: Config) -> Tracker:
    llm = AnthropicLLMClient(
        api_key=config.classification.api_key,
        model=config.classification.model,
    )
    verdict_detector = VerdictDetector(config.no_issue_phrases, llm=llm)
    classifier = IssueClassifier(
        llm=llm,
        match_confidence_threshold=config.classification.match_confidence_threshold,
    )
    slack = SlackClient(
        bot_token=config.slack.bot_token,
        bot_identities=config.slack.bot_identities,
    )
    sheets = SheetsClient(config.google)
    return Tracker(
        config=config,
        slack=slack,
        sheets=sheets,
        verdict_detector=verdict_detector,
        classifier=classifier,
    )
