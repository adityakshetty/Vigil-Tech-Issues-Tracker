"""Vigil Technical Issue Tracker Agent.

Scans the `#vigil-technical-issues` Slack channel, qualifies recurring technical
issues from user messages and their bot diagnoses, and maintains a Google Sheet
that tracks each distinct issue, how often it occurs, who reported it, and when.
"""

__version__ = "1.0.0"
