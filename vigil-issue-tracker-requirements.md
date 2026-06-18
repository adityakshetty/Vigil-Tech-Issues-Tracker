# Vigil Technical Issue Tracker Agent: Requirements

## 1. Purpose

Build an agent that periodically scans the Slack channel `#vigil-technical-issues`, identifies recurring technical issues from user messages and their bot diagnoses, and maintains a Google Sheets spreadsheet (stored in Google Drive) that tracks each distinct issue, how often it occurs, who reported it, and when.

## 2. Goals and non-goals

**Goals**

- Read every top-level message and its replies in the target channel.
- Qualify which messages represent a real technical issue using both the user's message and the bot's reply.
- Group qualifying messages into distinct issue categories, count occurrences, and avoid double counting the same report.
- Maintain a Google Sheet that surfaces the most common issues first.
- Run on a configurable schedule, processing only new activity since the last run.

**Non-goals**

- The agent does not resolve issues or reply in Slack.
- The agent does not page or alert anyone (can be a future enhancement).
- The agent does not modify or delete Slack content.

## 3. Definitions

- **Task link**: a Mercor Studio annotator task URL. A message qualifies only if it contains one. The canonical form is `https://studio.mercor.com/annotator/tasks/task_<id>/`, where `<id>` is a 32-character hex string. An optional `?from_view=...` query string may follow and must be tolerated. The agent normalizes a task link by stripping the query string and uses the `task_<id>` segment as the stable task identifier. Suggested pattern: `https://studio\.mercor\.com/annotator/tasks/(task_[0-9a-f]{32})/?(\?\S*)?`. The pattern remains configurable.
- **Diagnostic bot**: the account that replies with a diagnosis. Identified by user or bot ID. Known names: `Justinbot` and `Cursor`. The set of bot identities is configurable.
- **Diagnosis**: the bot's reply to a user message. Classified as one of: issue diagnosed, no issue, or ambiguous.
- **No-issue reply**: a bot reply stating there is no issue, the task is not stuck, or an equivalent. Messages with a no-issue reply are excluded.
- **Issue category**: a canonical class of technical problem (for example "Stuck AutoQC"). Each category has a stable ID, a short name, and a longer description.
- **Occurrence**: one qualifying report of an issue. One qualifying message equals one occurrence.

## 4. Inputs

The agent reads from `#vigil-technical-issues`. The expected interaction pattern in the channel:

1. A user posts a message that includes a task link.
2. The diagnostic bot (`Justinbot` or `Cursor`) replies to that message with either a diagnosis of the issue or a statement that there is no issue.

The agent must read both the user message and the bot reply, since classification depends primarily on the bot's diagnosis.

Reply association: bot replies are usually threaded under the user's message, so the thread parent is the primary association. Rarely the bot posts its diagnosis as a separate top-level channel message. To associate those, the agent matches the normalized task identifier (`task_<id>`) in the bot's message to a recent user message that contains the same task link, within a configurable time window. If a separate bot message cannot be confidently matched to a single user message, route it to Needs Review rather than guessing.

## 5. Qualification filters

A message is processed as an occurrence only if all of the following are true:

1. The message contains a task link.
2. The message has at least one reply from a configured diagnostic bot.
3. The bot's diagnosis is "issue diagnosed" (not "no issue" and not "task not stuck").

Handling of non-qualifying messages:

- **No task link**: ignore permanently.
- **Has a task link but no bot reply yet**: defer. Mark as pending and re-evaluate on the next run. Do not discard, because the bot may reply after the current scan.
- **Bot replied with a no-issue or not-stuck verdict**: ignore permanently.
- **Bot reply is ambiguous** (neither a clear diagnosis nor a clear no-issue verdict): route to a "Needs Review" bucket rather than silently dropping or miscounting.

If a message has multiple bot replies, use the most recent reply that contains a diagnosis to determine the verdict.

## 6. Issue classification and deduplication

The core challenge is deciding whether two qualifying messages describe the same issue. The agent maintains an **issue registry** seeded with known categories and able to grow over time.

Classification flow for each qualifying message:

1. Combine the user message text, the task link context, and the bot diagnosis. The bot diagnosis is the strongest signal and should be weighted accordingly.
2. Compare against existing registry categories.
3. If it matches an existing category above a configurable confidence threshold, attach the occurrence to that category and increment its count.
4. If no existing category matches, create a new category, generate a short name and a longer description (a couple of sentences), assign a stable ID, and add it to the registry and the summary sheet.

Counting rules:

- Each qualifying message increments the issue's **occurrence count** by one, even if the same user reports the same issue more than once.
- A separate **distinct reporter count** deduplicates by user, so both "how often" and "how many different people" are available.

## 7. Output: Google Sheets schema

The spreadsheet lives in Google Drive at a configurable location (target folder plus sheet name, or an existing sheet ID). The design uses two tabs so that per-reporter detail and per-issue summary are both clean.

### Tab 1: Issue Summary (one row per issue, sorted by occurrence count descending)

| Column | Description |
| --- | --- |
| Issue ID | Stable identifier for the category |
| Issue Name | Short label (for example "Stuck AutoQC") |
| Description | A couple of sentences describing the issue |
| Occurrences | Total qualifying reports |
| Distinct Reporters | Number of unique users who reported it |
| Reporters | List of reporter display names or handles |
| First Reported | Timestamp of the earliest occurrence |
| Last Reported | Timestamp of the most recent occurrence |
| Example Message | Slack permalink to a representative message |
| Example Diagnosis | Short text or link for a representative bot diagnosis |

### Tab 2: Issue Log (one row per occurrence)

| Column | Description |
| --- | --- |
| Report Timestamp | When the user posted the message |
| Issue ID | Links the occurrence to its category |
| Issue Name | Short label, duplicated for readability |
| Reporter | Display name or handle |
| Reporter ID | Slack user ID |
| Message Permalink | Link to the user's Slack message |
| Task Link | The task URL from the message |
| Bot Diagnosis | Short summary of the bot's reply |
| Slack Message TS | Raw Slack timestamp, used as the dedup key |

This satisfies the requested tracking (the issue, how many times it came up, an example message, who all reported it, and what time) directly. "Who all reported it" and "what time" come from the Issue Log tab and are summarized on the Issue Summary tab.

The agent should sort the Issue Summary tab by Occurrences descending on each run so the most common issues stay at the top. There is no minimum occurrence threshold for flagging an issue as common. Every distinct issue gets a row and frequency is read directly from the sorted Occurrences column.

## 8. State management and idempotency

The agent runs repeatedly and must never double count. It persists state across runs:

- **Processing cursor**: the last successfully processed point in channel history (timestamp or cursor).
- **Processed message set**: Slack message timestamps already counted, each stored with its last-known edited timestamp so edits can be detected and reprocessed (see edit handling in Error handling and edge cases).
- **Pending set**: messages with a task link but no bot reply yet, to re-check next run.
- **Needs-review set**: messages with an ambiguous bot reply or an unmatched separate bot message.
- **Issue registry**: categories with IDs, names, and descriptions.

State lives in a dedicated hidden tab (for example `_State`) in the same spreadsheet, storing the cursor and the registry plus the processed, pending, and needs-review sets as structured rows or a JSON blob. Keeping state in the same file means the agent is self-contained, needs no extra credentials or services, and stays portable. This is appropriate for the scale of a single channel scanned on an interval. If state volume ever outgrows a tab, move it to a separate lightweight store without changing the rest of the design.

## 9. Scheduling and configuration

The agent runs on a schedule. The run interval must be configurable (for example a cron expression or an interval in minutes). Each run processes only new or previously deferred activity.

Configurable parameters:

| Parameter | Purpose | Decided value |
| --- | --- | --- |
| `slack_channel` | Channel name or ID to scan | `#vigil-technical-issues` |
| `slack_auth` | Bot token and required scopes | Configurable |
| `bot_identities` | IDs or names of diagnostic bots | Justinbot, Cursor |
| `task_link_pattern` | Regex that defines a valid task link | `https://studio\.mercor\.com/annotator/tasks/(task_[0-9a-f]{32})/?(\?\S*)?` |
| `no_issue_detection` | Phrases or rules for detecting no-issue replies | Configurable |
| `run_interval` | Schedule (cron expression or minutes) | Configurable |
| `google_sheet_target` | Existing sheet ID, or Drive folder plus sheet name | Configurable |
| `google_auth` | Service account or OAuth credentials | Configurable |
| `first_run_lookback` | How far back to scan on the first run | Past 24 hours |
| `match_confidence_threshold` | Minimum confidence to attach to an existing category | Configurable |
| `timezone` | Timezone for stored and displayed timestamps | `America/Los_Angeles` (Pacific) |

## 10. Authentication and permissions

- **Slack**: a bot token with history read access for the channel (`channels:history` for public channels or `groups:history` for private), channel read access, and user read access (`users:read`) to resolve reporter names. Permalinks require the appropriate scope as well.
- **Google**: Sheets API and Drive API access. Use a service account with the target sheet shared to it, or OAuth. Drive scope is needed to locate or create the spreadsheet in the chosen folder.

## 11. Error handling and edge cases

- **Pagination and rate limits**: handle Slack pagination and back off on Slack and Google API rate limits with retries.
- **Message edited after processing**: edited messages are reprocessed. The agent compares each message's current edited timestamp against the value in the processed message set, and on a change it re-runs classification for that message and updates the existing Issue Log row (keyed by Slack Message TS) in place rather than adding a new one. If reclassification moves the occurrence to a different category, decrement the old category and increment the new one so counts stay correct. An edit to the associated bot diagnosis triggers the same reprocessing, since the diagnosis drives classification.
- **Message or reply deleted**: default is to leave existing counts as recorded.
- **Bot replies late**: handled by the pending set, which is re-checked each run.
- **Timezone consistency**: timestamps are stored and displayed in `America/Los_Angeles`. Using the named zone rather than a fixed offset means it correctly shows PDT in summer and PST in winter, and keeps First and Last Reported coherent.
- **Partial run failure**: do not advance the processing cursor past messages that were not fully committed to the sheet, so a failed run can be safely retried.

## 12. Example issues with expanded descriptions

The three provided examples are brief. Below are longer descriptions in the style the sheet should use. These are illustrative and should be refined against the real meaning of each tool in your environment.

- **Stuck AutoQC**: An AutoQC process started but failed to progress past a stage and remains in a running or pending state well beyond its expected completion time. The task neither finishes nor errors out cleanly, so it requires manual intervention to find the blocking step and restart or unblock the pipeline.
- **Incomplete Taiga run**: A Taiga run began executing but stopped before producing all expected outputs. Some stages completed while others did not, leaving the run in a partial state where downstream steps cannot proceed until the missing outputs are regenerated.
- **No Taiga run started**: A task that should have triggered a Taiga run shows no sign that the run was ever initiated. The expected pipeline never launched, so there are no logs or outputs to inspect, which usually points to a trigger, configuration, or upstream dependency problem rather than the run itself.

## 13. Resolved decisions

1. **Reply association**: bot replies are usually threaded under the user message and rarely posted as separate channel messages. The agent handles both, matching separate messages by task identifier within a time window and routing unmatched ones to Needs Review.
2. **Task link pattern**: Mercor Studio annotator task URLs, `https://studio.mercor.com/annotator/tasks/task_<id>/`, with an optional `?from_view=...` query string. The `task_<id>` segment is the stable identifier.
3. **First run lookback**: scan the past 24 hours.
4. **Schema**: two-tab design (Issue Summary and Issue Log).
5. **Edited messages**: reprocessed, updating the existing log row in place and adjusting category counts if the classification changes.
6. **Common issue flagging**: no threshold. Sort by occurrence count descending and let frequency speak for itself.
7. **Timezone**: Pacific time, stored as `America/Los_Angeles` so it tracks PDT and PST automatically.
8. **State location**: a hidden `_State` tab in the same spreadsheet.

## 14. Possible future enhancements

- Alerting when a known issue spikes in frequency.
- A trend tab showing occurrences per issue over time.
- Linking each occurrence back to a resolution status.
